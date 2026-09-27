"""Cronologia leggera degli output STT/OCR per il menu della tray icon —
distinta da storage.py (archivio file su disco, opt-in, retention lunga):
qui è un JSON always-on, cap a N voci, pensato per "ricopia l'ultimo/i".
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

from .atomic_io import atomic_write_json as _atomic_write_json

HISTORY_PATH = Path.home() / ".cache" / "bravoric-stt-clipboard" / "output_history.json"


@contextlib.contextmanager
def _locked():
    """Serializza read-modify-write tra processi CLI concorrenti (STT e OCR
    lanciati vicini nel tempo). _write() è atomica sul singolo file, ma senza
    lock due append_entry() concorrenti leggono lo stesso stato iniziale e
    l'ultimo _write() vince, perdendo la voce dell'altro (race confermata dal
    vivo, giro 13: 11-17/20 voci sopravvivevano su scritture concorrenti).
    HISTORY_PATH è letto qui (non congelato a livello di modulo) perché i
    test lo riassegnano a runtime."""
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(HISTORY_PATH.with_suffix(".lock")), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(HISTORY_PATH.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    # JSON valido ma di forma inattesa (oggetto/scalare/null): senza questa
    # guardia entries.insert(0, ...) in append_entry solleverebbe AttributeError,
    # non catturato, bloccando lo status su 'processing'.
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _write(entries: list[dict]) -> None:
    """Scrittura atomica: l'estensione legge questo file in modo asincrono;
    un write_text diretto può essere letto a metà e mostrare "No history yet"."""
    _atomic_write_json(HISTORY_PATH, entries)


def append_entry(service: str, kind: str, text: str, max_entries: int) -> None:
    """service: 'stt'|'ocr', kind: 'raw'|'clean'. Più recente in testa."""
    with _locked():
        entries = _read()
        entries.insert(0, {
            "timestamp": time.time(),
            "service": service,
            "kind": kind,
            "text": text,
        })
        # max(1, ...): con max(0, ...) un valore <=0 svuoterebbe l'intera
        # cronologia invece di limitarla (il clamp era invertito).
        _write(entries[:max(1, max_entries)])


def read_history() -> list[dict]:
    return _read()


def clear_history() -> None:
    with _locked():
        _write([])
