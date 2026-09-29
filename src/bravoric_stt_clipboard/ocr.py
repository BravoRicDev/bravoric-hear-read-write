"""Orchestrazione modalità OCR: clipboard immagine -> vision -> cleanup -> clipboard.

OCR mode orchestration: clipboard image -> vision -> cleanup -> clipboard.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import tempfile
import time

from . import audio, clipboard, notify, output_history, screenshot, status, storage
from .api_client import vision_extract
from .config import Config
from .fallback import AllLevelsFailedError, cleanup_with_validation, try_with_fallback
from .i18n import _

logger = logging.getLogger(__name__)

# Lock dell'OCR in corso: pid del processo `bravoric-ocr-capture` (e del
# gnome-screenshot di selezione). E' cio' che rende l'OCR annullabile: `cancel`
# lo legge direttamente, senza dipendere da status.json.
# Lock of the running OCR: pid of the `bravoric-ocr-capture` process (and of the
# selection gnome-screenshot). It is what makes the OCR cancellable: `cancel`
# reads it directly, without depending on status.json.
OCR_LOCK_PATH = audio._runtime_dir() / "ocr.lock"
OCR_MARKERS = ("ocr-capture", "bravoric_stt_clipboard")
CANCEL_WAIT_SECONDS = 5.0


class OcrCancelled(BaseException):
    """BaseException di proposito: nessun `except Exception` dell'OCR deve
    inghiottire l'annullamento.

    BaseException on purpose: no `except Exception` in the OCR must swallow the
    cancellation.
    """


class _Run:
    committed = False  # gia' alla scrittura negli appunti: non annullabile | past the clipboard write: not cancellable
    cancelled = False


def _lock_data() -> dict | None:
    try:
        data = json.loads(OCR_LOCK_PATH.read_text())
    except (OSError, ValueError):
        return None
    pid = data.get("pid") if isinstance(data, dict) else None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return data


def is_active() -> bool:
    """C'e' un OCR vivo? Un lock di un processo morto (o con pid riusato) e' un
    residuo e viene rimosso.

    Is there a live OCR? A lock of a dead process (or with a reused pid) is a
    leftover and is removed.
    """
    data = _lock_data()
    if data is not None and audio._pid_alive(data["pid"]) and audio.pid_matches(data["pid"], OCR_MARKERS):
        return True
    OCR_LOCK_PATH.unlink(missing_ok=True)
    return False


def _write_lock(extra: dict | None = None, exclusive: bool = True) -> bool:
    audio.ensure_private_dir(OCR_LOCK_PATH.parent)
    payload = {"pid": os.getpid(), "started_at": time.time(), **(extra or {})}
    fd, tmp = tempfile.mkstemp(dir=str(OCR_LOCK_PATH.parent), prefix="ocr.lock.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(payload))
        if exclusive:
            os.link(tmp, str(OCR_LOCK_PATH))  # atomico, fallisce se esiste | atomic, fails if it exists
        else:
            os.replace(tmp, str(OCR_LOCK_PATH))
        return True
    except FileExistsError:
        return False
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _acquire_lock() -> bool:
    if _write_lock():
        return True
    if is_active():
        return False
    return _write_lock()


def _on_cancel_signal(signum, frame) -> None:
    if _Run.committed or _Run.cancelled:
        return
    _Run.cancelled = True
    raise OcrCancelled


def _commit() -> None:
    """Da qui in poi l'OCR scrive negli appunti: l'annullamento non e' piu'
    possibile e lo stato lo dichiara (l'estensione non offre piu' "Annulla").

    From here on the OCR writes to the clipboard: cancellation is no longer
    possible and the state says so (the extension no longer offers "Cancel").
    """
    _Run.committed = True
    with contextlib.suppress(Exception):
        status.write_status(status.STATE_PROCESSING, service="ocr", cancellable=False)


def cancel() -> bool:
    """Annulla l'OCR in corso (selezione o elaborazione). IDEMPOTENTE: senza OCR
    attivo non fa nulla (mai un nuovo avvio). Ritorna True se ha inviato
    l'annullamento. Se l'OCR e' gia' alla scrittura negli appunti lo lascia finire.

    Cancels the running OCR (selection or processing). IDEMPOTENT: with no active
    OCR it does nothing (never a new start). Returns True if it sent the
    cancellation. If the OCR is already at the clipboard write it lets it finish.
    """
    if not is_active():
        _repair_status()
        return False
    data = _lock_data() or {}
    pid = data["pid"]
    current = status.read_status()
    if current.get("state") == status.STATE_PROCESSING and current.get("cancellable") is False:
        logger.info("OCR annullamento rifiutato: gia' alla scrittura negli appunti")
        return False
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + CANCEL_WAIT_SECONDS
    while time.time() < deadline and audio._pid_alive(pid) and _lock_data() is not None:
        time.sleep(0.05)
    if audio._pid_alive(pid) and _lock_data() is not None:
        current = status.read_status()
        if current.get("cancellable") is False:
            return True
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        child = data.get("child_pid")
        if isinstance(child, int) and child > 0 and audio._pid_alive(child) \
                and audio.pid_matches(child, ("gnome-screenshot",)):
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)
        OCR_LOCK_PATH.unlink(missing_ok=True)
        _repair_status()
    return True


def _repair_status() -> None:
    """Nessun OCR vivo ma status.json dice ancora 'processing' di OCR: residuo,
    si riporta a idle cosi' il controllo non resta su "Annulla".

    No live OCR but status.json still says OCR 'processing': leftover, back to
    idle so the control does not stay on "Cancel".
    """
    current = status.read_status()
    if current.get("state") == status.STATE_PROCESSING and current.get("service") == "ocr":
        with contextlib.suppress(Exception):
            status.write_status(status.STATE_IDLE, service="ocr")


def handle_capture(cfg: Config) -> None:
    """Avvio OCR annullabile: lock + SIGTERM = annulla. Un secondo avvio con un OCR
    vivo viene ignorato (mai due catture sovrapposte).

    Cancellable OCR start: lock + SIGTERM = cancel. A second start with a live OCR
    is ignored (never two overlapping captures).
    """
    if not _acquire_lock():
        logger.info("OCR gia' attivo, avvio ignorato")
        return
    _Run.committed = False
    _Run.cancelled = False
    previous = None
    try:
        previous = signal.signal(signal.SIGTERM, _on_cancel_signal)
    except ValueError:  # non nel thread principale | not in the main thread
        pass
    try:
        _capture(cfg)
    except OcrCancelled:
        logger.info("OCR annullato dall'utente")
        with contextlib.suppress(Exception):
            status.write_status(status.STATE_IDLE, service="ocr")
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        OCR_LOCK_PATH.unlink(missing_ok=True)


def _capture(cfg: Config) -> None:
    # Con capture_screenshot=True una doppia pressione della scorciatoia (o
    # l'attesa lunga della selezione, fino a SELECTION_TIMEOUT_SECONDS)
    # lancerebbe un secondo gnome-screenshot interattivo sopra il primo: due
    # selezioni sovrapposte, nessun crash ma un'esperienza confusa. La
    # lettura clipboard (ramo di default) e' invece istantanea e idempotente,
    # quindi non ha bisogno di questa guardia: una doppia pressione ci legge
    # la stessa immagine due volte, innocuo.
    # With capture_screenshot=True a double press of the shortcut (or the long
    # selection wait, up to SELECTION_TIMEOUT_SECONDS) would launch a second
    # interactive gnome-screenshot on top of the first: two overlapping
    # selections, no crash but a confusing experience. Reading the clipboard
    # (default branch) is instead instantaneous and idempotent, so it does not
    # need this guard: a double press reads the same image twice, harmless.
    if cfg.ocr_capture_screenshot:
        current = status.read_status()
        if current.get("state") == status.STATE_PROCESSING and current.get("service") == "ocr":
            # Solo se lo stato e' FRESCO: due selezioni possono sovrapporsi
            # unicamente entro SELECTION_TIMEOUT_SECONDS dall'avvio della
            # prima. Uno 'processing' piu' vecchio e' un residuo (processo
            # ucciso con kill -9 o crash durante la selezione, nessun
            # finally che lo corregga): senza questo limite il guard
            # bloccherebbe in silenzio OGNI cattura successiva fino al
            # watchdog dell'estensione (120 min per ocr).
            # Only if the state is FRESH: two selections can overlap only within
            # SELECTION_TIMEOUT_SECONDS from the start of the first. An older
            # 'processing' is a leftover (process killed with kill -9 or crashed during
            # the selection, no finally to fix it): without this limit the guard would
            # silently block EVERY later capture until the extension watchdog (120 min
            # for ocr).
            ts = current.get("timestamp")
            age = time.time() - ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0.0
            if age < cfg.screenshot_timeout_seconds:
                logger.info("cattura OCR gia' in corso, secondo tasto ignorato")
                return
            logger.info("stato 'processing' OCR vecchio di %.0fs: residuo, si procede", age)
        # Diverso dall'annullamento (Esc) gestito piu' sotto: qui la feature
        # e' STATA attivata dall'utente ma non puo' funzionare AFFATTO,
        # sempre, ad ogni pressione — merita un avviso esplicito UNA volta,
        # non lo stesso silenzio di un cambio idea. Senza questo controllo
        # capture_area_png() fallirebbe comunque in modo sicuro (None), ma
        # l'utente non avrebbe alcun segnale del perche' non succede nulla.
        # Different from the cancel (Esc) handled further below: here the feature
        # WAS enabled by the user but cannot work AT ALL, always, on every press —
        # it deserves one explicit warning, not the same silence as a change of
        # mind. Without this check capture_area_png() would still fail safely
        # (None), but the user would have no signal about why nothing happens.
        if not screenshot.is_available():
            status.write_status(status.STATE_ERROR)
            if cfg.notifications and cfg.notif_ocr.error:
                notify.send(_("OCR: screenshot tool missing"), _("Install gnome-screenshot to use this feature"), icon=notify.resolve_icon("error_general", cfg.icons.error_general))
            return
    try:
        status.write_status(status.STATE_PROCESSING, service="ocr")
    except Exception:
        logger.debug("impossibile aggiornare lo status su PROCESSING", exc_info=True)
    notify.maybe_send_simple(
        cfg.notifications, cfg.notif_ocr.processing_start, _("OCR: processing"),
        icon=notify.resolve_icon("ocr_start", cfg.icons.ocr_start),
    )

    if cfg.ocr_capture_screenshot:
        image_bytes = screenshot.capture_area_png(
            cfg.screenshot_timeout_seconds,
            on_spawn=lambda pid: _write_lock({"child_pid": pid}, exclusive=False),
        )
        if image_bytes is None:
            # Annullato (Esc) o nessuna risposta: un cambio idea dell'utente,
            # non un errore. Si torna a idle senza notifica, cosi' come non
            # si notifica mai una scorciatoia premuta per sbaglio due volte.
            # Cancelled (Esc) or no answer: a change of mind by the user, not an error.
            # We go back to idle without a notification, just as we never notify a
            # shortcut pressed twice by mistake.
            try:
                status.write_status(status.STATE_IDLE)
            except Exception:
                logger.debug("impossibile aggiornare lo status su IDLE", exc_info=True)
            return
    else:
        try:
            image_bytes = clipboard.read_image_png(cfg.clipboard_paste_tool, cfg.clipboard_timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - fail fast con notifica utente | fail fast with a user notification
            status.write_status(status.STATE_ERROR)
            if cfg.notifications and cfg.notif_ocr.error:
                notify.send(_("OCR: no image in clipboard"), str(exc), icon=notify.resolve_icon("error_general", cfg.icons.error_general))
            return

    try:
        storage.save_if_enabled(cfg.storage.base_dir, "ocr/original", cfg.storage.ocr_original, image_bytes, "png")
    except Exception:
        logger.warning("impossibile salvare ocr/original su disco", exc_info=True)

    try:
        raw_text = try_with_fallback(
            cfg.ocr_fallback,
            lambda level: vision_extract(level, cfg.ocr_system_prompt, image_bytes),
        )
    except AllLevelsFailedError as exc:
        status.write_status(status.STATE_ERROR)
        if cfg.notifications and cfg.notif_ocr.error:
            notify.send(_("OCR: extraction error"), str(exc), icon=notify.resolve_icon("error_general", cfg.icons.error_general))
        return

    # D1 (mandato: debito più serio mai chiuso, lato OCR): stessa guardia
    # lato chiamante già presente in stt.py. vision_extract ora solleva
    # ApiError sul vuoto, ma questa è difesa in profondità indipendente:
    # se mai un livello tornasse "" senza sollevare, qui si segnala
    # l'errore invece di scrivere una stringa vuota negli appunti.
    # D1 (mandate: the most serious debt ever closed, OCR side): same
    # caller-side guard already present in stt.py. vision_extract now raises
    # ApiError on empty output, but this is an independent defense in depth: if
    # a level ever returned "" without raising, the error is reported here
    # instead of writing an empty string to the clipboard.
    if not raw_text or not raw_text.strip():
        status.write_status(status.STATE_ERROR, service="ocr")
        if cfg.notifications and cfg.notif_ocr.error:
            notify.send(_("OCR: extraction error"), _("Empty extraction"), icon=notify.resolve_icon("error_general", cfg.icons.error_general))
        return

    _commit()
    if cfg.double_injection:
        try:
            clipboard.write_text(raw_text, cfg.clipboard_tool, cfg.clipboard_timeout_seconds)
        except Exception:
            # Non fatale: la scrittura finale (sotto) e' quella che conta. Se
            # anche quella fallisce l'utente viene avvisato esplicitamente.
            # Not fatal: the final write (below) is the one that matters. If that fails
            # too, the user is warned explicitly.
            logger.warning("impossibile scrivere il testo grezzo negli appunti", exc_info=True)

    notify.maybe_send(
        cfg.notifications, cfg.notif_ocr.raw_ready, _("OCR: raw text ready"), raw_text,
        icon=notify.resolve_icon("ocr_raw", cfg.icons.ocr_raw),
    )
    try:
        storage.save_text_if_enabled(cfg.storage.base_dir, "ocr/raw", cfg.storage.ocr_raw, raw_text)
    except Exception:
        logger.warning("impossibile salvare ocr/raw su disco", exc_info=True)
    try:
        output_history.append_entry("ocr", "raw", raw_text, cfg.history_max_entries)
    except Exception:
        logger.debug("impossibile salvare la voce raw nella cronologia", exc_info=True)

    final_text = raw_text
    cleanup_ran = False
    if cfg.ocr_cleanup.enabled and cfg.ocr_cleanup.fallback:
        try:
            final_text = cleanup_with_validation(
                cfg.ocr_cleanup.fallback, cfg.ocr_cleanup.system_prompt, raw_text,
                min_length_ratio=cfg.cleanup_min_length_ratio,
            )
            cleanup_ran = True
        except AllLevelsFailedError as exc:
            logger.warning("OCR LLM cleanup failed, keeping raw text: %s", exc)

    try:
        clipboard.write_text(final_text, cfg.clipboard_tool, cfg.clipboard_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - fail fast con notifica utente | fail fast with a user notification
        # Senza questa guardia l'eccezione salterebbe write_status(IDLE)
        # lasciando lo stato bloccato su "processing" fino al timeout
        # dell'estensione (120 min), con l'icona ferma su content-loading.
        # Without this guard the exception would skip write_status(IDLE), leaving
        # the state stuck on "processing" until the extension timeout (120 min),
        # with the icon stuck on content-loading.
        logger.error("impossibile scrivere negli appunti: %s", exc)
        status.write_status(status.STATE_ERROR)
        if cfg.notifications and cfg.notif_ocr.error:
            notify.send(_("OCR: clipboard error"), str(exc), icon=notify.resolve_icon("error_general", cfg.icons.error_general))
        return

    try:
        status.write_status(status.STATE_IDLE, last_output=final_text, service="ocr")
    except Exception:
        logger.debug("impossibile aggiornare lo status su IDLE", exc_info=True)
    if cleanup_ran:
        notify.maybe_send(
            cfg.notifications, cfg.notif_ocr.cleanup_ready, _("OCR: cleaned text ready"), final_text,
            icon=notify.resolve_icon("ocr_clean", cfg.icons.ocr_clean),
        )
        try:
            storage.save_text_if_enabled(cfg.storage.base_dir, "ocr/clean", cfg.storage.ocr_clean, final_text)
        except Exception:
            logger.warning("impossibile salvare ocr/clean su disco", exc_info=True)
        try:
            output_history.append_entry("ocr", "clean", final_text, cfg.history_max_entries)
        except Exception:
            logger.debug("impossibile salvare la voce clean nella cronologia", exc_info=True)
