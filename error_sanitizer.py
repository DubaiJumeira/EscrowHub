from __future__ import annotations

import re

_MAX_ERROR_LEN = 220
_URL_CREDENTIALS_RE = re.compile(r"\b([a-z][a-z0-9+.-]*://)([^\s/@:]+):([^\s/@]+)@", re.IGNORECASE)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._\-+/=]+", re.IGNORECASE)
_SECRET_PAIR_RE = re.compile(r"\b(token|authorization|api[_-]?key|secret|password)\s*[:=]\s*[^\s,;]+", re.IGNORECASE)


def sanitize_runtime_error(raw: object, max_len: int = _MAX_ERROR_LEN) -> str:
    """Return concise operator-safe error text for persistence/display surfaces."""
    text = " ".join(str(raw or "").split())
    if not text:
        return "unknown runtime error"
    text = _URL_CREDENTIALS_RE.sub(r"\1<redacted>:<redacted>@", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _SECRET_PAIR_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    if "Traceback (most recent call last):" in text:
        text = text.split("Traceback (most recent call last):", 1)[0].strip() or "runtime failure"
    if "{" in text or "[" in text:
        text = "provider/runtime error (payload redacted)"
    return text[:max_len]

