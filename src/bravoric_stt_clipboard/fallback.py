"""Catena di fallback: prova ogni livello in ordine finché uno risponde.

Fallback chain: try each level in order until one answers.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

from .api_client import ApiError, chat_cleanup
from .chunk_log import redact
from .config import FallbackLevel

logger = logging.getLogger(__name__)
T = TypeVar("T")

CLEANUP_MIN_LENGTH_RATIO = 0.7


class AllLevelsFailedError(RuntimeError):
    pass


def try_with_fallback(levels: list[FallbackLevel], call: Callable[[FallbackLevel], T]) -> T:
    if not levels:
        raise AllLevelsFailedError("No levels configured")

    errors = []
    for level in levels:
        try:
            return call(level)
        except (ApiError, OSError, TimeoutError) as exc:
            # redact(): un errore requests include l'URL, e con un endpoint
            # del tipo ...?api_key=XXX la chiave finiva nel journal e, via
            # AllLevelsFailedError, in una notifica desktop (misurato).
            # redact(): a requests error includes the URL, and with an endpoint like
            # ...?api_key=XXX the key ended up in the journal and, via
            # AllLevelsFailedError, in a desktop notification (measured).
            message = redact(str(exc))
            logger.warning("Level %s failed: %s", level.name, message)
            errors.append(f"{level.name}: {message}")

    raise AllLevelsFailedError("All levels failed: " + " | ".join(errors))


def cleanup_with_validation(
    levels: list[FallbackLevel], system_prompt: str, raw_text: str, retry_count: int = 2,
    min_length_ratio: float = CLEANUP_MIN_LENGTH_RATIO,
) -> str:
    """Il modello di cleanup a volte tronca parole in modo non deterministico
    (osservato: stesso input, stesso output atteso, esito variabile). Scarta
    risultati troppo corti rispetto all'originale e riprova."""
    last_error: Exception | None = None
    for attempt in range(max(1, retry_count)):
        try:
            result = try_with_fallback(levels, lambda level: chat_cleanup(level, system_prompt, raw_text))
        except AllLevelsFailedError as exc:
            last_error = exc
            continue

        # min_length_ratio = 0 disattiva il controllo / 0 disables the check.
        if len(result) >= min_length_ratio * len(raw_text):
            return result

        logger.warning(
            "Cleanup attempt %d: output too short (%d/%d chars), retrying",
            attempt + 1, len(result), len(raw_text),
        )
        last_error = AllLevelsFailedError(
            f"cleanup output truncated: {len(result)}/{len(raw_text)} chars"
        )

    raise last_error or AllLevelsFailedError("cleanup failed")
