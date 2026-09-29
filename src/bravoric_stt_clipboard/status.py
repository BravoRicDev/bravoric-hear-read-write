"""Status file condiviso con l'estensione GNOME (top bar).

Status file shared with the GNOME extension (top bar).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .atomic_io import atomic_write_text as _atomic_write_text

STATUS_PATH = Path.home() / ".cache" / "bravoric-stt-clipboard" / "status.json"

STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_PROCESSING = "processing"
STATE_ERROR = "error"


def write_status(state: str, last_output: str | None = None, service: str | None = None,
                 cancellable: bool | None = None) -> None:
    """service: 'stt'|'ocr', solo informativo per distinguere il processing
    nel menu dell'estensione (quale servizio sta elaborando).

    Eccezione (giro 17, bug reale confermato dal vivo): status.json è
    condiviso tra STT e OCR, scorciatoie indipendenti senza esclusione
    reciproca. Se OCR termina (scrive IDLE) mentre STT sta ancora
    registrando, spegneva silenziosamente il stato 'recording' — l'utente
    vedeva l'icona idle col microfono ancora fisicamente aperto, e il
    timeout di sicurezza sul recording (15 min) smetteva di applicarsi
    perché lo stato non era più 'recording'.

    Giro 2 (B4): il guard era valido SOLO sul ramo IDLE, quindi copriva
    metà dei casi. Con l'indicatore in 'recording' (STT) e l'OCR che passa
    a 'processing', il file diventava {state: processing, service: ocr}:
    l'icona usciva dal microfono e il timeout di sicurezza smetteva di
    applicarsi anche col microfono ancora aperto. Esteso a STATE_PROCESSING
    e STATE_ERROR, gli altri due modi in cui un servizio diverso può
    spegnere una registrazione in corso sovrascrivendone lo stato.

    Giro 3 (B4): restava però la clausola `service is not None` davanti al
    guard, cioè il guard NON esisteva per le scritture senza servizio — e sono
    proprio i rami di errore: `ocr.py` e `stt.py` chiamano
    `write_status(STATE_ERROR)` senza `service`, `cli.py` idem. Con STT in
    registrazione e l'OCR che fallisce sulla clipboard vuota, il file passava
    da recording a error: l'indicatore usciva dal microfono con il microfono
    ancora aperto, e con lui il timeout di sicurezza. La clausola è tolta: ora
    il guard confronta il servizio, e anche `None` (scrittura senza servizio)
    è coperto. `service=None` non è più "ignora il guard" ma "un servizio
    diverso dal servizio che sta registrando": per un recording con
    `service='stt'`/`'stream'` la differenza è 'stt' != None, quindi la
    scrittura viene respinta. Il percorso di default resta aperto perché chi
    scrive IDLE/ERROR passando per PROCESSING porta con sé `service=<proprio>`
    (ocr.py:17, stt.py:53, stream.py:1298) e il confronto lo riconosce come
    completamento genuino della propria registrazione.

    service: 'stt'|'ocr', informational only, to tell which service is
    processing in the extension menu.

    Exception (round 17, real bug confirmed live): status.json is shared
    between STT and OCR, independent shortcuts with no mutual exclusion. If
    OCR finishes (writes IDLE) while STT is still recording, it silently
    switched off the 'recording' state — the user saw the idle icon with the
    microphone still physically open, and the safety timeout on recording
    (15 min) stopped applying because the state was no longer 'recording'.

    Round 2 (B4): the guard was valid ONLY on the IDLE branch, so it covered
    half of the cases. With the indicator in 'recording' (STT) and OCR moving
    to 'processing', the file became {state: processing, service: ocr}: the
    icon left the microphone and the safety timeout stopped applying even with
    the microphone still open. Extended to STATE_PROCESSING and STATE_ERROR,
    the other two ways a different service can switch off a running recording
    by overwriting its state.

    Round 3 (B4): the `service is not None` clause in front of the guard was
    still there, i.e. the guard did NOT exist for writes without a service —
    which are exactly the error branches: `ocr.py` and `stt.py` call
    `write_status(STATE_ERROR)` without `service`, `cli.py` too. With STT
    recording and OCR failing on an empty clipboard, the file went from
    recording to error: the indicator left the microphone while the microphone
    was still open, and the safety timeout went with it. The clause is gone:
    the guard now compares the service, and `None` (a write without a service)
    is covered too. `service=None` is no longer "skip the guard" but "a
    service different from the one that is recording": for a recording with
    `service='stt'`/`'stream'` the difference is 'stt' != None, so the write is
    rejected. The default path stays open because whoever writes IDLE/ERROR
    after PROCESSING carries `service=<its own>` (ocr.py:17, stt.py:53,
    stream.py:1298) and the comparison recognizes it as the genuine completion
    of its own recording.
    """
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if state in (STATE_IDLE, STATE_PROCESSING, STATE_ERROR):
        current = read_status()
        if current.get("state") == STATE_RECORDING and current.get("service") != service:
            return
    payload = {"state": state, "timestamp": time.time()}
    if last_output is not None:
        payload["last_output"] = last_output
    if service is not None:
        payload["service"] = service
    if cancellable is not None:
        # False = da qui in poi il servizio non si puo' piu' annullare (es. OCR
        # gia' arrivato alla scrittura negli appunti): l'estensione non offre "Annulla".
        # False = from here on the service can no longer be cancelled (e.g. OCR
        # already at the clipboard write): the extension does not offer "Cancel".
        payload["cancellable"] = cancellable
    _atomic_write_text(STATUS_PATH, json.dumps(payload))


def read_status() -> dict:
    """Best-effort: file assente o corrotto -> idle, mai un'eccezione.
    B8: ritorna un dict con timestamp corrente (non 0) per non invalidare
    i timeout impostati dall'estensione.

    Best-effort: missing or corrupt file -> idle, never an exception.
    B8: returns a dict with the current timestamp (not 0) so as not to
    invalidate the timeouts set by the extension.
    """
    try:
        data = json.loads(STATUS_PATH.read_text())
        if isinstance(data, dict) and "state" in data:
            return data
        return {"state": STATE_IDLE, "timestamp": time.time()}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"state": STATE_IDLE, "timestamp": time.time()}
