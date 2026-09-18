"""Логирование с вычисткой секретов.

Любая строка лога проходит через фильтр, который заменяет значения секретов на `***`.
Это страховка: даже если исключение сторонней библиотеки включит в текст заголовки
запроса, ключ API в лог не попадёт.
"""

from __future__ import annotations

import logging
import re

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[-_]?key[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;&}]+)"),
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)([^\"'\s,;&}]+)"),
]


class SecretRedactingFilter(logging.Filter):
    """Заменяет известные секреты и похожие на секреты пары ключ=значение."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = [s for s in (secrets or []) if s and len(s) >= 6]

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(r"\1***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # кривой формат лог-записи не должен ронять приложение
            return True
        redacted = self.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    """Настраивает корневой логгер один раз; повторный вызов только обновляет фильтр."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        root.addHandler(handler)
    redactor = SecretRedactingFilter(secrets)
    for handler in root.handlers:
        handler.filters = [f for f in handler.filters if not isinstance(f, SecretRedactingFilter)]
        handler.addFilter(redactor)
    # httpx на уровне INFO пишет URL каждого запроса — достаточно WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
