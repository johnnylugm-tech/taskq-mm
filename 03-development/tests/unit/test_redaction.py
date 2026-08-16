"""[NFR-04] redaction patterns."""
from __future__ import annotations

from taskq_api.redaction import redact_line, redact_text


def test_openai_style_key_redacted() -> None:
    line = "Authorization: sk-abcdefghijklmnop"
    assert redact_line(line) == "[REDACTED]"


def test_token_kv_redacted() -> None:
    line = "GET /api?token=abcdefghijklmnop"
    assert redact_line(line) == "[REDACTED]"


def test_bearer_header_redacted() -> None:
    line = "Bearer abc123def456"
    assert redact_line(line) == "[REDACTED]"


def test_postgres_url_redacted() -> None:
    line = "connecting to postgresql://user:pass@host/db"
    assert redact_line(line) == "[REDACTED]"


def test_safe_line_passes_through() -> None:
    line = "ordinary log line without secrets"
    assert redact_line(line) == line


def test_redact_text_per_line() -> None:
    text = "first safe line\nsk-abcdefghijklmnop leaked\nthird safe line"
    redacted = redact_text(text)
    assert "first safe line" in redacted
    assert "[REDACTED]" in redacted
    assert "sk-" not in redacted
    assert "third safe line" in redacted


def test_empty_string_returns_empty() -> None:
    assert redact_text("") == ""