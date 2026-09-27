"""Registrazione microfono toggle (start/stop) in OGG/Opus via ffmpeg."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .config import AudioConfig

logger = logging.getLogger(__name__)


def _runtime_dir() -> Path:
    """XDG_RUNTIME_DIR è per-utente e privato (0700). Fallback: sottocartella
    per-uid nella temp dir di sistema, per non condividere il lock tra utenti."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "bravoric-stt-clipboard"
    return Path(tempfile.gettempdir()) / f"bravoric-stt-clipboard-{os.getuid()}"


LOCK_PATH = _runtime_dir() / "recording.lock"


class ToggleDebouncedError(RuntimeError):
    """Seconda pressione arrivata prima del debounce minimo: ignorata."""


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
    started_at = data["started_at"]
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        return None
    if not isinstance(data["audio_path"], str):
        return None
    return data


def is_recording() -> bool:
    """Un lock con pid non più vivo è un residuo (ffmpeg morto/crash): va
    ripulito, altrimenti l'estensione resta convinta di stare registrando e la
    pressione successiva non fa ripartire nulla."""
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
        if lock["audio_path"].strip():
            try:
                Path(lock["audio_path"]).unlink(missing_ok=True)
            except (OSError, KeyError):
                logger.debug("impossibile rimuovere audio residuo da lock morto")
        LOCK_PATH.unlink(missing_ok=True)
        return False
    return True


def start_recording(audio_cfg: AudioConfig) -> Path:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    # B1: lock atomico con O_CREAT|O_EXCL: se il lock esiste già, l'avvio
    # fallisce immediatamente (il secondo toggle viene scartato).
    # Senza questo fix, due invocazioni concorrenti possono entrambe creare
    # un processo ffmpeg, con il primo che resta orfano.
    try:
        fd = os.open(str(LOCK_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Lock presente: c'è già una registrazione in corso, o un lock residuo.
        # Verifica se il processo è vivo prima di decidere.
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
        os.close(fd)
    return out_path


def stop_recording(audio_cfg: AudioConfig) -> Path:
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
    if not lock["audio_path"].strip():
        LOCK_PATH.unlink(missing_ok=True)
        raise RuntimeError("No recording in progress")
    audio_path = Path(lock["audio_path"])
    if _pid_alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGINT)
        for _ in range(50):
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
        # B4: fallback SIGTERM -> SIGKILL se il processo non muore dopo 5s.
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
    try:
        empty = not audio_path.exists() or audio_path.stat().st_size == 0
    except OSError:
        empty = True
    if empty:
        with contextlib.suppress(OSError):
            audio_path.unlink()
        raise RuntimeError("No recording in progress")
    return audio_path


def _pid_alive(pid: int) -> bool:
    # B7: type guard — un lock manomesso con pid non-int causerebbe TypeError
    # non catturato in os.kill.
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
