"""Registrazione del microfono a toggle (start/stop) in OGG/Opus via ffmpeg.

Registrazione microfono toggle (start/stop) in OGG/Opus via ffmpeg.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from .config import AudioConfig

logger = logging.getLogger(__name__)


def _runtime_dir() -> Path:
    """XDG_RUNTIME_DIR è per-utente e privato (0700). Fallback: sottocartella
    per-uid nella temp dir di sistema, per non condividere il lock tra utenti.

    XDG_RUNTIME_DIR is per-user and private (0700). Fallback: a per-uid
    subfolder in the system temp dir, so the lock is not shared between users.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "bravoric-stt-clipboard"
    return Path(tempfile.gettempdir()) / f"bravoric-stt-clipboard-{os.getuid()}"


LOCK_PATH = _runtime_dir() / "recording.lock"
# Sottostringhe attese nella cmdline del registratore (vedi pid_matches).
# Substrings expected in the recorder's cmdline (see pid_matches).
RECORDER_MARKERS = ("ffmpeg",)


def ensure_private_dir(path: Path) -> None:
    """Crea `path` (e i genitori) e garantisce che sia NOSTRA e 0700.

    Serve per la directory di runtime: contiene i lock e, per lo streaming, le
    WAV con la VOCE dell'utente. Con XDG_RUNTIME_DIR (0700, per-utente) e'
    gia' protetta; nel fallback `<tmp>/bravoric-stt-clipboard-<uid>` un
    mkdir con umask la crea 0755 (WAV leggibili da altri utenti locali) e un
    nome prevedibile in /tmp puo' essere PRE-CREATO da un altro utente: senza
    controllo si scriverebbero lock e audio in casa sua. Symlink e directory
    di un altro uid vengono rifiutate, non usate.

    Creates `path` (and its parents) and guarantees it is OURS and 0700.

    Needed for the runtime directory: it holds the locks and, for streaming,
    the WAVs with the user's VOICE. With XDG_RUNTIME_DIR (0700, per-user) it is
    already protected; in the `<tmp>/bravoric-stt-clipboard-<uid>` fallback a
    mkdir with umask creates it 0755 (WAVs readable by other local users) and
    a predictable name in /tmp can be PRE-CREATED by another user: without a
    check, locks and audio would be written into their home. Symlinks and
    directories of another uid are rejected, not used.
    """
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"unsafe runtime directory (symlink or not a directory): {path}")
    if info.st_uid != os.getuid():
        raise RuntimeError(f"runtime directory owned by another user: {path}")
    if info.st_mode & 0o077:
        path.chmod(0o700)


class ToggleDebouncedError(RuntimeError):
    """Seconda pressione arrivata prima del debounce minimo: ignorata.

    Second press arrived before the minimum debounce: ignored.
    """


# Tetto dell'attesa della finestra di avvio in uno stop esplicito (vedi
# stop_recording, wait=True): un avvio che non pubblica il lock completo entro
# questo limite e' morto o bloccato.
# Cap on the start-up window wait in an explicit stop (see stop_recording,
# wait=True): a start that does not publish the full lock within this limit is
# dead or stuck.
START_WINDOW_TIMEOUT_SECONDS = 10.0


def _read_lock() -> dict | None:
    if not LOCK_PATH.exists():
        return None
    try:
        data = json.loads(LOCK_PATH.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # B3: valida che tutti i campi necessari siano presenti e del tipo corretto.
    # Un lock parziale/manomesso in stop_recording causerebbe KeyError/TypeError.
    # B3: validates that all the needed fields are present and of the right
    # type. A partial/tampered lock in stop_recording would cause
    # KeyError/TypeError.
    try:
        pid = data["pid"]
        _ = data["started_at"]
        _ = data["audio_path"]
    except KeyError:
        return None
    if not isinstance(pid, int) or pid <= 0:
        return None
    # Un lock manomesso/parziale con started_at non numerico farebbe fallire
    # `time.time() - lock["started_at"]` in stop_recording (TypeError non
    # catturato); audio_path non-stringa farebbe fallire Path(). Trattali
    # come lock stale.
    # A tampered/partial lock with a non-numeric started_at would make
    # `time.time() - lock["started_at"]` fail in stop_recording (uncaught
    # TypeError); a non-string audio_path would make Path() fail. Treat them as
    # stale locks.
    started_at = data["started_at"]
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        return None
    if not isinstance(data["audio_path"], str):
        return None
    return data


def is_recording() -> bool:
    """Un lock con pid non più vivo è un residuo (ffmpeg morto/crash): va
    ripulito, altrimenti l'estensione resta convinta di stare registrando e la
    pressione successiva non fa ripartire nulla.

    A lock whose pid is no longer alive is a leftover (ffmpeg dead/crashed):
    it must be cleaned up, otherwise the extension stays convinced it is
    recording and the next press restarts nothing.
    """
    lock = _read_lock()
    if lock is None:
        return False
    if not _pid_alive(lock["pid"]):
        # B2: pulizia del file audio residuo quando ffmpeg muore/crasha.
        # Senza questo fix, un file .ogg resta per sempre in /tmp.
        # audio_path vuoto = placeholder della finestra di avvio: non c'e'
        # nessun file associato, e Path("") e' la cwd (cancellarla non ha
        # senso, e il tentativo e' innocuo solo per fortuna: non solleva, ma
        # non cancella nemmeno il .ogg vero). Lo stesso segnale che in
        # stop_recording, trattato uguale: si rimuove il lock e basta.
        # B2: cleanup of the leftover audio file when ffmpeg dies/crashes. Without
        # this fix, an .ogg file stays in /tmp forever. Empty audio_path = start-up
        # window placeholder: there is no associated file, and Path("") is the cwd
        # (deleting it makes no sense, and the attempt is harmless only by luck: it
        # does not raise, but it does not delete the real .ogg either). The same
        # signal as in stop_recording, treated the same way: only the lock is
        # removed.
        if lock["audio_path"].strip():
            try:
                Path(lock["audio_path"]).unlink(missing_ok=True)
            except (OSError, KeyError):
                logger.debug("impossibile rimuovere audio residuo da lock morto")
        LOCK_PATH.unlink(missing_ok=True)
        return False
    return True


def start_recording(audio_cfg: AudioConfig) -> Path:
    ensure_private_dir(LOCK_PATH.parent)

    # B1: lock atomico con O_CREAT|O_EXCL: se il lock esiste già, l'avvio
    # fallisce immediatamente (il secondo toggle viene scartato).
    # Senza questo fix, due invocazioni concorrenti possono entrambe creare
    # un processo ffmpeg, con il primo che resta orfano.
    # B1: atomic lock with O_CREAT|O_EXCL: if the lock already exists, the start
    # fails immediately (the second toggle is discarded). Without this fix, two
    # concurrent invocations can both create an ffmpeg process, with the first
    # one left orphaned.
    try:
        fd = os.open(str(LOCK_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Lock presente: c'è già una registrazione in corso, o un lock residuo.
        # Verifica se il processo è vivo prima di decidere.
        # Lock present: there is already a recording in progress, or a leftover
        # lock. Check whether the process is alive before deciding.
        lock = _read_lock()
        if lock and _pid_alive(lock["pid"]):
            raise RuntimeError("Recording already in progress") from None
        # Lock stale (processo morto): pulisci e riprova.
        LOCK_PATH.unlink(missing_ok=True)
        fd = os.open(str(LOCK_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    # P3 (giro 12): scrivi subito un placeholder valido (pid del processo
    # corrente, vivo per tutta la finestra di avvio) invece di lasciare il
    # lock vuoto fino a dopo lo spawn di ffmpeg. Senza questo, un
    # start/stop_recording concorrente in quella finestra legge JSON vuoto,
    # lo tratta come lock stale e avvia un secondo ffmpeg orfano.
    # P3 (round 12): write a valid placeholder right away (pid of the current
    # process, alive for the whole start-up window) instead of leaving the lock
    # empty until after the ffmpeg spawn. Without this, a concurrent
    # start/stop_recording in that window reads empty JSON, treats it as a stale
    # lock and starts a second orphan ffmpeg.
    os.write(fd, json.dumps({
        "pid": os.getpid(), "audio_path": "", "started_at": time.time(),
    }).encode())

    fd_audio, path_str = tempfile.mkstemp(suffix=f".{audio_cfg.format}", prefix="bravoric-stt-")
    os.close(fd_audio)
    out_path = Path(path_str)

    try:
        proc = subprocess.Popen(
            [
                "ffmpeg", "-y", "-f", "pulse", "-i", "default",
                "-ac", "1", "-ar", str(audio_cfg.sample_rate),
                "-c:a", audio_cfg.codec, "-b:a", f"{audio_cfg.bitrate_kbps}k",
                str(out_path),
            ],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        # B9: se Popen fallisce (ffmpeg assente/errore), il file vuoto resta in
        # /tmp permanentemente. Rimuovilo prima di rilanciare l'eccezione.
        # B9: if Popen fails (ffmpeg missing/error), the empty file stays in /tmp
        # permanently. Remove it before re-raising the exception.
        out_path.unlink(missing_ok=True)
        os.close(fd)
        LOCK_PATH.unlink(missing_ok=True)
        raise

    lock_data = json.dumps({
        "pid": proc.pid,
        "audio_path": str(out_path),
        "started_at": time.time(),
    })
    # B3 (giro 3): il lock non viene più riscritto in place. La sequenza
    # lseek(0) + ftruncate(0) + write() lasciava il file a ZERO BYTE per tutta
    # la finestra fra il troncamento e la scrittura (~0.01 ms reali, ma una
    # concorrenza la può centrare): in quel momento un toggle concorrente leggeva
    # '' , _read_lock() restituiva None e la catena is_recording()->False +
    # stop "No recording in progress" + secondo ffmpeg partiva con l'audio
    # del primo ancora aperto. Qui il lock è serializzato su un file
    # temporaneo e pubblicato con os.replace, che è atomico: il percorso
    # LOCK_PATH passa dal placeholder valido (scritto sopra, giro 12 P3) al
    # lock completo senza mai attraversare lo zero byte. Stesso pattern già
    # in uso in status._atomic_write_text e stream._atomic_write_json.
    # B3 (round 3): the lock is no longer rewritten in place. The sequence
    # lseek(0) + ftruncate(0) + write() left the file at ZERO BYTES for the
    # whole window between the truncation and the write (~0.01 ms in practice,
    # but a concurrent call can hit it): at that moment a concurrent toggle read
    # '', _read_lock() returned None and the chain is_recording()->False + stop
    # "No recording in progress" + a second ffmpeg started while the audio of
    # the first was still open. Here the lock is serialized to a temporary file
    # and published with os.replace, which is atomic: the LOCK_PATH path goes
    # from the valid placeholder (written above, round 12 P3) to the full lock
    # without ever crossing zero bytes. Same pattern already used in
    # status._atomic_write_text and stream._atomic_write_json.
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(LOCK_PATH.parent),
                                        prefix=LOCK_PATH.name + ".",
                                        suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(lock_data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(LOCK_PATH))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    finally:
        # L'inode del placeholder non è più quello di LOCK_PATH (os.replace ha
        # pubblicato un inode nuovo): va chiuso o il fd resta aperto fino al
        # termine del processo.
        # The placeholder's inode is no longer LOCK_PATH's (os.replace published a
        # new inode): it must be closed or the fd stays open until the process ends.
        os.close(fd)
    return out_path


def _wait_until_stoppable(audio_cfg: AudioConfig) -> None:
    """Stop ESPLICITO (bottone/menu): non si ignora ne' si fallisce se arriva
    dentro il debounce o la finestra di avvio, si ATTENDE che la registrazione
    sia fermabile. Cosi' un click "Ferma" subito dopo "Avvia" ferma davvero,
    invece di sparire in silenzio (il toggle da scorciatoia resta invariato).

    EXPLICIT stop (button/menu): it is neither ignored nor failed when it arrives
    inside the debounce or the start-up window, it WAITS until the recording can
    be stopped. So a "Stop" click right after "Start" really stops, instead of
    vanishing silently (the shortcut toggle stays unchanged).
    """
    deadline = time.time() + START_WINDOW_TIMEOUT_SECONDS + audio_cfg.toggle_debounce_seconds
    while time.time() < deadline:
        lock = _read_lock()
        if lock is None:
            return
        remaining = audio_cfg.toggle_debounce_seconds - (time.time() - lock["started_at"])
        if remaining > 0:
            time.sleep(min(remaining, 0.2))
            continue
        if not lock["audio_path"].strip() and _pid_alive(lock["pid"]):
            time.sleep(0.1)
            continue
        return


def stop_recording(audio_cfg: AudioConfig, wait: bool = False) -> Path:
    if wait:
        _wait_until_stoppable(audio_cfg)
    lock = _read_lock()
    if lock is None:
        raise RuntimeError("No recording in progress")

    elapsed = time.time() - lock["started_at"]
    if elapsed < audio_cfg.toggle_debounce_seconds:
        raise ToggleDebouncedError(
            f"Debounce active: wait {audio_cfg.toggle_debounce_seconds - elapsed:.2f}s"
        )

    pid = lock["pid"]
    # B5: audio_path VUOTO = siamo dentro la finestra di avvio. start_recording
    # pubblica quel placeholder (Popen -> os.replace, ~1 s, la stessa del
    # debounce di default) perche' un toggle concorrente veda "registrazione
    # in corso" e non apra un secondo ffmpeg; qui pero' non c'e' ancora nessun
    # file da restituire, e Path("") NON e' "nessun file": e' la directory di
    # lavoro. Senza questa guardia stop_recording restituiva la cwd e la
    # catena di trascrizione finiva su una directory (IsADirectoryError, toast
    # "STT: transcription error", registrazione persa) lasciando il .ogg vero
    # a 0 byte in /tmp per tutta la sessione, gia' col nome definitivo.
    # La guardia sta qui e NON in _read_lock: li' il vuoto e' un segnale
    # LEGITTIMO (start_recording lo usa per rifiutare un secondo avvio), e
    # respingerlo farebbe partire un ffmpeg fantasma.
    # B5: EMPTY audio_path = we are inside the start-up window. start_recording
    # publishes that placeholder (Popen -> os.replace, ~1 s, the same as the
    # default debounce) so that a concurrent toggle sees "recording in
    # progress" and does not open a second ffmpeg; but here there is no file to
    # return yet, and Path("") is NOT "no file": it is the working directory.
    # Without this guard stop_recording returned the cwd and the transcription
    # chain ended up on a directory (IsADirectoryError, toast "STT:
    # transcription error", recording lost) leaving the real .ogg at 0 bytes in
    # /tmp for the whole session, already under its final name. The guard lives
    # here and NOT in _read_lock: there the empty value is a LEGITIMATE signal
    # (start_recording uses it to refuse a second start), and rejecting it
    # would start a phantom ffmpeg.
    if not lock["audio_path"].strip():
        LOCK_PATH.unlink(missing_ok=True)
        raise RuntimeError("No recording in progress")
    audio_path = Path(lock["audio_path"])
    if _pid_alive(pid) and not pid_matches(pid, RECORDER_MARKERS):
        # Lock stale con pid RIUSATO da un altro processo: non e' il nostro
        # ffmpeg, nessun segnale (SIGKILL compreso). Si prosegue come per un
        # ffmpeg gia' morto: lock rimosso e controllo del file sotto.
        # Stale lock with a pid REUSED by another process: it is not our ffmpeg, no
        # signal (SIGKILL included). We proceed as for an already dead ffmpeg: lock
        # removed and file check below.
        logger.warning("lock di registrazione stale: il pid %d non e' ffmpeg, non lo segnalo", pid)
    elif _pid_alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGINT)
        for _ in range(50):
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
        # B4: fallback SIGTERM -> SIGKILL se il processo non muore dopo 5s.
        # B4: SIGTERM -> SIGKILL fallback if the process does not die after 5 s.
        if _pid_alive(pid):
            logger.warning("ffmpeg PID %d still alive after SIGINT+5s, sending SIGTERM", pid)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
        if _pid_alive(pid):
            logger.warning("ffmpeg PID %d still alive, sending SIGKILL", pid)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            for _ in range(10):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
    # Il lock va SEMPRE rimosso, anche se ffmpeg è già morto: altrimenti la
    # registrazione successiva non parte più (toggle fantasma permanente).
    # The lock must ALWAYS be removed, even if ffmpeg is already dead:
    # otherwise the next recording never starts again (permanent phantom
    # toggle).
    LOCK_PATH.unlink(missing_ok=True)
    # P1: un file a ZERO BYTE non è una registrazione. Il ramo normale (ffmpeg
    # vivo, chiuso con SIGINT) è proprio quello in cui il file è quasi certo
    # vuoto: l'utente ha premuto due volte di fila, o ffmpeg non ha ancora
    # scritto nulla. Prima tornava come registrazione legittima e la
    # trascrizione partiva su un file vuoto (wl-copy con stringa vuota,
    # cioè appunti azzerati). Stessa guardia del gemello in stream.py, dentro
    # `_stop_at_end_transcribe` (`audio_path.exists() and
    # audio_path.stat().st_size > 0`), che qui
    # mancava. Il file vuoto viene rimosso subito: lasciarlo a terra è
    # esattamente il residuo che nessuno possiede piu'.
    #
    # Attenzione: questa guardia e' DOPO l'eliminazione del lock, quindi
    # vale anche per il ramo _pid_alive falso (ffmpeg già morto). Prima si
    # controllava solo l'esistenza del percorso, che resta vero anche se il
    # file è sparito fra is_recording() e stop_recording() (misurato dal
    # reviewer come caso B): qui si richiede che il file ci sia E non sia
    # vuoto.
    # P1: a ZERO-BYTE file is not a recording. The normal branch (ffmpeg alive,
    # closed with SIGINT) is exactly the one where the file is almost certainly
    # empty: the user pressed twice in a row, or ffmpeg has not written anything
    # yet. Before, it came back as a legitimate recording and the transcription
    # started on an empty file (wl-copy with an empty string, i.e. clipboard
    # wiped). Same guard as the twin in stream.py, inside
    # `_stop_at_end_transcribe` (`audio_path.exists() and
    # audio_path.stat().st_size > 0`), which was missing here. The empty file is
    # removed immediately: leaving it on the floor is exactly the leftover that
    # nobody owns any more.
    #
    # Note: this guard comes AFTER the lock removal, so it also applies to the
    # false _pid_alive branch (ffmpeg already dead). Before, only the existence
    # of the path was checked, which stays true even if the file vanished
    # between is_recording() and stop_recording() (measured by the reviewer as
    # case B): here the file is required to exist AND not be empty.
    try:
        empty = not audio_path.exists() or audio_path.stat().st_size == 0
    except OSError:
        empty = True
    if empty:
        with contextlib.suppress(OSError):
            audio_path.unlink()
        raise RuntimeError("No recording in progress")
    return audio_path


def pid_matches(pid: int, markers: tuple[str, ...]) -> bool:
    """Il processo `pid` e' ancora QUELLO che ci aspettiamo (cmdline contiene
    uno dei `markers`)?

    Serve prima di mandare segnali (SIGINT/SIGTERM/SIGKILL) al pid scritto in
    un lock: dopo un crash il lock resta, e se quel pid e' stato RIUSATO da
    un processo qualunque dell'utente, os.kill lo ucciderebbe. _pid_alive
    non basta: dice solo che *qualcuno* ha quel pid.

    Se /proc non e' leggibile (non-Linux, processo appena sparito, permessi)
    non si puo' giudicare: True, cioe' il comportamento di prima.

    Is process `pid` still THE one we expect (its cmdline contains one of the
    `markers`)?

    Needed before sending signals (SIGINT/SIGTERM/SIGKILL) to the pid written
    in a lock: after a crash the lock stays, and if that pid was REUSED by any
    process of the user, os.kill would kill it. _pid_alive is not enough: it
    only says that *someone* has that pid.

    If /proc is not readable (non-Linux, process just gone, permissions) we
    cannot judge: True, i.e. the behaviour from before.
    """
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except (OSError, ValueError):
        return True
    cmdline = raw.replace(b"\0", b" ").decode("utf-8", "replace")
    return any(marker in cmdline for marker in markers)


def _pid_alive(pid: int) -> bool:
    # B7: type guard — un lock manomesso con pid non-int causerebbe TypeError
    # non catturato in os.kill.
    # B7: type guard — a tampered lock with a non-int pid would cause an
    # uncaught TypeError in os.kill.
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
