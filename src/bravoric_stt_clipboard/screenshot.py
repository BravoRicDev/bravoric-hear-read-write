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
SELECTION_TIMEOUT_SECONDS = 120


def is_available() -> bool:
    """gnome-screenshot e' installato? Il chiamante lo usa per avvisare
    l'utente UNA volta, in modo chiaro, invece di lasciare capture_area_png
    fallire in silenzio ad ogni pressione (stesso trattamento del vero
    annullamento, indistinguibile per l'utente da 'non funziona e basta')."""
    return shutil.which("gnome-screenshot") is not None


def capture_area_png() -> bytes | None:
    """Selezione interattiva di un'area (mouse) e cattura in PNG.

    Ritorna None se l'utente annulla (Esc: gnome-screenshot esce senza
    scrivere il file), se il comando manca, o se scade il timeout — nessuno
    di questi è un errore da notificare: è un cambio idea o un'attesa senza
    risposta, non un difetto di OCR. Il chiamante distingue "nessuna
    immagine" (None) da un vero errore di trascrizione a valle.
    """
    with tempfile.TemporaryDirectory(prefix="bravoric-screenshot-") as tmp_dir:
        out_path = Path(tmp_dir) / "capture.png"
        try:
            result = subprocess.run(
                ["gnome-screenshot", "--area", "--file", str(out_path)],
                check=False, timeout=SELECTION_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            logger.warning("gnome-screenshot non trovato: installa gnome-screenshot per usare capture_screenshot")
            return None
        except subprocess.TimeoutExpired:
            logger.info("selezione area schermata scaduta dopo %ds senza risposta", SELECTION_TIMEOUT_SECONDS)
            return None
        except OSError as exc:
            # Altri modi in cui avviare il processo puo' fallire (permessi,
            # risorse esaurite, ...): stesso trattamento di FileNotFoundError,
            # non un errore di OCR da notificare come "Unexpected error" —
            # senza questa guardia l'eccezione risalirebbe fino a
            # cli.ocr_capture_main(), che la tratterebbe come un difetto vero.
            logger.warning("impossibile avviare gnome-screenshot: %s", exc)
            return None
        if result.returncode != 0 or not out_path.is_file():
            # Annullamento (Esc) o area vuota: gnome-screenshot esce con un
            # codice non-zero e non scrive il file. Niente da segnalare.
            return None
        return out_path.read_bytes() or None
