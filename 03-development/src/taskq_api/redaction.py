"""Independence helper — sensitive-data redaction [NFR-04].

Matches the four secret formats called out in SPEC §4 NFR-04 and replaces
whole lines containing a match with ``[REDACTED]``.

Public surface
--------------
- :func:`redact_line` — redact a single line if it carries a secret.
- :func:`redact_text` — redact a multi-line text block.
"""
from __future__ import annotations

import re
from typing import Iterable

# Each pattern matches one of the four secret forms named in SPEC NFR-04.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),                  # OpenAI-style keys
    re.compile(r"token=\S+"),                            # generic token=...
    re.compile(r"Bearer\s+\S+"),                         # Authorization header
    re.compile(r"postgres(?:ql)?://[^\s]+"),             # postgres URLs
)


def _matches(patterns: Iterable[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def redact_line(line: str) -> str:
    """Return the line with any matched secret-bearing line collapsed to ``[REDACTED]`` [NFR-04]."""
    if _matches(_PATTERNS, line):
        return "[REDACTED]"
    return line


def redact_text(text: str) -> str:
    """Redact every secret-bearing line in ``text`` [NFR-04]."""
    if not text:
        return text
    return "\n".join(redact_line(line) for line in text.splitlines())