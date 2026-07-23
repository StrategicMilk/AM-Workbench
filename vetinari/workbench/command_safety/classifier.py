"""Parser and classifier for Workbench command-safety profiles."""

from __future__ import annotations

import hashlib
import logging
import re
import shlex

from vetinari.workbench.command_safety.contracts import (
    CommandClassification,
    CommandSafetyProfile,
    CommandSafetyReason,
    CommandSafetyVerdict,
    CommandSurface,
)

logger = logging.getLogger(__name__)


_CHAINING_OPERATORS = ("&&", "||", ";", "|")
_REDIRECT_OPERATORS = (">>", ">", "<")
_INTERPRETERS = {"powershell", "pwsh", "cmd", "bash", "sh", "python", "python.exe", "node", "node.exe"}


def normalize_command(command: str) -> str:
    return " ".join(str(command).strip().split())


def command_fingerprint(command: str) -> str:
    return hashlib.sha256(normalize_command(command).encode("utf-8")).hexdigest()


def classify_command(
    command: str, *, profile: CommandSafetyProfile, surface: CommandSurface | str
) -> CommandClassification:
    """Execute the classify command operation.

    Returns:
        CommandClassification value produced by classify_command().
    """
    normalized = normalize_command(command)
    surface_value = CommandSurface(surface)
    if surface_value not in profile.surfaces:
        return _classification(
            normalized, (), CommandSafetyVerdict.BLOCK, (CommandSafetyReason.UNKNOWN_TOOL_SURFACE,), hard_block=True
        )
    if not normalized:
        return _classification(
            normalized, (), CommandSafetyVerdict.BLOCK, (CommandSafetyReason.BLOCKED,), hard_block=True
        )
    tokens = tuple(_split_tokens(normalized))
    operator_reasons, operator_fragments = _scan_shell_operators(normalized)
    pattern_reasons, pattern_fragments = _scan_patterns(normalized, tokens)
    reasons = tuple(dict.fromkeys((*operator_reasons, *pattern_reasons)))
    fragments = tuple(dict.fromkeys((*operator_fragments, *pattern_fragments)))
    safe_prefix = _matched_prefix(normalized, profile.safe_prefixes)
    approval_prefix = _matched_prefix(normalized, profile.approval_prefixes)
    if _matches_profile_blocked_pattern(normalized, profile.blocked_patterns):
        reasons = tuple(dict.fromkeys((*reasons, CommandSafetyReason.BLOCKED)))
        return _classification(normalized, tokens, CommandSafetyVerdict.BLOCK, reasons, fragments, hard_block=True)
    if _hard_block_reasons(reasons):
        return _classification(normalized, tokens, CommandSafetyVerdict.BLOCK, reasons, fragments, hard_block=True)
    if reasons:
        return _classification(
            normalized, tokens, CommandSafetyVerdict.REQUIRE_HUMAN_APPROVAL, reasons, fragments, requires_approval=True
        )
    if approval_prefix:
        return CommandClassification(
            normalized,
            tokens,
            approval_prefix,
            CommandSafetyVerdict.REQUIRE_HUMAN_APPROVAL,
            (CommandSafetyReason.APPROVAL_REQUIRED,),
            (),
            True,
            False,
        )
    if safe_prefix:
        return CommandClassification(
            normalized,
            tokens,
            safe_prefix,
            CommandSafetyVerdict.ALLOW,
            (CommandSafetyReason.SAFE_PREFIX_ALLOWED,),
            (),
            not profile.allow_without_human_approval,
            False,
        )
    return _classification(
        normalized,
        tokens,
        CommandSafetyVerdict.REQUIRE_HUMAN_APPROVAL,
        (CommandSafetyReason.APPROVAL_REQUIRED,),
        requires_approval=True,
    )


def _classification(
    normalized: str,
    tokens: tuple[str, ...],
    verdict: CommandSafetyVerdict,
    reasons: tuple[CommandSafetyReason, ...],
    unsafe_fragments: tuple[str, ...] = (),
    *,
    requires_approval: bool = False,
    hard_block: bool = False,
) -> CommandClassification:
    return CommandClassification(
        normalized, tokens, "", verdict, tuple(dict.fromkeys(reasons)), unsafe_fragments, requires_approval, hard_block
    )


def _split_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return [token.strip("\"'") for token in lexer]
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return command.split()


def _scan_shell_operators(command: str) -> tuple[tuple[CommandSafetyReason, ...], tuple[str, ...]]:
    reasons: list[CommandSafetyReason] = []
    fragments: list[str] = []
    quote: str | None = None
    escaped = False
    i = 0
    while i < len(command):
        char = command[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if char == "\\":
            escaped = True
            i += 1
            continue
        if quote:
            if char == quote:
                quote = None
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            i += 1
            continue
        two = command[i : i + 2]
        if two == "$(":
            reasons.append(CommandSafetyReason.COMMAND_SUBSTITUTION)
            fragments.append(two)
            i += 2
            continue
        if char == "`":
            reasons.append(CommandSafetyReason.COMMAND_SUBSTITUTION)
            fragments.append(char)
        elif two in _CHAINING_OPERATORS or two in _REDIRECT_OPERATORS:
            reasons.append(CommandSafetyReason.UNSAFE_OPERATOR)
            fragments.append(two)
            i += 2
            continue
        elif char in _CHAINING_OPERATORS or char in _REDIRECT_OPERATORS:
            reasons.append(CommandSafetyReason.UNSAFE_OPERATOR)
            fragments.append(char)
        elif char == "&" and i == len(command) - 1:
            reasons.append(CommandSafetyReason.BACKGROUND_EXECUTION)
            fragments.append(char)
        i += 1
    return tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(fragments))


def _scan_patterns(command: str, tokens: tuple[str, ...]) -> tuple[tuple[CommandSafetyReason, ...], tuple[str, ...]]:
    lowered = command.lower()
    token_text = " ".join(token.lower() for token in tokens)
    reasons: list[CommandSafetyReason] = []
    fragments: list[str] = []
    patterns: tuple[tuple[CommandSafetyReason, tuple[str, ...]], ...] = (
        (
            CommandSafetyReason.DESTRUCTIVE_PATTERN,
            ("rm -rf", "remove-item -recurse", "del /s", "rmdir /s", "erase /s", "move-item -force", "mv -f"),
        ),
        (
            CommandSafetyReason.SECRET_EXPOSURE,
            ("printenv", "get-childitem env:", "$env:", "set |", "env |", "secret", "token"),
        ),
        (
            CommandSafetyReason.NETWORK_EXFILTRATION,
            ("curl ", "wget ", "invoke-webrequest", "iwr ", "invoke-restmethod", "scp "),
        ),
        (
            CommandSafetyReason.PROCESS_MUTATION,
            ("kill ", "taskkill", "stop-process", "restart-service", "stop-service", "sc stop"),
        ),
        (
            CommandSafetyReason.PACKAGE_MUTATION,
            ("npm install", "npm update", "pip install", "pip uninstall", "uv add", "poetry add"),
        ),
        (
            CommandSafetyReason.PRIVILEGE_ESCALATION,
            ("sudo ", "runas ", "start-process -verb runas", "set-executionpolicy"),
        ),
        (CommandSafetyReason.PATH_TRAVERSAL, ("../", "..\\")),
        (CommandSafetyReason.BACKGROUND_EXECUTION, ("start-job", "nohup ", "start-process ")),
    )
    for reason, needles in patterns:
        for needle in needles:
            if needle in lowered or needle in token_text:
                reasons.append(reason)
                fragments.append(needle)
                break
    first = tokens[0].lower() if tokens else ""
    if first in _INTERPRETERS and re.search(
        r"\b(rm\s+-rf|remove-item\s+-recurse|del\s+/s|taskkill|stop-process|curl\s+http)", lowered
    ):
        reasons.append(CommandSafetyReason.DESTRUCTIVE_PATTERN)
        fragments.append("interpreter-destructive-payload")
    return tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(fragments))


def _matched_prefix(command: str, prefixes: tuple[str, ...]) -> str:
    lowered = command.lower()
    for prefix in prefixes:
        clean = prefix.strip().lower()
        if lowered == clean or lowered.startswith(clean + " "):
            return prefix
    return ""


def _matches_profile_blocked_pattern(command: str, patterns: tuple[str, ...]) -> bool:
    lowered = command.lower()
    return any(pattern.lower() in lowered for pattern in patterns if pattern.strip())


def _hard_block_reasons(reasons: tuple[CommandSafetyReason, ...]) -> bool:
    hard = {
        CommandSafetyReason.DESTRUCTIVE_PATTERN,
        CommandSafetyReason.SECRET_EXPOSURE,
        CommandSafetyReason.NETWORK_EXFILTRATION,
        CommandSafetyReason.PROCESS_MUTATION,
        CommandSafetyReason.PACKAGE_MUTATION,
        CommandSafetyReason.PRIVILEGE_ESCALATION,
    }
    return bool(hard.intersection(reasons))
