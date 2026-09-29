"""Cattura schermo interattiva via gnome-screenshot (selezione area col mouse)."""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Limite superiore dell'attesa per la selezione dell'area: l'utente ha tutto
# il tempo che vuole per scegliere, ma un processo mai risposto (compositor
# occupato, gnome-screenshot bloccato) non deve restare appeso all'infinito
# con lo stato bloccato su "processing" (stessa logica di STOP_TIMEOUT in
# stream.py: un'attesa bloccante ha sempre un tetto).
# Upper bound on the wait for the area selection: the user has all the time
# they want to choose, but a process that never answers (busy compositor,
# stuck gnome-screenshot) must not hang forever with the state stuck on
# "processing" (same logic as STOP_TIMEOUT in stream.py: a blocking wait
# always has a cap).
SELECTION_TIMEOUT_SECONDS = 120


def is_available() -> bool:
    """gnome-screenshot e' installato? Il chiamante lo usa per avvisare
    l'utente UNA volta, in modo chiaro, invece di lasciare capture_area_png
    fallire in silenzio ad ogni pressione (stesso trattamento del vero
    annullamento, indistinguibile per l'utente da 'non funziona e basta').

    Is gnome-screenshot installed? The caller uses this to warn the user ONCE,
    clearly, instead of letting capture_area_png fail silently on every press
    (same treatment as a real cancel, indistinguishable for the user from
    "it just does not work").
    """
    return shutil.which("gnome-screenshot") is not None


def capture_area_png(timeout: float = SELECTION_TIMEOUT_SECONDS, on_spawn=None) -> bytes | None:
    """Selezione interattiva di un'area (mouse) e cattura in PNG.

    Ritorna None se l'utente annulla (Esc: gnome-screenshot esce senza
    scrivere il file), se il comando manca, o se scade il timeout — nessuno
    di questi è un errore da notificare: è un cambio idea o un'attesa senza
    risposta, non un difetto di OCR. Il chiamante distingue "nessuna
    immagine" (None) da un vero errore di trascrizione a valle.

    Interactive selection of an area (mouse) and capture as PNG.

    Returns None if the user cancels (Esc: gnome-screenshot exits without
    writing the file), if the command is missing, or if the timeout expires —
    none of these is an error to notify: it is a change of mind or an
    unanswered wait, not an OCR defect. The caller tells "no image" (None)
    apart from a real transcription error downstream.

    `on_spawn(pid)` (opzionale) riceve il pid del processo di selezione appena
    avviato, cosi' chi annulla l'OCR puo' chiudere l'overlay anche se questo
    processo viene ucciso. Un'eccezione qualunque (annullamento compreso) chiude
    sempre il processo di selezione.

    `on_spawn(pid)` (optional) receives the pid of the selection process just
    started, so whoever cancels the OCR can close the overlay even if this process
    is killed. Any exception (cancellation included) always closes the selection
    process.
    """
    with tempfile.TemporaryDirectory(prefix="bravoric-screenshot-") as tmp_dir:
        out_path = Path(tmp_dir) / "capture.png"
        try:
            proc = subprocess.Popen(["gnome-screenshot", "--area", "--file", str(out_path)])
            try:
                if on_spawn is not None:
                    on_spawn(proc.pid)
                returncode = proc.wait(timeout=timeout)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        except FileNotFoundError:
            logger.warning("gnome-screenshot non trovato: installa gnome-screenshot per usare capture_screenshot")
            return None
        except subprocess.TimeoutExpired:
            logger.info("selezione area schermata scaduta dopo %ds senza risposta", timeout)
            return None
        except OSError as exc:
            # Altri modi in cui avviare il processo puo' fallire (permessi,
            # risorse esaurite, ...): stesso trattamento di FileNotFoundError,
            # non un errore di OCR da notificare come "Unexpected error" —
            # senza questa guardia l'eccezione risalirebbe fino a
            # cli.ocr_capture_main(), che la tratterebbe come un difetto vero.
            # Other ways starting the process can fail (permissions, exhausted
            # resources, ...): same treatment as FileNotFoundError, not an OCR error to
            # notify as "Unexpected error" — without this guard the exception would
            # bubble up to cli.ocr_capture_main(), which would treat it as a real defect.
            logger.warning("impossibile avviare gnome-screenshot: %s", exc)
            return None
        if returncode != 0 or not out_path.is_file():
            # Annullamento (Esc) o area vuota: gnome-screenshot esce con un
            # codice non-zero e non scrive il file. Niente da segnalare.
            # Cancel (Esc) or empty area: gnome-screenshot exits with a non-zero code
            # and does not write the file. Nothing to report.
            return None
        return out_path.read_bytes() or None
