"""Cronologia leggera degli output STT/OCR per il menu della tray icon —
distinta da storage.py (archivio file su disco, opt-in, retention lunga):
qui è un JSON always-on, cap a N voci, pensato per "ricopia l'ultimo/i".

Lightweight history of STT/OCR outputs for the tray icon menu —
distinct from storage.py (on-disk file archive, opt-in, long retention):
this one is an always-on JSON, capped at N entries, meant for "copy the
last one(s) again".
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
    test lo riassegnano a runtime.

    Serializes read-modify-write across concurrent CLI processes (STT and OCR
    launched close in time). _write() is atomic on a single file, but without
    a lock two concurrent append_entry() calls read the same initial state and
    the last _write() wins, losing the other's entry (race confirmed live,
    round 13: 11-17/20 entries survived under concurrent writes).
    HISTORY_PATH is read here (not frozen at module level) because the tests
    reassign it at runtime.
    """
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
    # Valid JSON but of an unexpected shape (object/scalar/null): without this
    # guard entries.insert(0, ...) in append_entry would raise AttributeError,
    # uncaught, leaving the status stuck on 'processing'.
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _write(entries: list[dict]) -> None:
    """Scrittura atomica: l'estensione legge questo file in modo asincrono;
    un write_text diretto può essere letto a metà e mostrare "No history yet".

    Atomic write: the extension reads this file asynchronously; a direct
    write_text could be read halfway and show "No history yet".
    """
    _atomic_write_json(HISTORY_PATH, entries)


def append_entry(service: str, kind: str, text: str, max_entries: int) -> None:
    """service: 'stt'|'ocr', kind: 'raw'|'clean'. Più recente in testa.

    service: 'stt'|'ocr', kind: 'raw'|'clean'. Most recent first.
    """
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
        # max(1, ...): with max(0, ...) a value <=0 would empty the whole history
        # instead of limiting it (the clamp was inverted).
        _write(entries[:max(1, max_entries)])


def read_history() -> list[dict]:
    return _read()


def clear_history() -> None:
    with _locked():
        _write([])
