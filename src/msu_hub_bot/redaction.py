"""Protect credentials at local logging and explicit diagnostic boundaries."""

import json
import logging
import os
import re
from collections.abc import Iterator
from urllib.parse import quote, quote_plus, urlsplit

from msu_hub_bot.settings import settings

MASK = "[REDACTED]"
_sensitive_key = re.compile(r"token|password|secret|authorization|cookie|api.?key|credential|dsn", re.I)
_patterns = (
    re.compile(r"\b(?:bot)?\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{25,})\b"),
    re.compile(r"(?<=://)[^\s/@]+:[^\s/@]+@"),
    re.compile(r"(?i)((?:authorization|password|api_key|access_token)\s*[:=]\s*)[^\s,;}]+"),
)


def _secret_strings(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _secret_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _secret_strings(item)
    elif isinstance(value, str) and value:
        yield value
        if "://" in value:
            try:
                password = urlsplit(value).password
                if password:
                    yield password
            except ValueError:
                pass
    elif isinstance(value, int) and not isinstance(value, bool):
        yield str(value)


def redact(value: object) -> str:
    text = str(value)
    variants: set[str] = set()
    credentials = [getattr(settings, name) for name, field in type(settings).model_fields.items() if field.repr is False]
    credentials.append(os.environ.get("LOGFIRE_TOKEN", ""))
    for secret in _secret_strings(credentials):
        variants.update(
            (secret, quote(secret, safe=""), quote_plus(secret), json.dumps(secret, ensure_ascii=False)[1:-1], repr(secret)[1:-1])
        )
        variants.update(line for line in secret.splitlines() if len(line) >= 8)
    for secret in sorted(variants, key=len, reverse=True):
        text = text.replace(secret, MASK)
    for pattern in _patterns:
        text = pattern.sub(MASK, text)
    return text


def redact_json(value: object) -> object:
    """Protect diagnostic values without replacing JSON keys or numeric IDs."""
    if isinstance(value, dict):
        return {key: MASK if _sensitive_key.search(str(key)) else redact_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_json(item) for item in value]
    return redact(value) if isinstance(value, str) else value


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
