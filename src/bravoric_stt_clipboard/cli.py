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


def stt_toggle_main(argv: list[str] | None = None) -> int:
    """Senza argomenti e' il toggle della scorciatoia. `start` e `stop` sono
    espliciti e IDEMPOTENTI (usati da bottoni e menu): `stop` senza registrazione
    non ne avvia una, `start` con una registrazione in corso non fa nulla.

    Without arguments it is the shortcut toggle. `start` and `stop` are explicit
    and IDEMPOTENT (used by buttons and menu): `stop` with no recording does not
    start one, `start` with a recording running does nothing.
    """
    _setup_logging()
    command = argv if argv is not None else sys.argv[1:]
    action = command[0] if command and command[0] in ("start", "stop") else "toggle"
    try:
        cfg = load_config()
        notify.configure(cfg)  # timeout e lunghezza corpo / timeout and body length
    except ConfigError as exc:
        print(_("Config error: {exc}").format(exc=exc), file=sys.stderr)
        notify.send(
            _("Config error"), str(exc), icon=notify.ICON_ERROR,
        )
        return 1
    try:
        if action == "stop":
            stt.handle_stop(cfg)
        elif action == "start":
            stt.handle_start(cfg)
        else:
            stt.handle_toggle(cfg)
    except Exception:
        # Lanciato da una scorciatoia globale: stderr è invisibile all'utente,
        # quindi un traceback non segnalerebbe nulla. Registriamo l'errore e lo
        # notifichiamo; l'estensione riporterà comunque l'icona a idle tramite
        # il proprio timeout.
        # Launched from a global shortcut: stderr is invisible to the user, so a
        # traceback would report nothing. We log the error and notify it; the
        # extension will bring the icon back to idle anyway through its own timeout.
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
        # B28: write STATE_ERROR so the extension does not stay stuck on
        # 'processing' until the timeout (30 min STT / 120 min OCR).
        #
        # P2: the repair used to write STATE_ERROR WITHOUT service. The guard in
        # status.write_status compares the service and REJECTS the write when the
        # current state is recording with a different service: with
        # {recording, service: stt} on disk, the repair (service=None) was
        # discarded and the repair was INERT — it fixed nothing and warned nobody
        # (measured by the reviewer: identical state before and after). The timeout
        # is not disarmed, it is ARMED and fixes things itself while declaring a
        # false reason ("Recording timed out", measured in extension.js).
        #
        # The fix is to declare its own service — `stt` — so the repair passes the
        # guard, which compares the service and REJECTS the write of a different
        # service. Blindly declaring 'stt' is correct because this branch handles
        # the STT path: if the current state is 'recording' with service='stream',
        # the guard REJECTS anyway (stream != stt) and the live session is not
        # touched. Measured after the first attempt, which propagated the service
        # read from disk: with the state {recording, service=stream} the repair
        # passed the guard and switched off the indicator of a session THAT WAS
        # STILL RECORDING (final state: error/stream) — i.e. the defect this point
        # was meant to close, created by the other half. The fallback when the state
        # declares no service stays useful (old statuses), but the propagated value
        # is no longer the one read.
        try:
            status.write_status(status.STATE_ERROR, service="stt")
        except Exception:
            logger.debug("impossibile aggiornare lo status su ERROR", exc_info=True)
        _report_unexpected_error(_errors_enabled(cfg, "notif_stt"))
        return 1
    return 0


def ocr_capture_main(argv: list[str] | None = None) -> int:
    """Senza argomenti: toggle (OCR attivo = annulla, altrimenti avvia). `start`
    avvia (no-op se gia' attivo), `cancel` annulla (no-op se non attivo): entrambi
    idempotenti. `cancel` non legge la config: deve funzionare anche se e' rotta.

    No arguments: toggle (OCR active = cancel, otherwise start). `start` starts
    (no-op if already active), `cancel` cancels (no-op if not active): both
    idempotent. `cancel` does not read the config: it must work even if it is broken.
    """
    _setup_logging()
    command = argv if argv is not None else sys.argv[1:]
    action = command[0] if command and command[0] in ("start", "cancel") else "toggle"
    if action == "cancel" or (action == "toggle" and ocr.is_active()):
        ocr.cancel()
        return 0
    try:
        cfg = load_config()
        notify.configure(cfg)  # timeout e lunghezza corpo / timeout and body length
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
    """Attiva/disattiva la sessione streaming; `paste` (opzionale) consuma il
    chunk successivo; `stop` (opzionale) termina la sessione se ce n'è una in
    corso ed è un no-op altrimenti (idempotente).

    Toggle streaming session; optional `paste` consumes the next chunk;
    optional `stop` ends the session if one is running and is a no-op
    otherwise (idempotent).
    """
    _setup_logging()
    try:
        cfg = load_config()
        notify.configure(cfg)  # timeout e lunghezza corpo / timeout and body length
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
            # Explicit and IDEMPOTENT close. The default toggle does NOT fit here: with
            # no live session it starts a new recording instead of closing (measured:
            # rc 0, lock created, state.active True), so firing it to "close the one I
            # just closed" would reopen the microphone. Here the end-of-session request
            # is idempotent by construction: no active session = nothing to do, exit 0.
            # It is the path that closes the session dictated by voice command, which
            # must not depend on a state read that may be stale in the millisecond the
            # command arrives.
            if not session.is_active():
                logger.info("stream stop: nessuna sessione attiva, niente da fare")
                return 0
            return 0 if session.stop() else 1
        if session.is_active():
            if not session.stop():
                return 1
            # at_end rende disponibile un solo risultato finale solo dopo lo stop.
            # Nell'estensione GNOME, invece, paste_next verrà attivato dallo stato.
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
        # Same fix as stt_toggle_main (B28/P2), never applied here: without this
        # write the state stays 'recording'/'processing' and for the stream there is
        # NO timeout that fixes it on its own (P4 explicitly excludes 'stream' from
        # the recording limit, precisely because a live session can last hours) — an
        # exception here would block the indicator forever, not just for 30/120
        # minutes like stt/ocr. service='stream' with eyes closed is correct for the
        # same reason documented in stt_toggle_main: this branch handles the stream
        # only, so the write_status guard accepts it when the running state really
        # is its own, and (rightly) rejects an stt/ocr session that meanwhile became
        # active.
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
        # _report_unexpected_error(), not a direct notify.send as before: the same
        # string as stt_toggle_main/ocr_capture_main duplicated here WITHOUT their
        # surrounding try/except — a notify.send that raised at this point (the last
        # except of the function) would have bubbled up with no safety net, crashing
        # the whole CLI process instead of returning 1.
        _report_unexpected_error(_errors_enabled(cfg, "notif_stream"))
        return 1
    return 0


def chunk_log_main(argv: list[str] | None = None) -> int:
    """Legge il log JSONL dei chunk di dettatura.

    Stesso stile delle altre entry point: ritorna un codice di uscita e
    delega al modulo che conosce il formato. Il percorso del log e'
    iniettabile con `--path`, cosi' si puo' leggere una copia senza toccare
    quella della sessione viva.

    Reads the JSONL log of dictation chunks.

    Same style as the other entry points: returns an exit code and delegates
    to the module that knows the format. The log path is injectable with
    `--path`, so a copy can be read without touching the one of the live
    session.
    """
    _setup_logging()
    from . import chunk_log

    return chunk_log.main(argv)


def _errors_enabled(cfg: Any, service_attr: str) -> bool:
    """Notifiche master + interruttore d'errore del servizio. Difensivo: e'
    chiamato DENTRO un except, deve restare acceso (True) se la config e'
    incompleta, mai sollevare a sua volta.

    Master notifications + the service's error switch. Defensive: it is
    called INSIDE an except, it must stay on (True) if the config is
    incomplete, and never raise itself.
    """
    try:
        return bool(cfg.notifications and getattr(cfg, service_attr).error)
    except AttributeError:
        return True


def _report_unexpected_error(enabled: bool = True) -> None:
    """`enabled`: interruttore del servizio ([notifications] <servizio>_on_error,
    GUI: pagina Notifiche). Non si applica a "Config error", che resta sempre
    attivo: con la config illeggibile non c'e' nessuno switch da leggere.

    `enabled`: the service switch ([notifications] <service>_on_error, GUI:
    Notifications page). It does not apply to "Config error", which always
    stays on: with an unreadable config there is no switch to read.
    """
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
