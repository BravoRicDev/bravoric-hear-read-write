"""Scrittura atomica su file JSON/testo, condivisa dai 3 siti byte-per-byte
identici (P17, mandato perfetto): status.py, output_history.py, stream.py.

Schema: tmp univoco nella stessa directory -> scrivi -> flush -> fsync ->
os.replace (atomico) -> in caso di eccezione, unlink del tmp e ri-solleva.
Estratto qui SOLO dai 3 siti verificati identici; config_editor.py (niente
fsync, chmod, Path.replace), storage.py (riserva O_EXCL) ed
endpoint_breaker.py (chmod sul tmp, indent/sort_keys) hanno varianti
deliberate e restano intoccati (P17, proposta alternativa a rischio più
basso di REVISIONE-PULIZIA.md riga 272).

Atomic write of a JSON/text file, shared by the 3 byte-for-byte identical
call sites (P17, "perfect" mandate): status.py, output_history.py, stream.py.

Scheme: unique tmp in the same directory -> write -> flush -> fsync ->
os.replace (atomic) -> on any exception, unlink the tmp and re-raise.
Extracted here ONLY from the 3 sites verified identical; config_editor.py
(no fsync, chmod, Path.replace), storage.py (O_EXCL reservation) and
endpoint_breaker.py (chmod on the tmp, indent/sort_keys) have deliberate
variants and stay untouched (P17, lower-risk alternative proposal in
REVISIONE-PULIZIA.md line 272).
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str) -> None:
    """Scrive `text` così com'è (già serializzato dal chiamante).

    Writes `text` as is (already serialized by the caller).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp",
                                     prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    """Serializza `payload` con `json.dump` direttamente nel file temporaneo.

    Serializes `payload` with `json.dump` straight into the temporary file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp",
                                     prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
