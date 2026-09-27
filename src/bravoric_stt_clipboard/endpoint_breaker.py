"""Circuit breaker per endpoint STT.

Oggi, se un endpoint e' rotto, ogni chunk ci riprova e ogni chunk aspetta il
timeout prima di passare al livello successivo: una sessione lunga con un
server morto costa 3 timeout per utterance, per sempre. Qui un endpoint che
fallisce viene escluso per un'ora (cooldown) e poi riprovato con UNA sola
richiesta di prova (half-open). Se la prova va a buon fine l'endpoint
"risorge" e torna a essere usato normalmente.

Scelte di progetto (vedi plans/CONTRATTO-PARALLEL.md sezione 3):

- CHIAVE DI IDENTITA' = endpoint normalizzato + "|" + model, e la FUNZIONE
  unica e' config.endpoint_key(level) (SPEC-MAX-CONCURRENCY sez. 5): breaker,
  lease e persistenza usano quella, non una chiave propria. I livelli 1 e 2 della
  config utente sono entrambi 10.9.0.2:4001 con model diversi: con una chiave
  host:port i due livelli collasserebbero in uno solo e un fallimento escluderebbe
  entrambi. endpoint_id()/level_id() qui sotto sono solo scorciatoie che
  passano da endpoint_key.
- Il clock e' INIETTATO (parametro `now`): il calcolo non chiama mai time.time()
  da solo, cosi' i test possono far avanzare il tempo senza aspettare un'ora.
- Il cooldown usa time.time() e NON il monotono: il file di stato sopravvive al
  riavvio e un clock monotono ripartirebbe da zero, quindi ogni endpoint
  ripartirebbe "fresco" dopo un crash. Il monotono serve solo al timing
  in-sessione della sonda (durata della richiesta di prova), che non viene
  mai persistito: qui non serve perche' la sonda e' limitata dal single-flight,
  non da un timeout interno.
- Il calcolo non legge il clock di default da solo: `now` e' iniettato e
  I/O solo ai bordi, load()/save() separati dal nucleo puro, che resta
  testabile.
- Scrittura su disco SOLO alle transizioni di stato (primo fallimento,
  fallimento che estende il cooldown, resurrezione). Non una write per chunk:
  con `writes` si puo' verificarlo.
- Lock a due livelli: threading.RLock per il processo (il lease e la sonda
  half-open sono single-flight) e fcntl.flock per il file (stream.py e i
  comandi CLI sono processi separati, pattern gia' in config_editor._locked()).
- Solo stdlib, nessuna dipendenza nuova. L'unico import del progetto e' .config
  per endpoint_key, imposta da SPEC-MAX-CONCURRENCY sez. 5. Import a livello di
  modulo e NON in un try/except: una chiave di fallback silenziosa qui
  riporterebbe esattamente il difetto che la spec vuole eliminare (endpoint con
  e senza slash finale in due record diversi, quindi un endpoint rotto che
  sfugge al cooldown). Se config.py e' rotto, e' meglio che il breaker lo dica.
- Un livello in cooldown non e' eleggibile e non ha slot (SPEC sez. "Breaker"):
  acquire() rifiuta in OPEN e in HALF_OPEN con sonda gia' in volo, quindi
  il dispatcher scarta da solo i livelli da scartare: non serve un ordinamento
  dedicato.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import FallbackLevel, endpoint_key

__all__ = [
    "CLOSED",
    "DEFAULT_COOLDOWN",
    "HALF_OPEN",
    "OPEN",
    "EndpointBreaker",
    "compute_state",
    "endpoint_id",
    "level_id",
]

# Fratello di stream_state.json: stato RUNTIME, non configurazione. Non va
# nella config utente perche' deve sopravvivere alla sessione (un endpoint
# rotto resta rotto anche al riavvio dell'interfaccia).
# Letto qui, non congelato a livello di modulo, perche' i test lo riassegnano
# a runtime (convenzione gia' usata in config_editor.py/output_history.py).
BREAKER_PATH = Path.home() / ".cache" / "bravoric-stt-clipboard" / "endpoint_breaker.json"

CLOSED = "CLOSED"
OPEN = "OPEN"
HALF_OPEN = "HALF_OPEN"

DEFAULT_COOLDOWN = 3600.0


def compute_state(last_failure: float | None, now: float, cooldown: float) -> str:
    """Stato del breaker per un endpoint. Funzione PURA: nessun orologio, nessun
    I/O, nessun accesso alla configurazione.

    last_failure None   -> CLOSED   (mai fallito, o resuscitato)
    age < 0             -> CLOSED   (timestamp nel futuro: orologio indietro o
                                       record scritto da un altro processo con
                                       un clock diverso -> scartato, non bloccare)
    age >= cooldown     -> HALF_OPEN (cooldown scaduto: si riprova, ma con una
                                       sola richiesta di prova)
    altrimenti          -> OPEN     (in cooldown: escluso dalle rotazioni)
    """
    if last_failure is None:
        return CLOSED
    age = now - last_failure
    if age < 0:
        return CLOSED
    if age >= cooldown:
        return HALF_OPEN
    return OPEN


def endpoint_id(endpoint: str, model: str) -> str:
    """Chiave stabile di un endpoint, delegando a config.endpoint_key().

    NON e' una seconda implementazione della regola: e' endpoint_key() con
    l'endpoint gia' normalizzato. La normalizzazione (rstrip("/"), come in
    api_client.py) e' applicata PRIMA e non dentro endpoint_key perche' e'
    idempotente: `http://h:4001/v1` e `http://h:4001/v1/` producono la stessa
    chiave qui, e produrranno la stessa chiave anche dopo che endpoint_key
    normalizzerà da solo, senza dover cambiare le chiavi gia' su disco.

    Il model NON viene normalizzato oltre lo strip: e' parte della chiave e non
    va inventato, se l'utente ha due model diversi devono restare due chiavi.
    """
    normalized = (endpoint or "").strip().rstrip("/")
    # FallbackLevel costruito per PAROLA CHIAVE: endpoint_key() accetta un
    # FallbackLevel, e i campi nuovi che Track A appende (parallel,
    # max_concurrency) hanno un default, quindi questa riga non si rompe quando
    # la lista dei campi cresce. Niente posizionali, per la regola "mai in
    # mezzo" che vale per i campi.
    return endpoint_key(
        FallbackLevel(
            name="",
            endpoint=normalized,
            model=(model or "").strip(),
            api_key_env="",
            api_key="",
            ca_cert="",
            timeout_seconds=0,
        )
    )


def level_id(level) -> str:
    """Chiave stabile di un livello di fallback, per duck typing.

    Accetta un FallbackLevel (quello che arriva da config.py), una tupla
    (endpoint, model) o un mapping con le stesse chiavi. In tutti i casi la
    regola resta quella di config.endpoint_key(): qui si accetta solo il
    modo di ottenere i due valori, non di combinarli.
    """
    if isinstance(level, Mapping):
        return endpoint_id(level.get("endpoint", ""), level.get("model", ""))
    if isinstance(level, Sequence) and not isinstance(level, (str, bytes)):
        endpoint, model = (list(level) + ["", ""])[:2]
        return endpoint_id(endpoint, model)
    return endpoint_id(getattr(level, "endpoint", ""), getattr(level, "model", ""))


@contextlib.contextmanager
def _locked():
    """flock cross-processo sul file di stato, come config_editor._locked().

    Serve per il read-modify-write: senza lock, due processi (stream.py e un
    comando CLI) che aprono e chiudono breaker in simultanea leggono lo stesso
    stato iniziale e l'ultima replace() vince, perdendo il fallimento
    dell'altro. Il path e' risolto qui dentro, non a livello di modulo, perche'
    i test lo riassegnano a runtime.
    """
    target = Path(BREAKER_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(target.with_suffix(".lock")), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@dataclass
class _Record:
    """Stato persistito di un endpoint. Solo dati: nessun orologio, nessun
    riferimento a path, cosi' il file puo' essere riaperto da un altro processo."""

    last_failure: float
    failures: int = 1

    def as_dict(self) -> dict:
        return {"last_failure": self.last_failure, "failures": self.failures}


def _record_from_dict(value) -> _Record | None:
    """Record malformati -> None (scartati), non eccezioni: vedi _read_file()."""
    if not isinstance(value, dict):
        return None
    last = value.get("last_failure")
    if not isinstance(last, (int, float)) or isinstance(last, bool):
        return None
    try:
        failures = int(value.get("failures", 1))
    except (TypeError, ValueError):
        failures = 1
    return _Record(last_failure=float(last), failures=max(1, failures))


def _read_file(target: Path) -> dict:
    """Lettura tollerante: file assente, JSON corrotto o record malformati
    valgono come stato vuoto. Un breaker non deve mai far cadere la
    trascrizione perche' il file di stato e' illeggibile."""
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    records = raw.get("records")
    if not isinstance(records, dict):
        return {}
    clean: dict[str, _Record] = {}
    for key, value in records.items():
        if not isinstance(key, str):
            continue
        record = _record_from_dict(value)
        if record is not None:
            clean[key] = record
    return clean


def _write_file(target: Path, records: dict) -> None:
    """Scrittura atomica: mkstemp + replace(), come config_editor._atomic_replace
    (un path .tmp fisso condiviso perderebbe scritture concorrenti)."""
    payload = {
        "version": 1,
        "records": {key: records[key].as_dict() for key in sorted(records)},
    }
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix="endpoint_breaker.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        Path(tmp_name).chmod(0o600)
        Path(tmp_name).replace(target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


class EndpointBreaker:
    """Stato di raffreddamento degli endpoint, con lease e sonda single-flight.

    path=None  -> usa BREAKER_PATH (risolto al momento dell'I/O, non all'import)
    cooldown   -> secondi di esclusione dopo un fallimento
    now        -> clock iniettato, default time.time (serve wall clock: lo stato
                  è persistito e deve valere anche dopo un riavvio)

    Uso tipico per ogni chunk:

        if not breaker.acquire(key):
            continue          # in cooldown, o sonda half-open gia' in volo
        try:
            ok = transcribe(level)
            breaker.record_success(key) if ok else breaker.record_failure(key)
        finally:
            breaker.release(key)

    record_failure() da solo non e' sufficiente a riservare la sonda: e'
    record_success()/record_failure() a decidere lo stato, acquire() a decidere
    chi puo' parlare con l'endpoint in questo momento.
    """

    def __init__(
        self,
        path: str | os.PathLike | None = None,
        cooldown: float = DEFAULT_COOLDOWN,
        now: Callable[[], float] = time.time,
    ):
        self._path = Path(path) if path is not None else None
        self.cooldown = float(cooldown)
        self._clock: Callable[[], float] = now if callable(now) else time.time
        # RLock e non Lock: record_failure() chiama save() mentre tiene gia' il
        # lock, e una riscrittura della stessa sezione deve poter riuscire.
        self._lock = threading.RLock()
        self._records: dict[str, _Record] = {}
        # Chiavi resuscitate, con l'istante della resurrezione. Servono perche'
        # save() REGRA: cancellare un record non si propaga da solo a un merge
        # (il merge tiene il last_failure piu' recente): senza tombstone, la
        # resurrezione resterebbe solo in memoria e il cooldown ripartirebbe da
        # capo al processo successivo. Il tombstone porta un orario perche' se
        # nel frattempo un altro processo ha registrato un fallimento PIU'
        # recente, quello vince e non va cancellato.
        self._tombstones: dict[str, float] = {}
        self._attempts: dict[str, int] = {}
        self._loaded = False
        # Contatore diagnostico: quante volte si e' toccato il disco. Deve
        # restare proporzionato alle transizioni, non ai chunk.
        self.writes = 0

    # ------------------------------------------------------------------ I/O

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else Path(BREAKER_PATH)

    def load(self) -> None:
        """Carica da disco e FUSIONA con lo stato in memoria.

        Merge, non sostituzione: un altro processo puo' aver registrato un
        fallimento su un endpoint che qui non e' mai fallito, e deve comunque
        valere. In caso di conflitto vince il last_failure piu' recente.
        Attenzione: un merge non cancella nulla, quindi i record resuscitati
        vengono tolti con tombstone espliciti (vedi save())."""
        with self._lock:
            self._merge_into_memory(_read_file(self.path))
            self._loaded = True

    def save(self) -> None:
        """Scrive su disco SOLO se lo stato e' cambiato (transizione).

        Sotto flock rilegge il file e riparte da li': salvare uno snapshot
        cieco di cio' che si ha in memoria cancellerebbe i record scritti da un
        altro processo nel frattempo."""
        with self._lock:
            self._ensure_loaded()
            if not self._records and not self._tombstones:
                return
            with _locked():
                on_disk = _read_file(self.path)
                merged: dict[str, _Record] = dict(on_disk)
                for key, resurrected_at in self._tombstones.items():
                    if key in self._records:
                        continue
                    stale = merged.get(key)
                    if stale is None or stale.last_failure <= resurrected_at:
                        merged.pop(key, None)
                for key, record in self._records.items():
                    previous = merged.get(key)
                    if previous is None or record.last_failure > previous.last_failure:
                        merged[key] = record
                if merged == on_disk:
                    self._tombstones = {}
                    return
                _write_file(self.path, merged)
                self.writes += 1
            self._records = dict(merged)
            self._tombstones = {}

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _merge_into_memory(self, records: dict) -> None:
        for key, record in records.items():
            # Record con age < 0 (orologio andato indietro, o clock diverso da
            # un altro processo) vengono scartati: trattarli come falliti
            # bloccherebbe l'endpoint indefinitamente. compute_state() li
            # classifica gia' CLOSED, qui si evita anche di riscriverli.
            if compute_state(record.last_failure, self._clock(), self.cooldown) == CLOSED:
                continue
            previous = self._records.get(key)
            if previous is None or record.last_failure > previous.last_failure:
                self._records[key] = record

    # ---------------------------------------------------------------- stato

    def _now(self, now: float | None = None) -> float:
        return float(self._clock()) if now is None else float(now)

    def state(self, key: str, now: float | None = None) -> str:
        """CLOSED / OPEN / HALF_OPEN per key. now=None usa il clock iniettato."""
        with self._lock:
            self._ensure_loaded()
            record = self._records.get(key)
            last_failure = record.last_failure if record else None
            return compute_state(last_failure, self._now(now), self.cooldown)

    def remaining(self, key: str, now: float | None = None) -> float:
        """Secondi che mancano alla fine del cooldown (0 se non e' in cooldown).
        Esposto per il caller che vuole sapere quanto manca alla riprova."""
        with self._lock:
            self._ensure_loaded()
            record = self._records.get(key)
            if record is None:
                return 0.0
            left = self.cooldown - (self._now(now) - record.last_failure)
            return left if left > 0 else 0.0

    def record_failure(self, key: str) -> None:
        """Registra un fallimento: apre (o estende) il cooldown. E' la
        transizione CLOSED->OPEN: unica scrittura su disco per questo evento."""
        with self._lock:
            self._ensure_loaded()
            previous = self._records.get(key)
            self._records[key] = _Record(
                last_failure=self._now(),
                failures=(previous.failures if previous else 0) + 1,
            )
            self.save()

    def record_success(self, key: str) -> None:
        """Resurrezione: azzera il timer e cancella il record. Se l'endpoint non
        era in cooldown non c'e' nessuna transizione, quindi nessuna scrittura
        (altrimenti ogni chunk riuscito toccherebbe il disco)."""
        with self._lock:
            self._ensure_loaded()
            if key not in self._records:
                return
            del self._records[key]
            self._tombstones[key] = self._now()
            self.save()

    def attempts(self, key: str) -> int:
        """Tentativi in corso su key (lease attivi). In-memoria per definizione:
        un tentativo non deve mai sopravvivere al processo che lo ha iniziato."""
        with self._lock:
            return self._attempts.get(key, 0)

    def acquire(self, key: str, now: float | None = None) -> bool:
        """Prende il lease su key. False = non si puo' procedere.

        OPEN      -> sempre False, l'endpoint e' in cooldown
        HALF_OPEN -> True solo se non c'e' gia' una sonda in volo (single-flight:
                     una sola richiesta di prova per cooldown)
        CLOSED    -> True (quanti tentativi vuole, e' il dispatcher a mettere il
                     cap con la semantica AUTO clamp(3*N, 1, 8))"""
        with self._lock:
            self._ensure_loaded()
            state = self.state(key, now=now)
            if state == OPEN:
                return False
            in_flight = self._attempts.get(key, 0)
            if state == HALF_OPEN and in_flight > 0:
                return False
            self._attempts[key] = in_flight + 1
            return True

    def release(self, key: str) -> int:
        """Rilascia il lease. Idempotente: non va mai sotto zero, altrimenti un
        doppio release (eccezione nel finally + chiamata esplicita) azzererebbe il
        conteggio e aprirebbe la porta a una seconda sonda half-open."""
        with self._lock:
            in_flight = self._attempts.get(key, 0)
            if in_flight <= 0:
                return 0
            in_flight -= 1
            if in_flight:
                self._attempts[key] = in_flight
            else:
                del self._attempts[key]
            return in_flight

    # ------------------------------------------------------------ stato
