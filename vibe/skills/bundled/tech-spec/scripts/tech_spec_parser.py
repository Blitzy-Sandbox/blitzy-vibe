#!/usr/bin/env python3
"""
Technical Specification parser using tree-sitter-markdown.

Parses tech spec documents — large structured markdown files with numbered
section hierarchies (# 1. / ## 1.X / ### 1.X.Y), feature catalogs (F-xxx),
component definitions, and architecture diagrams — and extracts structural
data for review.

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
class TechSpecConfig:
    """Configuration for tech spec parsing."""
    file_path: str = ""
    verbose: bool = False
    focus: str = ""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class HeadingNode:
    """A section heading in the tech spec hierarchy."""
    level: int
    number: str  # e.g. "1.2.1"
    title: str
    line: int
    children: list["HeadingNode"] = field(default_factory=list)


@dataclass
class FeatureEntry:
    """A feature from the feature catalog (F-xxx)."""
    feature_id: str  # e.g. "F-001"
    name: str
    category: str
    priority: str
    status: str
    has_overview: bool = False
    has_business_value: bool = False
    has_user_benefits: bool = False
    has_technical_context: bool = False
    has_dependencies: bool = False
    line: int = 0


@dataclass
class ComponentEntry:
    """A component from the system architecture."""
    name: str
    source_location: str
    responsibility: str
    line: int = 0


@dataclass
class TechnologyEntry:
    """A technology choice from the tech stack."""
    component: str
    technology: str
    details: str


@dataclass
class IntegrationEntry:
    """An external integration point."""
    system: str
    integration_type: str
    data_exchange: str


@dataclass
class ScopeItem:
    """An in-scope or out-of-scope item."""
    description: str
    in_scope: bool = True


@dataclass
class TechSpecDocument:
    """Aggregated data extracted from a tech spec document."""
    headings: list[HeadingNode] = field(default_factory=list)
    features: list[FeatureEntry] = field(default_factory=list)
    components: list[ComponentEntry] = field(default_factory=list)
    technologies: list[TechnologyEntry] = field(default_factory=list)
    integrations: list[IntegrationEntry] = field(default_factory=list)
    scope_items: list[ScopeItem] = field(default_factory=list)
    mermaid_diagrams: int = 0
    code_blocks: int = 0
    tables: int = 0
    total_lines: int = 0

    @property
    def major_sections(self) -> int:
        return len([h for h in self.headings if h.level == 1])

    @property
    def feature_count(self) -> int:
        return len(self.features)

    @property
    def complete_features(self) -> int:
        """Features with all required attributes."""
        return sum(1 for f in self.features if all([
            f.has_overview,
            f.has_business_value,
            f.has_user_benefits,
            f.has_technical_context,
            f.has_dependencies,
        ]))

    @property
    def in_scope_count(self) -> int:
        return sum(1 for s in self.scope_items if s.in_scope)

    @property
    def out_scope_count(self) -> int:
        return sum(1 for s in self.scope_items if not s.in_scope)


# ---------------------------------------------------------------------------
# Expected Sections
# ---------------------------------------------------------------------------

EXPECTED_SECTIONS = {
    "1": "Introduction",
    "1.1": "Executive Summary",
    "1.2": "System Overview",
    "1.3": "Scope",
    "1.4": "Technology Stack",
    "2": "Product Requirements",
    "2.1": "Feature Catalog",
    "3": "Technology Stack",
    "4": "Process Flowchart",
    "5": "System Architecture",
    "5.1": "High-Level Architecture",
    "5.2": "Component Details",
    "6": "System Components Design",
    "7": "User Interface Design",
    "8": "Infrastructure",
    "9": "Appendices",
}


# ---------------------------------------------------------------------------
# Node Visitors
# ---------------------------------------------------------------------------

class NodeVisitor:
    """Base class for markdown AST node visitors."""

    def __init__(self, config: TechSpecConfig):
        self.config = config

    def visit(self, node, doc: TechSpecDocument, source_lines: list[str]) -> None:
        """Visit a node and optionally extract data."""


class HeadingVisitor(NodeVisitor):
    """Builds numbered section hierarchy from atx_heading nodes."""

    # Match tech spec heading numbers like "1.", "1.1", "2.1.2"
    _NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)*\.?)\s+(.+)$")

    def visit(self, node, doc: TechSpecDocument, source_lines: list[str]) -> None:
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


class FeatureVisitor(NodeVisitor):
    """Extracts feature entries (F-xxx) from the feature catalog."""

    # Match feature IDs like F-001, F-012
    _FEATURE_ID_RE = re.compile(r"\bF-(\d{3})\b")

    def visit(self, node, doc: TechSpecDocument, source_lines: list[str]) -> None:
        if node.type != "section":
            return

        # Look for feature headings (#### F-xxx: Feature Name)
        for child in node.children:
            if child.type == "atx_heading":
                text = ""
                for sub in child.children:
                    if sub.type == "inline":
                        text = sub.text.decode("utf-8").strip()

                match = self._FEATURE_ID_RE.search(text)
                if match:
                    feature_id = f"F-{match.group(1)}"
                    # Extract feature name (after the ID)
                    name_match = re.search(r"F-\d{3}[:\s]+(.+)", text)
                    name = name_match.group(1).strip() if name_match else text

                    # Scan section content for required attributes
                    section_text = self._get_section_text(node, source_lines)
                    section_lower = section_text.lower()

                    feature = FeatureEntry(
                        feature_id=feature_id,
                        name=name,
                        category=self._extract_table_field(section_text, "category"),
                        priority=self._extract_table_field(section_text, "priority"),
                        status=self._extract_table_field(section_text, "status"),
                        has_overview="**overview**" in section_lower or "overview:" in section_lower,
                        has_business_value="**business value**" in section_lower or "business value:" in section_lower,
                        has_user_benefits="**user benefits**" in section_lower or "user benefits:" in section_lower,
                        has_technical_context="**technical context**" in section_lower or "technical context:" in section_lower,
                        has_dependencies="prerequisite features" in section_lower or "dependency type" in section_lower,
                        line=child.start_point[0] + 1,
                    )
                    doc.features.append(feature)

    def _get_section_text(self, section_node, source_lines: list[str]) -> str:
        """Get the full text content of a section."""
        start = section_node.start_point[0]
        end = min(section_node.end_point[0] + 1, len(source_lines))
        return "\n".join(source_lines[start:end])

    def _extract_table_field(self, text: str, field_name: str) -> str:
        """Extract a value from a markdown table row."""
        pattern = rf"\|\s*\*?\*?{field_name}\*?\*?\s*\|\s*([^|]+)\s*\|"
        match = re.search(pattern, text, re.IGNORECASE)
        return match.group(1).strip() if match else ""


class ComponentVisitor(NodeVisitor):
    """Extracts component definitions from architecture sections."""

    def visit(self, node, doc: TechSpecDocument, source_lines: list[str]) -> None:
        if node.type != "section":
            return

        # Look for component headings (### 5.2.X ComponentName)
        for child in node.children:
            if child.type == "atx_heading":
                text = ""
                for sub in child.children:
                    if sub.type == "inline":
                        text = sub.text.decode("utf-8").strip()

                # Check if this is in a component section (5.2.x or 6.x)
                match = re.match(r"^(5\.2\.\d+|6\.\d+(?:\.\d+)?)\s+(.+)", text)
                if match:
                    section_num = match.group(1)
                    component_name = match.group(2).strip()

                    # Extract source location and responsibility from content
                    section_text = self._get_section_text(node, source_lines)
                    source_loc = self._extract_source_location(section_text)
                    responsibility = self._extract_responsibility(section_text)

                    component = ComponentEntry(
                        name=component_name,
                        source_location=source_loc,
                        responsibility=responsibility,
                        line=child.start_point[0] + 1,
                    )
                    doc.components.append(component)

    def _get_section_text(self, section_node, source_lines: list[str]) -> str:
        start = section_node.start_point[0]
        end = min(section_node.end_point[0] + 1, len(source_lines))
        return "\n".join(source_lines[start:end])

    def _extract_source_location(self, text: str) -> str:
        """Extract source location from section text."""
        match = re.search(r"\*\*Source Location\*\*[:\s]*`?([^`\n]+)`?", text, re.IGNORECASE)
        if not match:
            match = re.search(r"Source Location[:\s]*`?([^`\n]+)`?", text, re.IGNORECASE)
        return match.group(1).strip() if match else ""

    def _extract_responsibility(self, text: str) -> str:
        """Extract responsibility from Purpose section."""
        match = re.search(r"Purpose and Responsibilities?\s*\n+(.+?)(?:\n\n|\n###|\n\|)", text, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()[:200] + "..." if len(match.group(1)) > 200 else match.group(1).strip()
        return ""


class TableVisitor(NodeVisitor):
    """Extracts technology and integration tables."""

    def visit(self, node, doc: TechSpecDocument, source_lines: list[str]) -> None:
        if node.type != "pipe_table":
            return

        doc.tables += 1

        header_cells = self._get_row_cells(node, "pipe_table_header")
        if not header_cells:
            return

        header_lower = [c.lower().strip() for c in header_cells]

        # Detect technology tables
        if self._is_tech_table(header_lower):
            self._extract_tech_entries(node, header_lower, doc)
        # Detect integration tables
        elif self._is_integration_table(header_lower):
            self._extract_integration_entries(node, header_lower, doc)

    def _is_tech_table(self, headers: list[str]) -> bool:
        return ("component" in headers or "technology" in headers) and "details" in headers

    def _is_integration_table(self, headers: list[str]) -> bool:
        return ("system" in headers or "system name" in headers) and "integration" in " ".join(headers)

    def _get_row_cells(self, table_node, row_type: str) -> list[str]:
        for child in table_node.children:
            if child.type == row_type:
                return self._cells_from_row(child)
        return []

    def _cells_from_row(self, row_node) -> list[str]:
        cells = []
        for child in row_node.children:
            if child.type == "pipe_table_cell":
                text = child.text.decode("utf-8").strip()
                text = text.strip("`")
                cells.append(text)
        return cells

    def _extract_tech_entries(self, table_node, headers, doc):
        comp_idx = -1
        for i, h in enumerate(headers):
            if "component" in h:
                comp_idx = i
                break
        tech_idx = -1
        for i, h in enumerate(headers):
            if "technology" in h:
                tech_idx = i
                break
        details_idx = headers.index("details") if "details" in headers else -1

        for child in table_node.children:
            if child.type != "pipe_table_row":
                continue
            cells = self._cells_from_row(child)

            component = cells[comp_idx].strip() if comp_idx >= 0 and len(cells) > comp_idx else ""
            technology = cells[tech_idx].strip() if tech_idx >= 0 and len(cells) > tech_idx else ""
            details = cells[details_idx].strip() if details_idx >= 0 and len(cells) > details_idx else ""

            if component or technology:
                doc.technologies.append(TechnologyEntry(
                    component=component,
                    technology=technology,
                    details=details,
                ))

    def _extract_integration_entries(self, table_node, headers, doc):
        sys_idx = -1
        for i, h in enumerate(headers):
            if "system" in h:
                sys_idx = i
                break
        int_idx = -1
        for i, h in enumerate(headers):
            if "integration" in h:
                int_idx = i
                break
        data_idx = -1
        for i, h in enumerate(headers):
            if "data" in h or "exchange" in h:
                data_idx = i
                break

        for child in table_node.children:
            if child.type != "pipe_table_row":
                continue
            cells = self._cells_from_row(child)

            system = cells[sys_idx].strip() if sys_idx >= 0 and len(cells) > sys_idx else ""
            int_type = cells[int_idx].strip() if int_idx >= 0 and len(cells) > int_idx else ""
            data_ex = cells[data_idx].strip() if data_idx >= 0 and len(cells) > data_idx else ""

            if system:
                doc.integrations.append(IntegrationEntry(
                    system=system,
                    integration_type=int_type,
                    data_exchange=data_ex,
                ))


class CodeBlockVisitor(NodeVisitor):
    """Detects mermaid diagrams and code blocks."""

    def visit(self, node, doc: TechSpecDocument, source_lines: list[str]) -> None:
        if node.type != "fenced_code_block":
            return

        doc.code_blocks += 1

        for child in node.children:
            if child.type == "info_string":
                lang_text = child.text.decode("utf-8").strip().lower()
                if lang_text == "mermaid":
                    doc.mermaid_diagrams += 1
                    break


class ScopeVisitor(NodeVisitor):
    """Extracts in-scope/out-of-scope items from Section 1.3."""

    def visit(self, node, doc: TechSpecDocument, source_lines: list[str]) -> None:
        if node.type != "section":
            return

        heading = self._get_section_heading(node)
        if not heading or not heading.startswith("1.3"):
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

        is_in_scope = "in-scope" in title_text or "in scope" in title_text
        is_out_scope = "out-of-scope" in title_text or "out of scope" in title_text or "excluded" in title_text

        # Also check section numbers
        if not is_in_scope and not is_out_scope:
            if heading == "1.3.1":
                is_in_scope = True
            elif heading == "1.3.2":
                is_out_scope = True
            else:
                return

        start_line = heading_node.end_point[0] + 1
        end_line = node.end_point[0]

        for i in range(start_line, min(end_line + 1, len(source_lines))):
            line = source_lines[i].strip()
            if line.startswith("- ") or line.startswith("* "):
                desc = line.lstrip("-* ").strip()
                if desc and not desc.startswith("|"):
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

class TechSpecParser:
    """Parses a tech spec document using tree-sitter-markdown."""

    def __init__(self, config: TechSpecConfig | None = None):
        self.config = config or TechSpecConfig()
        self._visitors = [
            HeadingVisitor(self.config),
            FeatureVisitor(self.config),
            ComponentVisitor(self.config),
            TableVisitor(self.config),
            CodeBlockVisitor(self.config),
            ScopeVisitor(self.config),
        ]

    def parse(self, source: str) -> TechSpecDocument:
        """Parse a tech spec document and return extracted data."""
        doc = TechSpecDocument()
        source_lines = source.splitlines()
        doc.total_lines = len(source_lines)

        parser = get_parser("markdown")
        if parser is None:
            return self._fallback_parse(source, source_lines)

        tree = parser.parse(source.encode("utf-8"))
        self._walk_tree(tree.root_node, doc, source_lines)
        self._build_hierarchy(doc)

        return doc

    def _walk_tree(self, node, doc: TechSpecDocument, source_lines: list[str]) -> None:
        for visitor in self._visitors:
            visitor.visit(node, doc, source_lines)
        for child in node.children:
            self._walk_tree(child, doc, source_lines)

    def _build_hierarchy(self, doc: TechSpecDocument) -> None:
        if not doc.headings:
            return

        stack: list[HeadingNode] = []
        for heading in doc.headings:
            while stack and stack[-1].level >= heading.level:
                stack.pop()
            if stack:
                stack[-1].children.append(heading)
            stack.append(heading)

    def _fallback_parse(self, source: str, source_lines: list[str]) -> TechSpecDocument:
        """Regex-based fallback when tree-sitter-markdown is unavailable."""
        doc = TechSpecDocument()
        doc.total_lines = len(source_lines)

        heading_re = re.compile(r"^(#{1,6})\s+(.+)$")
        number_re = re.compile(r"^(\d+(?:\.\d+)*\.?)\s+(.+)$")
        feature_re = re.compile(r"\bF-(\d{3})\b")

        for i, line in enumerate(source_lines):
            # Headings
            m = heading_re.match(line)
            if m:
                level = len(m.group(1))
                text = m.group(2).strip()
                nm = number_re.match(text)
                number = nm.group(1).rstrip(".") if nm else ""
                title = nm.group(2).strip() if nm else text
                doc.headings.append(HeadingNode(level=level, number=number, title=title, line=i + 1))

                # Check for feature IDs
                fm = feature_re.search(text)
                if fm:
                    feature_id = f"F-{fm.group(1)}"
                    name_match = re.search(r"F-\d{3}[:\s]+(.+)", text)
                    name = name_match.group(1).strip() if name_match else text
                    doc.features.append(FeatureEntry(
                        feature_id=feature_id,
                        name=name,
                        category="",
                        priority="",
                        status="",
                        line=i + 1,
                    ))

            # Mermaid diagrams
            if line.strip().startswith("```mermaid"):
                doc.mermaid_diagrams += 1
            if line.strip().startswith("```") and not line.strip() == "```":
                doc.code_blocks += 1

            # Tables
            if line.strip().startswith("|") and "|" in line[1:]:
                doc.tables += 1

        self._build_hierarchy(doc)
        return doc

    def format_output(self, doc: TechSpecDocument) -> str:
        """Format extracted data as structured markdown output."""
        lines = []

        # --- Summary ---
        lines.append("# Tech Spec Analysis Summary")
        lines.append("")
        lines.append(f"- **Total lines**: {doc.total_lines:,}")
        lines.append(f"- **Major sections**: {doc.major_sections}")
        lines.append(f"- **Total headings**: {len(doc.headings)}")
        lines.append(f"- **Features (F-xxx)**: {doc.feature_count} ({doc.complete_features} complete)")
        lines.append(f"- **Components**: {len(doc.components)}")
        lines.append(f"- **Technology entries**: {len(doc.technologies)}")
        lines.append(f"- **Integration points**: {len(doc.integrations)}")
        lines.append(f"- **Scope items**: {len(doc.scope_items)} ({doc.in_scope_count} in-scope, {doc.out_scope_count} out-of-scope)")
        lines.append(f"- **Mermaid diagrams**: {doc.mermaid_diagrams}")
        lines.append(f"- **Code blocks**: {doc.code_blocks}")
        lines.append(f"- **Tables**: {doc.tables}")
        lines.append("")

        # --- Section Coverage ---
        lines.append("## Section Coverage")
        lines.append("")
        found_sections = {h.number for h in doc.headings if h.number}
        missing = []
        present = []
        for num, name in sorted(EXPECTED_SECTIONS.items()):
            if num in found_sections:
                present.append(f"[{num}] {name}")
            else:
                missing.append(f"[{num}] {name}")

        if missing:
            lines.append("### Missing Sections")
            for s in missing:
                lines.append(f"- {s}")
            lines.append("")

        lines.append(f"**Coverage**: {len(present)}/{len(EXPECTED_SECTIONS)} expected sections found")
        lines.append("")

        # --- Section Hierarchy ---
        lines.append("## Section Hierarchy")
        lines.append("")
        top_level = [h for h in doc.headings if h.level == 1]
        for h in top_level:
            self._format_heading_tree(h, lines, indent=0)
        lines.append("")

        # --- Features ---
        if doc.features:
            lines.append("## Feature Catalog")
            lines.append("")
            lines.append("| Feature ID | Name | Category | Priority | Status | Complete |")
            lines.append("|------------|------|----------|----------|--------|----------|")
            for f in doc.features:
                complete = "Yes" if all([
                    f.has_overview, f.has_business_value, f.has_user_benefits,
                    f.has_technical_context, f.has_dependencies
                ]) else "Partial"
                lines.append(f"| {f.feature_id} | {f.name[:40]}{'...' if len(f.name) > 40 else ''} | {f.category or '-'} | {f.priority or '-'} | {f.status or '-'} | {complete} |")
            lines.append("")

            # Feature completeness breakdown
            incomplete = [f for f in doc.features if not all([
                f.has_overview, f.has_business_value, f.has_user_benefits,
                f.has_technical_context, f.has_dependencies
            ])]
            if incomplete:
                lines.append("### Incomplete Features")
                lines.append("")
                for f in incomplete:
                    missing_attrs = []
                    if not f.has_overview:
                        missing_attrs.append("Overview")
                    if not f.has_business_value:
                        missing_attrs.append("Business Value")
                    if not f.has_user_benefits:
                        missing_attrs.append("User Benefits")
                    if not f.has_technical_context:
                        missing_attrs.append("Technical Context")
                    if not f.has_dependencies:
                        missing_attrs.append("Dependencies")
                    lines.append(f"- **{f.feature_id}**: Missing {', '.join(missing_attrs)}")
                lines.append("")

        # --- Components ---
        if doc.components:
            lines.append("## Components")
            lines.append("")
            lines.append("| Component | Source Location |")
            lines.append("|-----------|-----------------|")
            for c in doc.components:
                lines.append(f"| {c.name} | `{c.source_location or 'N/A'}` |")
            lines.append("")

        # --- Technologies ---
        if doc.technologies:
            lines.append("## Technology Stack")
            lines.append("")
            lines.append("| Component | Technology | Details |")
            lines.append("|-----------|------------|---------|")
            for t in doc.technologies:
                lines.append(f"| {t.component} | {t.technology} | {t.details[:50]}{'...' if len(t.details) > 50 else ''} |")
            lines.append("")

        # --- Integrations ---
        if doc.integrations:
            lines.append("## Integration Points")
            lines.append("")
            lines.append("| System | Integration Type | Data Exchange |")
            lines.append("|--------|------------------|---------------|")
            for i in doc.integrations:
                lines.append(f"| {i.system} | {i.integration_type} | {i.data_exchange[:40]}{'...' if len(i.data_exchange) > 40 else ''} |")
            lines.append("")

        # --- Scope ---
        in_scope = [s for s in doc.scope_items if s.in_scope]
        out_scope = [s for s in doc.scope_items if not s.in_scope]
        if in_scope or out_scope:
            lines.append("## Scope Boundaries")
            lines.append("")
            if in_scope:
                lines.append("### In Scope")
                for s in in_scope[:10]:  # Limit to first 10
                    lines.append(f"- {s.description[:100]}{'...' if len(s.description) > 100 else ''}")
                if len(in_scope) > 10:
                    lines.append(f"- *(+{len(in_scope) - 10} more)*")
                lines.append("")
            if out_scope:
                lines.append("### Out of Scope")
                for s in out_scope[:10]:
                    lines.append(f"- {s.description[:100]}{'...' if len(s.description) > 100 else ''}")
                if len(out_scope) > 10:
                    lines.append(f"- *(+{len(out_scope) - 10} more)*")
                lines.append("")

        # --- Verbose output ---
        if self.config.verbose and doc.headings:
            lines.append("## Verbose: All Headings")
            lines.append("")
            for h in doc.headings:
                prefix = "  " * (h.level - 1)
                lines.append(f"{prefix}- [{h.number}] {h.title} (line {h.line})")
            lines.append("")

        return "\n".join(lines)

    def _format_heading_tree(self, heading: HeadingNode, lines: list[str], indent: int) -> None:
        prefix = "  " * indent
        num_str = f"[{heading.number}] " if heading.number else ""
        lines.append(f"{prefix}- {num_str}{heading.title} (L{heading.line})")
        for child in heading.children:
            self._format_heading_tree(child, lines, indent + 1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Parse and analyze Technical Specification documents.",
    )
    parser.add_argument(
        "file", type=str,
        help="path to the tech spec markdown file",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="include verbose heading dump in output",
    )
    parser.add_argument(
        "--focus", type=str, default="", metavar="AREA",
        help="focus review on a specific area (e.g. 'requirements', 'architecture')",
    )

    args = parser.parse_args()

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"Error: {file_path} not found.", file=sys.stderr)
        sys.exit(1)

    config = TechSpecConfig(
        file_path=str(file_path),
        verbose=args.verbose,
        focus=args.focus,
    )

    _load_languages()

    source = file_path.read_text(errors="replace")
    ts_parser = TechSpecParser(config)
    doc = ts_parser.parse(source)
    print(ts_parser.format_output(doc))


if __name__ == "__main__":
    main()
