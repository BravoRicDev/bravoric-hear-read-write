"""Entry point CLI: richiamati dalle scorciatoie globali GNOME."""
from __future__ import annotations

import logging
import sys
from typing import Any

from . import notify, ocr, status, stt
from .config import ConfigError, load_config
from .i18n import _

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def stt_toggle_main() -> int:
    _setup_logging()
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(_("Config error: {exc}").format(exc=exc), file=sys.stderr)
        notify.send(
            _("Config error"), str(exc), icon=notify.ICON_ERROR,
        )
        return 1
    try:
        stt.handle_toggle(cfg)
    except Exception:
        # Lanciato da una scorciatoia globale: stderr è invisibile all'utente,
        # quindi un traceback non segnalerebbe nulla. Registriamo l'errore e lo
        # notifichiamo; l'estensione riporterà comunque l'icona a idle tramite
        # il proprio timeout.
        logger.exception("unexpected error in STT toggle")
        # B28: scrivi STATE_ERROR così l'estensione non resta bloccata su
        # 'processing' fino al timeout (30 min STT / 120 min OCR).
        #
        # P2: la riparazione scriveva STATE_ERROR SENZA service. Il guard di
        # status.write_status confronta il servizio e RESPINGE la scrittura
        # quando lo stato corrente e' recording con un servizio diverso: con
        # {recording, service: stt} sul disco, la riparazione (service=None)
        # veniva scartata e la riparazione era INERTE — non rimetteva a posto
        # nulla e non avvisava nessuno (misurato dal reviewer: stato
        # identico prima e dopo). Il timeout non e' disarmato, e' ARMATO e
        # rimette lui a posto dichiarando un motivo falso ("Recording timed
        # out", misurato in extension.js).
        #
        # Il fix e' dichiarare il proprio servizio — `stt` — cosi' la
        # riparazione passa il guard, che confronta il servizio e RESPINTA
        # la scrittura di un servizio diverso. Dichiarare a cieco 'stt' e'
        # corretto perche' questo ramo gestisce il percorso STT: se lo stato
        # corrente e' 'recording' con service='stream', il guard RESPINGE
        # comunque (stream != stt) e la sessione viva non viene toccata.
        # Misurato dopo il primo tentativo, che propagava il service letto
        # da disco: con lo stato {recording, service=stream} la riparazione
        # passava il guard e spegneva l'indicatore di una sessione CHE
        # STAVA ANCORA REGISTRANDO (stato finale: error/stream) — cioe' il
        # difetto che questo punto doveva chiudere, creato dall'altra meta.
        # Il fallback quando lo stato non dichiara un servizio resta utile
        # (status vecchi), ma il valore propagato non e' piu' quello letto.
        try:
            status.write_status(status.STATE_ERROR, service="stt")
        except Exception:
            logger.debug("impossibile aggiornare lo status su ERROR", exc_info=True)
        _report_unexpected_error(_errors_enabled(cfg, "notif_stt"))
        return 1
    return 0


def ocr_capture_main() -> int:
    _setup_logging()
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(_("Config error: {exc}").format(exc=exc), file=sys.stderr)
        notify.send(
            _("Config error"), str(exc), icon=notify.ICON_ERROR,
        )
        return 1
    try:
        ocr.handle_capture(cfg)
    except Exception:
        logger.exception("unexpected error in OCR capture")
        try:
            status.write_status(status.STATE_ERROR)
        except Exception:
            logger.debug("impossibile aggiornare lo status su ERROR", exc_info=True)
        _report_unexpected_error(_errors_enabled(cfg, "notif_ocr"))
        return 1
    return 0


def stream_toggle_main(argv: list[str] | None = None) -> int:
    """Toggle streaming session; optional `paste` consumes the next chunk;
    optional `stop` ends the session if one is running and is a no-op
    otherwise (idempotent)."""
    _setup_logging()
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(_("Config error: {exc}").format(exc=exc), file=sys.stderr)
        notify.send(_("Config error"), str(exc), icon=notify.ICON_ERROR)
        return 1

    from . import stream as _stream_mod
    session = _stream_mod.StreamSession(cfg)
    command = argv if argv is not None else sys.argv[1:]
    try:
        if command and command[0] == "paste":
            return 0 if session.paste_next() else 1
        if command and command[0] == "stop":
            # Chiusura esplicita e IDEMPOTENTE. Il toggle di default NON
            # serve qui: con nessuna sessione viva avvia una nuova
            # registrazione invece di chiudere (misurato: rc 0, lock
            # creato, state.active True), quindi sparargli per 'chiudere
            # quella che ho appena chiuso' riaprirebbe il microfono. Qui la
            # richiesta di fine sessione e' idempotente per costruzione:
            # nessuna sessione attiva = niente da fare, uscita 0. E' il
            # percorso che chiude la sessione dettata a comando vocale,
            # che non deve dipendere da una lettura di stato che puo'
            # essere obsoletta nel millisecondo in cui arriva il comando.
            if not session.is_active():
                logger.info("stream stop: nessuna sessione attiva, niente da fare")
                return 0
            return 0 if session.stop() else 1
        if session.is_active():
            if not session.stop():
                return 1
            # at_end makes one final result available only after stop. In the
            # GNOME extension, paste_next will instead be triggered by state.
            if cfg.stream.mode == "at_end":
                session.paste_next()
        else:
            if not session.start():
                return 1
    except Exception:
        logger.exception("unexpected error in streaming toggle")
        # Stesso fix di stt_toggle_main (B28/P2), mai applicato qui: senza
        # questa scrittura lo stato resta 'recording'/'processing' e per lo
        # stream NON c'e' un timeout che lo corregga da solo (P4 esclude
        # esplicitamente 'stream' dal limite di recording, apposta perche'
        # una sessione live puo' durare ore) — un'eccezione qui bloccherebbe
        # l'indicatore per sempre, non solo per 30/120 minuti come stt/ocr.
        # service='stream' a occhi chiusi e' corretto per lo stesso motivo
        # documentato in stt_toggle_main: questo ramo gestisce solo lo
        # stream, quindi il guard di write_status lo accetta quando lo
        # stato in corso e' davvero il proprio, e respinge (giustamente)
        # una sessione stt/ocr che nel frattempo fosse diventata attiva.
        try:
            status.write_status(status.STATE_ERROR, service="stream")
        except Exception:
            logger.debug("impossibile aggiornare lo status su ERROR", exc_info=True)
        # _report_unexpected_error(), non un notify.send diretto come prima:
        # stessa stringa di stt_toggle_main/ocr_capture_main duplicata qui
        # SENZA il loro try/except attorno — un notify.send che avesse
        # sollevato in questo punto (l'ultimo except della funzione) sarebbe
        # risalito senza rete, facendo crashare l'intero processo CLI invece
        # di tornare 1.
        _report_unexpected_error(_errors_enabled(cfg, "notif_stream"))
        return 1
    return 0


def chunk_log_main(argv: list[str] | None = None) -> int:
    """Legge il log JSONL dei chunk di dettatura.

    Stesso stile delle altre entry point: ritorna un codice di uscita e
    delega al modulo che conosce il formato. Il percorso del log e'
    iniettabile con `--path`, cosi' si puo' leggere una copia senza toccare
    quella della sessione viva.
    """
    _setup_logging()
    from . import chunk_log

    return chunk_log.main(argv)


def _errors_enabled(cfg: Any, service_attr: str) -> bool:
    """Notifiche master + interruttore d'errore del servizio. Difensivo: e'
    chiamato DENTRO un except, deve restare acceso (True) se la config e'
    incompleta, mai sollevare a sua volta."""
    try:
        return bool(cfg.notifications and getattr(cfg, service_attr).error)
    except AttributeError:
        return True


def _report_unexpected_error(enabled: bool = True) -> None:
    """`enabled`: interruttore del servizio ([notifications] <servizio>_on_error,
    GUI: pagina Notifiche). Non si applica a "Config error", che resta sempre
    attivo: con la config illeggibile non c'e' nessuno switch da leggere."""
    if not enabled:
        return
    try:
        notify.send(
            _("Unexpected error"), _("Check the system log for details"),
            icon=notify.ICON_ERROR,
        )
    except Exception:
        logger.exception("failed to send error notification")


if __name__ == "__main__":
    sys.exit(stt_toggle_main())
