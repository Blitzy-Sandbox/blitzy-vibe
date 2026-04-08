#!/usr/bin/env python3
"""
Tree-sitter codebase mapper and code smell detector.

Walks the current working directory, parses source files with tree-sitter,
and emits a structural map + prioritized code smell report.

Requires: pip install tree-sitter tree-sitter-python tree-sitter-javascript
           tree-sitter-typescript tree-sitter-rust tree-sitter-go
           tree-sitter-java tree-sitter-ruby tree-sitter-c tree-sitter-cpp
           tree-sitter-kotlin tree-sitter-php
"""

import argparse
import difflib
import os
import subprocess
import sys
import warnings
from collections import defaultdict
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
    """Load all available tree-sitter languages."""
    loaders = {
        "python": ("tree_sitter_python", "language"),
        "javascript": ("tree_sitter_javascript", "language"),
        "typescript": ("tree_sitter_typescript", "language_typescript"),
        "tsx": ("tree_sitter_typescript", "language_tsx"),
        "rust": ("tree_sitter_rust", "language"),
        "go": ("tree_sitter_go", "language"),
        "java": ("tree_sitter_java", "language"),
        "ruby": ("tree_sitter_ruby", "language"),
        "c": ("tree_sitter_c", "language"),
        "cpp": ("tree_sitter_cpp", "language"),
        "kotlin": ("tree_sitter_kotlin", "language"),
        "php": ("tree_sitter_php", "language_php"),
    }
    for lang_name, (module_name, func_name) in loaders.items():
        try:
            mod = __import__(module_name)
            lang_func = getattr(mod, func_name)
            _LANGUAGES[lang_name] = Language(lang_func())
        except ImportError:
            pass  # Language not installed, skip
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

DEFAULT_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", "venv", ".venv", "env",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".next", ".nuxt", "target", "out", ".eggs", "*.egg-info",
    ".claude", ".idea", ".vscode", "coverage", "htmlcov",
    ".cache", ".parcel-cache", ".turbo", ".vercel", ".output",
    "vendor", "bower_components", ".gradle", ".mvn",
    "_build", "site-packages", ".bundle",
}

EXT_TO_LANG = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".php": "php",
}


@dataclass
class MapperConfig:
    """Configuration for codebase mapping thresholds and behavior."""
    long_func_lines: int = 50
    god_class_methods: int = 15
    deep_nesting_levels: int = 4
    many_params: int = 5
    large_file_lines: int = 500
    skip_smells: set[str] = field(default_factory=set)
    extra_ignore_dirs: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Tree-sitter node types per language
# ---------------------------------------------------------------------------

LANG_NODE_TYPES = {
    "python": {
        "classes": ["class_definition"],
        "functions": ["function_definition"],
        "imports": ["import_statement", "import_from_statement"],
        "decorators": ["decorator"],
        "docstrings": ["expression_statement"],
        "except_clauses": ["except_clause"],
    },
    "typescript": {
        "classes": ["class_declaration"],
        "functions": ["function_declaration", "method_definition", "arrow_function"],
        "imports": ["import_statement"],
        "interfaces": ["interface_declaration"],
        "type_aliases": ["type_alias_declaration"],
    },
    "tsx": {
        "classes": ["class_declaration"],
        "functions": ["function_declaration", "method_definition", "arrow_function"],
        "imports": ["import_statement"],
        "interfaces": ["interface_declaration"],
        "type_aliases": ["type_alias_declaration"],
    },
    "javascript": {
        "classes": ["class_declaration"],
        "functions": ["function_declaration", "method_definition", "arrow_function"],
        "imports": ["import_statement"],
    },
    "rust": {
        "classes": ["struct_item", "enum_item", "impl_item"],
        "functions": ["function_item"],
        "imports": ["use_declaration"],
    },
    "go": {
        "classes": ["type_declaration"],
        "functions": ["function_declaration", "method_declaration"],
        "imports": ["import_declaration"],
    },
    "java": {
        "classes": ["class_declaration", "interface_declaration", "enum_declaration"],
        "functions": ["method_declaration", "constructor_declaration"],
        "imports": ["import_declaration"],
    },
    "ruby": {
        "classes": ["class", "module"],
        "functions": ["method", "singleton_method"],
        "imports": ["call"],  # require/require_relative
    },
    "c": {
        "classes": ["struct_specifier"],
        "functions": ["function_definition"],
        "imports": ["preproc_include"],
    },
    "cpp": {
        "classes": ["class_specifier", "struct_specifier"],
        "functions": ["function_definition"],
        "imports": ["preproc_include"],
    },
    "kotlin": {
        "classes": ["class_declaration", "object_declaration"],
        "functions": ["function_declaration"],
        "imports": ["import_header"],
    },
    "php": {
        "classes": ["class_declaration", "interface_declaration", "trait_declaration"],
        "functions": ["function_definition", "method_declaration"],
        "imports": ["namespace_use_declaration"],
    },
}

# ---------------------------------------------------------------------------
# FileContext dataclass
# ---------------------------------------------------------------------------


@dataclass
class FileContext:
    """Per-file mutable state during AST traversal."""
    rel_path: str
    lang: str
    node_types: dict
    source_text: str
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)
    type_aliases: list[str] = field(default_factory=list)
    smells: list[dict] = field(default_factory=list)
    function_bodies: list[tuple] = field(default_factory=list)  # (name, line, source)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def should_ignore(path: Path, ignore_dirs: set[str]) -> bool:
    """Check if any component of the path matches an ignore pattern."""
    parts = path.parts
    for part in parts:
        if part in ignore_dirs:
            return True
        for pattern in ignore_dirs:
            if "*" in pattern and part.endswith(pattern.replace("*", "")):
                return True
    return False


def get_node_name(node) -> str | None:
    """Extract the name from a tree-sitter node."""
    for child in node.children:
        if child.type in (
            "identifier", "name", "type_identifier",
            "property_identifier", "simple_identifier",
        ):
            return child.text.decode("utf-8")
        if child.type == "dotted_name":
            return child.text.decode("utf-8")
    return None


def get_function_params(node, lang: str) -> list[str]:
    """Extract parameter names from a function node."""
    params = []
    for child in node.children:
        if child.type in (
            "parameters", "formal_parameters", "parameter_list",
            "function_value_parameters",
        ):
            for param in child.children:
                if param.type in (
                    "identifier", "typed_parameter", "typed_default_parameter",
                    "default_parameter", "required_parameter", "optional_parameter",
                    "formal_parameter", "parameter",
                ):
                    name = get_node_name(param)
                    if name and name not in ("self", "cls"):
                        params.append(name)
                    elif param.type == "identifier" and param.text:
                        text = param.text.decode("utf-8")
                        if text not in ("self", "cls"):
                            params.append(text)
    return params


def count_nesting_depth(node, current_depth: int = 0) -> int:
    """Find the maximum nesting depth of control flow in a node."""
    nesting_types = {
        "if_statement", "for_statement", "while_statement", "try_statement",
        "with_statement", "for_in_statement", "if_expression",
        "match_statement", "case_clause",
        "switch_statement", "for_of_statement",
    }
    max_depth = current_depth
    for child in node.children:
        if child.type in nesting_types:
            child_depth = count_nesting_depth(child, current_depth + 1)
            max_depth = max(max_depth, child_depth)
        else:
            child_depth = count_nesting_depth(child, current_depth)
            max_depth = max(max_depth, child_depth)
    return max_depth


def has_docstring(node) -> bool:
    """Check if a Python function or class has a docstring as its first statement."""
    body = None
    for child in node.children:
        if child.type == "block":
            body = child
            break
    if body is None:
        return True  # Can't tell, assume fine
    for child in body.children:
        if child.type == "expression_statement":
            for sub in child.children:
                if sub.type == "string":
                    return True
            return False
        elif child.type == "comment":
            continue
        else:
            return False
    return False


def is_catchall_except(node) -> bool:
    """Check if an except clause is a bare except or catches Exception."""
    children_types = [c.type for c in node.children]
    if "except" in children_types or node.type == "except_clause":
        has_type = False
        for child in node.children:
            if child.type in ("identifier", "as_pattern"):
                text = child.text.decode("utf-8") if child.text else ""
                if "Exception" in text:
                    return True
                has_type = True
        # bare except: with no type specified
        if not has_type and node.child_count <= 3:
            texts = [c.text.decode("utf-8") for c in node.children if c.text]
            if "except" in texts and not any(
                c.type in ("identifier", "as_pattern", "tuple") for c in node.children
            ):
                return True
    return False


def get_import_text(node) -> str:
    """Get a readable import string from an import node."""
    text = node.text.decode("utf-8").strip()
    if len(text) > 80:
        text = text[:77] + "..."
    return text


# ---------------------------------------------------------------------------
# Node Visitors
# ---------------------------------------------------------------------------


class NodeVisitor:
    """Base class for AST node visitors."""

    def __init__(self, config: MapperConfig):
        self.config = config

    def visit(self, node, ctx: FileContext) -> None:
        """Visit a node and optionally extract data or record smells."""


class ClassVisitor(NodeVisitor):
    """Extracts class declarations and detects god classes / missing docstrings."""

    def visit(self, node, ctx: FileContext) -> None:
        if node.type not in ctx.node_types.get("classes", []):
            return
        name = get_node_name(node)
        if not name:
            return
        ctx.classes.append(name)

        # Count methods for god class detection
        method_count = 0
        for child in node.children:
            if child.type in ("block", "class_body", "declaration_list", "field_declaration_list"):
                for member in child.children:
                    if member.type in (
                        "function_definition", "method_definition",
                        "method_declaration", "function_item", "function_declaration",
                    ):
                        method_count += 1
        if method_count > self.config.god_class_methods:
            ctx.smells.append({
                "type": "GOD_CLASS",
                "severity": "high",
                "file": ctx.rel_path,
                "line": node.start_point[0] + 1,
                "detail": f"{name} has {method_count} methods",
            })

        # Missing docstring (Python) — skip test files
        if (ctx.lang == "python"
                and not has_docstring(node)
                and not ctx.rel_path.startswith("tests/")):
            ctx.smells.append({
                "type": "MISSING_DOCSTRING",
                "severity": "low",
                "file": ctx.rel_path,
                "line": node.start_point[0] + 1,
                "detail": f"class {name}",
            })


class FunctionVisitor(NodeVisitor):
    """Extracts function declarations and detects long/deep/many-param functions."""

    def visit(self, node, ctx: FileContext) -> None:
        if node.type not in ctx.node_types.get("functions", []):
            return
        name = get_node_name(node)
        if not name:
            return
        ctx.functions.append(name)

        func_lines = node.end_point[0] - node.start_point[0] + 1

        # Long function detection
        if func_lines > self.config.long_func_lines:
            ctx.smells.append({
                "type": "LONG_FUNCTION",
                "severity": "medium",
                "file": ctx.rel_path,
                "line": node.start_point[0] + 1,
                "detail": f"{name}() is {func_lines} lines",
            })

        # Deep nesting detection
        depth = count_nesting_depth(node)
        if depth > self.config.deep_nesting_levels:
            ctx.smells.append({
                "type": "DEEP_NESTING",
                "severity": "medium",
                "file": ctx.rel_path,
                "line": node.start_point[0] + 1,
                "detail": f"{name}() has {depth} nesting levels",
            })

        # Too many parameters
        params = get_function_params(node, ctx.lang)
        if len(params) > self.config.many_params:
            ctx.smells.append({
                "type": "MANY_PARAMS",
                "severity": "medium",
                "file": ctx.rel_path,
                "line": node.start_point[0] + 1,
                "detail": f"{name}() has {len(params)} parameters",
            })

        # Missing docstring (Python) — skip test files and private/test functions
        if (ctx.lang == "python" and not has_docstring(node)
                and not name.startswith("_")
                and not name.startswith("test_")
                and not ctx.rel_path.startswith("tests/")):
            ctx.smells.append({
                "type": "MISSING_DOCSTRING",
                "severity": "low",
                "file": ctx.rel_path,
                "line": node.start_point[0] + 1,
                "detail": f"{name}()",
            })

        # Store function body for duplicate detection (min 10 lines)
        if func_lines >= 10:
            func_source = node.text.decode("utf-8", errors="replace")
            ctx.function_bodies.append((name, node.start_point[0] + 1, func_source))


class ImportVisitor(NodeVisitor):
    """Extracts import statements."""

    def visit(self, node, ctx: FileContext) -> None:
        if node.type not in ctx.node_types.get("imports", []):
            return
        imp_text = get_import_text(node)
        if imp_text:
            ctx.imports.append(imp_text)


class InterfaceVisitor(NodeVisitor):
    """Extracts interface declarations (TS/TSX)."""

    def visit(self, node, ctx: FileContext) -> None:
        if node.type not in ctx.node_types.get("interfaces", []):
            return
        name = get_node_name(node)
        if name:
            ctx.interfaces.append(name)


class TypeAliasVisitor(NodeVisitor):
    """Extracts type alias declarations (TS/TSX)."""

    def visit(self, node, ctx: FileContext) -> None:
        if node.type not in ctx.node_types.get("type_aliases", []):
            return
        name = get_node_name(node)
        if name:
            ctx.type_aliases.append(name)


class CatchAllVisitor(NodeVisitor):
    """Detects bare except / catch-all exception handlers (Python)."""

    def visit(self, node, ctx: FileContext) -> None:
        if ctx.lang != "python" or node.type != "except_clause":
            return
        if is_catchall_except(node):
            ctx.smells.append({
                "type": "CATCH_ALL_EXCEPTION",
                "severity": "high",
                "file": ctx.rel_path,
                "line": node.start_point[0] + 1,
                "detail": node.text.decode("utf-8", errors="replace").split("\n")[0].strip(),
            })


class DeadCodeVisitor(NodeVisitor):
    """Detects unreachable statements after return/raise/break/continue."""

    BLOCK_TYPES = {"block", "statement_block", "compound_statement"}
    TERMINAL_TYPES = {
        "return_statement", "raise_statement", "break_statement",
        "continue_statement", "throw_statement",
    }
    SKIP_TYPES = {"comment", "newline", "NEWLINE", "INDENT", "DEDENT"}

    def visit(self, node, ctx: FileContext) -> None:
        if node.type not in self.BLOCK_TYPES:
            return
        children = [c for c in node.children if c.is_named and c.type not in self.SKIP_TYPES]
        found_terminal = False
        terminal_type = ""
        for child in children:
            if found_terminal:
                ctx.smells.append({
                    "type": "DEAD_CODE",
                    "severity": "medium",
                    "file": ctx.rel_path,
                    "line": child.start_point[0] + 1,
                    "detail": f"unreachable code after {terminal_type.replace('_statement', '')}",
                })
                break  # Only report once per block
            if child.type in self.TERMINAL_TYPES:
                found_terminal = True
                terminal_type = child.type


# ---------------------------------------------------------------------------
# Main mapper
# ---------------------------------------------------------------------------


class CodebaseMapper:
    def __init__(self, root: Path, config: MapperConfig | None = None):
        self.root = root
        self.config = config or MapperConfig()
        self.files_parsed = 0
        self.file_data: dict[str, dict] = {}  # rel_path -> parsed info
        self.smells: list[dict] = []
        self.import_graph: dict[str, set[str]] = defaultdict(set)  # module -> imports
        self._ignore_dirs = DEFAULT_IGNORE_DIRS | self.config.extra_ignore_dirs
        self._function_bodies: list[tuple] = []  # (file, name, line, source)

        # Initialize visitors
        self._visitors = [
            ClassVisitor(self.config),
            FunctionVisitor(self.config),
            ImportVisitor(self.config),
            InterfaceVisitor(self.config),
            TypeAliasVisitor(self.config),
            CatchAllVisitor(self.config),
            DeadCodeVisitor(self.config),
        ]

    def scan(self):
        """Walk the codebase and parse all recognized source files."""
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = Path(dirpath).relative_to(self.root)
            if should_ignore(rel_dir, self._ignore_dirs):
                dirnames.clear()
                continue
            # Prune ignored dirs from traversal
            dirnames[:] = [
                d for d in dirnames
                if not should_ignore(rel_dir / d, self._ignore_dirs)
            ]
            for fname in sorted(filenames):
                fpath = Path(dirpath) / fname
                ext = fpath.suffix
                if ext not in EXT_TO_LANG:
                    continue
                lang = EXT_TO_LANG[ext]
                rel_path = str(fpath.relative_to(self.root))
                self._parse_file(fpath, rel_path, lang)

    def _parse_file(self, fpath: Path, rel_path: str, lang: str):
        """Parse a single file with tree-sitter."""
        try:
            source = fpath.read_bytes()
        except (OSError, PermissionError):
            return

        parser = get_parser(lang)
        if parser is None:
            return

        try:
            tree = parser.parse(source)
        except Exception:
            return

        self.files_parsed += 1
        source_text = source.decode("utf-8", errors="replace")
        line_count = source_text.count("\n") + 1

        # Check large file smell
        if line_count > self.config.large_file_lines:
            self.smells.append({
                "type": "LARGE_FILE",
                "severity": "low",
                "file": rel_path,
                "line": None,
                "detail": f"{line_count:,} lines",
            })

        node_types = LANG_NODE_TYPES.get(lang, {})
        ctx = FileContext(
            rel_path=rel_path,
            lang=lang,
            node_types=node_types,
            source_text=source_text,
        )

        self._walk_tree(tree.root_node, ctx)

        # Collect smells from context
        self.smells.extend(ctx.smells)

        # Collect function bodies for duplicate detection
        self._function_bodies.extend(
            (rel_path, name, line, src)
            for name, line, src in ctx.function_bodies
        )

        # Build import graph (Python-specific)
        if lang == "python":
            module_name = rel_path.replace("/", ".").replace(".py", "")
            for imp_text in ctx.imports:
                parts = imp_text.split()
                if len(parts) >= 2 and parts[0] == "from":
                    imported_module = parts[1]
                elif len(parts) >= 2 and parts[0] == "import":
                    imported_module = parts[1].rstrip(",")
                else:
                    continue
                self.import_graph[module_name].add(imported_module)

        self.file_data[rel_path] = {
            "lang": lang,
            "classes": ctx.classes,
            "functions": ctx.functions,
            "imports": ctx.imports,
            "interfaces": ctx.interfaces,
            "type_aliases": ctx.type_aliases,
            "line_count": line_count,
        }

    def _walk_tree(self, node, ctx: FileContext):
        """Iteratively walk the AST and dispatch to visitors."""
        stack = [node]
        while stack:
            current = stack.pop()
            for visitor in self._visitors:
                visitor.visit(current, ctx)
            # Push children in reverse order so leftmost child is processed first
            stack.extend(reversed(current.children))

    def detect_circular_imports(self):
        """Detect circular import cycles in the import graph."""
        known_modules = set(self.import_graph.keys())

        def find_cycles():
            visited = set()
            rec_stack = set()
            cycles = []

            def dfs(module, path):
                visited.add(module)
                rec_stack.add(module)
                path.append(module)

                for imported in self.import_graph.get(module, set()):
                    matched = None
                    for known in known_modules:
                        if known == imported or known.endswith("." + imported):
                            matched = known
                            break
                        if imported.startswith("."):
                            base = ".".join(module.split(".")[:-1])
                            candidate = base + imported
                            if candidate in known_modules:
                                matched = candidate
                                break

                    if matched:
                        if matched in rec_stack:
                            cycle_start = path.index(matched)
                            cycle = path[cycle_start:] + [matched]
                            cycles.append(cycle)
                        elif matched not in visited:
                            dfs(matched, path)

                path.pop()
                rec_stack.discard(module)

            for module in known_modules:
                if module not in visited:
                    dfs(module, [])

            return cycles

        cycles = find_cycles()
        seen = set()
        for cycle in cycles:
            key = " -> ".join(sorted(cycle[:-1]))
            if key not in seen:
                seen.add(key)
                self.smells.append({
                    "type": "CIRCULAR_IMPORT",
                    "severity": "high",
                    "file": cycle[0].replace(".", "/") + ".py",
                    "line": None,
                    "detail": " -> ".join(cycle),
                })

    def detect_unused_imports(self):
        """Detect imported names not used in non-import source text (Python only)."""
        for rel_path, info in self.file_data.items():
            if info["lang"] != "python":
                continue
            fpath = self.root / rel_path
            try:
                source_text = fpath.read_text(errors="replace")
            except (OSError, PermissionError):
                continue

            # Build non-import source text
            non_import_lines = []
            for line in source_text.splitlines():
                stripped = line.strip()
                if not stripped.startswith(("import ", "from ")):
                    non_import_lines.append(line)
            non_import_text = "\n".join(non_import_lines)

            for imp_text in info["imports"]:
                names = self._extract_imported_names(imp_text)
                for name in names:
                    if name and name != "*" and name not in non_import_text:
                        self.smells.append({
                            "type": "UNUSED_IMPORT",
                            "severity": "low",
                            "file": rel_path,
                            "line": None,
                            "detail": f"'{name}' appears unused",
                        })

    @staticmethod
    def _extract_imported_names(imp_text: str) -> list[str]:
        """Extract the names that are actually used from an import statement."""
        parts = imp_text.split()
        if not parts:
            return []
        if parts[0] == "from" and "import" in parts:
            idx = parts.index("import")
            rest = " ".join(parts[idx + 1:])
            rest = rest.strip("()")
            names = []
            for item in rest.split(","):
                item = item.strip()
                if " as " in item:
                    names.append(item.split(" as ")[-1].strip())
                elif item and item != "*":
                    names.append(item)
            return names
        elif parts[0] == "import":
            names = []
            for item in " ".join(parts[1:]).split(","):
                item = item.strip()
                if " as " in item:
                    names.append(item.split(" as ")[-1].strip())
                else:
                    name = item.split(".")[0].strip()
                    if name:
                        names.append(name)
            return names
        return []

    def detect_duplicate_logic(self):
        """Detect functions with high token similarity (potential duplicates).

        Uses length-based pre-filtering and a comparison cap to stay fast on
        large codebases (O(n²) SequenceMatcher is expensive).
        """
        MAX_FUNCTIONS = 2000  # Skip duplicate detection if too many functions
        MAX_DUPLICATES = 50   # Stop after finding this many duplicates

        if len(self._function_bodies) < 2:
            return
        if len(self._function_bodies) > MAX_FUNCTIONS:
            print(
                f"Warning: {len(self._function_bodies)} functions found, "
                f"skipping duplicate detection (cap: {MAX_FUNCTIONS}). "
                f"Use --skip-smells DUPLICATE_LOGIC to silence.",
                file=sys.stderr,
            )
            return

        def normalize(source: str) -> str:
            lines = []
            for line in source.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and not stripped.startswith("//"):
                    lines.append(stripped)
            return "\n".join(lines)

        normalized = [(f, n, l, normalize(s)) for f, n, l, s in self._function_bodies]
        # Sort by source length for efficient length-based pre-filtering
        normalized.sort(key=lambda x: len(x[3]))
        found = 0

        for i in range(len(normalized)):
            if found >= MAX_DUPLICATES:
                break
            file_a, name_a, line_a, src_a = normalized[i]
            len_a = len(src_a)
            if len_a < 40:  # Skip very short normalized sources
                continue
            for j in range(i + 1, len(normalized)):
                file_b, name_b, line_b, src_b = normalized[j]
                len_b = len(src_b)
                # Length pre-filter: if lengths differ by >40%, similarity can't exceed 0.8
                if len_b > len_a * 1.4:
                    break  # Sorted by length, so all remaining are longer
                ratio = difflib.SequenceMatcher(None, src_a, src_b).ratio()
                if ratio > 0.8:
                    self.smells.append({
                        "type": "DUPLICATE_LOGIC",
                        "severity": "medium",
                        "file": file_a,
                        "line": line_a,
                        "detail": f"{name_a}() ~{ratio:.0%} similar to {name_b}() in {file_b}:{line_b}",
                    })
                    found += 1
                    if found >= MAX_DUPLICATES:
                        break

    def format_output(self) -> str:
        """Format the structural map and smell report."""
        # Filter out skipped smells
        active_smells = [
            s for s in self.smells
            if s["type"] not in self.config.skip_smells
        ]

        lines = []
        lines.append(f"# Codebase Structure ({self.files_parsed} files parsed)\n")

        # --- Summary statistics ---
        lang_counts: dict[str, int] = defaultdict(int)
        total_lines = 0
        total_classes = 0
        total_functions = 0
        for info in self.file_data.values():
            lang_counts[info["lang"]] += 1
            total_lines += info["line_count"]
            total_classes += len(info["classes"])
            total_functions += len(info["functions"])

        severity_counts: dict[str, int] = defaultdict(int)
        for smell in active_smells:
            severity_counts[smell["severity"]] += 1

        lang_breakdown = ", ".join(
            f"{lang.capitalize()} ({count})"
            for lang, count in sorted(lang_counts.items(), key=lambda x: -x[1])
        )

        lines.append("## Summary\n")
        lines.append(f"- **Files parsed**: {self.files_parsed}")
        lines.append(f"- **Languages**: {lang_breakdown}")
        lines.append(f"- **Total lines**: {total_lines:,}")
        lines.append(f"- **Classes**: {total_classes} | **Functions**: {total_functions}")
        smell_summary = (
            f"{severity_counts.get('high', 0)} high, "
            f"{severity_counts.get('medium', 0)} medium, "
            f"{severity_counts.get('low', 0)} low"
        )
        lines.append(f"- **Smells**: {smell_summary}")
        lines.append("")

        # --- File structure ---
        # Group files by top-level directory
        by_dir: dict[str, list[str]] = defaultdict(list)
        for rel_path in sorted(self.file_data.keys()):
            parts = rel_path.split("/")
            top_dir = parts[0] if len(parts) > 1 else "."
            by_dir[top_dir].append(rel_path)

        for dir_name in sorted(by_dir.keys()):
            lines.append(f"## {dir_name}/\n")
            for rel_path in by_dir[dir_name]:
                info = self.file_data[rel_path]
                lines.append(f"**{rel_path}** ({info['lang']}, {info['line_count']} lines)")

                if info["classes"]:
                    lines.append(f"  - classes: {', '.join(info['classes'])}")
                if info["interfaces"]:
                    lines.append(f"  - interfaces: {', '.join(info['interfaces'])}")
                if info["type_aliases"]:
                    lines.append(f"  - types: {', '.join(info['type_aliases'])}")
                if info["functions"]:
                    funcs = info["functions"]
                    if len(funcs) > 20:
                        display = ", ".join(funcs[:20]) + f" ... (+{len(funcs) - 20} more)"
                    else:
                        display = ", ".join(funcs)
                    lines.append(f"  - functions: {display}")
                if info["imports"]:
                    imp_names = []
                    for imp in info["imports"]:
                        parts = imp.split()
                        if len(parts) >= 2:
                            mod = parts[1] if parts[0] in ("import", "from", "#include") else parts[0]
                            imp_names.append(mod.rstrip(","))
                    unique_imports = list(dict.fromkeys(imp_names))
                    if len(unique_imports) > 15:
                        display = ", ".join(unique_imports[:15]) + f" ... (+{len(unique_imports) - 15} more)"
                    else:
                        display = ", ".join(unique_imports)
                    lines.append(f"  - imports: {display}")
                lines.append("")

        # --- Smell report ---
        if active_smells:
            lines.append(f"\n# Code Smells ({len(active_smells)} issues found)\n")

            severity_labels = {
                "high": "High Priority",
                "medium": "Medium Priority",
                "low": "Low Priority",
            }

            by_severity: dict[str, list[dict]] = defaultdict(list)
            for smell in active_smells:
                by_severity[smell["severity"]].append(smell)

            MAX_PER_TYPE = 15  # Cap output per smell type to avoid noise

            for sev in ("high", "medium", "low"):
                items = by_severity.get(sev, [])
                if not items:
                    continue
                lines.append(f"## {severity_labels[sev]}\n")

                # Group by type within severity, cap each type
                by_type: dict[str, list[dict]] = defaultdict(list)
                for s in sorted(items, key=lambda x: (x["file"], x.get("line") or 0)):
                    by_type[s["type"]].append(s)

                for smell_type, type_items in by_type.items():
                    shown = type_items[:MAX_PER_TYPE]
                    for s in shown:
                        loc = s["file"]
                        if s.get("line"):
                            loc += f":{s['line']}"
                        lines.append(f"- [{s['type']}] {loc} — {s['detail']}")
                    if len(type_items) > MAX_PER_TYPE:
                        lines.append(f"  ... and {len(type_items) - MAX_PER_TYPE} more {smell_type} issues")
                lines.append("")
        else:
            lines.append("\n# Code Smells (0 issues found)\n")
            lines.append("No code smells detected. Nice!\n")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def get_git_head() -> str | None:
    """Get the current HEAD commit hash and subject."""
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H %s", "-1"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Tree-sitter codebase mapper and code smell detector.",
    )
    parser.add_argument(
        "--long-func", type=int, default=50, metavar="N",
        help="lines threshold for LONG_FUNCTION smell (default: 50)",
    )
    parser.add_argument(
        "--god-class", type=int, default=15, metavar="N",
        help="methods threshold for GOD_CLASS smell (default: 15)",
    )
    parser.add_argument(
        "--deep-nesting", type=int, default=4, metavar="N",
        help="nesting threshold for DEEP_NESTING smell (default: 4)",
    )
    parser.add_argument(
        "--many-params", type=int, default=5, metavar="N",
        help="parameters threshold for MANY_PARAMS smell (default: 5)",
    )
    parser.add_argument(
        "--large-file", type=int, default=500, metavar="N",
        help="lines threshold for LARGE_FILE smell (default: 500)",
    )
    parser.add_argument(
        "--skip-smells", type=str, default="", metavar="TYPE,TYPE",
        help="comma-separated smell types to skip (e.g. MISSING_DOCSTRING,LARGE_FILE)",
    )
    parser.add_argument(
        "--skip-dirs", type=str, default="", metavar="dir,dir",
        help="additional directories to ignore (e.g. generated,vendor)",
    )

    args = parser.parse_args()

    config = MapperConfig(
        long_func_lines=args.long_func,
        god_class_methods=args.god_class,
        deep_nesting_levels=args.deep_nesting,
        many_params=args.many_params,
        large_file_lines=args.large_file,
        skip_smells={s.strip() for s in args.skip_smells.split(",") if s.strip()},
        extra_ignore_dirs={d.strip() for d in args.skip_dirs.split(",") if d.strip()},
    )

    _load_languages()
    root = Path.cwd()

    # Print HEAD commit info
    head = get_git_head()
    if head:
        print(f"# Git HEAD at onboarding: {head}\n")

    mapper = CodebaseMapper(root, config)
    mapper.scan()
    mapper.detect_circular_imports()
    mapper.detect_unused_imports()
    mapper.detect_duplicate_logic()
    print(mapper.format_output())


if __name__ == "__main__":
    main()
