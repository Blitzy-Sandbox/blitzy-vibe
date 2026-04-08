#!/usr/bin/env python3
"""
Agent Action Plan (AAP) parser using tree-sitter-markdown.

Parses AAP documents — large structured markdown files with numbered heading
hierarchies (# 0. / ## 0.X / ### 0.X.Y), markdown tables, mermaid diagrams,
and code blocks — and extracts structural data for review.

Requires: pip install tree-sitter tree-sitter-markdown
"""

import argparse
import re
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Language, Parser

# Suppress deprecation warnings from tree-sitter internals
warnings.filterwarnings("ignore", category=FutureWarning, module="tree_sitter")

# ---------------------------------------------------------------------------
# Language loading (tree-sitter 0.25+ with individual packages)
# ---------------------------------------------------------------------------

_LANGUAGES: dict[str, Language] = {}
_PARSERS: dict[str, Parser] = {}


def _load_languages():
    """Load the tree-sitter-markdown language."""
    loaders = {
        "markdown": ("tree_sitter_markdown", "language"),
    }
    for lang_name, (module_name, func_name) in loaders.items():
        try:
            mod = __import__(module_name)
            lang_func = getattr(mod, func_name)
            _LANGUAGES[lang_name] = Language(lang_func())
        except ImportError:
            pass
        except (AttributeError, TypeError, RuntimeError) as e:
            print(
                f"Warning: failed to load {lang_name} ({module_name}): {e}",
                file=sys.stderr,
            )


def get_parser(lang: str) -> Parser | None:
    """Get a parser for the given language."""
    if lang not in _PARSERS:
        if lang not in _LANGUAGES:
            return None
        p = Parser(_LANGUAGES[lang])
        _PARSERS[lang] = p
    return _PARSERS[lang]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class AAPConfig:
    """Configuration for AAP parsing and LoC estimation."""
    file_path: str = ""
    loc_create: int = 150
    loc_modify: int = 50
    verbose: bool = False
    focus: str = ""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class HeadingNode:
    """A section heading in the AAP hierarchy."""
    level: int
    number: str  # e.g. "0.2.1"
    title: str
    line: int
    children: list["HeadingNode"] = field(default_factory=list)


@dataclass
class FileEntry:
    """A file from the repository scope tables."""
    path: str
    action: str  # CREATE or MODIFY
    purpose: str
    source_section: str = ""
    source_file: str = ""  # content of "Source File" column if present
    source_lines: int = 0  # extracted/enriched from source line counts


@dataclass
class DependencyEntry:
    """A dependency from the dependency inventory tables."""
    registry: str
    package: str
    version: str
    purpose: str


@dataclass
class RuleEntry:
    """A rule extracted from section 0.7."""
    section_number: str
    title: str
    constraints: list[str] = field(default_factory=list)


@dataclass
class ScopeItem:
    """An in-scope or out-of-scope item from section 0.6."""
    description: str
    in_scope: bool = True


@dataclass
class AAPDocument:
    """Aggregated data extracted from an AAP document."""
    headings: list[HeadingNode] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)
    dependencies: list[DependencyEntry] = field(default_factory=list)
    rules: list[RuleEntry] = field(default_factory=list)
    scope_items: list[ScopeItem] = field(default_factory=list)
    source_line_map: dict[str, int] = field(default_factory=dict)  # source file → line count
    mermaid_diagrams: int = 0
    code_blocks: int = 0
    total_lines: int = 0

    @property
    def create_count(self) -> int:
        return sum(1 for f in self.files if f.action == "CREATE")

    @property
    def modify_count(self) -> int:
        return sum(1 for f in self.files if f.action == "MODIFY")

    @property
    def reference_count(self) -> int:
        return sum(1 for f in self.files if f.action == "REFERENCE")

    @property
    def delete_count(self) -> int:
        return sum(1 for f in self.files if f.action == "DELETE")

    @property
    def major_sections(self) -> int:
        return len([h for h in self.headings if h.level == 1])


# ---------------------------------------------------------------------------
# Node Visitors
# ---------------------------------------------------------------------------

class NodeVisitor:
    """Base class for markdown AST node visitors."""

    def __init__(self, config: AAPConfig):
        self.config = config

    def visit(self, node, doc: AAPDocument, source_lines: list[str]) -> None:
        """Visit a node and optionally extract data."""


class HeadingVisitor(NodeVisitor):
    """Builds numbered section hierarchy from atx_heading nodes."""

    # Match AAP heading numbers like "0.", "0.1", "0.1.1", "0.2.3"
    _NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)*\.?)\s+(.+)$")

    def visit(self, node, doc: AAPDocument, source_lines: list[str]) -> None:
        if node.type != "atx_heading":
            return

        level = 0
        title_text = ""
        for child in node.children:
            if child.type.startswith("atx_h") and child.type.endswith("_marker"):
                level = len(child.text.decode("utf-8").strip())
            elif child.type == "inline":
                title_text = child.text.decode("utf-8").strip()

        if not title_text or level == 0:
            return

        match = self._NUMBER_RE.match(title_text)
        if match:
            number = match.group(1).rstrip(".")
            title = match.group(2).strip()
        else:
            number = ""
            title = title_text

        heading = HeadingNode(
            level=level,
            number=number,
            title=title,
            line=node.start_point[0] + 1,
        )
        doc.headings.append(heading)


class TableVisitor(NodeVisitor):
    """Extracts File|Action|Purpose and dependency tables from pipe_table nodes."""

    def visit(self, node, doc: AAPDocument, source_lines: list[str]) -> None:
        if node.type != "pipe_table":
            return

        header_cells = self._get_row_cells(node, "pipe_table_header")
        if not header_cells:
            return

        header_lower = [c.lower().strip() for c in header_cells]

        # Detect file tables (File|Action|Purpose)
        if self._is_file_table(header_lower):
            self._extract_file_entries(node, header_lower, doc, source_lines)
        # Detect dependency tables (Registry|Package|Version|Purpose)
        elif self._is_dep_table(header_lower):
            self._extract_dep_entries(node, header_lower, doc)

    def _is_file_table(self, headers: list[str]) -> bool:
        # Check if any header contains "file" as a word (handles "Target File", "File Path", etc.)
        has_file_col = any("file" in h for h in headers)
        # Explicit action/transformation column
        has_action_col = any(h in ("action", "transformation") for h in headers)
        if has_file_col and has_action_col:
            return True
        # Also match tables with just a file-like column (action inferred from section heading)
        file_cols = ("file", "file path", "file_path", "target file")
        return any(h in file_cols for h in headers)

    def _is_dep_table(self, headers: list[str]) -> bool:
        return "registry" in headers and "package" in headers

    def _get_row_cells(self, table_node, row_type: str) -> list[str]:
        """Extract cell text from a table row."""
        for child in table_node.children:
            if child.type == row_type:
                return self._cells_from_row(child)
        return []

    def _cells_from_row(self, row_node) -> list[str]:
        """Extract text from pipe_table_cell nodes in a row."""
        cells = []
        for child in row_node.children:
            if child.type == "pipe_table_cell":
                text = child.text.decode("utf-8").strip()
                # Strip backticks from inline code
                text = text.strip("`")
                cells.append(text)
        return cells

    def _infer_action_from_section(self, section_title: str) -> str:
        """Infer CREATE/MODIFY/REFERENCE/DELETE from section heading text."""
        lower = section_title.lower()
        # Keywords suggesting modification of existing files
        modify_kw = ("modify", "modif", "existing", "requiring modification", "touchpoint",
                      "updates", "update", "integration point")
        # Keywords suggesting new file creation
        create_kw = ("new", "create", "new file", "requirements")
        # Keywords suggesting reference-only
        ref_kw = ("reference", "searched", "cross-reference", "discovered", "discovery")
        # Keywords suggesting deletion
        del_kw = ("delete", "removed", "deprecated")

        for kw in del_kw:
            if kw in lower:
                return "DELETE"
        for kw in modify_kw:
            if kw in lower:
                return "MODIFY"
        for kw in create_kw:
            if kw in lower:
                return "CREATE"
        for kw in ref_kw:
            if kw in lower:
                return "REFERENCE"
        return ""

    # Normalize transformation/action values to canonical actions
    _ACTION_MAP = {
        "CREATE": "CREATE",
        "MODIFY": "MODIFY",
        "UPDATE": "MODIFY",
        "REFERENCE": "REFERENCE",
        "DELETE": "DELETE",
        "AUTO-GENERATED": "AUTO-GENERATED",
    }

    def _extract_file_entries(self, table_node, headers, doc, source_lines):
        """Extract FileEntry objects from a file table."""
        # Find the file path column (may be "file", "file path", "target file", etc.)
        file_cols = ("file", "file path", "file_path", "target file")
        file_idx = -1
        for col in file_cols:
            if col in headers:
                file_idx = headers.index(col)
                break
        # Fallback: find any header containing "file"
        if file_idx < 0:
            for i, h in enumerate(headers):
                if "file" in h:
                    file_idx = i
                    break
        if file_idx < 0:
            return

        # Look for action column (may be "action" or "transformation")
        action_idx = -1
        for col in ("action", "transformation"):
            if col in headers:
                action_idx = headers.index(col)
                break
        # Match "purpose" or "modification purpose" or similar
        purpose_idx = -1
        for i, h in enumerate(headers):
            if "purpose" in h:
                purpose_idx = i
                break
        # Find source file column (for extracting line counts from source file references)
        source_file_idx = -1
        for i, h in enumerate(headers):
            if h in ("source file", "source file(s)", "source files"):
                source_file_idx = i
                break
        # Also check "key changes" column for line count mentions
        key_changes_idx = -1
        for i, h in enumerate(headers):
            if "key changes" in h or "changes" in h or "role" in h:
                key_changes_idx = i
                break

        # Find the section this table belongs to
        section = self._find_parent_section(table_node, source_lines)

        # Infer action from section heading when no explicit action column
        inferred_action = ""
        if action_idx < 0:
            inferred_action = self._infer_action_from_section(section)

        for child in table_node.children:
            if child.type != "pipe_table_row":
                continue
            cells = self._cells_from_row(child)
            if len(cells) <= file_idx:
                continue

            path = cells[file_idx].strip()
            if action_idx >= 0 and len(cells) > action_idx:
                raw_action = cells[action_idx].strip().upper()
                action = self._ACTION_MAP.get(raw_action, raw_action)
            else:
                action = inferred_action
            purpose = cells[purpose_idx].strip() if purpose_idx >= 0 and len(cells) > purpose_idx else ""
            source_file = cells[source_file_idx].strip() if source_file_idx >= 0 and len(cells) > source_file_idx else ""

            # Extract source line count from any cell that mentions lines
            src_lines = self._extract_line_count(cells, file_idx, source_file_idx, key_changes_idx)

            if path and action in ("CREATE", "MODIFY", "AUTO-GENERATED", "REFERENCE", "DELETE"):
                doc.files.append(FileEntry(
                    path=path,
                    action=action,
                    purpose=purpose,
                    source_section=section,
                    source_file=source_file,
                    source_lines=src_lines,
                ))

    # Regex to find line counts like "6,274 lines", "9,210 lines", "(5,323 lines)", "6274"
    _LINE_COUNT_RE = re.compile(r"([\d,]+)\s*lines?\b", re.IGNORECASE)
    # Fallback: bare number in parens like "(6,274)" when near a .c filename
    _BARE_COUNT_RE = re.compile(r"\((\d[\d,]*)\)")

    def _extract_line_count(self, cells: list[str], file_idx: int,
                            source_file_idx: int, key_changes_idx: int) -> int:
        """Extract source line count from table cells.

        Scans source file column, key changes column, and all other columns
        for patterns like '9,210 lines' or '(6,274 lines)'.
        """
        # Priority order: source file column, key changes, then all cells
        priority_indices = []
        if source_file_idx >= 0:
            priority_indices.append(source_file_idx)
        if key_changes_idx >= 0:
            priority_indices.append(key_changes_idx)
        # Add remaining indices
        for i in range(len(cells)):
            if i not in priority_indices and i != file_idx:
                priority_indices.append(i)

        for idx in priority_indices:
            if idx >= len(cells):
                continue
            cell = cells[idx]
            # Try explicit "N lines" pattern first
            match = self._LINE_COUNT_RE.search(cell)
            if match:
                return int(match.group(1).replace(",", ""))
            # Try bare parenthesized number near a .c file reference
            if ".c" in cell:
                match = self._BARE_COUNT_RE.search(cell)
                if match:
                    val = int(match.group(1).replace(",", ""))
                    if val > 50:  # filter out small numbers that aren't line counts
                        return val
        return 0

    def _extract_dep_entries(self, table_node, headers, doc):
        """Extract DependencyEntry objects from a dependency table."""
        reg_idx = headers.index("registry")
        pkg_idx = headers.index("package")
        ver_idx = headers.index("version") if "version" in headers else -1
        pur_idx = headers.index("purpose") if "purpose" in headers else -1

        for child in table_node.children:
            if child.type != "pipe_table_row":
                continue
            cells = self._cells_from_row(child)
            if len(cells) <= max(reg_idx, pkg_idx):
                continue

            doc.dependencies.append(DependencyEntry(
                registry=cells[reg_idx].strip(),
                package=cells[pkg_idx].strip(),
                version=cells[ver_idx].strip() if ver_idx >= 0 and len(cells) > ver_idx else "",
                purpose=cells[pur_idx].strip() if pur_idx >= 0 and len(cells) > pur_idx else "",
            ))

    def _find_parent_section(self, node, source_lines: list[str]) -> str:
        """Walk backwards from node's line to find the nearest heading."""
        line = node.start_point[0]
        for i in range(line, -1, -1):
            if i < len(source_lines):
                stripped = source_lines[i].strip()
                if stripped.startswith("#"):
                    return stripped.lstrip("#").strip()
        return ""


class CodeBlockVisitor(NodeVisitor):
    """Detects mermaid diagrams and code blocks from fenced_code_block nodes."""

    def visit(self, node, doc: AAPDocument, source_lines: list[str]) -> None:
        if node.type != "fenced_code_block":
            return

        doc.code_blocks += 1

        # Check for mermaid info string
        for child in node.children:
            if child.type == "info_string":
                lang_text = child.text.decode("utf-8").strip().lower()
                if lang_text == "mermaid":
                    doc.mermaid_diagrams += 1
                    break


class RuleVisitor(NodeVisitor):
    """Extracts rules from 0.7-numbered sections (MUST/MUST NOT patterns)."""

    _MUST_RE = re.compile(r"\b(MUST(?:\s+NOT)?|MUST NEVER|NEVER)\b")

    def visit(self, node, doc: AAPDocument, source_lines: list[str]) -> None:
        if node.type != "section":
            return

        # Check if this section has a heading starting with 0.7
        heading = self._get_section_heading(node)
        if not heading or not heading.startswith("0.7"):
            return

        # Extract the heading info
        heading_node = None
        for child in node.children:
            if child.type == "atx_heading":
                heading_node = child
                break

        if not heading_node:
            return

        title = ""
        number = ""
        for child in heading_node.children:
            if child.type == "inline":
                text = child.text.decode("utf-8").strip()
                match = re.match(r"^(\d+(?:\.\d+)*\.?)\s+(.+)$", text)
                if match:
                    number = match.group(1).rstrip(".")
                    title = match.group(2).strip()

        if not number.startswith("0.7"):
            return

        # Only process subsections (0.7.X), not the parent 0.7
        if number == "0.7":
            return

        # Extract MUST/MUST NOT constraints from the section content
        constraints = []
        start_line = heading_node.start_point[0]
        end_line = node.end_point[0]
        for i in range(start_line, min(end_line + 1, len(source_lines))):
            line = source_lines[i]
            if self._MUST_RE.search(line):
                stripped = line.strip().lstrip("- ")
                if stripped:
                    constraints.append(stripped)

        if title:
            doc.rules.append(RuleEntry(
                section_number=number,
                title=title,
                constraints=constraints,
            ))

    def _get_section_heading(self, section_node) -> str:
        """Get the heading number from a section node."""
        for child in section_node.children:
            if child.type == "atx_heading":
                for sub in child.children:
                    if sub.type == "inline":
                        text = sub.text.decode("utf-8").strip()
                        match = re.match(r"^(\d+(?:\.\d+)*)", text)
                        if match:
                            return match.group(1)
        return ""


class SourceLineMapVisitor(NodeVisitor):
    """Builds a source file → line count map from tables and code blocks.

    Extracts from:
    - Tables with 'Lines' or 'Lines (approx)' columns (e.g., complexity hotspot tables)
    - Code block lines matching patterns like 'filename.c  (N,NNN lines — description)'
    - Table cells containing 'N,NNN lines' alongside a .c/.h filename
    """

    # Match "filename.c" or "filename.h" with optional path prefix
    _FILE_RE = re.compile(r"(?:^|[`/])([a-zA-Z_][\w.-]*\.[ch])\b")
    # Match line counts: "6,274 lines", "9210 lines", bare "9,210" in parens
    _LINES_RE = re.compile(r"([\d,]+)\s*lines?\b", re.IGNORECASE)
    # Code block pattern: "├── filename.c  (6,274 lines — description)" or "crypto/ (327,616 lines — ...)"
    _TREE_LINE_RE = re.compile(r"([a-zA-Z_][\w./-]*(?:\.[ch]|/))\s+\(([\d,]+)\s*lines?")
    # Table with source file and bare number lines column
    _BARE_NUM_RE = re.compile(r"^[\d,]+$")

    def visit(self, node, doc: AAPDocument, source_lines: list[str]) -> None:
        if node.type == "fenced_code_block":
            self._extract_from_code_block(node, doc)
        elif node.type == "pipe_table":
            self._extract_from_table(node, doc)

    def _extract_from_code_block(self, node, doc: AAPDocument) -> None:
        """Extract line counts from tree-style code blocks."""
        text = node.text.decode("utf-8")
        for match in self._TREE_LINE_RE.finditer(text):
            filename = match.group(1)
            count = int(match.group(2).replace(",", ""))
            if count > 50:  # filter noise
                doc.source_line_map[filename] = max(doc.source_line_map.get(filename, 0), count)

    def _extract_from_table(self, node, doc: AAPDocument) -> None:
        """Extract line counts from tables with a 'Lines' column."""
        # Get header cells
        header_cells = []
        for child in node.children:
            if child.type == "pipe_table_header":
                for cell in child.children:
                    if cell.type == "pipe_table_cell":
                        header_cells.append(cell.text.decode("utf-8").strip().lower())
                break

        if not header_cells:
            return

        # Find relevant columns
        file_idx = -1
        lines_idx = -1
        for i, h in enumerate(header_cells):
            if "file" in h or "source" in h:
                file_idx = i
            if h in ("lines", "lines (approx)") or "lines" in h:
                lines_idx = i

        if file_idx < 0 or lines_idx < 0:
            return

        for child in node.children:
            if child.type != "pipe_table_row":
                continue
            cells = []
            for cell in child.children:
                if cell.type == "pipe_table_cell":
                    cells.append(cell.text.decode("utf-8").strip().strip("`"))
            if len(cells) <= max(file_idx, lines_idx):
                continue

            file_cell = cells[file_idx]
            lines_cell = cells[lines_idx]

            # Extract filename
            file_match = self._FILE_RE.search(file_cell)
            if not file_match:
                continue
            filename = file_match.group(1)

            # Extract line count
            lines_match = self._LINES_RE.search(lines_cell)
            if lines_match:
                count = int(lines_match.group(1).replace(",", ""))
            elif self._BARE_NUM_RE.match(lines_cell.replace(",", "")):
                count = int(lines_cell.replace(",", ""))
            else:
                continue

            if count > 50:
                doc.source_line_map[filename] = max(doc.source_line_map.get(filename, 0), count)


class ScopeVisitor(NodeVisitor):
    """Extracts in-scope/out-of-scope items from 0.6-numbered sections."""

    def visit(self, node, doc: AAPDocument, source_lines: list[str]) -> None:
        if node.type != "section":
            return

        heading = self._get_section_heading(node)
        if not heading or not heading.startswith("0.6"):
            return

        heading_node = None
        for child in node.children:
            if child.type == "atx_heading":
                heading_node = child
                break

        if not heading_node:
            return

        title_text = ""
        for child in heading_node.children:
            if child.type == "inline":
                title_text = child.text.decode("utf-8").strip().lower()

        is_in_scope = "in scope" in title_text or "in-scope" in title_text or title_text.endswith("in scope")
        is_out_scope = "out of scope" in title_text or "out-of-scope" in title_text

        if not is_in_scope and not is_out_scope:
            # Check subsection number to guess: 0.6.1 = in scope, 0.6.2 = out of scope
            match = re.match(r"^(\d+(?:\.\d+)*)", title_text)
            if not match:
                return
            # Fall through to extract items generically
            heading_num = self._get_section_heading(node)
            if heading_num == "0.6.1":
                is_in_scope = True
            elif heading_num == "0.6.2":
                is_out_scope = True
            else:
                return

        start_line = heading_node.end_point[0] + 1
        end_line = node.end_point[0]

        for i in range(start_line, min(end_line + 1, len(source_lines))):
            line = source_lines[i].strip()
            if line.startswith("- **") or line.startswith("- "):
                desc = line.lstrip("- ").strip()
                if desc:
                    doc.scope_items.append(ScopeItem(
                        description=desc,
                        in_scope=is_in_scope,
                    ))

    def _get_section_heading(self, section_node) -> str:
        for child in section_node.children:
            if child.type == "atx_heading":
                for sub in child.children:
                    if sub.type == "inline":
                        text = sub.text.decode("utf-8").strip()
                        match = re.match(r"^(\d+(?:\.\d+)*)", text)
                        if match:
                            return match.group(1)
        return ""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class AAPParser:
    """Parses an AAP document using tree-sitter-markdown."""

    def __init__(self, config: AAPConfig | None = None):
        self.config = config or AAPConfig()
        self._visitors = [
            HeadingVisitor(self.config),
            SourceLineMapVisitor(self.config),
            TableVisitor(self.config),
            CodeBlockVisitor(self.config),
            RuleVisitor(self.config),
            ScopeVisitor(self.config),
        ]

    def parse(self, source: str) -> AAPDocument:
        """Parse an AAP document and return extracted data."""
        doc = AAPDocument()
        source_lines = source.splitlines()
        doc.total_lines = len(source_lines)

        parser = get_parser("markdown")
        if parser is None:
            # Fall back to regex-based extraction
            return self._fallback_parse(source, source_lines)

        tree = parser.parse(source.encode("utf-8"))
        self._walk_tree(tree.root_node, doc, source_lines)

        # Enrich file entries with source line counts from the map
        self._enrich_file_source_lines(doc)

        # Build heading hierarchy
        self._build_hierarchy(doc)

        return doc

    def _walk_tree(self, node, doc: AAPDocument, source_lines: list[str]) -> None:
        """Recursive dispatch to visitors."""
        for visitor in self._visitors:
            visitor.visit(node, doc, source_lines)
        for child in node.children:
            self._walk_tree(child, doc, source_lines)

    def _enrich_file_source_lines(self, doc: AAPDocument) -> None:
        """Cross-reference file entries with the source line count map.

        Two-pass approach:
        1. Map each CREATE file to its referenced C source files
        2. Split source line counts proportionally among all target files
           that reference the same source (avoids double-counting)
        """
        if not doc.source_line_map:
            return

        _file_re = re.compile(r"([a-zA-Z_][\w.-]*\.[ch])")
        _dir_re = re.compile(r"([a-zA-Z_][\w.-]*/)")
        _source_exts = (".rs", ".c", ".cpp", ".cc", ".h", ".py", ".go", ".java", ".ts", ".js")

        # Pass 1: Map each eligible file to its referenced source entries (files or dirs)
        file_to_sources: dict[int, set[str]] = {}  # index → set of source keys
        for i, f in enumerate(doc.files):
            if f.source_lines > 0 or f.action != "CREATE":
                continue
            basename = f.path.rsplit("/", 1)[-1] if "/" in f.path else f.path
            if not any(basename.endswith(ext) for ext in _source_exts):
                continue

            sources = set()
            # Priority 1: source_file column (per-row)
            # Priority 2: purpose text
            # Priority 3: section heading (only for primary files)
            for text, is_section in [
                (f.source_file, False),
                (f.purpose, False),
                (f.source_section, True),
            ]:
                if not text:
                    continue
                # Check for file matches first
                matches = _file_re.findall(text)
                found = {m for m in matches if m in doc.source_line_map}
                # Also check for directory matches
                dir_matches = _dir_re.findall(text)
                dir_found = {m for m in dir_matches if m in doc.source_line_map}
                found.update(dir_found)

                if found:
                    if is_section and basename not in ("lib.rs", "main.rs", "mod.rs"):
                        continue
                    sources.update(found)
                    break  # use highest priority match only

            if sources:
                file_to_sources[i] = sources

        # Pass 2: Count how many target files reference each source file
        source_ref_count: dict[str, int] = {}
        for sources in file_to_sources.values():
            for s in sources:
                source_ref_count[s] = source_ref_count.get(s, 0) + 1

        # Pass 3: Assign proportional line counts
        for i, sources in file_to_sources.items():
            total = 0
            for s in sources:
                # Split this source's lines among all targets that reference it
                total += doc.source_line_map[s] // source_ref_count[s]
            if total > 0:
                doc.files[i].source_lines = total

    def _build_hierarchy(self, doc: AAPDocument) -> None:
        """Nest headings into parent-child relationships."""
        if not doc.headings:
            return

        stack: list[HeadingNode] = []
        for heading in doc.headings:
            while stack and stack[-1].level >= heading.level:
                stack.pop()
            if stack:
                stack[-1].children.append(heading)
            stack.append(heading)

    def _fallback_parse(self, source: str, source_lines: list[str]) -> AAPDocument:
        """Regex-based fallback when tree-sitter-markdown is unavailable."""
        doc = AAPDocument()
        doc.total_lines = len(source_lines)

        heading_re = re.compile(r"^(#{1,6})\s+(.+)$")
        number_re = re.compile(r"^(\d+(?:\.\d+)*\.?)\s+(.+)$")

        for i, line in enumerate(source_lines):
            m = heading_re.match(line)
            if m:
                level = len(m.group(1))
                text = m.group(2).strip()
                nm = number_re.match(text)
                number = nm.group(1).rstrip(".") if nm else ""
                title = nm.group(2).strip() if nm else text
                doc.headings.append(HeadingNode(level=level, number=number, title=title, line=i + 1))

            if line.strip().startswith("```mermaid"):
                doc.mermaid_diagrams += 1
            if line.strip().startswith("```") and not line.strip() == "```":
                doc.code_blocks += 1

        # Simple table extraction for file tables (with explicit action/transformation column)
        table_re = re.compile(r"^\|\s*`?([^|`]+?)`?\s*\|\s*(CREATE|MODIFY|UPDATE|REFERENCE|DELETE)\s*\|\s*(.+?)\s*\|")
        for line in source_lines:
            m = table_re.match(line)
            if m:
                action = {"UPDATE": "MODIFY"}.get(m.group(2), m.group(2))
                doc.files.append(FileEntry(path=m.group(1).strip(), action=action, purpose=m.group(3).strip()))

        self._build_hierarchy(doc)
        return doc

    def format_output(self, doc: AAPDocument) -> str:
        """Format extracted data as structured markdown output."""
        lines = []

        # --- Summary ---
        lines.append(f"# AAP Analysis Summary")
        lines.append("")
        lines.append(f"- **Total lines**: {doc.total_lines:,}")
        lines.append(f"- **Major sections**: {doc.major_sections}")
        lines.append(f"- **Total headings**: {len(doc.headings)}")
        file_parts = [f"{doc.create_count} CREATE", f"{doc.modify_count} MODIFY"]
        if doc.reference_count:
            file_parts.append(f"{doc.reference_count} REFERENCE")
        if doc.delete_count:
            file_parts.append(f"{doc.delete_count} DELETE")
        lines.append(f"- **Files referenced**: {len(doc.files)} ({', '.join(file_parts)})")
        lines.append(f"- **Dependencies**: {len(doc.dependencies)}")
        lines.append(f"- **Rules**: {len(doc.rules)}")
        lines.append(f"- **Scope items**: {len(doc.scope_items)} ({sum(1 for s in doc.scope_items if s.in_scope)} in-scope, {sum(1 for s in doc.scope_items if not s.in_scope)} out-of-scope)")
        lines.append(f"- **Mermaid diagrams**: {doc.mermaid_diagrams}")
        lines.append(f"- **Code blocks**: {doc.code_blocks}")
        lines.append("")

        # --- Section Hierarchy ---
        lines.append("## Section Hierarchy")
        lines.append("")
        top_level = [h for h in doc.headings if h.level == 1]
        for h in top_level:
            self._format_heading_tree(h, lines, indent=0)
        lines.append("")

        # --- File Inventory ---
        if doc.files:
            lines.append("## File Inventory")
            lines.append("")
            lines.append("| # | File | Action | Purpose |")
            lines.append("|---|------|--------|---------|")
            for i, f in enumerate(doc.files, 1):
                lines.append(f"| {i} | `{f.path}` | {f.action} | {f.purpose} |")
            lines.append("")

        # --- Dependencies ---
        if doc.dependencies:
            lines.append("## Dependencies")
            lines.append("")
            lines.append("| Registry | Package | Version | Purpose |")
            lines.append("|----------|---------|---------|---------|")
            for d in doc.dependencies:
                lines.append(f"| {d.registry} | {d.package} | {d.version} | {d.purpose} |")
            lines.append("")

        # --- Rules ---
        if doc.rules:
            lines.append("## Rules (Section 0.7)")
            lines.append("")
            for rule in doc.rules:
                lines.append(f"### {rule.section_number} {rule.title}")
                if rule.constraints:
                    for c in rule.constraints:
                        lines.append(f"- {c}")
                else:
                    lines.append("- *(no MUST/MUST NOT constraints extracted)*")
                lines.append("")

        # --- Scope ---
        in_scope = [s for s in doc.scope_items if s.in_scope]
        out_scope = [s for s in doc.scope_items if not s.in_scope]
        if in_scope or out_scope:
            lines.append("## Scope Boundaries")
            lines.append("")
            if in_scope:
                lines.append("### In Scope")
                for s in in_scope:
                    lines.append(f"- {s.description}")
                lines.append("")
            if out_scope:
                lines.append("### Out of Scope")
                for s in out_scope:
                    lines.append(f"- {s.description}")
                lines.append("")

        # --- LoC Estimates ---
        if doc.files:
            lines.append("## LoC Estimates")
            lines.append("")

            # Per-file estimates
            file_locs = [(f, self._estimate_file_loc(f)) for f in doc.files]
            total_loc = sum(loc for _, loc in file_locs)
            create_loc = sum(loc for f, loc in file_locs if f.action == "CREATE")
            modify_loc = sum(loc for f, loc in file_locs if f.action == "MODIFY")

            # Count how many files had source line data vs heuristic
            source_informed = sum(1 for f in doc.files if f.source_lines > 0 and f.action == "CREATE")
            heuristic_only = doc.create_count - source_informed
            total_source_lines = sum(f.source_lines for f in doc.files if f.source_lines > 0)

            method_parts = []
            if source_informed:
                method_parts.append(f"{source_informed} files estimated from source line counts "
                                    f"({total_source_lines:,} source lines × {self._RUST_EXPANSION_RATIO}x ratio)")
            if heuristic_only:
                method_parts.append(f"{heuristic_only} files estimated via extension heuristics")
            method_parts.append(f"MODIFY baseline: {self.config.loc_modify} LoC")
            lines.append("Estimation method: " + "; ".join(method_parts))
            lines.append("")
            lines.append("| Category | Files | Estimated LoC |")
            lines.append("|----------|-------|---------------|")
            lines.append(f"| CREATE | {doc.create_count} | {create_loc:,} |")
            lines.append(f"| MODIFY | {doc.modify_count} | {modify_loc:,} |")
            if doc.reference_count:
                lines.append(f"| REFERENCE | {doc.reference_count} | 0 |")
            if doc.delete_count:
                lines.append(f"| DELETE | {doc.delete_count} | 0 |")
            lines.append(f"| **Total** | **{len(doc.files)}** | **{total_loc:,}** |")
            lines.append("")

            # Layer breakdown
            layers = self._group_files_by_layer(doc)
            if layers:
                lines.append("### LoC by Layer")
                lines.append("")
                lines.append("| Layer | CREATE | MODIFY | Est. LoC |")
                lines.append("|-------|--------|--------|----------|")
                for layer_name, layer_files in sorted(layers.items()):
                    c = sum(1 for f in layer_files if f.action == "CREATE")
                    m = sum(1 for f in layer_files if f.action == "MODIFY")
                    layer_loc = sum(self._estimate_file_loc(f) for f in layer_files)
                    lines.append(f"| {layer_name} | {c} | {m} | {layer_loc:,} |")
                lines.append("")

            # Top files by estimated LoC (verbose only)
            if self.config.verbose:
                sorted_files = sorted(file_locs, key=lambda x: x[1], reverse=True)
                top_n = min(20, len(sorted_files))
                lines.append(f"### Top {top_n} Files by Estimated LoC")
                lines.append("")
                lines.append("| # | File | Action | Est. LoC |")
                lines.append("|---|------|--------|----------|")
                for i, (f, loc) in enumerate(sorted_files[:top_n], 1):
                    lines.append(f"| {i} | `{f.path}` | {f.action} | {loc:,} |")
                lines.append("")

        if self.config.verbose and doc.headings:
            lines.append("## Verbose: All Headings")
            lines.append("")
            for h in doc.headings:
                prefix = "  " * (h.level - 1)
                lines.append(f"{prefix}- [{h.number}] {h.title} (line {h.line})")
            lines.append("")

        return "\n".join(lines)

    def _format_heading_tree(self, heading: HeadingNode, lines: list[str], indent: int) -> None:
        """Format a heading and its children as an indented tree."""
        prefix = "  " * indent
        num_str = f"[{heading.number}] " if heading.number else ""
        lines.append(f"{prefix}- {num_str}{heading.title} (L{heading.line})")
        for child in heading.children:
            self._format_heading_tree(child, lines, indent + 1)

    # Per-extension LoC heuristics for CREATE files (more realistic than flat default)
    _LOC_BY_EXT: dict[str, int] = {
        # C++ implementation files — dialect IR, passes, conversions tend to be dense
        ".cpp": 350,
        ".cc": 300,
        ".c": 250,
        # C++ / TableGen headers
        ".h": 80,
        ".td": 120,
        # Rust source
        ".rs": 300,
        # Python source
        ".py": 200,
        # MLIR test files
        ".mlir": 80,
        # Build files
        "CMakeLists.txt": 25,
        # Shell scripts
        ".sh": 150,
        # HTML
        ".html": 200,
        # Markdown reports
        ".md": 100,
        # Config / metadata
        ".json": 30,
        ".toml": 20,
        ".yaml": 30,
        ".yml": 30,
        ".cfg": 15,
    }

    # Refinements based on path patterns (multiplied against base ext estimate)
    # More specific patterns MUST come before general ones — first match wins
    _LOC_PATH_MULTIPLIERS: list[tuple[str, float]] = [
        # Filename-specific overrides (check before directory patterns)
        ("__init__.py", 0.15),
        ("conftest.py", 0.5),
        ("config.py", 0.6),
        ("errors.py", 0.4),
        ("utils.py", 0.8),
        ("lit.cfg.py", 0.3),
        # Pybind11 bindings are dense
        ("python/src/", 1.5),
        # Test files are usually longer than source (setup, assertions, fixtures)
        ("test/integration/", 1.8),
        ("test/unit/", 1.4),
        ("tests/", 1.3),
    ]

    # C-to-Rust expansion ratio: Rust rewrites are typically 1.0–1.3x the C source
    # due to explicit error handling, type annotations, and pattern matching
    _RUST_EXPANSION_RATIO = 1.1

    def _estimate_file_loc(self, f: FileEntry) -> int:
        """Estimate LoC for a single file based on source line counts, extension, and path context.

        If the AAP table includes source line counts (e.g., '9,210 lines'), use those
        scaled by a language expansion ratio. Otherwise fall back to extension heuristics.
        """
        if f.action == "MODIFY":
            return self.config.loc_modify
        if f.action in ("REFERENCE", "DELETE"):
            return 0

        # If we have source line counts from the AAP, use them
        if f.source_lines > 0:
            return max(int(f.source_lines * self._RUST_EXPANSION_RATIO), 10)

        path = f.path
        basename = path.rsplit("/", 1)[-1] if "/" in path else path

        # Match full filename first (e.g., CMakeLists.txt)
        base_loc = self._LOC_BY_EXT.get(basename, 0)
        if not base_loc:
            # Match by extension
            ext = ""
            if "." in basename:
                ext = "." + basename.rsplit(".", 1)[-1]
            base_loc = self._LOC_BY_EXT.get(ext, self.config.loc_create)

        # Apply path-based multiplier (first match wins)
        for pattern, mult in self._LOC_PATH_MULTIPLIERS:
            if pattern in path:
                base_loc = int(base_loc * mult)
                break

        return max(base_loc, 5)  # floor at 5 LoC

    def _group_files_by_layer(self, doc: AAPDocument) -> dict[str, list[FileEntry]]:
        """Group files by architectural layer based on path patterns.

        Supports multiple project layouts:
        - Rust: src/, tests/
        - Python: python/, lib/, include/, test/
        - C++/MLIR: lib/, include/, bin/, third_party/
        - Generic: docs/, .github/, etc.
        """
        # Ordered rules — first match wins
        rules: list[tuple[str, list[str]]] = [
            # Tests (check before source so test/ under python/ matches here)
            ("Tests", [
                "python/test/", "test/", "tests/", "spec/",
            ]),
            # C++ MLIR dialect IR (TableGen defs + implementations)
            ("C++ Dialect IR", [
                "include/triton/Dialect/", "lib/Dialect/",
            ]),
            # C++ conversion passes
            ("C++ Conversion", [
                "include/triton/Conversion/", "lib/Conversion/",
            ]),
            # C++ analysis / target / tools
            ("C++ Infrastructure", [
                "lib/Analysis/", "lib/Target/", "lib/Tools/",
                "include/triton/Analysis/", "include/triton/Target/",
            ]),
            # PyBind11 bridge
            ("Python Bindings (pybind11)", [
                "python/src/",
            ]),
            # Python package source
            ("Python Source", [
                "python/triton/", "python/",
            ]),
            # Backend / third-party
            ("Backends", [
                "third_party/",
            ]),
            # CLI / binary tools
            ("CLI / Tools", [
                "bin/",
            ]),
            # Rust layout (crate directories and src/)
            ("Rust Source", [
                "exim-", "openssl-", "src/main.rs", "src/lib.rs", "src/",
            ]),
            # Build system
            ("Build System", [
                "CMakeLists.txt", "setup.py", "pyproject.toml",
                "Cargo.toml", "Makefile", ".cmake",
            ]),
            # CI/CD
            ("CI/CD", [
                ".github/", ".gitlab-ci", "Jenkinsfile",
            ]),
            # Benchmarking
            ("Benchmarking", [
                "bench/",
            ]),
            # Documentation
            ("Documentation", [
                "docs/", "doc/", "README", "CHANGELOG",
            ]),
        ]

        layers: dict[str, list[FileEntry]] = {}
        for f in doc.files:
            path = f.path
            matched = False
            for layer_name, prefixes in rules:
                for prefix in prefixes:
                    if prefix in path:
                        layers.setdefault(layer_name, []).append(f)
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                layers.setdefault("Other", []).append(f)
        return layers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Parse and analyze Agent Action Plan (AAP) documents.",
    )
    parser.add_argument(
        "file", type=str,
        help="path to the AAP markdown file",
    )
    parser.add_argument(
        "--loc-create", type=int, default=150, metavar="N",
        help="estimated LoC per CREATE file (default: 150)",
    )
    parser.add_argument(
        "--loc-modify", type=int, default=50, metavar="N",
        help="estimated LoC per MODIFY file (default: 50)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="include verbose heading dump in output",
    )
    parser.add_argument(
        "--focus", type=str, default="", metavar="AREA",
        help="focus review on a specific area (e.g. 'frontend', 'backend', 'rules')",
    )

    args = parser.parse_args()

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"Error: {file_path} not found.", file=sys.stderr)
        sys.exit(1)

    config = AAPConfig(
        file_path=str(file_path),
        loc_create=args.loc_create,
        loc_modify=args.loc_modify,
        verbose=args.verbose,
        focus=args.focus,
    )

    _load_languages()

    source = file_path.read_text(errors="replace")
    aap_parser = AAPParser(config)
    doc = aap_parser.parse(source)
    print(aap_parser.format_output(doc))


if __name__ == "__main__":
    main()
