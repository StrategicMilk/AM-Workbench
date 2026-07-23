"""Extraction helpers for token compression preprocessing."""

from __future__ import annotations

import ast
import logging
import re

logger = logging.getLogger(__name__)


def _ast_annotation_str(node: ast.expr) -> str:
    return ast.unparse(node)


def _ast_default_str(node: ast.expr) -> str:
    return ast.unparse(node)


def _ast_docstring(node: ast.AST) -> str | None:
    body = getattr(node, "body", [])
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[0].value.value
    return None


def _format_ast_arg(arg: ast.arg) -> str:
    if arg.annotation:
        return f"{arg.arg}: {_ast_annotation_str(arg.annotation)}"
    return arg.arg


def _format_ast_arguments(args: ast.arguments) -> str:
    parts: list[str] = []
    n_posonly_defaults = len(args.posonlyargs) - len(args.defaults)
    for i, arg in enumerate(args.posonlyargs):
        default_idx = i - n_posonly_defaults
        parts.append(
            f"{_format_ast_arg(arg)} = {_ast_default_str(args.defaults[default_idx])}"
            if default_idx >= 0
            else _format_ast_arg(arg)
        )
    if args.posonlyargs:
        parts.append("/")

    n_pos = len(args.posonlyargs)
    n_reg = len(args.args)
    n_defaults = len(args.defaults)
    for i, arg in enumerate(args.args):
        default_idx = n_pos + i - (n_reg + n_pos - n_defaults)
        parts.append(
            f"{_format_ast_arg(arg)} = {_ast_default_str(args.defaults[default_idx])}"
            if default_idx >= 0
            else _format_ast_arg(arg)
        )
    if args.vararg:
        parts.append(f"*{_format_ast_arg(args.vararg)}")
    elif args.kwonlyargs:
        parts.append("*")
    for i, arg in enumerate(args.kwonlyargs):
        kw_default = args.kw_defaults[i]
        parts.append(
            f"{_format_ast_arg(arg)} = {_ast_default_str(kw_default)}"
            if kw_default is not None
            else _format_ast_arg(arg)
        )
    if args.kwarg:
        parts.append(f"**{_format_ast_arg(args.kwarg)}")
    return ", ".join(parts)


def _emit_ast_func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, indent: str, lines: list[str]) -> None:
    lines.extend(f"{indent}@{ast.unparse(dec)}" for dec in node.decorator_list)
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    sig = f"{indent}{prefix} {node.name}({_format_ast_arguments(node.args)})"
    if node.returns:
        sig += f" -> {_ast_annotation_str(node.returns)}"
    lines.append(sig + ":")
    doc = _ast_docstring(node)
    if doc:
        short_doc = doc.strip().split("\n")[0]
        lines.append(f'{indent}    """{short_doc}"""')
    else:
        lines.append(f"{indent}    ...")
    lines.append("")


def _emit_ast_class_signature(node: ast.ClassDef, indent: str, lines: list[str]) -> None:
    lines.extend(f"{indent}@{ast.unparse(dec)}" for dec in node.decorator_list)
    bases = [ast.unparse(b) for b in node.bases]
    keywords = [f"{kw.arg}={ast.unparse(kw.value)}" for kw in node.keywords]
    all_bases = bases + keywords
    suffix = f"({', '.join(all_bases)})" if all_bases else ""
    lines.append(f"{indent}class {node.name}{suffix}:")
    doc = _ast_docstring(node)
    if doc:
        short_doc = doc.strip().split("\n")[0]
        lines.extend((f'{indent}    """{short_doc}"""', ""))
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _emit_ast_func_signature(child, indent + "    ", lines)
        elif isinstance(child, ast.ClassDef):
            _emit_ast_class_signature(child, indent + "    ", lines)
    lines.append("")


def _emit_ast_assign_signature(node: ast.Assign, lines: list[str]) -> None:
    lines.extend(
        f"{target.id} = {ast.unparse(node.value)}"
        for target in node.targets
        if (isinstance(target, ast.Name) and target.id == target.id.upper() and target.id.replace("_", "").isalpha())
        or (isinstance(target, ast.Name) and re.match(r"^[A-Z][A-Z_0-9]*$", target.id))
    )


def _emit_ast_ann_assign_signature(node: ast.AnnAssign, lines: list[str]) -> None:
    if not (isinstance(node.target, ast.Name) and re.match(r"^[A-Z][A-Z_0-9]*$", node.target.id)):
        return
    ann = f": {_ast_annotation_str(node.annotation)}"
    val = f" = {ast.unparse(node.value)}" if node.value else ""
    lines.append(f"{node.target.id}{ann}{val}")


def _extract_code_signatures_from_ast(code: str) -> str:
    tree = ast.parse(code)
    lines: list[str] = []
    mod_doc = _ast_docstring(tree)
    if mod_doc:
        short_doc = mod_doc.strip().split("\n")[0]
        lines.extend((f'"""{short_doc}"""', ""))

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            lines.append(ast.unparse(node))
        elif isinstance(node, ast.ClassDef):
            lines.append("")
            _emit_ast_class_signature(node, "", lines)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append("")
            _emit_ast_func_signature(node, "", lines)
        elif isinstance(node, ast.Assign):
            _emit_ast_assign_signature(node, lines)
        elif isinstance(node, ast.AnnAssign):
            _emit_ast_ann_assign_signature(node, lines)

    return "\n".join(lines).strip()


class TokenCompressionExtractionMixin:
    """Heuristic extraction and truncation helpers for ``LocalPreprocessor``."""

    CONTEXT_WINDOW_CHARS: int

    @staticmethod
    def _extract_code_signatures_ast(code: str) -> str:
        """Extract code structure using ast.parse() for accurate Python parsing."""
        return _extract_code_signatures_from_ast(code)

    def _extract_code_signatures(self, context: str) -> str:
        """Extract function/class definitions and docstrings from code."""
        try:
            result = self._extract_code_signatures_ast(context)
            if result:
                return result
        except Exception:
            logger.warning("AST extraction failed, falling back to regex")

        lines = context.split("\n")
        extracted = []
        in_docstring = False
        docstring_delim = None

        for line in lines:
            stripped = line.strip()

            if re.match(r"^(class |def |async def )", stripped):
                extracted.append(line)
                continue

            if stripped.startswith("@"):
                extracted.append(line)
                continue

            if stripped.startswith(("import ", "from ")):
                extracted.append(line)
                continue

            if in_docstring:
                extracted.append(line)
                if docstring_delim and docstring_delim in stripped:
                    in_docstring = False
                continue

            if stripped.startswith(('"""', "'''")):
                in_docstring = True
                docstring_delim = stripped[:3]
                extracted.append(line)
                if stripped.count(docstring_delim) >= 2:
                    in_docstring = False
                continue

            if re.match(r"^[A-Z_][A-Z_0-9]*\s*[:=]", stripped):
                extracted.append(line)
                continue

        return "\n".join(extracted) if extracted else ""

    @staticmethod
    def _extract_key_lines(context: str) -> str:
        """Extract key factual lines: headers, bullet points, definitions, URLs."""
        lines = context.split("\n")
        key_lines = []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith(("#", "##", "###", "//", "/*", "* ")):
                key_lines.append(line)
                continue

            if re.match(r"^[-*•]\s+|^\d+[.)]\s+", stripped):
                key_lines.append(line)
                continue

            if re.match(r"^\w[\w_]*\s*[:=]", stripped):
                key_lines.append(line)
                continue

            if "http://" in stripped or "https://" in stripped:
                key_lines.append(line)
                continue

            if re.search(
                r"\b(error|warning|constraint|require[ds]?|must|endpoint|api|url|port|host|timeout|limit)\b",
                stripped,
                re.IGNORECASE,
            ):
                key_lines.append(line)
                continue

        return "\n".join(key_lines) if key_lines else ""

    def _truncate(self, context: str) -> str:
        """Simple truncation fallback - keep head and tail to preserve framing."""
        if len(context) <= self.CONTEXT_WINDOW_CHARS:
            return context
        head = context[: self.CONTEXT_WINDOW_CHARS // 3]
        tail = context[-(2 * self.CONTEXT_WINDOW_CHARS // 3) :]
        return f"{head}\n\n[... context truncated for token efficiency ...]\n\n{tail}"
