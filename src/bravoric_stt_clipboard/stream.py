"""Sessione di dettatura "streaming" con incolla diretta (feature sperimentale).

Due modalità configurabili in [stream].mode:

- "at_end": registrazione unica continua; alla seconda pressione trascrive
  e incolla il testo finale. Nessun chunk, nessun cleanup LLM (D1).
- "per_chunk": registrazione continua segmentata sui silenzi (un solo
  processo ffmpeg che emette PCM grezzo su stdout, con il VAD calcolato
  qui sui frame); ogni utterance viene
  trascritta appena pronta e incollata nel campo in focus.

La sessione è avviata/terminata da un toggle CLI (bin/stream-toggle).
Per "per_chunk" un supervisore staccato possiede ffmpeg, legge il suo
stderr, assembla le utterance e aggiorna stream_state.json.
"""
from __future__ import annotations

import collections
import concurrent.futures
import contextlib
import dataclasses
import itertools
import json
import logging
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, BinaryIO

import requests
import requests.adapters

from . import audio, chunk_log, clipboard, notify, output_history, status
from .api_client import transcribe_audio
from .atomic_io import atomic_write_json
from .config import (
    AudioConfig,
    Config,
    ConfigError,
    _command_norm,
    load_config,
    parse_blacklist,
)
from .fallback import AllLevelsFailedError, try_with_fallback
from .i18n import _

logger = logging.getLogger(__name__)

MODE_AT_END = "at_end"
MODE_PER_CHUNK = "per_chunk"

# P4 (watchdog): l'estensione confronta l'eta' di status.json con
# STATE_TIMEOUT_SECONDS.recording (15 min) e, se e' superata, forza lo stato
# 'idle'. In per_chunk il timestamp veniva scritto UNA volta sola, all'avvio
# della sessione (stream.py, write_status RECORDING in _start_per_chunk):
# quindi l'eta' era quella dell'avvio per costruzione e una dettatura
# continua piu' lunga di 15 minuti veniva data per morta, con l'icona che
# tornava a idle e la notifica "Recording timed out"Mentro il supervisore
# era VIVO e teneva ancora il microfono. Il battito rinfresca il timestamp
# mentre la sessione gira, cosi' l'eta' misura l'attivita'.
# 60 s: molto piu' corto del limite, cosi' nemmeno una finestra di 15
# minuti puo' essere attraversata senza un battito.
STREAM_HEARTBEAT_SECONDS = 60.0

STREAM_LOCK_PATH = audio._runtime_dir() / "stream.lock"
STREAM_STATE_PATH = Path.home() / ".cache" / "bravoric-stt-clipboard" / "stream_state.json"
# Testo VIVO del campo, scritto dall'estensione GNOME. Contiene solo cio' che
# l'utente ha davvero nel campo: i chunk cancellati non ci sono piu' e le
# parole comando non ci sono mai state (sono state eseguite, non incollate).
# Il backend lo usa al posto della propria ricostruzione da last_chunks.
STREAM_LIVE_TEXT_PATH = Path.home() / ".cache" / "bravoric-stt-clipboard" / "stream_live_text.json"

# ---------------------------------------------------------------- VAD (Voice Activity Detection)
# RMS-based VAD: ffmpeg emette PCM grezzo su stdout (s16le), noi calcoliamo RMS
# frame-by-frame. La segmentazione sui silenzi e' qui, non in ffmpeg: nessun
# filtro silencedetect/segment e nessuna regex sui messaggi di silenzio.
# La soglia non e' una costante fissa ma adattiva (noise floor + margine,
# vedi _adaptive_threshold_db piu' in basso, clampata fra
# VAD_THRESHOLD_MIN_DB e VAD_THRESHOLD_MAX_DB).

# Sentinella per RMS nullo (evita float("-inf") nel hot path): un frame
# digitalmente muto non e' un campione valido per stimare il noise floor.
_MIN_DB = -200.0

# Finestra/stima del noise floor adattivo (usate dal supervisore per_chunk).
FLOOR_WINDOW_FRAMES = 100  # ~3s di storia per stimare il floor
MIN_FLOOR_FRAMES = 20      # ~0.6s prima di fidarsi della stima

# Chunk noti da filtrare (allucinazioni comuni)
KNOWN_HALLUCINATIONS = frozenset({
    "Sottotitoli a cura di",
    "Sottotitoli a cura di Whisper",
})

class _Stop:
    """Sentinella di arresto per _result_queue: tipo dedicato (non `object`
    generico) cosi' `isinstance(item, _Stop)` restringe `item` a `_ChunkResult`
    nel ramo else (un `is` semplice non basta a mypy per il narrowing)."""


_STOP = _Stop()

@dataclasses.dataclass(frozen=True)
class _ChunkResult:
    seq_id: int
    text: str
    success: bool
    error: str | None = None
    # Provenienza e tempi del chunk, una voce per livello TENTATO, in ordine
    # di tentativo. APPESI IN CODA con default: i test costruiscono
    # _ChunkResult(0, "Primo", True) per posizione e non devono cambiare.
    # `attempts` e' la sola fonte del campo `attempts` del log JSONL, e
    # `served_by`/`fallback` nel log sono DERIVATI da qui, non passati a
    # mano: altrimenti potrebbero contraddire i tentativi.
    attempts: tuple = ()
    # Durata dell'audio in secondi, calcolata dai byte PCM (non inventata),
    # e durata totale del chunk. Servono alla riga di log.
    audio_s: float = 0.0
    total_ms: float = 0.0


_tls = threading.local()

def _thread_session() -> requests.Session:
    session = getattr(_tls, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=1)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _tls.session = session
    return session


def _parallel_slots(level) -> int:
    """Capienza dichiarata di un livello parallelo, con guardia stretta.

    Un valore non intero o fuori 1..8 non puo' diventare una capienza di
    semaforo: 0 bloccherebbe per sempre, un bool o un intero enorme
    (max_concurrency = true) aprirebbero la porta a chiunque. In quel caso si
    torna al default 3, stessa semantica di `_coerce_max_concurrency` in
    config.py.
    """
    value = getattr(level, "max_concurrency", 3)
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(str(value).strip())
        except (TypeError, ValueError):
            return 3
    return max(1, min(8, value))


class _NullBreaker:
    """Breaker disattivato, stessa interfaccia di EndpointBreaker.

    Serve quando nessun livello e' parallelo (o il cooldown e' 0): il percorso
    sequenziale deve restare IDENTICO a oggi, quindi nessuna esclusione per
    cooldown e nessuna scrittura su disco. Non e' una scorciatoia: e' il
    motivo per cui una config senza `parallel` non cambia comportamento.
    """

    def state(self, key: str, now: float | None = None) -> str:
        return "CLOSED"

    def acquire(self, key: str, now: float | None = None) -> bool:
        return True

    def release(self, key: str) -> int:
        return 0

    def attempts(self, key: str) -> int:
        return 0

    def remaining(self, key: str, now: float | None = None) -> float:
        return 0.0

    def record_success(self, key: str) -> None:
        return None

    def record_failure(self, key: str) -> None:
        return None


def _resolve_dispatch(stream) -> str:
    """UNICO punto che traduce la config in comportamento: "parallel" o
    "sequential". Ritorna SEMPRE una delle due stringhe.

    Semantica (vincolante, vedi [stream].dispatch_mode):

      sequential + qualunque  -> "sequential", IGNORA tutti i flag per-livello
      auto       + >=1 checked-> "parallel"   (comportamento di oggi)
      auto       + zero      -> "sequential" (degrada, NON e' un errore)

    Il "degrada" e' una PROPRIETA' della semantica, non un fallback sparso: e'
    la stessa condizione che da' `dispatcher = None`. Per questo questa e'
    l'unica funzione che legge `dispatch_mode`: il punto di costruzione del
    dispatcher, il ramo di _worker e la scelta del breaker devono TUTTI passare
    di qui, altrimenti la decisione si duplica e i tre punti possono divergere
    (in particolare `dispatcher.active` in _worker direbbe "parallel" solo se il
    dispatcher ha davvero dei livelli, mentre in "sequential" un dispatcher
    vuoto riprodurrebbe silenziosamente il ramo sequenziale invece di batterlo).
    """
    if getattr(stream, "dispatch_mode", "auto") == "sequential":
        return "sequential"
    if any(getattr(level, "parallel", False) for level in stream.fallback):
        return "parallel"
    return "sequential"


def _build_breaker(stream, dispatch: str):
    """Breaker del supervisore, o _NullBreaker se il cooldown non vale.

    Riceve la DECISIONE (`_resolve_dispatch`) invece di ricalcolare
    `any(parallel)`: in "sequential" il percorso non chiama mai il breaker
    (tutte le chiamate breaker sono dentro _Dispatcher), quindi in quel caso si
    passa _NullBreaker e il breaker REALE non viene ne' costruito ne' messo su
    disco. In "auto" col cooldown disattivato (endpoint_cooldown_seconds = 0)
    resta disattivato per scelta dell'utente: nessuna esclusione, nessun file.
    """
    if dispatch == "sequential":
        return _NullBreaker()
    try:
        cooldown = float(getattr(stream, "endpoint_cooldown_seconds", 3600.0))
    except (TypeError, ValueError):
        cooldown = 3600.0
    if not math.isfinite(cooldown) or cooldown <= 0:
        return _NullBreaker()
    from .endpoint_breaker import EndpointBreaker
    return EndpointBreaker(cooldown=cooldown)


def _auto_worker_count(levels, parallel_levels, breaker) -> int:
    """Cap dei worker in AUTO: la formula di sempre, con un pavimento per le retrovie.

    Il nucleo e' INVARIATO rispetto a prima: clamp(somma degli slot dei livelli
    paralleli e NON in cooldown, 1, 8). Nel caso sano (almeno un checked
    aperto) il risultato e' esattamente lo stesso di oggi, quindi il cap non
    cambia per l'utente (contratto A) e 3 x N_parallel coi default resta
    valido.

    L'unica differenza e' il pavimento del contratto E: quando NON c'e' nessun
    slot nel pool (tutti i checked in cooldown) il clamp darebbe 1, e con un
    solo worker l'unico chunk in circolo occuperebbe il worker mentre aspetta un
    endpoint che non puo' rispondere: il backpressure non lo fa nemmeno entrare
    in coda e la retrovia sequenziale, che non passa dal semaforo, diventa
    l'unica via e l'unico posto dove il worker puo' lavorare. In quel caso il
    cap e' il numero di endpoint UTILIZZABILI (quelli che il breaker non ha in
    OPEN, di qualunque tipo: e' su quelli che la catena puo' ottenere una
    risposta), cosi' piu' chunk possono servire la catena insieme. Se non
    c'e' nemmeno un endpoint utilizzabile resta 1: il minimo serve a non
    azzerare l'executor, e la catena funziona lo stesso perche' non passa dal
    semaforo.
    """
    capacity = 0
    for level in parallel_levels:
        if breaker.state(_level_key(level)) != "OPEN":
            capacity += _parallel_slots(level)
    if capacity == 0:
        capacity = sum(
            1 for level in levels
            if breaker.state(_level_key(level)) != "OPEN"
        )
    return max(1, min(8, capacity))


class _Dispatcher:
    """Sceglie il livello parallelo per ogni chunk e ne tiene il lease.

    Un semaforo PER LIVELLO (capacity `max_concurrency`), non un semaforo
    globale: whisper.cpp serializza le richieste dietro un mutex interno, quindi
    un endpoint con un solo slot non deve essere svuotato da tre worker.

    Selezione (SPEC-MAX-CONCURRENCY sez. "Dispatcher"): fra i livelli paralleli,
    non in cooldown e con slot liberi, il least-busy; a parita' il
    longest-waiting (chi aspetta da piu') e come ultimo criterio l'ordine dei
    fallback del config, cosi' il livello 1 resta il primo. Se sono TUTTI in
    cooldown si usa l'ordine per scadenza imminente invece di aspettare inerti.

    Il carico e' un contatore esplicito per chiave, non il valore interno del
    semaforo: non si tocca `_value` (privato) e non si fa una sonda
    acquire/release che lascerebbe una finestra in cui due worker vedono lo
    stesso carico e scelgono lo stesso endpoint.

    Ogni lease e' rilasciato in `finally`, sempre: uno slot perso e' un
    endpoint che si satura per sempre e sembra un bug di concorrenza.
    """

    #: attesa fra due tentativi di lease, breve ma non nullo: senza sleep un
    #: dispatcher saturo farebbe busy-waiting e burns CPU con ffmpeg aperto.
    POLL_INTERVAL = 0.05

    def __init__(self, levels, breaker, fallback_chain=None):
        self._breaker = breaker
        # Catena SEQUENZIALE di riserva, sull'INTERA lista dei livelli
        # (contatto per costruzione, vedi _run_supervisor) in ordine di config.
        # Non e' un pool piu' largo: i livelli non-checked restano FUORI da
        # _order/_capacity, quindi non prendono mai un lease e non possono
        # aprirsi un canale concorrente (contratto C). Qui ci finiscono solo
        # quando il pool non ha un endpoint utilizzabile.
        self._fallback_chain = tuple(fallback_chain if fallback_chain is not None
                                     else levels)
        # Una sola istanza per chiave: due livelli con lo stesso endpoint e lo
        # stesso modello condividono gli stessi slot, altrimenti la capienza
        # dichiarata sarebbe applicata due volte.
        self._capacity: dict[str, int] = {}
        self._free: dict[str, int] = {}
        self._order: list[str] = []
        # Istante in cui un endpoint e' diventato LIBERO l'ultima volta: e' il
        # clock del longest-waiting. Va aggiornato quando lo slot torna
        # indietro, non letto pigro al primo sguardo, altrimenti un endpoint
        # libero da sempre sembrerebbe "appena liberato" e perderebbe la
        # prioritita' proprio quando e' il candidato piu' adatto.
        self._free_since: dict[str, float] = {}
        self._lock = threading.Lock()
        for level in levels:
            if not getattr(level, "parallel", False):
                continue
            key = _level_key(level)
            if key in self._capacity:
                continue
            capacity = _parallel_slots(level)
            self._capacity[key] = capacity
            self._free[key] = capacity
            self._free_since[key] = time.time()
            self._order.append(key)

    @property
    def active(self) -> bool:
        """Nessun livello parallelo => percorso sequenziale identico a oggi."""
        return bool(self._order)

    @property
    def fallback_chain(self) -> tuple:
        """Catena sequenziale di riserva: TUTTI i livelli, ordine di config."""
        return self._fallback_chain

    def has_pending_capacity(self, now: float | None = None) -> bool:
        """C'e' nel pool un endpoint che il chunk PUO' aspettare senza attesa?

        "Capacita' pendente" = slot liberi E lease ottenibile dal breaker. E'
        la domanda che autorizza a RESTARE sul pool: se l'endpoint non ha slot
        ora ma ne avra' fra poco (coda del pool, lease che torna) l'attesa e'
        breve e giusta, e aspettare e' il comportamento del pool; se invece e'
        tutto in cooldown l'attesa puo' valere un'ora, e allora il chunk va
        ripiegato sulla catena sequenziale (contratto B).

        Rispone sul mondo REALE (stato del breaker e slot liberi), non sulla
        sola readiness logica del pool: un endpoint in cooldown non e'
        utilizzabile e non deve far aspettare il chunk.

        Nota: con un pool VUOTO la risposta e' False, quindi il chiamante passa
        alla catena. Il supervisor costruisce il dispatcher solo se esiste
        almeno un livello checked, quindi quel caso non cambia comportamento.
        """
        with self._lock:
            now = time.time() if now is None else now
            if not self._order:
                return False
            return any(
                self._free.get(key, 0) > 0 and self._breaker.state(key, now) != "OPEN"
                for key in self._order
            )

    @property
    def level_count(self) -> int:
        """Quanti endpoint distinti sono nel pool parallelo."""
        return len(self._order)

    def _candidates(self, now: float) -> list[str]:
        """Chiavi utilizzabili, con l'ordine di priorita' gia' calcolato.

        Restituisce una lista di CHIAVI (non di tuple): le tuple sono solo
        intermediate per il sort. L'ordinamento minimo e' least-busy, poi
        longest-waiting, poi l'ordine dei fallback del config.
        """
        usable = []
        for position, key in enumerate(self._order):
            if self._free[key] <= 0:
                continue
            if self._breaker.state(key) == "OPEN":
                # Endpoint in cooldown: non e' un candidato, punto. Il
                # fallback sequenziale (non questo elenco) e' il modo in cui
                # un chunk raggiunge un endpoint in cooldown senza aspettare.
                continue
            load = self._capacity[key] - self._free[key]
            # LONGEST-WAITING: l'endpoint libero da piu' tempo vince, quindi si
            # ordina per _free_since CRESCENTE (il piu' vecchio primo). Il
            # segno meno qui sbaglierebbe la regola: sceglierebbe quello
            # liberato piu' di recente, cioe' il meno affamato.
            usable.append((load, self._free_since.get(key, now), position, key))
        if usable:
            usable.sort()
            return [entry[3] for entry in usable]
        # Tutti in cooldown: si restituisce il vuoto, NON l'endpoint che esce
        # prima dal cooldown. Prima qui si ordinava per scadenza e si restituiva
        # comunque qualcosa: acquire() allora riprovava la stessa chiave in
        # loop, _take_slot/_give_slot tenevano la CPU accesa e il chunk
        # aspettava inerti un endpoint che non poteva rispondere. Un endpoint
        # in cooldown e' per definizione NON utilizzabile (contratto B): se il
        # pool non ha nulla di utilizzabile tocca al chiamante passare il
        # chunk alla catena sequenziale, che e' l'unica attesa ammissibile
        # (l'attesa la paga la richiesta HTTP di un livello, non il dispatcher).
        return []

    def candidates_excluding(self, levels, tried: set) -> list:
        """Candidati in ordine di priorita', saltando quelli gia' tentati.

        Serve al retry: il secondo giro prova solo i livelli non ancora
        toccati, quindi nessun livello viene tentato due volte per chunk e non
        si puo' entrare in ping-pong fra due endpoint che si rifiutano a
        vicenda.
        """
        by_key = {_level_key(level): level for level in levels}
        with self._lock:
            order = self._candidates(time.time())
        return [by_key[key] for key in order if key in by_key and key not in tried]

    def report_success(self, key: str) -> None:
        """Risposta OK: azzera il timer del cooldown (transizione)."""
        with contextlib.suppress(Exception):
            self._breaker.record_success(key)

    def report_failure(self, key: str) -> None:
        """Errore di rete/timeout: apre il cooldown per quell'endpoint."""
        with contextlib.suppress(Exception):
            self._breaker.record_failure(key)

    def _take_slot(self, key: str) -> bool:
        with self._lock:
            if self._free.get(key, 0) <= 0:
                return False
            self._free[key] -= 1
            return True

    def _give_slot(self, key: str) -> None:
        with self._lock:
            self._free[key] = min(self._capacity[key], self._free.get(key, 0) + 1)
        # Da questo istante l'endpoint e' di nuovo libero: riparte il clock del
        # longest-waiting.
        self._free_since[key] = time.time()

    def acquire(self, levels, stop_check=None, stop_timeout: float = 30.0,
                prefer: str | None = None):
        """Prende slot + lease sul livello scelto, aspettando se serve.

        Restituisce (level, key) in lease, oppure (None, None) se si ferma.
        Lo slot si prende PRIMA del lease e sempre non-bloccante: si attende
        tenendo solo lo slot, mai un lease che non si puo' usare, altrimenti un
        worker bloccato in HALF_OPEN saturerebbe il breaker senza fare nulla.

        `prefer` fissa il livello da prendere (retry): viene provato per primo,
        ma se nel frattempo non e' piu' eleggible si accetta comunque un altro
        endpoint libero, cosi' il worker non si blocca su una chiave sparita.

        Il check di stop e' dentro OGNI loop di attesa: con i semafori per
        endpoint i modi di appendersi aumentano e il backpressure globale,
        da solo, non basta piu'.
        """
        by_key = {_level_key(level): level for level in levels}
        deadline = time.time() + stop_timeout
        while True:
            now = time.time()
            with self._lock:
                candidates = self._candidates(now)
            if prefer is not None and prefer in self._capacity:
                candidates = [prefer] + [k for k in candidates if k != prefer]
            for key in candidates:
                # lo slot e' la risorsa scarsa, il lease solo il permesso:
                # si prende lo slot e poi si chiede il permesso.
                if not self._take_slot(key):
                    continue
                if self._breaker.acquire(key):
                    level = by_key.get(key)
                    if level is not None:
                        return level, key
                    # livello sparito dalla config nel frattempo: si rilascia
                    self._give_slot(key)
                    self._breaker.release(key)
                    continue
                # endpoint in cooldown o sonda half-open gia' in volo: si
                # restituisce lo slot, altrimenti l'attesa lo consumerebbe.
                self._give_slot(key)
            if stop_check is not None and stop_check() and time.time() > deadline:
                return None, None
            time.sleep(self.POLL_INTERVAL)

    def release(self, key: str) -> None:
        """Rilascio idempotente: prima il lease, poi lo slot.

        Lo slot torna sempre indietro, anche se il lease non era stato preso:
        perdere uno slot significa un endpoint saturo per sempre.
        """
        with contextlib.suppress(Exception):
            self._breaker.release(key)
        if key in self._capacity:
            self._give_slot(key)

    def try_acquire_now(self, levels):
        """Come acquire() ma senza aspettare: None se non c'e' subito un slot.

        Percorso di prova NON bloccante sul dispatcher. NOTA: oggi e' chiamato
        solo dai test, non dal supervisore — non esiste un backpressure del
        supervisor che lo invochi, e non costruirci sopra: se un giorno il
        supervisore lo userà, dovrà essere lui a decidere cosa fare con
        (None, None).
        """
        by_key = {_level_key(level): level for level in levels}
        with self._lock:
            candidates = self._candidates(time.time())
        for key in candidates:
            if not self._take_slot(key):
                continue
            if self._breaker.acquire(key):
                level = by_key.get(key)
                if level is not None:
                    return level, key
                self._give_slot(key)
                self._breaker.release(key)
                continue
            self._give_slot(key)
        return None, None


def _level_key(level) -> str:
    """Chiave dell'endpoint: config.endpoint_key e' la fonte unica (contratto
    sez. 1 e 2b). level_id() delega gia' a endpoint_key(), quindi non si
    duplica qui la regola della normalizzazione."""
    from .endpoint_breaker import level_id
    return level_id(level)


def _transcribe(level, wav_path, stream, prompt) -> str:
    """Chiamata HTTP per un livello: identica in sequenziale e in parallelo.

    Il timeout non si passa: transcribe_audio usa il solo
    ``level.timeout_seconds`` (l'unica fonte autoritativa), quindi il valore
    ``stream.chunk_timeout_seconds`` non puo' piu' sovrascriverlo."""
    return transcribe_audio(
        level, wav_path, language=stream.language or None, prompt=prompt,
        hotwords=stream.hotwords or None, session=_thread_session(),
        personal_prompt=stream.prompt,
    )


# --- tracciamento dei tentativi per il log JSONL dei chunk -------------------
# Il raccoglitore e' per THREAD (threading.local), non globale: due worker
# concurrenti su chunk diversi non devono mescolare i propri tentativi. Senza
# raccoglitore attivo `_transcribe_traced` e' un puro pass-through a
# `_transcribe`: il percorso di oggi resta identico quando nessuno ascolta, e i
# test che sostituiscono `stream._transcribe` continuano a intercettare tutto.
_attempts_tls = threading.local()


def _transcribe_traced(level, wav_path, stream, prompt) -> str:
    """`_transcribe` che registra il tentativo (livello, ms, ok/err) in corsa.

    L'eccezione si PROPAGA dopo aver registrato il fallimento: qui non si
    cambia la semantica, si aggiunge solo la prova. Registrare l'errore e'
    essenziale, altrimenti il log mostrerebbe solo i endpoint che hanno
    risposto e la metrica che serve per scegliere (quanti tentativi falliti)
    resterebbe sempre zero.
    """
    collector = getattr(_attempts_tls, "attempts", None)
    if collector is None:
        return _transcribe(level, wav_path, stream, prompt)
    started = time.monotonic()
    try:
        text = _transcribe(level, wav_path, stream, prompt)
    except Exception as exc:
        collector.append(chunk_log.make_attempt(
            level, (time.monotonic() - started) * 1000.0, False, exc))
        raise
    collector.append(chunk_log.make_attempt(
        level, (time.monotonic() - started) * 1000.0, True, None))
    return text


def _wav_seconds(wav_path) -> float:
    """Durata in secondi letta dall'header WAV (16 bit mono).

    Deriva dai campioni effettivamente scritti, non e' un valore passato a
    mano: se il file non si apre (gia' cancellato, WAV rotto) torna 0.0 e
    basta — il campo del log diventa 0, non inventato.
    """
    with contextlib.suppress(Exception), wave.open(str(wav_path), "rb") as handle:
        rate = handle.getframerate()
        if rate > 0:
            return handle.getnframes() / float(rate)
    return 0.0


def _push_attempts() -> tuple[Any, list[dict]]:
    """Arma un raccoglitore di tentativi per il THREAD corrente.

    thread-local perche' due worker su chunk diversi non devono mescolare i
    propri tentativi: un log con `attempts` presi a caso da due chunk
    direbbe che un endpoint ha risposto a una domanda che non gli e' stata
    fatta. Ritorna il valore PRECEDENTE, da passare a `_pop_attempts`: senza
    quello un worker lascerebbe il raccoglitore armato per il chunk seguente,
    e i tentativi dei due chunk finirebbero nello stesso log.
    """
    previous = getattr(_attempts_tls, "attempts", None)
    collector: list[dict] = []
    _attempts_tls.attempts = collector
    return previous, collector


def _pop_attempts(previous) -> None:
    """Ripristina il raccoglitore precedente (None = nessuno in ascolto)."""
    _attempts_tls.attempts = previous


def _levels_untried(levels, tried: set[str]) -> list:
    """La lista dei livelli che il pool NON ha ancora tentato, ordine di config.

    Il ripiego del pool passa i livelli gia' tentati cosi' la catena parte da
    li': ogni livello viene tentato UNA volta sola per chunk. Senza questo, un
    endpoint rotto veniva ripagato due volte (una dal pool, una dalla catena):
    sulla config personale, un whisper-gpu in timeout (timeout_seconds = 8)
    costava 8 secondi due volte prima di arrivare all'endpoint buono, ed e'
    quello che fa sembrare la dettazione "bloccata" quando un endpoint cade.

    `tried` vuoto = percorso sequenziale puro: la lista resta quella di
    config, cioe' il comportamento legacy e' identico.
    """
    if not tried:
        return list(levels)
    return [level for level in levels if _level_key(level) not in tried]


class _EndpointGate:
    """Capienza per CHIAVE endpoint, valida anche FUORI dal pool parallelo.

    Il semaforo per endpoint esisteva solo dentro `_Dispatcher`, e il
    dispatcher viene costruito solo in `dispatch == "parallel"`: in
    sequenziale `max_concurrency` non vincolava NESSUNA richiesta. Misurato
    (TEMA2 V1): con `dispatch_mode = "sequential"` e
    `max_concurrent_chunks = 6` esplicito, 6 worker mandavano 6 richieste
    CONTEMPORANEE allo stesso endpoint contro un `max_concurrency = 1`
    dichiarato. Lo stesso accadeva con `dispatch_mode = "auto"` e zero livelli
    `parallel = true`, che degrada a sequenziale.

    Qui il gate e' costruito su TUTTI i livelli, senza il flag `parallel` e
    senza il breaker: la catena sequenziale tocca anche i livelli non-checked
    (contratto C) e i checked in cooldown, e sono esattamente quelli che devono
    poter essere limitati senza essere esclusi. La CHIAVE resta `_level_key`,
    che delega a `config.endpoint_key`: qui non si duplica la semantica
    "endpoint + modello", altrimenti lo stesso backend aprirebbe due canali
    diversi.

    **Il gate LIMITA, non autorizza.** Una capienza non crea concorrenza: se il
    tetto globale resta 1 worker, resta 1 richiesta in volo. Il gate puo' solo
    fare meno di prima, mai di piu'.
    """

    def __init__(self, levels):
        self._slots: dict[str, threading.BoundedSemaphore] = {}
        for level in levels:
            key = _level_key(level)
            # Una sola istanza per chiave: due livelli con lo stesso endpoint
            # e lo stesso modello condividono la capienza dichiarata, che altrimenti
            # sarebbe applicata due volte.
            if key in self._slots:
                continue
            self._slots[key] = threading.BoundedSemaphore(_parallel_slots(level))

    def __len__(self) -> int:
        return len(self._slots)

    def _slot(self, level) -> threading.BoundedSemaphore | None:
        return self._slots.get(_level_key(level))

    def call(self, level, run):
        """Esegue `run()` tenendo la capienza del livello. Rilascio SEMPRE.

        Il rilascio sta nel `finally`: uno slot perso e' un endpoint che si
        satura per sempre e la catena si blocca sul primo livello. Senza gate
        (`None`) il tentativo passa diretto, che e' il comportamento di sempre
        per i chiamanti che non ce l'hanno.
        """
        slot = self._slot(level)
        if slot is None:
            return run()
        slot.acquire()
        try:
            return run()
        finally:
            slot.release()


def _sequential_chain(levels, wav_path, stream, prompt, endpoint_gate=None) -> str:
    """Catena SEQUENZIALE sull'INTERA lista dei livelli, ordine di config.

    Un unico punto per i due modi in cui si arriva qui: la scelta "sequential"
    (dispatch_mode esplicito o zero checked) e il RIPIEGO del pool parallelo
    (contratto B). Non puo' quindi essere che i due percorsi escano dal
    fallback: il ritorno al legacy e' letteralmente lo stesso codice di prima.

    La catena non prende lease e non tocca il breaker: i livelli non-checked
    restano endpoint SEQUENZIALI (contratto C) e i checked in cooldown sono
    proprio quelli che devono restare raggiungibili qui. Nessun doppio conteggio
    di fallimenti (contratto F): il breaker registra solo i tentativi del pool.

    `endpoint_gate` e' OPZIONALE con default None, e non per comodita': i
    test sostituiscono `_sequential_chain` con doppioni a firma fissa e
    chiamano `_worker` senza l'argomento nuovo. Un default obbligatorio li
    manderebbe in rosso. Con None la catena si comporta esattamente come prima.

    Solleva AllLevelsFailedError se ogni livello fallisce.
    """
    if endpoint_gate is None:
        return try_with_fallback(
            levels, lambda level: _transcribe_traced(level, wav_path, stream, prompt),
        )
    return try_with_fallback(
        levels,
        lambda level: endpoint_gate.call(
            level, lambda: _transcribe_traced(level, wav_path, stream, prompt),
        ),
    )


def _worker(seq, wav_path, prompt, *, stream, sem, result_queue,
            dispatcher=None, stop_check=None, stop_timeout=30.0,
            endpoint_gate=None):
    text, success, error = "", False, None
    leased_key = None
    pool_errors: list[str] = []
    # La catena riceve il gate SOLO se esiste, e la chiama con la firma di
    # sempre quando non esiste. Motivo, non comodita': i test sostituiscono
    # `_sequential_chain` con doppioni a firma fissa
    # (levels, wav_path, stream, prompt) e chiamano `_worker` senza questo
    # argomento. Passare `endpoint_gate=None` come parola chiave romperebbe
    # quei doppioni con TypeError, quindi il default None della firma
    # servirebbe a niente. Qui si chiama la catena VERA col gate e i
    # doppioni restano quelli di prima: il gate si spegne quando non c'e'.
    _chain: Callable[[Any, Any, Any, Any], str]
    if endpoint_gate is None:
        _chain = _sequential_chain
    else:
        def _chain(levels, wav_path, stream, prompt):
            return _sequential_chain(
                levels, wav_path, stream, prompt, endpoint_gate=endpoint_gate,
            )
    # Raccoglitore di tentativi per il log JSONL. Attivato PRIMA di qualunque
    # richiesta e smontato nel finally: senza questo, i test che sostituiscono
    # `_sequential_chain` continuano a essere intercettati (il doppione chiama la
    # catena vera, che chiama `_transcribe_traced`, che chiama `_transcribe`).
    _previous_attempts, attempts = _push_attempts()
    chunk_started = time.monotonic()
    # Fuori dal ramo parallelo: la catena ripiega anche su quello che il pool
    # ha gia' provato e ha fallito, quindi il insieme dei tentativi del pool
    # deve sopravvivere al ramo per poterlo ESCLUDERE dalla catena.
    tried: set[str] = set()
    try:
        # La CONDIZIONE e' il risultato di _resolve_dispatch (l'unico punto di
        # traduzione config->comportamento), NON `dispatcher.active`: i due
        # coincidono oggi, ma solo cosi' il "punto unico" e' reale e non una
        # copia travestita. In "sequential" un dispatcher vuoto riprodurrebbe
        # silenziosamente il ramo parallelo invece di batterlo.
        # `has_pending_capacity()` e' la porta del contratto B: se il pool non
        # ha endpoint utilizzabili e non ne avra' uno a breve (tutti in
        # cooldown, e il cooldown puo' valere un'ora) il chunk va direttamente
        # alla catena sequenziale sull'intera lista, invece di aspettare inerti
        # che nessuno possa rispondergli. E' il "ritorno al legacy".
        # Fin qui la condizione e' UN'unica espressione: `dispatcher is not
        # None` serve al type checker per restringere il tipo nei due rami.
        if (_resolve_dispatch(stream) == "parallel" and dispatcher is not None
                and dispatcher.has_pending_capacity()):
            # Percorso parallelo: il dispatcher sceglie il livello e tiene slot +
            # lease per tutta la richiesta HTTP. In caso di fallimento si
            # riprova UNA volta sugli altri livelli paralleli, non in ping-pong:
            # ogni livello e' tentato al massimo una volta per round, cosi' un
            # endpoint rotto non viene ripetuto all'infinito in un ciclo.
            errors: list[str] = []
            for _round in (0, 1):
                for level in dispatcher.candidates_excluding(stream.fallback, tried):
                    key = _level_key(level)
                    level_leased, leased_key = dispatcher.acquire(
                        stream.fallback, stop_check=stop_check,
                        stop_timeout=stop_timeout, prefer=key,
                    )
                    if level_leased is None:
                        # Deadline di attesa del lease scaduta: il pool e'
                        # saturo (o la sonda half-open e' tenuta da altri) e non
                        # e' un endpoint utilizzabile, quindi il chunk ripiega
                        # sulla catena sequenziale invece di aspettare ancora.
                        # `errors` resta vuoto: nessun endpoint e' stato
                        # tentato, non c'e' un fallimento da propagare.
                        break
                    if leased_key != key:
                        # nel frattempo un altro endpoint e' diventato
                        # preferibile: non lo sprechiamo, si rimette indietro.
                        dispatcher.release(leased_key)
                        leased_key = None
                        continue
                    # `tried` si aggiorna QUI, non prima: segnare un endpoint
                    # come tentato senza che nessuna richiesta HTTP lo abbia
                    # toccato lo escludeva anche dalla catena di ripiego. Il caso
                    # peggiore e' il lease che non arriva (pool saturo, deadline
                    # scaduta, o sonda half-open gia' in volo): il chunk moriva
                    # con "pool: <vuoto> | catena: nessun livello da tentare",
                    # cioe' nessuno interrogato e nessuno disponibile.
                    tried.add(key)
                    try:
                        # `_transcribe_traced`, non `_transcribe`: il ramo
                        # parallelo chiama l'endpoint direttamente e senza
                        # passare dalla catena, quindi e' l'unico modo che
                        # quei tentativi finiscano nel log. Con il
                        # raccoglitore attivo e' un semplice wrapper.
                        text = _transcribe_traced(level_leased, wav_path, stream, prompt)
                    except Exception as exc:  # noqa: BLE001 - un endpoint fallito non deve far fallire il chunk
                        errors.append(f"{key}: {exc}")
                        dispatcher.release(leased_key)
                        leased_key = None
                        dispatcher.report_failure(key)
                        continue
                    dispatcher.report_success(key)
                    dispatcher.release(leased_key)
                    leased_key = None
                    success = True
                    break
                if success or errors:
                    break
            if not success and errors:
                # Il pool ha risposto e ha risposto male. NON si alza qui: il
                # chunk non e' perso, e il contratto B dice che la catena
                # sequenziale sull'intera lista e' la rete sotto il pool. I
                # fallimenti sono gia' stati registrati dal breaker, una volta
                # sola per endpoint e per chunk (contratto F).
                pool_errors = list(errors)
        if not success:
            # Percorso sequenziale: catena sulla lista completa nell'ordine
            # del config, primo successo. Raggiunto in tre casi:
            # nessun livello `parallel = true` (degrada di "auto", non un
            # errore), dispatch_mode = "sequential" esplicito (che IGNORA i
            # flag per-livello e li vede come spenti), oppure - contratto B -
            # pool parallelo senza endpoint utilizzabili (tutti in cooldown, o
            # tutti falliti, o lista vuota).
            try:
                # La catena ripiega sui livelli CHE IL POOL NON HA ANCORA
                # TENTATO: il tentativo gia' fatto non si ripaga due volte.
                catena = _levels_untried(stream.fallback, tried)
                if not catena:
                    # Il pool ha gia' provato tutta la lista, una volta per
                    # livello: non resta niente da tentare, e l'esito e' quello
                    # del pool, non un fallimento nuovo.
                    raise AllLevelsFailedError(
                        "pool: " + "; ".join(pool_errors) + " | catena: nessun livello da tentare"
                    ) from None
                text = _chain(catena, wav_path, stream, prompt)
            except AllLevelsFailedError as exc:
                if pool_errors:
                    # Pool e catena hanno fallito entrambi: l'errore e' la
                    # somma delle due prove, cosi' il log dice davvero cosa ha
                    # provato a fare il chunk e non solo l'ultimo tentativo.
                    raise AllLevelsFailedError(
                        "pool: " + "; ".join(pool_errors) + " | catena: " + str(exc)
                    ) from exc
                raise
            except Exception as exc:
                if pool_errors:
                    # Stessa cosa per un'eccezione che NON e' un errore di
                    # livello (try_with_fallback lascia passare tutto cio' che
                    # non e' ApiError/OSError/TimeoutError). Il chunk ha gia'
                    # visto il pool rispondere male, quindi l'esito e' un
                    # fallimento DICHIARATO, non un "unexpected" che cancella
                    # la prova di cosa e' stato provato.
                    raise AllLevelsFailedError(
                        "pool: " + "; ".join(pool_errors) + " | catena: " + repr(exc)
                    ) from exc
                # Percorso sequenziale puro: si lascia propagare come prima, il
                # comportamento legacy deve restare identico.
                raise
            success = True
    except AllLevelsFailedError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - rete di sicurezza del worker: un'eccezione
        # imprevista qui non deve uccidere il thread e perdere il chunk in silenzio.
        error = f"unexpected: {exc!r}"
    finally:
        # Rilascio garantito, anche su eccezione o return anticipato: uno slot
        # perso e' un endpoint che si satura per sempre.
        if leased_key is not None and dispatcher is not None:
            dispatcher.release(leased_key)
        # audio_s PRIMA di cancellare il WAV: dopo, l'header non c'e' piu' e il
        # campo del log sarebbe 0 per tutti i chunk.
        audio_s = _wav_seconds(wav_path)
        with contextlib.suppress(OSError):
            wav_path.unlink()
        result_queue.put(_ChunkResult(
            seq, text, success, error, tuple(attempts), audio_s,
            (time.monotonic() - chunk_started) * 1000.0))
        # Lo smontaggio va DOPO il put: la riga deve vedere i tentativi
        # completi. Nel finally, quindi gira anche se il put solleva.
        _pop_attempts(_previous_attempts)
        sem.release()


def _normalize_chunk_text(text: str) -> str:
    """Rimuove spazi ai bordi e garantisce un unico spazio finale.

    Così chunk consecutivi incollati restano separati da esattamente uno
    spazio, anche quando il modello STT non ne emette alcuno in coda.
    """
    stripped = text.strip()
    return stripped + " " if stripped else ""


class _FifoSequencer:
    """Commits STT result in audio order; ingest is a pure testable core."""
    def __init__(self, state, stream, record_history, notify_chunk,
                 blacklist: frozenset[str] | None = None,
                 log_max_lines: Any = None):
        self._state, self._stream = state, stream
        self._record_history, self._notify_chunk = record_history, notify_chunk
        self._blacklist = blacklist if blacklist is not None else parse_blacklist(stream.blacklist)
        # Ritenzione del log in RIGHE (non in orari): il file e' di debug.
        # `None` = lascia decidere il default del modulo (2000).
        self._log_max_lines = log_max_lines
        self._lock = threading.Lock()
        self._result_queue: queue.Queue[_ChunkResult | _Stop] = queue.Queue()
        self._pending: dict[int, _ChunkResult] = {}
        self._next_expected = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _log_chunk(self, item: _ChunkResult) -> None:
        """Scrive la riga di log del chunk, al COMMIT.

        Sta qui e non nel worker perche' il COMMIT e' l'unico punto in cui
        l'ordine dei chunk e' quello dell'audio: qui il file e' in ordine
        FIFO, mentre il worker appenderebbe nell'ordine in cui i thread
        terminano. Copre OGNI chunk, anche quello fallito (served_by null) e
        anche quello scartato dalla blacklist: e' esattamente li' che l'utente
        deve poter vedere per capire perche' il testo non e' comparso.

        Non solleva MAI: `append_record` ritorna False, e questa funzione
        lascia comunque fuori il chunk dall'errore. Un log rotto non puo'
        perdere una parola.
        """
        try:
            text = item.text if isinstance(item.text, str) else ""
            record = chunk_log.make_record(
                session=self._state.get("session_id"),
                seq=item.seq_id,
                audio_s=item.audio_s,
                attempts=list(item.attempts),
                total_ms=item.total_ms,
                text=text,
            )
            chunk_log.append_record(record, max_lines=self._log_max_lines)
        except Exception:
            logger.debug("chunk log: riga non scritta per seq %s", item.seq_id,
                         exc_info=True)

    def ingest(self, result: _ChunkResult) -> list[str]:
        committed = []
        with self._lock:
            self._pending[result.seq_id] = result
            while self._next_expected in self._pending:
                item = self._pending.pop(self._next_expected)
                # Il log sta FUORI dal ramo blacklist: la riga descrive il
                # chunk, e un chunk scartato e' proprio uno di quelli che senza
                # log sembrano spariti.
                self._log_chunk(item)
                text = _normalize_chunk_text(item.text) if item.success and isinstance(item.text, str) else ""
                if text and _command_norm(text) not in self._blacklist:
                    self._state.setdefault("chunks", []).append(text)
                    if self._stream.context_enabled:
                        update_last_chunks(self._state, text)
                    try:
                        _write_state(self._state)
                    except OSError as exc:
                        logger.error("stream state write failed during ingest: %s", exc)
                    committed.append(text)
                self._next_expected += 1
        for text in committed:
            self._record_history(text)
            self._notify_chunk(text)
        return committed

    def get_context_snapshot(self) -> list[str]:
        """Testo di contesto per il chunk successivo.

        Preferisce il testo VIVO scritto dall'estensione GNOME: riflette cio'
        che l'utente ha davvero nel campo, quindi i chunk cancellati sono gia'
        spariti e le parole comando non ci sono mai state. Se il file non
        esiste, e' illeggibile, o appartiene a un'altra sessione, si ripiega
        sulla ricostruzione del backend (last_chunks), che puo' contenere testo
        ormai cancellato: degrada la qualita' del contesto, non la dettatura.
        """
        live = read_live_text(self._state.get("session_id"))
        if live is not None:
            return live
        with self._lock:
            value = self._state.get("last_chunks")
            return list(value) if isinstance(value, list) else []

    def _run(self):
        while True:
            item = self._result_queue.get()
            if isinstance(item, _Stop):
                return
            self.ingest(item)

    def start(self):
        self._thread.start()

    def drain_and_stop(self, total_chunks: int):
        self._result_queue.put(_STOP)
        if self._thread.is_alive():
            self._thread.join()
        committed = []
        with self._lock:
            orphaned = sorted(self._pending)
            for seq_id in orphaned:
                item = self._pending.pop(seq_id)
                # Gli orphan vanno loggati come gli altri: sono chunk finiti ma
                # mai committati in ordine, e senza riga il log mostrerebbe un
                # buco nella sequenza che l'utente deve potere spiegare.
                self._log_chunk(item)
                text = _normalize_chunk_text(item.text) if item.success and isinstance(item.text, str) else ""
                if text and _command_norm(text) not in self._blacklist:
                    self._state.setdefault("chunks", []).append(text)
                    if self._stream.context_enabled:
                        update_last_chunks(self._state, text)
                    committed.append(text)
                self._next_expected = max(self._next_expected, seq_id + 1)
            if orphaned:
                logger.warning("Orphaned stream chunks during drain: %s", orphaned)
                try:
                    # Giro 2: il reviewer chiedeva preserve_chunks=True qui,
                    # ispirandosi a paste_next. MISURATO SBAGLIATO, quindi
                    # lasciato com'e'. preserve_chunks=True fa si' che
                    # _write_state sostituisca `chunks` con l'elenco RILESTO da
                    # disco. paste_next puo' farlo perche' scrive una COPIA
                    # invecchiata (letta prima del suo sleep di pacing); qui
                    # invece self._state e' lo stato AUTOREVOLE, aggiornato un
                    # istante fa con gli orphan appena committati. Con un
                    # residuo stale su disco quei chunk venivano scartati
                    # (probe: in memoria ['Fine '] -> su disco ['VECCHIO 1',
                    # 'VECCHIO 2 ']): il difetto che la voce doveva chiudere,
                    # peggio. Il caso reale (drain e paste_next nella stessa
                    # finestra) resta aperto e va risolto alla fonte.
                    _write_state(self._state)
                except OSError as exc:
                    logger.error("stream state write failed during drain: %s", exc)
            if self._next_expected < total_chunks:
                logger.warning(
                    "Missing stream chunks during drain: %s",
                    list(range(self._next_expected, total_chunks)),
                )
        for text in committed:
            self._record_history(text)
            self._notify_chunk(text)


def _rms_to_db(rms: float) -> float:
    """Converte RMS in dB (riferimento: 1.0 = 0 dB)."""
    if rms <= 0:
        return _MIN_DB
    return 20.0 * math.log10(rms)


# Clamp della soglia VAD adattiva: mai troppo sensibile / mai troppo sordo.
VAD_THRESHOLD_MIN_DB = -55.0
VAD_THRESHOLD_MAX_DB = -15.0


def _adaptive_threshold_db(floor_db: float, margin_db: float) -> float:
    """Soglia VAD adattiva = noise floor stimato + margine, clampata in
    [VAD_THRESHOLD_MIN_DB, VAD_THRESHOLD_MAX_DB].

    Distinta da ``noise_db`` (soglia iniziale fissa usata finché non ci sono
    abbastanza campioni per stimare il floor). ``margin_db`` è configurabile
    via ``[stream].vad_margin_db``."""
    return max(VAD_THRESHOLD_MIN_DB, min(VAD_THRESHOLD_MAX_DB, floor_db + margin_db))


def _is_valid_floor_sample(rms_db: float, provisional_limit: float) -> bool:
    """True se il frame è un campione valido per la stima del noise floor.

    Esclude i frame a RMS nullo (sentinella ``_MIN_DB``: silenzio digitale
    esatto, non rumore reale) e quelli sopra la soglia provvisoria (probabile
    parlato). Senza l'esclusione della sentinella, una quota di zeri porta il
    10° percentile a ``_MIN_DB`` e la soglia adattiva al clamp minimo, così il
    rumore reale viene classificato come voce e il VAD non chiude più i chunk."""
    return _MIN_DB < rms_db <= provisional_limit


def _estimate_floor_db(samples: Iterable[float]) -> float:
    """Stima del noise floor: 10° percentile dei campioni raccolti."""
    ordered = sorted(samples)
    return ordered[max(0, len(ordered) // 10)]


def _stop_drain_budget(levels: Any) -> float:
    """Budget di attesa (s) per il drain del supervisore allo stop.

    Ogni worker puo' tentare piu' endpoint prima di arrendersi, e da STEP 1
    ogni endpoint ha il SUO timeout: il worst-case di un worker non e' piu'
    ``n_livelli * chunk_timeout`` (un unico valore comune) ma la SOMMA dei
    ``level.timeout_seconds`` dei livelli che puo' attraversare. Il percorso
    sequenziale (``try_with_fallback``) tenta tutti i livelli della lista, in
    parallelo ogni livello al massimo una volta per round: in entrambi i casi
    la somma dei livelli della lista e' l'unico bound che non tronca, quindi si
    sommano TUTTI i livelli e non solo i paralleli (sommare solo i paralleli
    sotto-stimerebbe proprio il caso sequenziale, che e' quello di default).

    Il budget copre quella somma piu' un margine per drain
    (executor.shutdown + sequencer.drain_and_stop) e cleanup. Un timeout di
    livello non numerico, non finito o <= 0 vale 30.0 (stessa difesa di
    transcribe_audio), lista vuota vale un livello singolo, e il risultato non
    scende mai sotto il minimo di 30s: sotto i 30s si perdono chunk ("blocca
    in volo"), quindi e' un pavimento, non una formula."""
    total = 0.0
    count = 0
    try:
        iterable = list(levels)  # type: ignore[arg-type]
    except TypeError:
        iterable = []
    for level in iterable:
        count += 1
        # `timeout_seconds` arriva per duck typing: puo' non esserci affatto.
        # None e' un caso ORDINARIO (livello senza timeout dichiarato) e vale
        # 30.0, lo stesso default di transcribe_audio: si tratta qui invece di
        # passarlo a float(), che su None non puo' funzionare. Sotto resta il
        # try/except per i valori presenti ma non numerici.
        raw_timeout = getattr(level, "timeout_seconds", None)
        if raw_timeout is None:
            per_level = 30.0
        else:
            try:
                per_level = float(raw_timeout)
            except (TypeError, ValueError):
                per_level = 30.0
        if not math.isfinite(per_level) or per_level <= 0:
            per_level = 30.0
        total += per_level
    if count == 0:
        total = 30.0
    return max(30.0, total + 15.0)


# ---------------------------------------------------------------- helpers

def _atomic_write_json(path: Path, payload: dict) -> None:
    """Scrittura atomica (stesso pattern di status.py / output_history.py,
    ora condiviso in atomic_io.py: P17, mandato perfetto)."""
    atomic_write_json(path, payload)


def read_state() -> dict:
    """Best-effort: file assente o corrotto -> dict vuoto, mai un'eccezione."""
    try:
        data = json.loads(STREAM_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict, *, preserve_chunks: bool = False) -> None:
    # Preserva campi di cursore o incolla aggiornati concorrentemente da paste_next()
    disk = read_state()
    if isinstance(disk, dict) and disk.get("session_id") == state.get("session_id"):
        # La stessa sessione: `chunks` sul disco e' l'elenco autorevole. Un
        # chiamante che ha letto lo stato PRIMA e lo riscrive DOPO una pausa
        # (vedi paste_next: dorme paste_delay_ms fra lettura e scrittura)
        # riscriverebbe la propria copia obsoleta e perderebbe i chunk
        # committati dal supervisore nella finestra (misurato: 1 chunk perso
        # con un commit a 10ms su una finestra di 250ms). Opt-in, non sempre:
        # il ramo at_end (_write_state con la lista finale dei chunk) deve
        # poter SOSTITUIRE chunks, e per farlo usa un session_id diverso o
        # questa bandiera spenta.
        #
        # Safe perche' _FifoSequencer.ingest() APPENDE soltanto: gli indici
        # gia' incollati continuano a identificare lo stesso chunk, quindi
        # ricalcolare solo il contatore sull'elenco riletto non duplica né
        # salta nulla.
        if preserve_chunks and isinstance(disk.get("chunks"), list):
            state["chunks"] = disk["chunks"]
        for k in ("next_chunk_index", "last_paste_at"):
            if k in disk and k not in state:
                state[k] = disk[k]
            elif k in disk and k == "next_chunk_index":
                # Mantieni l'indice avanzato se presente su disco
                disk_idx = disk.get(k)
                state_idx = state.get(k)
                if isinstance(disk_idx, int) and isinstance(state_idx, int):
                    state[k] = max(state_idx, disk_idx)
    state["last_activity"] = time.time()
    _atomic_write_json(STREAM_STATE_PATH, state)


def _read_lock() -> dict | None:
    if not STREAM_LOCK_PATH.exists():
        return None
    try:
        data = json.loads(STREAM_LOCK_PATH.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        pid = data["pid"]
        _ = data["started_at"]
    except KeyError:
        return None
    if not isinstance(pid, int) or pid <= 0:
        return None
    return data


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def is_stream_active() -> bool:
    """Restituisce True se una sessione streaming è attiva (lock valido)."""
    try:
        lock = _read_lock()
        if lock is None:
            return False
        if not _pid_alive(lock["pid"]):
            # Lock residuo (supervisore morto/crash): oltre al lock va ripulita
            # anche la session_dir coi segmenti OGG, altrimenti resta in
            # XDG_RUNTIME_DIR per sempre.
            STREAM_LOCK_PATH.unlink(missing_ok=True)
            # Giro 2 (B1): il file audio di at_end sta FUORI dalla session_dir
            # (mkstemp in /tmp) e il lock era l'unica cosa che ne conosceva il
            # percorso. Senza questo, un SIGKILL sul recorder lasciava la voce
            # integrale in /tmp per sempre, irraggiungibile da ogni altro
            # codice: stop() ritorna False e _stop_at_end non gira piu'.
            audio_path = lock.get("audio_path")
            if isinstance(audio_path, str) and audio_path:
                with contextlib.suppress(OSError):
                    Path(audio_path).unlink(missing_ok=True)
            session_id = lock.get("session_id")
            if isinstance(session_id, str) and session_id:
                session_dir = STREAM_LOCK_PATH.parent / f"stream-{session_id}"
                if session_dir.exists():
                    with contextlib.suppress(OSError):
                        shutil.rmtree(session_dir, ignore_errors=True)
            return False
        return True
    except (OSError, KeyError):
        return False


def _acquire_lock(pid: int, extra: dict | None = None) -> None:
    """Lock esclusivo (O_CREAT|O_EXCL) con pid del processo detentore."""
    STREAM_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(STREAM_LOCK_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _clear_stale_lock_or_raise()
        fd = os.open(str(STREAM_LOCK_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    payload: dict = {"pid": pid, "started_at": time.time()}
    if extra:
        payload.update(extra)
    os.write(fd, json.dumps(payload).encode())
    os.close(fd)


def _clear_stale_lock_or_raise() -> None:
    """Se il lock esistente ha un detentore vivo solleva; altrimenti lo rimuove.

    Estratto da _acquire_lock per tenere la logica fuori dal blocco except.
    """
    lock = _read_lock()
    alive = False
    try:
        alive = lock is not None and _pid_alive(lock["pid"])
    except (KeyError, OSError, TypeError):
        alive = False
    if alive:
        raise RuntimeError("Stream session already active")
    STREAM_LOCK_PATH.unlink(missing_ok=True)


def heartbeat() -> None:
    """Rinfresca il timestamp di status.json dichiarando ancora RECORDING.

    P4: senza questo il watchdog dell'estensione (15 min su 'recording')
    uccide una sessione per_chunk VIVA piu' lunga di 15 minuti: il
    timestamp era quello dell'avvio, perche' write_status(RECORDING) gira
    una volta sola in _start_per_chunk. Con il battito, l'eta' letta
    dall'estensione e' quella dell'attivita'.

    Il guard di status.write_status confronta il servizio e lascia passare
    solo chi dichiara lo stesso: qui si riscrive sempre service='stream',
    quindi la scrittura non viene respinta, e non tocca recording/processing
    di un altro servizio (stt/ocr restano intatti).

    Best-effort: un file di stato non scrivibile non deve abbattere il
    supervisore, quindi ogni errore e' solo loggato.
    """
    try:
        status.write_status(status.STATE_RECORDING, service="stream")
    except Exception:
        logger.debug("impossibile rinfrescare il cuore su RECORDING", exc_info=True)


def _update_lock(**extra: object) -> None:
    """Aggiorna il lock in-place (usato dal supervisore per aggiungere
    ffmpeg_pid senza rompere l'esclusione)."""
    lock: dict = {}
    try:
        lock = json.loads(STREAM_LOCK_PATH.read_text())
        if not isinstance(lock, dict):
            lock = {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    lock.update(extra)
    _atomic_write_json(STREAM_LOCK_PATH, lock)


def _terminate_pid(pid: int, graceful: bool = True) -> None:
    """Termina un processo (SIGINT poi SIGTERM poi SIGKILL)."""
    if not _pid_alive(pid):
        return
    sig = signal.SIGINT if graceful else signal.SIGTERM
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, sig)
    for _attempt in range(50):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    if _pid_alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
    if _pid_alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


# ---------------------------------------------------------------- recording

def _spawn_recorder(audio_cfg: AudioConfig, out_path: Path) -> subprocess.Popen:
    """Avvia ffmpeg per la registrazione a_end (file singolo)."""
    return subprocess.Popen(
        [
            "ffmpeg", "-y", "-f", "pulse", "-i", "default",
            "-ac", "1", "-ar", str(audio_cfg.sample_rate),
            "-c:a", audio_cfg.codec, "-b:a", f"{audio_cfg.bitrate_kbps}k",
            str(out_path),
        ],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------- session

def _filter_chunks(last_chunks: list[str]) -> list[str]:
    """Filtra chunk vuoti, allucinazioni note e duplicati consecutivi.
    Mantiene al massimo gli ultimi 3 elementi utili (in ordine cronologico)."""
    filtered: list[str] = []
    for chunk in last_chunks:
        stripped = chunk.strip() if isinstance(chunk, str) else ""
        if not stripped:
            continue
        if stripped in KNOWN_HALLUCINATIONS:
            continue
        if filtered and filtered[-1] == stripped:
            continue
        filtered.append(stripped)
    return filtered[-3:]


def read_live_text(session_id: Any) -> list[str] | None:
    """Legge il testo vivo del campo scritto dall'estensione GNOME.

    Ritorna la lista degli ultimi segmenti ancora presenti, oppure None se il
    file non e' utilizzabile (assente, corrotto, di un'altra sessione): in quel
    caso il chiamante ripiega su last_chunks.
    """
    try:
        raw = STREAM_LIVE_TEXT_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if session_id is not None and data.get("session_id") != session_id:
        return None
    segments = data.get("segments")
    if not isinstance(segments, list):
        return None
    cleaned = [s.strip() for s in segments if isinstance(s, str) and s.strip()]
    return cleaned[-3:] if cleaned else []


def _command_phrases(commands: list) -> frozenset[str]:
    """Insieme normalizzato di keyword + alias di tutti i comandi vocali.

    Serve a tenere fuori dal prompt di contesto le parole comando: se "cancella"
    o "invio" finissero in coda, il chunk successivo li tratterebbe come parte
    della frase da proseguire invece che come comando da eseguire.
    """
    phrases: set[str] = set()
    for command in commands or []:
        for phrase in getattr(command, "all_phrases", []):
            normalized = _command_norm(phrase) if isinstance(phrase, str) else ""
            if normalized:
                phrases.add(normalized)
    return frozenset(phrases)


def is_command_phrase(text: str, phrases: frozenset[str] | list[str] | None) -> bool:
    """True se il chunk normalizzato combacia con una parola comando.

    Accetta anche una lista: lo stato viene riletto da JSON, dove un frozenset
    non puo' esistere.
    """
    normalized = _command_norm(text) if isinstance(text, str) else ""
    if not normalized or not phrases:
        return False
    known = phrases if isinstance(phrases, (frozenset, set)) else frozenset(phrases)
    return normalized in known


def update_last_chunks(state: dict, text: str, max_chunks: int = 3) -> None:
    """Aggiorna state['last_chunks'] con il nuovo testo trascritto.

    Scarta chunk vuoti, allucinazioni note e duplicati consecutivi: non devono
    finire né nel prompt di contesto del chunk successivo né in last_chunks.
    """
    stripped = text.strip() if isinstance(text, str) else ""
    if not stripped or stripped in KNOWN_HALLUCINATIONS:
        return
    # Una parola comando non e' contesto: viene eseguita dall'estensione e non
    # deve diventare parte della frase che il chunk successivo deve completare.
    if is_command_phrase(stripped, state.get("command_phrases") or frozenset()):
        return
    last = state.get("last_chunks")
    if not isinstance(last, list):
        last = []
    if last and last[-1] == stripped:
        return
    state["last_chunks"] = (last + [stripped])[-max_chunks:]


def build_prompt(personal_prompt: str, last_chunks: list[str], max_chars: int = 800) -> str | None:
    """Combina il prompt personale fisso con la coda degli ultimi chunk (max 3).

    Priorità: il personal_prompt deve rimanere integro se possibile (senza
    essere mozzato all'inizio). I chunk più recenti vengono inseriti a riempire
    il budget rimanente (interi, eliminando i più vecchi se non ci stanno).
    Ritorna None se vuoto.
    """
    personal = personal_prompt.strip() if personal_prompt else ""
    chunks = _filter_chunks(last_chunks)

    if not personal and not chunks:
        return None

    # Il prompt personale resta integro finché ci sta; se da solo eccede il
    # budget viene troncato (mantenendo l'inizio, che è la parte istruttiva).
    if personal and len(personal) >= max_chars:
        return personal[:max_chars]

    selected: list[str] = []
    if personal:
        budget = max_chars - len(personal) - 1  # -1 per lo spazio separatore
    else:
        budget = max_chars
    # Aggiungi i chunk dal più recente al più vecchio, interi e finché ci
    # stanno: i più vecchi che non entrano vengono scartati.
    for chunk in reversed(chunks):
        cost = len(chunk) + (1 if selected else 0)
        if cost <= budget:
            selected.insert(0, chunk)
            budget -= cost
        else:
            break

    parts = ([personal] if personal else []) + selected
    result = " ".join(parts).strip()
    return result if result else None


def _paste_state(stream_cfg: Any) -> dict:
    """Config di incolla da specchiare nello stato, costruita una volta sola.

    Le sette copie identiche (start at_end, stop at_end, start per_chunk,
    stop per_chunk, supervisore, e i due rami di paste_next) leggevano tutti
    gli stessi campi di [stream]. Qui il blocco si costruisce una volta:
    stesse chiavi, stesso ordine, stessi valori di default. Il chiamante lo
    unisce al proprio dict con `**` (o `state.update(...)`).

    Funzione di modulo e non metodo perche' i test chiamano paste_next su una
    sessione finta che ha solo `_stream` e `_cfg`.
    """
    return {
        "paste_delay_ms": stream_cfg.paste_delay_ms,
        "paste_shortcut": stream_cfg.paste_shortcut,
        "paste_channel": stream_cfg.paste_channel,
        "commands": [dataclasses.asdict(rule) for rule in stream_cfg.commands],
        "blacklist": stream_cfg.blacklist,
    }


class StreamSession:
    """Sessione di dettatura streaming."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._stream = cfg.stream
        self._audio = cfg.audio

    def is_active(self) -> bool:
        return is_stream_active()

    def get_state(self) -> dict:
        return read_state()

    def _busy_notice(self, message: str) -> None:
        """Notifica di rifiuto condivisa dai tre guard di start().

        Il testo del rifiuto resta LETTERALE nella chiamata, nei tre punti
        che invocano questo helper: il gate i18n lo estrae dal sorgente con
        una regex e qui non deve comparire nessuna copia. Qui vivono solo
        titolo e icona, identici nei tre casi.
        """
        if self._cfg.notifications:
            notify.send(_("Streaming dictation"), message,
                        icon=notify.resolve_icon("error_general", self._cfg.icons.error_general))

    def start(self) -> bool:
        """Avvia la registrazione. Restituisce False se bloccato (D4)."""
        if audio.is_recording():
            self._busy_notice(_("Another operation in progress"))
            return False
        if is_stream_active():
            self._busy_notice(_("Another operation in progress"))
            return False
        if not self._stream.fallback:
            self._busy_notice(_("No endpoint configured"))
            return False
        try:
            if self._stream.mode == MODE_AT_END:
                self._start_at_end()
            else:
                self._start_per_chunk()
        except RuntimeError as exc:
            if self._cfg.notifications:
                notify.send(_("Streaming dictation"), str(exc), icon=notify.resolve_icon("error_general", self._cfg.icons.error_general))
            return False
        return True

    # -- at_end ---------------------------------------------------------

    def _start_at_end(self) -> None:
        fd, path_str = tempfile.mkstemp(
            suffix=f".{self._audio.format}", prefix="bravoric-stream-")
        os.close(fd)
        out_path = Path(path_str)
        try:
            proc = _spawn_recorder(self._audio, out_path)
        except BaseException:
            # ffmpeg assente o avvio fallito: senza cleanup resterebbe un file
            # temp orfano (il try/except sotto copre solo il lock).
            out_path.unlink(missing_ok=True)
            raise
        session_id = uuid.uuid4().hex
        try:
            _acquire_lock(proc.pid, {"mode": MODE_AT_END, "session_id": session_id,
                                     "audio_path": str(out_path)})
        except RuntimeError:
            # Il recorder è già partito: senza terminarlo resterebbe orfano a
            # registrare per sempre (e ricreerebbe il file appena rimosso).
            _terminate_pid(proc.pid, graceful=False)
            out_path.unlink(missing_ok=True)
            raise
        _write_state({
            "session_id": session_id, "active": True, "mode": MODE_AT_END,
            "chunks": [], "next_chunk_index": 0, "audio_path": str(out_path),
            **_paste_state(self._stream),
        })
        try:
            status.write_status(status.STATE_RECORDING, service="stream")
        except Exception:
            logger.debug("impossibile aggiornare lo status su RECORDING", exc_info=True)
        notify.maybe_send_simple(
            self._cfg.notifications, self._cfg.notif_stream.processing_start,
            _("Streaming dictation"), _("Recording..."),
            icon=notify.resolve_icon("stream_session_start", self._cfg.icons.stream_session_start),
        )

    def _stop_at_end(self, lock: dict) -> bool:
        audio_path = Path(lock.get("audio_path", ""))
        _terminate_pid(lock["pid"])
        STREAM_LOCK_PATH.unlink(missing_ok=True)
        state = read_state()
        session_id = state.get("session_id") or lock.get("session_id") or uuid.uuid4().hex
        text = ""
        # Giro 2 (B1): la cancellazione del file audio sta in FINALLY. Prima
        # era a meta' del ramo (riga 1314) e le uscite che la precedono la
        # saltavano: una notify che solleva, un _write_state che solleva, o
        # un'eccezione non contenuta. La lock era gia' stata rimossa due righe
        # sopra, quindi in quei casi la registrazione integrale della voce
        # restava in /tmp e nessun altro codice la incontra piu'.
        try:
            return self._stop_at_end_transcribe(audio_path, session_id, text)
        finally:
            audio_path.unlink(missing_ok=True)

    def _stop_at_end_transcribe(self, audio_path, session_id, text) -> bool:
        if audio_path.exists() and audio_path.stat().st_size > 0:
            notify.maybe_send_simple(
                self._cfg.notifications, self._cfg.notif_stream.processing_start,
                _("Streaming dictation"), _("Transcribing..."),
                icon=notify.ICON_PROCESSING,
            )
            try:
                text = try_with_fallback(
                    self._stream.fallback,
                    lambda level: transcribe_audio(
                        level, audio_path,
                        language=self._stream.language or None,
                        prompt=self._stream.prompt or None,
                        hotwords=self._stream.hotwords or None,
                    ),
                )
            except AllLevelsFailedError as exc:
                logger.warning("stream transcription failed: %s", exc)
                if self._cfg.notifications:
                    notify.send(_("Streaming dictation"),
                                _("Transcription failed"), icon=notify.resolve_icon("error_general", self._cfg.icons.error_general))
        # nessun cleanup LLM (D1)
        text = text.strip()
        if _command_norm(text) in parse_blacklist(self._stream.blacklist):
            text = ""
        chunks = [text] if text else []
        try:
            status.write_status(status.STATE_IDLE, last_output=text or None, service="stream")
        except Exception:
            logger.debug("impossibile aggiornare lo status su IDLE", exc_info=True)
        _write_state({
            "session_id": session_id, "active": False, "mode": MODE_AT_END,
            "chunks": chunks, "next_chunk_index": 0,
            **_paste_state(self._stream),
        })
        if text:
            self._record_history(text)
            notify.maybe_send(
                self._cfg.notifications, self._cfg.notif_stream.raw_ready,
                _("Streaming dictation"), text,
                icon=notify.resolve_icon("stream_chunk_delivered", self._cfg.icons.stream_chunk_delivered),
            )
        return True

    # -- per_chunk ------------------------------------------------------

    def _start_per_chunk(self) -> None:
        session_id = uuid.uuid4().hex
        session_dir = STREAM_LOCK_PATH.parent / f"stream-{session_id}"
        session_dir.mkdir(parents=True, exist_ok=True)
        try:
            log_path = session_dir / "supervisor.log"
            log = open(log_path, "ab")  # noqa: SIM115
        except OSError as exc:
            logger.error("impossibile aprire il file di log del supervisore: %s", exc)
            if session_dir.exists():
                with contextlib.suppress(OSError):
                    shutil.rmtree(session_dir, ignore_errors=True)
            raise
        proc = subprocess.Popen(
            [sys.executable, "-m", "bravoric_stt_clipboard.stream",
             "--supervise", session_id],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, cwd=str(session_dir),
        )
        log.close()
        try:
            _acquire_lock(proc.pid, {"mode": MODE_PER_CHUNK, "session_id": session_id,
                                     "session_dir": str(session_dir)})
        except RuntimeError:
            # Lock già detenuto da un'altra sessione: il supervisore appena
            # avviato resterebbe orfano (e ruberebbe il lock riscrivendolo).
            # Terminalo, pulisci la dir e propaga l'errore: start() deve
            # restituire False (prima ritornava True comunque).
            _terminate_pid(proc.pid, graceful=False)
            if session_dir.exists():
                with contextlib.suppress(OSError):
                    shutil.rmtree(session_dir, ignore_errors=True)
            raise
        _write_state({
            "session_id": session_id, "active": True, "mode": MODE_PER_CHUNK,
            "chunks": [], "next_chunk_index": 0,
            **_paste_state(self._stream),
        })
        try:
            status.write_status(status.STATE_RECORDING, service="stream")
        except Exception:
            logger.debug("impossibile aggiornare lo status su RECORDING", exc_info=True)
        notify.maybe_send_simple(
            self._cfg.notifications, self._cfg.notif_stream.processing_start,
            _("Streaming dictation"), _("Listening..."),
            icon=notify.resolve_icon("stream_session_start", self._cfg.icons.stream_session_start),
        )

    def _stop_per_chunk(self, lock: dict) -> bool:
        """Chiede la terminazione graceful al supervisore e attende il suo
        termine (tramite scomparsa del lock) con timeout di sicurezza."""
        try:
            pid = lock["pid"]
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)

            # Attendi che il supervisore termini e rimuova il lock da solo
            # (dopo aver completato il drain dell'audio e la trascrizione finale).
            wait = _stop_drain_budget(self._stream.fallback)
            deadline = time.time() + wait
            while time.time() < deadline:
                cur = _read_lock()
                if cur is None or not _pid_alive(cur.get("pid", 0)):
                    break
                time.sleep(0.1)
            else:
                # Timeout di sicurezza superato: fallback a terminazione forzata.
                _terminate_pid(pid, graceful=False)
        except (KeyError, OSError):
            pass

        # Rimuovi il lock solo se è ancora il nostro (se non è già stato
        # rimosso dal supervisore nel suo finally).
        cur = _read_lock()
        try:
            pid = lock["pid"]
        except KeyError:
            pid = None
        if cur is None or (pid is not None and cur.get("pid") == pid):
            STREAM_LOCK_PATH.unlink(missing_ok=True)

        try:
            session_dir = STREAM_LOCK_PATH.parent / f"stream-{lock.get('session_id', '')}"
            if session_dir.exists():
                shutil.rmtree(session_dir, ignore_errors=True)
        except OSError:
            pass

        state = read_state()
        state["active"] = False
        state.update(_paste_state(self._stream))
        _write_state(state)
        if self._cfg.notifications:
            notify.send(_("Streaming dictation"), _("Session ended"),
                        icon=notify.resolve_icon("stream_session_end", self._cfg.icons.stream_session_end))
        return True

    def stop(self) -> bool:
        lock = _read_lock()
        if lock is None:
            return False
        mode = lock.get("mode", self._stream.mode)
        if mode == MODE_AT_END:
            return self._stop_at_end(lock)
        return self._stop_per_chunk(lock)

    # -- chunk assembly -------------------------------------------------

    def _run_supervisor(self, session_id: str) -> int:
        """Loop del supervisore: ffmpeg emette PCM grezzo su stdout,
        calcoliamo RMS frame-by-frame per rilevare il silenzio.
        Assembla le utterance, trascrive e aggiorna stream_state.json."""
        stream = self._stream
        audio_cfg = self._audio
        session_dir = STREAM_LOCK_PATH.parent / f"stream-{session_id}"
        session_dir.mkdir(parents=True, exist_ok=True)

        # ffmpeg command: output PCM (16-bit little-endian) on stdout
        cmd = [
            "ffmpeg", "-y", "-f", "pulse", "-i", "default",
            "-ac", "1", "-ar", str(audio_cfg.sample_rate),
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-",  # output to stdout
        ]

        proc: subprocess.Popen[bytes] | None = None
        stopping = False
        stop_time: float | None = None
        STOP_TIMEOUT = 30.0

        def _on_stop(signum: int, frame) -> None:
            nonlocal stopping, stop_time
            stopping = True
            stop_time = time.time()
            if proc is not None:
                with contextlib.suppress(Exception):
                    proc.send_signal(signal.SIGINT)

        signal.signal(signal.SIGTERM, _on_stop)
        signal.signal(signal.SIGINT, _on_stop)

        state = {
            "session_id": session_id, "active": True, "mode": MODE_PER_CHUNK,
            "chunks": [], "next_chunk_index": 0, "last_chunks": [],
            **_paste_state(stream),
            # Lista (non frozenset): lo stato viene serializzato in JSON e un
            # frozenset farebbe fallire _write_state. Serve al backend per
            # tenere fuori dal contesto le parole comando.
            "command_phrases": sorted(_command_phrases(stream.commands)),
        }
        _write_state(state)
        sequencer = _FifoSequencer(
            state, stream, self._record_history,
            lambda text: notify.maybe_send(
                self._cfg.notifications, self._cfg.notif_stream.raw_ready,
                _("Streaming dictation"), text, icon=notify.resolve_icon("stream_chunk_delivered", self._cfg.icons.stream_chunk_delivered)),
            # Ritenzione del log in RIGHE: la riga di log la scrive il
            # sequencer, quindi la soglia gli passa da config.
            log_max_lines=getattr(stream, "chunk_log_max_lines", None),
        )
        sequencer.start()

        # --- pool endpoint paralleli (onda 2) --------------------------------
        # La decisione parallelo/sequenziale la prende _resolve_dispatch, che
        # e' l'UNICO lettore di dispatch_mode. "sequential" -> nessun
        # dispatcher e _NullBreaker (il percorso sequenziale non chiama MAI il
        # breaker: tutte le chiamate breaker.stato/acquire/release stanno
        # dentro _Dispatcher). "auto" con zero livelli `parallel = true`
        # degrada a sequenziale: non e' un errore ne' un fallback silenzioso.
        dispatch = _resolve_dispatch(stream)
        parallel_levels = [
            level for level in stream.fallback if getattr(level, "parallel", False)
        ]
        breaker = _build_breaker(stream, dispatch)
        # La RETROVIA e' sequenziale e copre TUTTA la lista, non solo i checked:
        # e' il "ritorno al legacy" (contratto B). Il pool, invece, resta
        # quello sui soli checked: i non-checked non entrano in _order e non
        # prendono mai un lease, quindi non possono aprirsi un canale
        # concorrente (contratto C). Nessun terzo stato "semi-parallel".
        dispatcher = (
            _Dispatcher(parallel_levels, breaker, fallback_chain=stream.fallback)
            if dispatch == "parallel" and parallel_levels else None
        )
        # Gate per chiave endpoint, su TUTTI i livelli e senza il flag
        # `parallel`: e' quello che rende `max_concurrency` una capienza REALE
        # anche quando il percorso e' sequenziale e il dispatcher non esiste.
        # Senza di lui il numero scelto dall'utente non governava niente in
        # sequenziale (TEMA2 V1, misurato). Non cambia `worker_count`: il tetto
        # globale resta quello di prima, il tetto per endpoint e' questo.
        endpoint_gate = _EndpointGate(stream.fallback)

        try:
            worker_count = int(stream.max_concurrent_chunks)
        except (TypeError, ValueError):
            worker_count = 3
        worker_count = max(1, min(8, worker_count))
        if dispatcher is not None and getattr(stream, "max_concurrent_chunks_auto", False):
            # AUTO: vedi _auto_worker_count per la formula e per il perche' del
            # secondo addendo (contratto E). Calcolata UNA volta qui: dopo,
            # executor e semaforo non si ricostruiscono, altrimenti un rilascio
            # finale puo' sollevare ValueError e perdere chunk. Se
            # max_concurrent_chunks e' esplicito (>0) vince l'utente e la
            # somma non conta.
            worker_count = _auto_worker_count(
                stream.fallback, parallel_levels, breaker,
            )
            logger.info(
                "stream parallel pool: %d levels, auto cap %d workers",
                dispatcher.level_count, worker_count,
            )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
        sem = threading.BoundedSemaphore(worker_count)
        seq_counter = itertools.count()

        # PCM parameters
        SAMPLE_RATE = audio_cfg.sample_rate
        BYTES_PER_SAMPLE = 2  # 16-bit
        CHANNELS = 1
        FRAME_SIZE = 480  # samples per frame (~30ms at 16kHz)
        BYTES_PER_FRAME = FRAME_SIZE * BYTES_PER_SAMPLE * CHANNELS

        # VAD parameters from config
        SILENCE_SECONDS = stream.silence_seconds
        NOISE_DB = stream.noise_db
        MAX_UTTERANCE_SECONDS = stream.max_utterance_seconds

        # VAD adattivo: una soglia fissa (noise_db) non regge su microfoni con
        # noise floor molto diverso dal default (es. webcam a ~-40 dB con soglia
        # -30 dB → nessun frame supera la soglia → 0 chunk). Stimiamo il noise
        # floor come 10° percentile dei frame *non in utterance* recenti e
        # poniamo la soglia MARGIN_DB sopra di esso. noise_db resta la soglia
        # iniziale finché non ci sono abbastanza campioni.
        MARGIN_DB = stream.vad_margin_db  # quanto sopra il noise floor = voce
        floor_history: collections.deque[float] = collections.deque(maxlen=FLOOR_WINDOW_FRAMES)
        calibrated_threshold_db: float | None = None
        calib_logged = False

        # State for VAD
        voiced_frames: list[bytes] = []  # PCM frames for the current utterance
        silent_frames = 0   # consecutive silent frames
        in_utterance = False
        utterance_start_time = 0.0
        started_at = time.time()

        # P4: orologio del battito sul cuore di status.json. `last_beat` parte
        # da 0 (non da time.time()) cosi' il PRIMO giro batte subito il
        # cuore: il timestamp con cui il watchdog misura l'eta' e' allora
        # quello del supervisore vivo, non quello ereditato dal genitore che
        # ha avviato la sessione (che puo' essere gia' vecchio di qualche
        # secondo, e in quel momento nessun altro batte).
        last_beat = 0.0

        # Thread-safe queue for passing PCM data from ffmpeg stdout to VAD loop
        _SENTINEL_EOF = object()
        pcm_queue: queue.Queue[object] = queue.Queue()

        def _pcm_reader(stdout: BinaryIO) -> None:
            """Read PCM data from ffmpeg stdout and put in queue."""
            try:
                while True:
                    chunk = stdout.read(BYTES_PER_FRAME)
                    if not chunk:
                        break
                    pcm_queue.put(chunk)
            finally:
                pcm_queue.put(_SENTINEL_EOF)  # sentinel indicating true EOF

        def _submit_utterance(pcm_bytes: bytes) -> None:
            if not pcm_bytes:
                return
            duration = len(pcm_bytes) / (BYTES_PER_SAMPLE * CHANNELS * SAMPLE_RATE)
            if duration < stream.min_utterance_seconds:
                logger.debug("Utterance too short (%.2fs < %ds), discarding", duration, stream.min_utterance_seconds)
                return
            seq = next(seq_counter)
            wav_path = session_dir / f"utt-{seq:05d}.wav"
            try:
                with wave.open(str(wav_path), "wb") as wf:
                    wf.setnchannels(CHANNELS)
                    wf.setsampwidth(BYTES_PER_SAMPLE)
                    wf.setframerate(SAMPLE_RATE)
                    wf.writeframes(pcm_bytes)
            except OSError as exc:
                logger.warning("stream chunk WAV write failed: %s", exc)
                # attempts vuoto: nessun endpoint e' stato interrogato, quindi
                # il log deve dire "nessun tentativo" e non inventarne uno.
                sequencer.ingest(_ChunkResult(
                    seq, "", False, str(exc), (), duration * 1000.0, 0.0))
                return
            prompt = (build_prompt(stream.prompt, sequencer.get_context_snapshot())
                      if stream.context_enabled else (stream.prompt or None))
            # Backpressure is lossless: anche dopo SIGTERM si aspetta uno slot
            # per ogni utterance gia' emessa dal VAD. Lo scarto e' l'ULTIMA
            # risorsa, e la retrovia ci arriva PRIMA: scaduta la deadline, se il
            # chunk non ha potuto prendere il posto in coda viene servito dalla
            # catena sequenziale, che non consuma worker (contratto B/D).
            # Non esiste piu' il ramo che cancellava il WAV e ingoiava un
            # _ChunkResult vuoto: quello e' il modo in cui i chunk sparivano
            # in silenzio.
            deadline = time.time() + STOP_TIMEOUT
            slot_taken = False
            while True:
                if sem.acquire(timeout=0.25):
                    slot_taken = True
                    break
                if time.time() > deadline:
                    break
            if slot_taken:
                executor.submit(
                    _worker, seq, wav_path, prompt, stream=stream, sem=sem,
                    result_queue=sequencer._result_queue,
                    dispatcher=dispatcher,
                    # il check di fermata va passato al worker: i suoi loop di
                    # attesa sullo slot per endpoint devono poter uscire,
                    # altrimenti un supervisor fermo aspetterebbe un lease che
                    # non arriva mai.
                    stop_check=lambda: stopping,
                    stop_timeout=STOP_TIMEOUT,
                    endpoint_gate=endpoint_gate,
                )
                return
            # Ultimo tentativo, FUORI dalla coda dei worker: la catena
            # sequenziale sincrona sull'intera lista dei livelli, in ordine di
            # config. Non e' un doppione: e' la via che il contratto chiede
            # quando il chunk non ha un endpoint utilizzabile nel pool.
            logger.warning(
                "semaphore acquire timeout exceeded (%.1fs), chunk %d served by the sequential chain",
                STOP_TIMEOUT, seq,
            )
            # Questa via NON passa da _worker, quindi il raccoglitore va armato
            # qui: senza, i tentativi della catena sarebbero persi e la riga
            # direbbe served_by null su un chunk che invece ha risposto.
            _previous_attempts, _sync_attempts = _push_attempts()
            _sync_started = time.monotonic()
            try:
                # Stessa regola del ripiego del pool: la catena riceve i
                # livelli MENO quelli gia' tentati. Qui il chunk non e' mai
                # passato da un worker, quindi non e' stato tentato nulla e
                # l'insieme dei tentativi e' vuoto: la lista e' quella di
                # config, cioe' il legacy. Scritto cosi' perche' il call site
                # dica la regola invece di sembrare un'eccezione.
                text = _sequential_chain(
                    _levels_untried(stream.fallback, set()), wav_path, stream,
                    prompt, endpoint_gate=endpoint_gate,
                )
            except AllLevelsFailedError as exc:
                # Qui il chunk E' davvero perso: la stringa dell'errore lo dice
                # e resta nel log, invece di un _ChunkResult vuoto che il
                # sequenziatore scarterebbe senza lasciare traccia.
                logger.error("utterance %d lost, all levels failed: %s", seq, exc)
                _sync_audio_s = _wav_seconds(wav_path)
                with contextlib.suppress(OSError):
                    wav_path.unlink()
                sequencer.ingest(_ChunkResult(
                    seq, "", False, str(exc), tuple(_sync_attempts),
                    _sync_audio_s, (time.monotonic() - _sync_started) * 1000.0))
                _pop_attempts(_previous_attempts)
                return
            except Exception as exc:  # noqa: BLE001 - l'ultima rete non butta via il supervisore
                logger.error("utterance %d lost: %r", seq, exc)
                _sync_audio_s = _wav_seconds(wav_path)
                with contextlib.suppress(OSError):
                    wav_path.unlink()
                sequencer.ingest(_ChunkResult(
                    seq, "", False, repr(exc), tuple(_sync_attempts),
                    _sync_audio_s, (time.monotonic() - _sync_started) * 1000.0))
                _pop_attempts(_previous_attempts)
                return
            # audio_s PRIMA dell'unlink: dopo, l'header del WAV non c'e' piu'.
            _sync_audio_s = _wav_seconds(wav_path)
            with contextlib.suppress(OSError):
                wav_path.unlink()
            sequencer.ingest(_ChunkResult(
                seq, text, bool(text), None, tuple(_sync_attempts),
                _sync_audio_s, (time.monotonic() - _sync_started) * 1000.0))
            _pop_attempts(_previous_attempts)

        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            _update_lock(ffmpeg_pid=proc.pid)
            # Start PCM reader thread
            pcm_stdout = proc.stdout
            assert pcm_stdout is not None, "ffmpeg stdout must be PIPE"
            threading.Thread(target=_pcm_reader, args=(pcm_stdout,), daemon=True).start()

            try:
                while True:
                    # P4: battito del cuore. Sta in CAPA al ciclo, non dentro
                    # il ramo `pcm_item is None`: quel ramo fa `continue`, e il
                    # battito non potrebbe mai arrivare proprio quando il
                    # supervisore sta aspettando silenzio (che e' il caso
                    # peggiore: utente che detta, pausa, continua). Il ciclo
                    # gira comunque almeno ogni 0.5 s (timeout della get), quindi
                    # il battito non puo' accumulare ritardo.
                    if (now := time.time()) - last_beat >= STREAM_HEARTBEAT_SECONDS:
                        last_beat = now
                        heartbeat()
                    try:
                        pcm_item = pcm_queue.get(timeout=0.5)
                    except queue.Empty:
                        pcm_item = None

                    # Fine effettiva dello stream PCM emesso da ffmpeg
                    if pcm_item is _SENTINEL_EOF:
                        break

                    # Timeout di lettura senza dati: controlla se è scattato il timeout di stop
                    if pcm_item is None:
                        if stopping and stop_time is not None and (time.time() - stop_time) > STOP_TIMEOUT:
                            logger.warning("Supervisor stop timeout exceeded (%.1fs), forcing exit", STOP_TIMEOUT)
                            if proc is not None:
                                with contextlib.suppress(Exception):
                                    proc.kill()
                            break
                        continue

                    assert isinstance(pcm_item, bytes)
                    pcm_chunk = pcm_item

                    # Compute RMS of the chunk
                    sum_squares = 0
                    num_samples = len(pcm_chunk) // BYTES_PER_SAMPLE
                    if num_samples == 0:
                        continue
                    for i in range(num_samples):
                        sample = int.from_bytes(
                            pcm_chunk[i*BYTES_PER_SAMPLE:(i+1)*BYTES_PER_SAMPLE],
                            byteorder='little', signed=True
                        )
                        sum_squares += sample * sample
                    rms = math.sqrt(sum_squares / num_samples) / 32768.0  # normalize to [-1, 1]
                    rms_db = _rms_to_db(rms)

                    # Stima adattiva del noise floor solo fuori dall'utterance.
                    # Per evitare che parlato iniziale basso (che non supera NOISE_DB)
                    # finisca nella stima e alzi indebitamente la soglia, accettiamo
                    # solo frame che non superano una soglia prudenziale (NOISE_DB o calibrated).
                    if not in_utterance:
                        provisional_limit = calibrated_threshold_db if calibrated_threshold_db is not None else NOISE_DB
                        if _is_valid_floor_sample(rms_db, provisional_limit):
                            floor_history.append(rms_db)
                            if len(floor_history) >= MIN_FLOOR_FRAMES:
                                floor_db = _estimate_floor_db(floor_history)
                                calibrated_threshold_db = _adaptive_threshold_db(
                                    floor_db, MARGIN_DB,
                                )
                                if not calib_logged:
                                    logger.info(
                                        "stream VAD calibrated: noise_floor=%.1f dB threshold=%.1f dB",
                                        floor_db, calibrated_threshold_db,
                                    )
                                    calib_logged = True

                    threshold_db = (
                        calibrated_threshold_db
                        if calibrated_threshold_db is not None
                        else NOISE_DB
                    )
                    is_silent = rms_db < threshold_db

                    current_time = time.time() - started_at

                    if is_silent:
                        silent_frames += 1
                        if in_utterance:
                            silent_duration = silent_frames * (FRAME_SIZE / SAMPLE_RATE)
                            if silent_duration >= SILENCE_SECONDS:
                                if voiced_frames:
                                    utterance_pcm = b"".join(voiced_frames)
                                    utterance_end_time = current_time - silent_duration
                                    utterance_start_time = utterance_end_time - (len(voiced_frames) * FRAME_SIZE / SAMPLE_RATE)
                                    _submit_utterance(utterance_pcm)
                                    voiced_frames = []
                                    in_utterance = False
                                silent_frames = 0
                    else:
                        silent_frames = 0
                        if not in_utterance:
                            in_utterance = True
                            utterance_start_time = current_time
                        voiced_frames.append(pcm_chunk)

                    # Forced flush if utterance too long
                    if in_utterance:
                        utterance_duration = current_time - utterance_start_time
                        if utterance_duration >= MAX_UTTERANCE_SECONDS:
                            if voiced_frames:
                                utterance_pcm = b"".join(voiced_frames)
                                _submit_utterance(utterance_pcm)
                                voiced_frames = []
                                in_utterance = False
                            silent_frames = 0

                    # Controllo timeout di stop
                    if stopping and stop_time is not None and (time.time() - stop_time) > STOP_TIMEOUT:
                        logger.warning("Supervisor stop timeout exceeded (%.1fs), forcing exit", STOP_TIMEOUT)
                        if proc is not None:
                            with contextlib.suppress(Exception):
                                proc.kill()
                        break

            finally:
                # Flush any remaining utterance at the end
                if in_utterance and voiced_frames:
                    _submit_utterance(b"".join(voiced_frames))
                total = next(seq_counter)
                executor.shutdown(wait=True)
                sequencer.drain_and_stop(total)
                state["active"] = False
                _write_state(state)
                try:
                    status.write_status(status.STATE_IDLE, service="stream")
                except Exception:
                    logger.debug("impossibile aggiornare lo status su IDLE", exc_info=True)
                cur = _read_lock()
                if cur is None or cur.get("pid") == os.getpid():
                    STREAM_LOCK_PATH.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    shutil.rmtree(session_dir, ignore_errors=True)
        finally:
            if proc is not None:
                with contextlib.suppress(Exception):
                    proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
                if proc.poll() is None:
                    with contextlib.suppress(Exception):
                        proc.kill()
                    with contextlib.suppress(Exception):
                        proc.wait(timeout=5)
        return 0

    # -- clipboard ------------------------------------------------------

    def paste_next(self) -> bool:
        """Incolla il prossimo chunk e avanza l'indice (D3, D5)."""
        state = read_state()
        chunks = state.get("chunks")
        if not isinstance(chunks, list):
            return False
        try:
            idx = int(state.get("next_chunk_index", 0))
        except (TypeError, ValueError):
            idx = 0
        if idx < 0 or idx >= len(chunks):
            return False
        chunk = chunks[idx]
        if not isinstance(chunk, str) or not chunk.strip():
            return False  # D3: chunk vuoto scartato, indice invariato
        if _command_norm(chunk) in parse_blacklist(self._stream.blacklist):
            state["next_chunk_index"] = idx + 1
            state.update(_paste_state(self._stream))
            # preserve_chunks: nessuna pausa e' avvenuta fra lettura e
            # scrittura, ma il supervisore puo' aver committato un chunk in
            # mezzo e questo stato e' gia' invecchiato: si scrive comunque
            # sull'elenco riletto, non sulla copia locale.
            _write_state(state, preserve_chunks=True)
            return False  # stale state defense; no clipboard or pacing update
        # pacing (D5)
        try:
            delay = max(0, int(self._stream.paste_delay_ms or 0)) / 1000.0
        except (TypeError, ValueError):
            delay = 0.0
        last = state.get("last_paste_at")
        if delay and isinstance(last, (int, float)) and not isinstance(last, bool):
            wait = delay - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
        # Unica chiamata a processo esterno rimasta FUORI da un try: con
        # wl-copy/wl-paste assente (sessione Wayland senza compositor), in
        # timeout o con exit != 0, la subprocess.run(check=True) di
        # clipboard.write_text solleva e l'eccezione usciva da paste_next
        # senza toccare lo stato: il chunk non veniva incollato e la coda si
        # fermava, senza che nulla fosse registrato. Comportamento scelto:
        # NON si avanza l'indice (il chunk resta in coda, nessuna perdita di
        # testo) e si restituisce False come un incolla non riuscito, cosi' il
        # chiamante CLI lo tratta come fallimento. Il risultato e' ambiguo come
        # nel ramo "paste incompleto" dell'estensione: se wl-copy avesse
        # scritto prima di fallire, un ritentativo potrebbe duplicare, quindi
        # il caso viene loggato perche' l'utente sappia di dover verificare il
        # campo di destinazione prima di riprovare.
        try:
            clipboard.write_text(chunk, self._cfg.clipboard_tool)
        except Exception as exc:  # noqa: BLE001 - wl-copy puo' fallire in molti modi
            # (assente, timeout, exit!=0): tutti devono lasciare il chunk in coda, non
            # far propagare un'eccezione che fermerebbe paste_next senza registrare nulla.
            logger.error("clipboard write fallito per il chunk %d: %s", idx, exc)
            return False
        state["next_chunk_index"] = idx + 1
        state["last_paste_at"] = time.time()
        state.update(_paste_state(self._stream))
        # preserve_chunks=True: fra la lettura iniziale e questa scrittura puo'
        # essere passato quasi un paste_delay_ms (250ms) di sleep di pacing, e in
        # quella finestra il supervisore puo' aver committato chunk. Scrivendo
        # `state` — che contiene la COPIA di chunks letta prima — quei chunk
        # verrebbero sovrascritti e mai incollati (misurato: 1 chunk perso).
        # _write_state ricarica chunks da disco e conserva l'indice: la
        # posizione letta prima e' ancora valida perche' ingest() appende.
        _write_state(state, preserve_chunks=True)
        return True

    # -- util -----------------------------------------------------------

    def _record_history(self, text: str) -> None:
        try:
            output_history.append_entry("stream", "raw", text,
                                        self._cfg.history_max_entries)
        except Exception:
            logger.debug("impossibile registrare in cronologia", exc_info=True)


# ---------------------------------------------------------------- CLI hook

def main(argv: list[str] | None = None) -> int:
    """Punto d'ingresso per il supervisore staccato:
    `python -m bravoric_stt_clipboard.stream --supervise <session_id>`."""
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "--supervise":
        session_id = args[1] if len(args) > 1 else uuid.uuid4().hex
        try:
            cfg = load_config()
        except ConfigError as exc:
            logger.error("supervisor: config error: %s", exc)
            return 1
        return StreamSession(cfg)._run_supervisor(session_id)
    print("usage: python -m bravoric_stt_clipboard.stream --supervise <session_id>",
          file=sys.stderr)
    return 1


# D4 (STT<->stream, esclusione reciproca): CHIUSA. La guardia vive in
# stt.py:41 (_is_stream_active(), simmetrica a StreamSession.start() che
# controlla audio.is_recording() qui sotto), coperta da test-backend.py
# "giro 18: P4 esclusione reciproca STT <-> streaming". Questo TODO era
# rimasto stantio dopo che il fix era gia' atterrato (mandato, ciclo 5):
# verificato leggendo stt.py, non solo per assenza di occorrenze.


if __name__ == "__main__":
    sys.exit(main())
