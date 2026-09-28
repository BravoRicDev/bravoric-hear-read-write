"""Orchestrazione modalità OCR: clipboard immagine -> vision -> cleanup -> clipboard."""
from __future__ import annotations

import logging
import time

from . import clipboard, notify, output_history, screenshot, status, storage
from .api_client import vision_extract
from .config import Config
from .fallback import AllLevelsFailedError, cleanup_with_validation, try_with_fallback
from .i18n import _
from .screenshot import SELECTION_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


def handle_capture(cfg: Config) -> None:
    # Con capture_screenshot=True una doppia pressione della scorciatoia (o
    # l'attesa lunga della selezione, fino a SELECTION_TIMEOUT_SECONDS)
    # lancerebbe un secondo gnome-screenshot interattivo sopra il primo: due
    # selezioni sovrapposte, nessun crash ma un'esperienza confusa. La
    # lettura clipboard (ramo di default) e' invece istantanea e idempotente,
    # quindi non ha bisogno di questa guardia: una doppia pressione ci legge
    # la stessa immagine due volte, innocuo.
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
            ts = current.get("timestamp")
            age = time.time() - ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0.0
            if age < SELECTION_TIMEOUT_SECONDS:
                logger.info("cattura OCR gia' in corso, secondo tasto ignorato")
                return
            logger.info("stato 'processing' OCR vecchio di %.0fs: residuo, si procede", age)
        # Diverso dall'annullamento (Esc) gestito piu' sotto: qui la feature
        # e' STATA attivata dall'utente ma non puo' funzionare AFFATTO,
        # sempre, ad ogni pressione — merita un avviso esplicito UNA volta,
        # non lo stesso silenzio di un cambio idea. Senza questo controllo
        # capture_area_png() fallirebbe comunque in modo sicuro (None), ma
        # l'utente non avrebbe alcun segnale del perche' non succede nulla.
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
        image_bytes = screenshot.capture_area_png()
        if image_bytes is None:
            # Annullato (Esc) o nessuna risposta: un cambio idea dell'utente,
            # non un errore. Si torna a idle senza notifica, cosi' come non
            # si notifica mai una scorciatoia premuta per sbaglio due volte.
            try:
                status.write_status(status.STATE_IDLE)
            except Exception:
                logger.debug("impossibile aggiornare lo status su IDLE", exc_info=True)
            return
    else:
        try:
            image_bytes = clipboard.read_image_png(cfg.clipboard_paste_tool)
        except Exception as exc:  # noqa: BLE001 - fail fast con notifica utente
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
    if not raw_text or not raw_text.strip():
        status.write_status(status.STATE_ERROR, service="ocr")
        if cfg.notifications and cfg.notif_ocr.error:
            notify.send(_("OCR: extraction error"), _("Empty extraction"), icon=notify.resolve_icon("error_general", cfg.icons.error_general))
        return

    if cfg.double_injection:
        try:
            clipboard.write_text(raw_text, cfg.clipboard_tool)
        except Exception:
            # Non fatale: la scrittura finale (sotto) e' quella che conta. Se
            # anche quella fallisce l'utente viene avvisato esplicitamente.
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
            )
            cleanup_ran = True
        except AllLevelsFailedError as exc:
            logger.warning("OCR LLM cleanup failed, keeping raw text: %s", exc)

    try:
        clipboard.write_text(final_text, cfg.clipboard_tool)
    except Exception as exc:  # noqa: BLE001 - fail fast con notifica utente
        # Senza questa guardia l'eccezione salterebbe write_status(IDLE)
        # lasciando lo stato bloccato su "processing" fino al timeout
        # dell'estensione (120 min), con l'icona ferma su content-loading.
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
