"""Status file condiviso con l'estensione GNOME (top bar)."""
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


def write_status(state: str, last_output: str | None = None, service: str | None = None) -> None:
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
    completamento genuino della propria registrazione."""
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
    _atomic_write_text(STATUS_PATH, json.dumps(payload))


def read_status() -> dict:
    """Best-effort: file assente o corrotto -> idle, mai un'eccezione.
    B8: ritorna un dict con timestamp corrente (non 0) per non invalidare
    i timeout impostati dall'estensione."""
    try:
        data = json.loads(STATUS_PATH.read_text())
        if isinstance(data, dict) and "state" in data:
            return data
        return {"state": STATE_IDLE, "timestamp": time.time()}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"state": STATE_IDLE, "timestamp": time.time()}
