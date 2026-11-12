"""Shared, single-line dotenv syntax; no interpolation or shell evaluation.

Unquoted comments start at ``#`` only at the start of a value or after
whitespace. Double quotes decode backslash, quote and tab escapes; escapes
that would create multiline values are refused. Single quotes are literal.
"""

from __future__ import annotations

from pathlib import Path
import re


KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_DOUBLE_QUOTED = re.compile(r'"((?:\\.|[^"\\])*)"')
_SINGLE_QUOTED = re.compile(r"'([^']*)'")
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}
_LINE_BREAKS = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"


class DotenvError(ValueError):
    """Invalid dotenv syntax; messages never include configured values."""


def validate_single_line(value: str) -> None:
    if any(char in value for char in _LINE_BREAKS + "\x00"):
        raise DotenvError("Configuration values must be single-line text without NUL.")


def _value_parts(raw: str) -> tuple[str, str]:
    """Decode a value and retain the spacing/text of its trailing comment."""
    validate_single_line(raw)
    stripped = raw.lstrip()
    comment_index = None
    if stripped.startswith(('"', "'")):
        pattern = _DOUBLE_QUOTED if stripped[0] == '"' else _SINGLE_QUOTED
        match = pattern.match(stripped)
        if match is None:
            raise DotenvError("Malformed quoted configuration value.")
        end = len(raw) - len(stripped) + match.end()
        tail = raw[end:]
        if tail.strip():
            if not tail.lstrip().startswith("#"):
                raise DotenvError("Malformed quoted configuration value.")
            comment_index = end + len(tail) - len(tail.lstrip())
        value = match[1]
        if stripped[0] == '"':
            value = re.sub(r"\\(.)", lambda m: _ESCAPES.get(m[1], m[0]), value)
    else:
        comment = re.search(r"(?<!\S)#", raw)
        if comment is not None:
            comment_index = comment.start()
        value = raw[:comment_index].strip()
    validate_single_line(value)
    if comment_index is None:
        return value, ""
    prefix = raw[:comment_index]
    spacing = prefix[len(prefix.rstrip()):] or " "
    return value, spacing + raw[comment_index:]


def decode_value(raw: str) -> str:
    return _value_parts(raw)[0]


def assignment(line: str) -> tuple[str, str] | None:
    """Read one assignment, or return None for a blank/comment template line."""
    validate_single_line(line)
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    match = ASSIGNMENT.fullmatch(stripped)
    if match is None:
        raise DotenvError("Malformed configuration assignment.")
    return match[1], decode_value(match[2])


def inline_comment(line: str) -> str:
    """Retain an assignment's trailing template comment, outside its value."""
    return _value_parts(line.split("=", 1)[1])[1]


def parse_text(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for number, line in enumerate(text.removeprefix("\ufeff").splitlines(), 1):
        try:
            pair = assignment(line)
        except DotenvError:
            raise DotenvError(f"Invalid dotenv assignment at line {number}") from None
        if pair is None:
            continue
        key, value = pair
        if key in result:
            raise DotenvError(f"Duplicate dotenv key {key} at line {number}")
        result[key] = value
    return result


def parse_file(path: str | Path) -> dict[str, str]:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except UnicodeError:
        raise DotenvError("Invalid dotenv text encoding") from None
    return parse_text(text)


def encode_value(value: str) -> str:
    """Encode a single-line value so parsing it recovers the exact string."""
    validate_single_line(value)
    if not value or re.fullmatch(r"[A-Za-z0-9_./:@+,-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
    return '"' + escaped + '"'
