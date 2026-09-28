"""Orchestrazione modalità STT: toggle registrazione -> trascrizione -> cleanup -> clipboard."""
from __future__ import annotations

import logging
from pathlib import Path

from . import audio, clipboard, notify, output_history, status, storage
from .api_client import transcribe_audio
from .config import Config, _command_norm, parse_blacklist
from .fallback import AllLevelsFailedError, cleanup_with_validation, try_with_fallback
from .i18n import _

logger = logging.getLogger(__name__)


def _is_stream_active() -> bool:
    """C'è una sessione streaming viva? (import differito, come in cli.py:
    `stream` e' un modulo grosso e questa scorciatoia gira a ogni pressione
    della scorciatoia; non c'e' ciclo, `stream` non importa `stt`.)"""
    from .stream import is_stream_active
    return is_stream_active()


def _is_blacklisted(text: str, cfg: Config) -> bool:
    """La trascrizione INTERA e' una frase della blacklist dell'utente
    (GUI: Streaming > Chunk blacklist, `[stream].blacklist`)? E' la stessa
    lista dello streaming, non una seconda: qui serve per le allucinazioni
    di Whisper su una registrazione senza voce (misurato dal vivo: ~1 minuto
    di rumore -> "Grazie per la visione!" negli appunti). Stessa normalizzazione
    (maiuscole, punteggiatura finale) e stesso match sull'intero testo, mai su
    sottostringa: una frase vera che la contiene non viene toccata."""
    return _command_norm(text) in parse_blacklist(cfg.stream.blacklist)


def handle_toggle(cfg: Config) -> None:
    if audio.is_recording():
        _stop_and_process(cfg)
    else:
        _start(cfg)


def _start(cfg: Config) -> None:
    # P4 (seconda parte): esclusione reciproca STT <-> streaming. I due lock
    # sono file DISTINTI (recording.lock vs stream.lock), quindi senza questa
    # riga una scorciatoia da tastiera durante una sessione streaming VIVA
    # avviava un secondo ffmpeg sul microfono gia' aperto: due scrittori di
    # audio, due file .ogg, due scritture in clipboard che si sovrascrivono.
    # Non serviva nemmeno il timeout dell'estensione per arrivarci.
    # Il controllo e' l'inverso di quello che StreamSession.start() fa gia'
    # (stream.py, `if audio.is_recording()`): li' e' il flusso a guardare il
    # lock dell'ALTRO, qui il simmetrico.
    if _is_stream_active():
        raise RuntimeError("Stream session already active")
    audio.start_recording(cfg.audio)
    try:
        status.write_status(status.STATE_RECORDING, service="stt")
    except Exception:
        logger.debug("impossibile aggiornare lo status su RECORDING", exc_info=True)
    if cfg.notifications and cfg.notif_stt.recording_start:
        notify.send(_("STT: recording started"), icon=notify.resolve_icon("stt_recording_start", cfg.icons.stt_recording_start))


def _stop_and_process(cfg: Config) -> None:
    try:
        audio_path = audio.stop_recording(cfg.audio)
    except audio.ToggleDebouncedError as exc:
        logger.info(str(exc))
        return
    except RuntimeError as exc:
        # P2: audio.stop_recording solleva RuntimeError in DUE punti
        # (audio.py, "No recording in progress" quando il lock manca e
        # quando audio_path e' vuoto, cioe' dentro la finestra di avvio).
        # Prima sfuggivano qui e risalivano fino a cli.py, che scriveva
        # STATE_ERROR senza service: il guard di status.write_status
        # confronta il servizio e RISPINTA la scrittura quando lo stato
        # corrente e' recording con un altro servizio. Lo stato restava
        # 'recording' con il microfono gia' chiuso, e il file .ogg
        # temporaneo restava a terra (il finally qui sotto non girava
        # perche' audio_path non era ancora stato assegnato). Nota: il
        # RuntimeError di start_recording (audio.py, "Recording already
        # in progress") NON e' una fuga di questo percorso.
        #
        # Giro 18 (P1): qui arriva anche il terzo RuntimeError, quello del
        # file a 0 byte o sparito. Tutti e tre sono lo stesso genere di
        # evento: non c'e' niente da trascrivere, e non e' un errore da
        # notificare (l'utente ha semplicemente premuto due volte).
        logger.info(str(exc))
        return

    try:
        _process_recording(cfg, audio_path)
    finally:
        # Il file audio temporaneo non deve sopravvivere all'elaborazione (né in
        # caso di successo né di errore): evita crescita illimitata in /tmp e
        # residuo di registrazioni vocali su disco.
        try:
            audio_path.unlink()
        except OSError:
            logger.debug("impossibile rimuovere il file audio temporaneo %s", audio_path)


def _process_recording(cfg: Config, audio_path: Path) -> None:
    status.write_status(status.STATE_PROCESSING, service="stt")
    notify.maybe_send_simple(
        cfg.notifications, cfg.notif_stt.processing_start, _("STT: processing"),
        icon=notify.resolve_icon("stt_start", cfg.icons.stt_start),
    )

    if cfg.storage.stt_original.enabled:
        try:
            storage.save_if_enabled(
                cfg.storage.base_dir, "stt/original", cfg.storage.stt_original,
                audio_path.read_bytes(), cfg.audio.format,
            )
        except Exception:
            logger.warning("impossibile salvare stt/original su disco", exc_info=True)

    attempts = cfg.audio.retry_count if cfg.audio.retry_on_error else 1
    raw_text = None
    # Annotato esplicitamente: senza, mypy inferisce AllLevelsFailedError|None
    # dalla prima assegnazione (riga sotto) e la riga 134 (RuntimeError su
    # vuoto senza eccezione a monte) diventa un'incompatibilita' di tipo —
    # innocua a runtime (str(last_error) funziona su qualunque eccezione),
    # ma un falso allarme da mypy che vale la pena chiudere con un tipo vero.
    last_error: Exception | None = None
    for _attempt in range(max(1, attempts)):
        try:
            raw_text = try_with_fallback(
                cfg.stt_fallback,
                lambda level: transcribe_audio(
                    level, audio_path,
                    language=cfg.stt.language or None,
                    prompt=cfg.stt.prompt or None,
                    hotwords=cfg.stt.hotwords or None,
                ),
            )
            break
        except AllLevelsFailedError as exc:
            last_error = exc

    # D1: trattare "" (o solo spazi) come NESSUNA trascrizione. Il test era
    # `raw_text is None`: la catena di fallback puo' pero' restituire una
    # stringa vuota senza aver sollevato (un livello che risponde
    # {"text": ""} e non viene trattato come errore a monte), e il vuoto
    # passava come successo: finiva a wl-copy e AZZERAVA gli appunti. E'
    # la stessa cosa che D1 definisce come difetto, vista dal lato
    # chiamante: qui non si azzera nulla, si segnala l'errore.
    # Stessa uscita dell'empty per una frase in blacklist che e' l'INTERA
    # trascrizione: su una registrazione senza voce (misurato dal vivo: ~1
    # minuto di rumore ambientale -> "Grazie per la visione!") finiva negli
    # appunti come se fosse dettatura vera, sovrascrivendoli.
    hallucinated = raw_text is not None and _is_blacklisted(raw_text, cfg)
    if not raw_text or not raw_text.strip() or hallucinated:
        if raw_text is not None:
            last_error = last_error or RuntimeError(
                "transcription matches the blacklist" if hallucinated
                else "empty transcription")
        status.write_status(status.STATE_ERROR, service="stt")
        if cfg.notifications and cfg.notif_stt.error:
            notify.send(_("STT: transcription error"), str(last_error), icon=notify.resolve_icon("error_general", cfg.icons.error_general))
        return

    # Con double_injection=True: scriviamo raw ora, poi clean dopo la cleanup
    # (due scritture separate: l'utente può catturare il raw nella finestra).
    # Con double_injection=False: scriviamo solo il testo finale una volta sola,
    # evitando che il raw compaia mai negli appunti.
    if cfg.double_injection:
        try:
            clipboard.write_text(raw_text, cfg.clipboard_tool)
        except Exception:
            # Non fatale: la scrittura finale (sotto) e' quella che conta. Se
            # anche quella fallisce l'utente viene avvisato esplicitamente.
            logger.warning("impossibile scrivere il testo grezzo negli appunti", exc_info=True)

    notify.maybe_send(
        cfg.notifications, cfg.notif_stt.raw_ready, _("STT: raw text ready"), raw_text,
        icon=notify.resolve_icon("stt_raw", cfg.icons.stt_raw),
    )
    try:
        storage.save_text_if_enabled(cfg.storage.base_dir, "stt/raw", cfg.storage.stt_raw, raw_text)
    except Exception:
        logger.warning("impossibile salvare stt/raw su disco", exc_info=True)
    try:
        output_history.append_entry("stt", "raw", raw_text, cfg.history_max_entries)
    except Exception:
        logger.debug("impossibile salvare la voce raw nella cronologia", exc_info=True)

    final_text = raw_text
    cleanup_ran = False
    if cfg.stt_cleanup.enabled and cfg.stt_cleanup.fallback:
        try:
            final_text = cleanup_with_validation(
                cfg.stt_cleanup.fallback, cfg.stt_cleanup.system_prompt, raw_text,
                retry_count=cfg.audio.retry_count if cfg.audio.retry_on_error else 1,
            )
            cleanup_ran = True
        except AllLevelsFailedError as exc:
            logger.warning("LLM cleanup failed, keeping raw text: %s", exc)

    # Scrivi il testo finale (clean se la cleanup è riuscita, raw altrimenti) —
    # per double_injection=True è la seconda sovrascrittura; per False è
    # l'unica scrittura negli appunti.
    try:
        clipboard.write_text(final_text, cfg.clipboard_tool)
    except Exception as exc:  # noqa: BLE001 - fail fast con notifica utente
        # Senza questa guardia l'eccezione salterebbe write_status(IDLE)
        # lasciando lo stato bloccato su "processing" fino al timeout
        # dell'estensione (30 min), con l'icona ferma su content-loading.
        logger.error("impossibile scrivere negli appunti: %s", exc)
        status.write_status(status.STATE_ERROR, service="stt")
        if cfg.notifications and cfg.notif_stt.error:
            notify.send(_("STT: clipboard error"), str(exc), icon=notify.resolve_icon("error_general", cfg.icons.error_general))
        return

    try:
        status.write_status(status.STATE_IDLE, last_output=final_text, service="stt")
    except Exception:
        logger.debug("impossibile aggiornare lo status su IDLE", exc_info=True)
    if cleanup_ran:
        notify.maybe_send(
            cfg.notifications, cfg.notif_stt.cleanup_ready, _("STT: cleaned text ready"), final_text,
            icon=notify.resolve_icon("stt_clean", cfg.icons.stt_clean),
        )
        try:
            storage.save_text_if_enabled(cfg.storage.base_dir, "stt/clean", cfg.storage.stt_clean, final_text)
        except Exception:
            logger.warning("impossibile salvare stt/clean su disco", exc_info=True)
        try:
            output_history.append_entry("stt", "clean", final_text, cfg.history_max_entries)
        except Exception:
            logger.debug("impossibile salvare la voce clean nella cronologia", exc_info=True)
