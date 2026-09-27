"""Cronologia opzionale su disco: file originali/testo grezzo/testo pulito
per STT e OCR, con cancellazione automatica configurabile per tipo. Nessun
demone: la pulizia scatta opportunisticamente dopo ogni salvataggio, coerente
con l'esecuzione on-demand del tool (nessun processo sempre attivo)."""
from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path

from .config import RetentionPolicy


def _resolve_base_dir(base_dir: str) -> Path:
    return Path(base_dir).expanduser()


def save_if_enabled(base_dir: str, subdir: str, policy: RetentionPolicy, content: bytes, ext: str) -> Path | None:
    """Salva `content` in <base_dir>/<subdir>/<timestamp>.<ext> se policy.enabled,
    poi ripulisce i file scaduti nella stessa sottocartella."""
    if not policy.enabled:
        return None

    target_dir = _resolve_base_dir(base_dir) / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir.chmod(0o700)  # stesso motivo del chmod sotto: non elencabile da altri

    timestamp = time.strftime("%Y-%m-%dT%H-%M-%S")
    path = target_dir / f"{timestamp}.{ext}"
    counter = 1
    # La premessa dello scout (un open(..., 'x') in un loop che fallirebbe in
    # silenzio se il file esistesse) e' FALSA: qui non c'e' nessuna open(), e
    # il ciclo while path.exists() termina sempre al primo nome libero (misurato
    # con 5 salvataggi nello stesso secondo e con 3 slot gia' occupati: 5/5
    # file distinti, 0 sovrascritture, 0 perdita silenziosa). Resta pero' una
    # finestra TOCTOU reale e diversa: exists() e poi write_bytes() non sono
    # atomici, quindi due processi che salvano lo stesso millisecondo possono
    # scegliere lo stesso nome e l'ultimo write_bytes() sovrascrive il primo.
    # Il tentativo con O_EXCL chiude la finestra: se il nome e' gia' preso da un
    # altro processo, l'eccezione NON e' silenziosa, e' il segnale per passare
    # al successivo. Un livello di protezione in piu', nessuna regressione.
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # nome gia' preso (stesso secondo, o race con un altro processo)
            path = target_dir / f"{timestamp}-{counter}.{ext}"
            counter += 1
            continue
        # Qualsiasi altro errore di apertura (filesystem read-only, quota
        # esaurita, permessi) e' reale e non risolvibile con un altro nome:
        # viene lasciato salire al chiamante, esattamente come prima della
        # modifica. Nessun fallback silenzioso, nessuna perdita di dati.
        #
        # B3 (giro 4): l'fd aperto con O_EXCL serve solo a RISERVARE il nome
        # (e' il meccanismo anti-TOCTOU descritto sopra); il contenuto non ci
        # passa piu'. Con `fh.write(content)` sul file definitivo, un ENOSPC a
        # meta' scriveva lasciando sul disco un file PARZIALE col nome
        # timestampato definitivo: l'utente lo credeva una registrazione
        # completa e, con retention_hours=0 (che fa return immediato in
        # _purge_expired), non sarebbe mai stato ripulito. Misurato dal vivo
        # prima della correzione: OSError [Errno 28] propagata al chiamante e
        # 10 byte su 500 rimasti su disco col nome definitivo.
        # Ora il contenuto va su un temporaneo univoco nella STESSA
        # directory (obbligatorio: os.replace non puo' attraversare i mount
        # point) e il nome definitivo compare solo per os.replace, che e'
        # atomico. Stesso pattern di status.py:26-34 e output_history.py:55-64.
        os.close(fd)
        fd_tmp, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".tmp",
                                            prefix=f"{path.name}.")
        try:
            with os.fdopen(fd_tmp, "wb") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            # BaseException, non solo Exception: e' la stessa scelta di
            # status.py e output_history.py, e serve a coprire anche
            # KeyboardInterrupt/SystemExit, che lascerebbero altrimenti un
            # .tmp (e il nome definitivo vuoto) in giro.
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            # Il nome definitivo era stato solo riservato: nessun dato
            # dell'utente ci vive, quindi si rimuove. Se non si potesse, non
            # deve mascherare l'errore originale: si sopprime e si rilancia
            # comunque l'eccezione della write, che e' la notizia importante.
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            raise
        break

    # P2 (giro 14): write_bytes non imposta permessi, soggetto a umask di
    # sistema — questi file possono contenere audio/testo dettato/OCR
    # riservato, non devono restare leggibili da altri utenti locali. La
    # modalita' 0o600 qui e' richiesta dall'argomento di os.open qui sopra; il
    # chmod resta per il percorso gia' creato da una versione precedente.
    path.chmod(0o600)
    _purge_expired(target_dir, policy.retention_hours)
    return path


def save_text_if_enabled(base_dir: str, subdir: str, policy: RetentionPolicy, text: str) -> Path | None:
    return save_if_enabled(base_dir, subdir, policy, text.encode("utf-8"), "txt")


def _purge_expired(target_dir: Path, retention_hours: int) -> None:
    if retention_hours <= 0:
        return
    cutoff = time.time() - retention_hours * 3600
    for f in target_dir.iterdir():
        if f.is_file():
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except FileNotFoundError:
                continue
