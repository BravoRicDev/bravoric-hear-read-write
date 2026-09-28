"""Caricamento configurazione modulare (fail-fast su config mancante)."""
from __future__ import annotations

import math
import os
import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .i18n import _

CONFIG_PATH_USER = Path.home() / ".config" / "bravoric-stt-clipboard" / "config.toml"
_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _example_config_path() -> Path:
    """Fallback usato solo se scripts/install.sh non ha ancora creato la
    config utente. Stessa logica di rilevamento lingua di install.sh."""
    lang = os.environ.get("LANGUAGE") or os.environ.get("LC_ALL") \
        or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    if lang.startswith("it"):
        return _CONFIG_DIR / "config.example.it.toml"
    return _CONFIG_DIR / "config.example.toml"


CONFIG_PATH_EXAMPLE = _example_config_path()


DEFAULT_STORAGE_BASE_DIR = "~/.local/share/bravoric-stt-clipboard/history"


class ConfigError(RuntimeError):
    pass


# P3: prompt di DEFAULT applicato quando la chiave `prompt` manca O e' vuota,
# in [stt] e in [stream]. Il testo vive qui, nel CODICE, e non solo nei file
# di esempio: chi scrive una config a mano (o la GUI, che puo' riscrivere
# prompt = "") non ereditava nessun contesto, e senza contesto `prompt` non
# viaggiava e il ramo del vocabolario non partiva mai.
# Il testo e' quello italiano gia' presente in config/config.example.it.toml
# (riga [stt].prompt). Una riga sola da cambiare se si preferisce un'altra
# lingua.
DEFAULT_PROMPT = (
    "Contesto di dettatura in italiano. Trascrivi fedelmente le parole pronunciate, "
    "mantenendo nomi propri, termini tecnici e la lingua originale; usa punteggiatura "
    "naturale senza parafrasare. Parole frequenti: dettatura, riconoscimento vocale, "
    "appunti, trascrizione."
)


@dataclass
class FallbackLevel:
    name: str
    endpoint: str
    model: str
    api_key_env: str
    api_key: str
    ca_cert: str
    timeout_seconds: int
    hotwords_in_prompt: bool = False
    # Appeso in CODA (non dopo timeout_seconds) per non spostare
    # hotwords_in_prompt, che i test costruiscono per posizione:
    # test-backend.py:1540 -> FallbackLevel(..., 1, True) dove True e'
    # hotwords_in_prompt. Inserirlo qui farebbe silenziosamente attribuire
    # quell'argomento a `parallel` (vedi CONTRATTO-PARALLEL sez. 2).
    parallel: bool = False
    # Slot CONCORRENTI per endpoint, appendesi in CODA dopo `parallel`
    # (SPEC-MAX-CONCURRENCY sez. "Campi"): vale per TUTTI i livelli, non solo
    # per quelli con parallel = true. Non e' un contatore globale di worker:
    # whisper.cpp serializza dietro un mutex interno, quindi dichiarare gli
    # slot rende esplicito cosa si guadagna e cosa no. Clamp 1..8, default 3,
    # MAI AUTO. Anche in percorso sequenziale e' il tetto di richieste
    # contemporanee per endpoint: il gate e' applicato alla catena su tutta
    # la lista dei livelli, quindi il numero vale anche con dispatch_mode =
    # "sequential" e con auto a zero livelli paralleli. Non cambia pero' il
    # numero di worker, che resta quello di max_concurrent_chunks: il gate
    # LIMITA, non autorizza concorrenza.
    max_concurrency: int = 3

    def resolved_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        if not self.api_key_env:
            return ""
        return os.environ.get(self.api_key_env, "")

    def ca_cert_path(self) -> str | None:
        if not self.ca_cert:
            return None
        return str(Path(self.ca_cert).expanduser())

    def is_configured(self) -> bool:
        return bool(self.endpoint and self.model)


def endpoint_key(level: FallbackLevel) -> str:
    """Chiave di identita' stabile di un endpoint (CONTRATTO-PARALLEL sez. 1).

    NON usare solo host:port: i livelli 1 e 2 della config utente sono ENTRAMBI
    `http://10.9.0.2:4001/v1` con modelli diversi, quindi una chiave host:port
    collasserebbe due livelli indipendenti e un solo fallimento escluderebbe
    entrambi. Fonte unica per breaker, lease e persistenza.

    Il `rstrip("/")` e' applicato PRIMA del join, non dopo: normalizzare
    l'intera chiave lascerebbe che il slash finale resti nel model e
    produrrebbe `h:4001/v1|model/` invece di `h:4001/v1|model`. Senza questa
    normalizzazione `http://h:4001/v1` e `http://h:4001/v1/` sarebbero DUE
    chiavi: il breaker si spacca in due, un endpoint rotto sfugge al cooldown e
    le due sue occorrenze non si escludono a vicenda. Stessa normalizzazione di
    api_client.py, cosi' la funzione resta la fonte unica anche per stream.py.
    """
    return f"{level.endpoint.rstrip('/')}|{level.model}"


@dataclass
class AudioConfig:
    format: str
    codec: str
    sample_rate: int
    bitrate_kbps: int
    toggle_debounce_seconds: float
    retry_on_error: bool
    retry_count: int


@dataclass
class CleanupConfig:
    enabled: bool
    system_prompt: str
    fallback: list[FallbackLevel] = field(default_factory=list)


@dataclass
class STTConfig:
    """Sezione [stt]: configurazione della trascrizione STT."""
    language: str = "it"
    prompt: str = DEFAULT_PROMPT
    hotwords: str = ""
    fallback: list[FallbackLevel] = field(default_factory=list)


STREAM_MODES = ("at_end", "per_chunk")

# Interruttore GLOBALE del percorso per-chunk. "auto" e' il default ed e'
# esattamente il comportamento di prima del toggle: se almeno un livello ha
# `parallel = true` parte il dispatcher, altrimenti si degrada a sequenziale.
# "sequential" IGNORA tutti i flag per-livello e usa la catena sequenziale su
# TUTTI i livelli. Non e' l'inverso di `FallbackLevel.parallel`: quel flag dice
# "partecipa al pool", questo dice "usa il pool, o no".
STREAM_DISPATCH_MODES = ("auto", "sequential")


@dataclass
class StreamCommand:
    keyword: str
    action: Literal["key", "delete"]
    key: str = ""
    scope: str = ""
    ends_session: bool = False
    aliases: list[str] = field(default_factory=list)

    @property
    def all_phrases(self) -> list[str]:
        phrases: list[str] = []
        seen: set[str] = set()
        for phrase in [self.keyword, *self.aliases]:
            normalized = _command_norm(phrase)
            if normalized and normalized not in seen:
                seen.add(normalized)
                phrases.append(phrase)
        return phrases


COMMAND_KEYS = frozenset({"Return", "Enter", "Tab", "space", "Escape", "BackSpace", "Delete", "Home", "End", "Page_Up", "Page_Down", "Left", "Right", "Up", "Down", *(f"F{i}" for i in range(1, 13))})


def _command_norm(value: str) -> str:
    return unicodedata.normalize("NFC", re.sub(r"[\s.,;:!?…]+$", "", value.strip())).lower()


def parse_blacklist(raw: object) -> frozenset[str]:
    """Parse comma-separated whole-chunk phrases using command normalization."""
    values = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return frozenset(
        normalized for value in values if isinstance(value, str)
        if (normalized := _command_norm(value))
    )


def _parse_stream_commands(raw: object) -> list[StreamCommand]:
    if not isinstance(raw, list):
        raise TypeError("stream.command must be an array of tables")
    result, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise TypeError("each stream.command must be a table")
        keyword = entry.get("keyword")
        action = entry.get("action")
        ends = entry.get("ends_session", False)
        if not isinstance(keyword, str) or not keyword.strip():
            raise ValueError("command keyword must be a non-empty string")
        if not isinstance(ends, bool):
            raise TypeError("command ends_session must be boolean")
        normalized = _command_norm(keyword)
        if not normalized:
            raise ValueError(f"command keyword has no usable text: {keyword!r}")
        raw_aliases = entry.get("aliases", [])
        if not isinstance(raw_aliases, list) or any(not isinstance(alias, str) for alias in raw_aliases):
            raise TypeError("command aliases must be an array of strings")
        aliases: list[str] = []
        local_seen = {normalized}
        for alias in raw_aliases:
            alias_norm = _command_norm(alias)
            if not alias_norm:
                raise ValueError(f"command alias has no usable text: {alias!r}")
            if alias_norm not in local_seen:
                local_seen.add(alias_norm)
                aliases.append(alias)
        for phrase_norm in local_seen:
            if phrase_norm in seen:
                raise ValueError(f"duplicate command phrase: {phrase_norm!r}")
        seen.update(local_seen)
        if action == "key":
            key = entry.get("key")
            if not isinstance(key, str) or key not in COMMAND_KEYS:
                raise ValueError(f"invalid command key: {key!r}")
            result.append(StreamCommand(keyword, action, key, "", ends, aliases))
        elif action == "delete":
            scope = entry.get("scope")
            if scope not in ("word", "chunk"):
                raise ValueError(f"invalid delete scope: {scope!r}")
            result.append(StreamCommand(keyword, action, "", scope, ends, aliases))
        else:
            raise ValueError(f"invalid command action: {action!r}")
    return result


@dataclass
class StreamConfig:
    """Sezione [stream]: dettatura con incolla diretto (feature sperimentale)."""
    mode: Literal["at_end", "per_chunk"]
    silence_seconds: float
    noise_db: float
    min_utterance_seconds: float
    max_utterance_seconds: float
    paste_delay_ms: int
    language: str = "it"
    prompt: str = DEFAULT_PROMPT
    hotwords: str = ""
    context_enabled: bool = True
    fallback: list[FallbackLevel] = field(default_factory=list)
    max_concurrent_chunks: int = 3
    chunk_timeout_seconds: float = 30.0
    # Margine (dB) sopra il noise floor adattivo stimato dal VAD. Appeso in
    # coda per non spostare i campi posizionali già usati dai test/costruttori.
    vad_margin_db: float = 6.0
    commands: list[StreamCommand] = field(default_factory=list)
    paste_shortcut: str = "ctrl+v"
    blacklist: str = ""
    paste_channel: str = "clipboard"
    # --- campi appendesi in CODA (vad_margin_db sopra e' il precedente) ---
    # max_concurrent_chunks resta 3 di default: e' pinnato da test-backend.py
    # ("StreamConfig positional keeps appended-field defaults") e NON diventa 0.
    # "Sono in AUTO" e' questo flag; il parser lo imposta True quando la chiave
    # e' assente o vale 0, lasciando max_concurrent_chunks == 0 (lo=0).
    max_concurrent_chunks_auto: bool = False
    # 0 = breaker disabilitato. Valore non finito/non parsabile -> 3600.0.
    endpoint_cooldown_seconds: float = 3600.0
    # Interruttore globale parallelo/sequenziale, APPESO IN CODA dopo
    # endpoint_cooldown_seconds per non spostare nessun campo posizionale
    # (StreamConfig e' costruito per posizione dai test). Default "auto" =
    # retrocompatibilita' esatta con la config di oggi.
    dispatch_mode: str = "auto"
    # Ritenzione del log JSONL dei chunk, in RIGHE (non in orari: il file e'
    # uno strumento di debug, non un archivio). 0 o chiave assente = default
    # del modulo chunk_log (2000). APPESA IN CODA per non spostare i campi
    # posizionali, come tutti i campi aggiunti dopo.
    chunk_log_max_lines: int = 0
    # Ex costanti di modulo, ora regolabili (GUI: pagina Streaming).
    # Former module constants, now tunable (GUI: Streaming page).
    # prompt_max_chars: tetto del prompt inviato a Whisper (api_client).
    # prompt_max_chars: cap on the prompt sent to Whisper (api_client).
    prompt_max_chars: int = 800
    # Finestra (in frame da ~30 ms) per stimare il noise floor del VAD e
    # minimo di frame prima di fidarsi della stima.
    # Window (in ~30 ms frames) to estimate the VAD noise floor, and the
    # minimum number of frames before the estimate is trusted.
    vad_floor_window_frames: int = 100
    vad_min_floor_frames: int = 20


@dataclass
class NotificationEvent:
    enabled: bool
    content: bool


@dataclass
class ServiceNotifications:
    processing_start: bool
    raw_ready: NotificationEvent
    cleanup_ready: NotificationEvent
    # Ogni notifica del backend ha il suo interruttore (GUI: pagina
    # Notifiche). Default True = comportamento di sempre per chi non li
    # ha nel config.toml. `recording_start` vale per stt, `session_end`
    # per stream: per gli altri servizi restano inerti.
    error: bool = True
    recording_start: bool = True
    session_end: bool = True


@dataclass
class RetentionPolicy:
    enabled: bool
    retention_hours: int


@dataclass
class StorageConfig:
    base_dir: str
    stt_original: RetentionPolicy
    stt_raw: RetentionPolicy
    stt_clean: RetentionPolicy
    ocr_original: RetentionPolicy
    ocr_raw: RetentionPolicy
    ocr_clean: RetentionPolicy


@dataclass
class IconSlot:
    key: str
    label: str
    meaning: str
    owner: str
    fallback_category: str


ICON_SLOT_REGISTRY = (
    IconSlot("stt_start", "STT — Processing started", "STT processing start (not recording start)", "backend", "processing"),
    IconSlot("stt_raw", "STT — Raw text ready", "STT raw text ready", "backend", "ready"),
    IconSlot("stt_clean", "STT — Cleaned text ready", "STT cleaned text ready", "backend", "ready"),
    IconSlot("ocr_start", "OCR — Processing started", "OCR processing start", "backend", "processing"),
    IconSlot("ocr_raw", "OCR — Raw text ready", "OCR raw extraction ready", "backend", "ready"),
    IconSlot("ocr_clean", "OCR — Cleaned text ready", "OCR cleaned extraction ready", "backend", "ready"),
    IconSlot("stt_recording_start", "STT — Recording started", "STT recording began", "backend", "recording"),
    IconSlot("stream_session_start", "Stream — Session started", "Stream listening session began", "backend", "recording"),
    IconSlot("stream_processing_start", "Stream — Transcribing", "Stream transcription in progress (at-end mode)", "backend", "processing"),
    IconSlot("stream_session_end", "Stream — Session ended", "Stream session ended", "backend", "ready"),
    IconSlot("stream_chunk_delivered", "Stream — Text delivered", "Stream transcription text delivered", "backend", "ready"),
    IconSlot("error_general", "General — Error", "Backend error without reliable narrower classification", "backend", "error"),
)
ICON_SLOT_KEYS = tuple(slot.key for slot in ICON_SLOT_REGISTRY)


@dataclass
class IconsConfig:
    stt_start: str = ""
    stt_raw: str = ""
    stt_clean: str = ""
    ocr_start: str = ""
    ocr_raw: str = ""
    ocr_clean: str = ""
    stt_recording_start: str = ""
    stream_session_start: str = ""
    stream_processing_start: str = ""
    stream_session_end: str = ""
    stream_chunk_delivered: str = ""
    error_general: str = ""


@dataclass
class Config:
    notifications: bool
    notif_stt: ServiceNotifications
    notif_ocr: ServiceNotifications
    notif_stream: ServiceNotifications
    clipboard_tool: str
    clipboard_paste_tool: str
    audio: AudioConfig
    stt: STTConfig
    stt_cleanup: CleanupConfig
    ocr_fallback: list[FallbackLevel]
    ocr_system_prompt: str
    ocr_cleanup: CleanupConfig
    double_injection: bool
    storage: StorageConfig
    history_max_entries: int
    icons: IconsConfig
    stream: StreamConfig
    # Se True, OCR scatta prima uno screenshot interattivo (gnome-screenshot
    # -a) invece di leggere un'immagine già presente in clipboard. Default
    # False: comportamento di sempre, invariato per chi non lo attiva.
    ocr_capture_screenshot: bool = False
    # Ex costanti di modulo ora regolabili da config.toml e GUI (pagina
    # General). Default = valori di sempre. APPESI in coda (Config e'
    # costruito anche per keyword dai test).
    # Former module constants, now tunable from config.toml and the GUI
    # (General page). Defaults = the historical values. Appended at the end
    # (tests also build Config by keyword).
    clipboard_timeout_seconds: float = 5.0        # wl-copy / wl-paste
    notify_timeout_seconds: float = 10.0          # notify-send
    notification_content_max_chars: int = 80      # testo nel corpo / text in body
    cleanup_min_length_ratio: float = 0.7         # 0 = nessun controllo / no check
    screenshot_timeout_seconds: float = 120.0     # selezione area / area selection
    # Retrocompatibilità: stt_fallback è un alias di stt.fallback
    stt_fallback: list[FallbackLevel] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.stt_fallback = self.stt.fallback


def _parse_fallback_list(raw: list[dict]) -> list[FallbackLevel]:
    levels = []
    for entry in raw:
        try:
            timeout = int(entry.get("timeout_seconds", 60))
        except (TypeError, ValueError, OverflowError):
            # OverflowError: un `timeout_seconds = inf` in TOML fa sollevare
            # int() con OverflowError (non una ValueError) e il livello non
            # veniva costruito. Stessa risposta degli altri due casi: default.
            timeout = 60
        if timeout <= 0:
            timeout = 60
        level = FallbackLevel(
            name=entry.get("name", ""),
            endpoint=entry.get("endpoint", ""),
            model=entry.get("model", ""),
            api_key_env=entry.get("api_key_env", ""),
            api_key=entry.get("api_key", ""),
            ca_cert=entry.get("ca_cert", ""),
            timeout_seconds=timeout,
            hotwords_in_prompt=(
                entry.get("hotwords_in_prompt", False)
                if isinstance(entry.get("hotwords_in_prompt", False), bool)
                else str(entry.get("hotwords_in_prompt", "false")).lower() in ("true", "1", "yes")
            ),
            parallel=_coerce_bool(entry.get("parallel", False)),
            max_concurrency=_coerce_max_concurrency(
                entry.get("max_concurrency"), 3, 1, 8
            ),
        )
        if level.is_configured():
            levels.append(level)
    return levels


def _coerce_bool(raw: object) -> bool:
    """Converte un valore TOML in bool senza mai applicare la verita' di Python.

    `bool("false")` e' True: una stringa sbagliata attiverebbe il livello
    parallelo (e con essa il dispatcher) per un semplice refuso. Accettiamo
    solo i token di verita' espliciti; tutto il resto (float, "yes", "on",
    stringhe vuote, None, liste) resta False.

    Nota: la coerzione di `context_enabled` accetta anche "yes"; qui no, per
    rispettare FINAL-PLAN-PARALLEL sez. D, test 3 (`parallel = "yes"` ->
    False). I due campi restano coerenti sui casi che contano davvero: il
    bool vero e False, e le stringhe "false"/"0" che non diventano mai True.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return raw == 1
    return False


def _coerce_int(raw: object, default: int, lo: int, hi: int) -> int:
    """Converte un valore TOML in int, con fallback e clamp sui limiti."""
    try:
        value = int(raw)  # type: ignore[call-overload]  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return default
    # OverflowError: TOML ammette `inf`, `nan`, `inf` e `-inf` come float.
    # int(float('inf')) solleva OverflowError, che NON e' una ValueError:
    # senza questo ramo sfuggiva dal try e attraversava load_config() come
    # traceback grezzo invece di diventare ConfigError (misurato: 2 righe,
    # [stream].max_concurrent_chunks = inf e [stream].paste_delay_ms = inf).
    # `nan` solleva invece ValueError, gia' coperto sopra.
    except OverflowError:
        return default
    return max(lo, min(hi, value))


def _coerce_max_concurrency(raw: object, default: int, lo: int, hi: int) -> int:
    """Coerzione STRETTA del numero di slot per endpoint (SPEC-MAX-CONCURRENCY).

    Diversamente da `_coerce_int`, che accetterebbe `int(3.9) == 3` e
    `int(True) == 1`, qui un valore non intero e' un errore di config e torna al
    default invece di essere silenziosamente troncato:

      assente / "" / "x" / float / NaN / inf / bool  -> default
      intero 1..8 rispettato, <1 -> lo, >8 -> hi

    `bool` non e' un intero valido: in TOML `max_concurrency = true` e' un
    errore di battitura, non "un endpoint che accetta 1 richiesta".

    Le stringhe numeriche SONO accettate perche' il percorso GUI scrive
    `max_concurrency = "3"` (config_editor._toml_line_value mette fra virgolette
    ogni campo non elencato esplicitamente): rifiutarle romperebbe il
    round-trip prefs.js -> config.toml senza avvisare. Il confronto resta
    esplicito, mai `bool(str)`: in Python `bool("false")` e' True, trappola
    gia' pagata su `parallel`.
    """
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return max(lo, min(hi, raw))
    if isinstance(raw, float):
        # float/NaN/inf non sono slot: intero solo se matematicamente esatto.
        if not math.isfinite(raw) or not raw.is_integer():
            return default
        return max(lo, min(hi, int(raw)))
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return default
        try:
            value = int(text)
        except ValueError:
            return default
        return max(lo, min(hi, value))
    return default


def _coerce_float_clamped(raw: object, default: float, lo: float, hi: float) -> float:
    """Converte un valore TOML in float con fallback, clamp [lo, hi] e guardia
    NaN/infinito. Valore invalido o non finito -> default; valore finito ->
    clampato nell'intervallo."""
    try:
        value = float(raw)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return max(lo, min(hi, value))


def _parse_stt_config(raw_stt: dict) -> STTConfig:
    """Parse la sezione [stt] del TOML, con supporto legacy."""
    language = str(raw_stt.get("language", "it"))
    # P3: la chiave manca O e' vuota -> default. Serve qui, non solo sul
    # dataclass: `raw.get("prompt", "")` passava "" esplicito e SOSTITUIVA
    # il default del dataclass, rendendolo vuoto proprio nel caso che il
    # default doveva coprire (config scritta a mano, o prefs.js che riscrive
    # prompt = ""). Da qui `or DEFAULT_PROMPT`: stringa vuota o assente
    # cadono entrambe sul default. Nota sul tradeoff: dopo questo, non c'e'
    # piu' modo di DIRE "nessun contesto" con prompt = "" (bisogna togliere
    # la chiave, che pero' riporta al default). E' la scelta del brief
    # ("se l'utente non ha impostato un prompt, si usa quello di default"):
    # il caso "nessun prompt" e' il caso normale, non l'eccezione, e per
    # disattivare il contesto esiste gia' context_enabled=false.
    prompt = str(raw_stt.get("prompt") or DEFAULT_PROMPT)
    hotwords = str(raw_stt.get("hotwords", ""))
    fallback = _parse_fallback_list(raw_stt.get("fallback", []))
    return STTConfig(
        language=language, prompt=prompt, hotwords=hotwords, fallback=fallback,
    )


def _build_config(raw: dict) -> Config:
    try:
        general = raw.get("general", {})
        notif_raw = raw.get("notifications", {})
        audio_raw = raw.get("audio", {})
        stt_cleanup_raw = raw.get("stt_cleanup", {})
        ocr_raw = raw.get("ocr", {})
        ocr_cleanup_raw = raw.get("ocr_cleanup", {})
        clipboard_raw = raw.get("clipboard", {})
        storage_raw = raw.get("storage", {})
        history_raw = raw.get("history", {})
        icons_raw = raw.get("icons", {})
        stream_raw = raw.get("stream", {})
        stt_raw = raw.get("stt", {})
        stream_mode = stream_raw.get("mode", "per_chunk")
        if stream_mode not in STREAM_MODES:
            raise ValueError(
                f"[stream].mode must be one of {STREAM_MODES}, got {stream_mode!r}"
            )

        # dispatch_mode: stesso trattamento di `mode` — un valore fuori enum
        # renderebbe la config non caricabile per tutto il plugin, quindi
        # meglio un errore LOCALE e leggibile che un fallback silenzioso a un
        # comportamento diverso da quello richiesto. L'assenza della chiave
        # resta "auto" (comportamento di oggi).
        dispatch_mode = stream_raw.get("dispatch_mode", "auto")
        if not isinstance(dispatch_mode, str) or dispatch_mode not in STREAM_DISPATCH_MODES:
            raise ValueError(
                f"[stream].dispatch_mode must be one of {STREAM_DISPATCH_MODES}, "
                f"got {dispatch_mode!r}"
            )

        # max_concurrent_chunks: lo=0 perche' 0 (o chiave assente) significa
        # AUTOMATICO. Il valore grezzo 0 resta 0 e viene marcato dal flag:
        # StreamConfig.max_concurrent_chunks resta 3 come default, quindi un
        # costruttore/manuale conserva la semantica fissa di oggi. 1..8 e'
        # override esplicito, 9+ viene clampato a 8. Valore non parsabile
        # (es. "abc") cade sul default 0 -> AUTO.
        max_chunks_raw = _coerce_int(stream_raw.get("max_concurrent_chunks"), 0, 0, 8)

        # Gestione legacy: se [stt] non esiste ma c'è [[stt.fallback]],
        # usa i default per language/prompt/hotwords e popola fallback.
        stt_cfg = _parse_stt_config(stt_raw) if stt_raw else STTConfig(
            language="it", prompt="", hotwords="",
            fallback=_parse_fallback_list(raw.get("stt", {}).get("fallback", [])),
        )

        def retention(key: str) -> RetentionPolicy:
            section = storage_raw.get(key, {})
            return RetentionPolicy(
                enabled=section.get("enabled", False),
                retention_hours=int(section.get("retention_hours", 0)),
            )

        def service_notif(prefix: str) -> ServiceNotifications:
            return ServiceNotifications(
                processing_start=notif_raw.get(f"{prefix}_on_processing_start", True),
                raw_ready=NotificationEvent(
                    enabled=notif_raw.get(f"{prefix}_on_raw_ready", True),
                    content=notif_raw.get(f"{prefix}_on_raw_ready_content", True),
                ),
                cleanup_ready=NotificationEvent(
                    enabled=notif_raw.get(f"{prefix}_on_cleanup_ready", True),
                    content=notif_raw.get(f"{prefix}_on_cleanup_ready_content", True),
                ),
                # _coerce_bool: la stringa "false" e' truthy in Python.
                error=_coerce_bool(notif_raw.get(f"{prefix}_on_error", True)),
                recording_start=_coerce_bool(notif_raw.get(f"{prefix}_on_recording_start", True)),
                session_end=_coerce_bool(notif_raw.get(f"{prefix}_on_session_end", True)),
            )

        cfg = Config(
            # _coerce_bool: la stringa "false" e' truthy in Python.
            # _coerce_bool: the string "false" is truthy in Python.
            notifications=_coerce_bool(general.get("notifications", True)),
            notif_stt=service_notif("stt"),
            notif_ocr=service_notif("ocr"),
            notif_stream=service_notif("stream"),
            clipboard_tool=general.get("clipboard_tool", "wl-copy"),
            clipboard_paste_tool=general.get("clipboard_paste_tool", "wl-paste"),
            audio=AudioConfig(
                format=audio_raw.get("format", "ogg"),
                codec=audio_raw.get("codec", "libopus"),
                sample_rate=int(audio_raw.get("sample_rate", 16000)),
                bitrate_kbps=int(audio_raw.get("bitrate_kbps", 16)),
                toggle_debounce_seconds=max(0.1, float(audio_raw.get("toggle_debounce_seconds", 1))),
                retry_on_error=_coerce_bool(audio_raw.get("retry_on_error", True)),
                retry_count=int(audio_raw.get("retry_count", 2)),
            ),
            stt=stt_cfg,
            stt_cleanup=CleanupConfig(
                enabled=stt_cleanup_raw.get("enabled", True),
                system_prompt=stt_cleanup_raw.get("system_prompt", ""),
                fallback=_parse_fallback_list(stt_cleanup_raw.get("fallback", [])),
            ),
            ocr_fallback=_parse_fallback_list(raw.get("ocr", {}).get("fallback", [])),
            ocr_system_prompt=ocr_raw.get("system_prompt", ""),
            # _coerce_bool, non il valore grezzo: la stringa "false" e' truthy
            # in Python e attiverebbe l'apertura di gnome-screenshot ad ogni
            # pressione per un refuso (o per un config.toml scritto prima
            # che config_editor serializzasse questo campo come bool vero).
            ocr_capture_screenshot=_coerce_bool(ocr_raw.get("capture_screenshot", False)),
            clipboard_timeout_seconds=_coerce_float_clamped(
                general.get("clipboard_timeout_seconds"), 5.0, 1.0, 60.0),
            notify_timeout_seconds=_coerce_float_clamped(
                general.get("notify_timeout_seconds"), 10.0, 1.0, 60.0),
            notification_content_max_chars=_coerce_int(
                general.get("notification_content_max_chars"), 80, 10, 500),
            cleanup_min_length_ratio=_coerce_float_clamped(
                general.get("cleanup_min_length_ratio"), 0.7, 0.0, 1.0),
            screenshot_timeout_seconds=_coerce_float_clamped(
                ocr_raw.get("screenshot_timeout_seconds"), 120.0, 5.0, 600.0),
            ocr_cleanup=CleanupConfig(
                enabled=ocr_cleanup_raw.get("enabled", False),
                system_prompt=ocr_cleanup_raw.get("system_prompt", ""),
                fallback=_parse_fallback_list(ocr_cleanup_raw.get("fallback", [])),
            ),
            double_injection=_coerce_bool(clipboard_raw.get("double_injection", True)),
            storage=StorageConfig(
                # `or DEFAULT`: una stringa vuota esplicita (config a mano, campo
                # svuotato) diventerebbe Path("") = cwd del processo, cioe' di
                # norma $HOME: audio e testo dettato salvati alla rinfusa li',
                # in silenzio. Assente e vuoto cadono entrambi sul default.
                base_dir=(str(storage_raw.get("base_dir", "")).strip() or DEFAULT_STORAGE_BASE_DIR),
                stt_original=retention("stt_original"),
                stt_raw=retention("stt_raw"),
                stt_clean=retention("stt_clean"),
                ocr_original=retention("ocr_original"),
                ocr_raw=retention("ocr_raw"),
                ocr_clean=retention("ocr_clean"),
            ),
            history_max_entries=int(history_raw.get("max_entries", 20)),
            icons=IconsConfig(**{key: str(icons_raw.get(key, "")) for key in ICON_SLOT_KEYS}),
            stream=StreamConfig(
                mode=stream_mode,
                silence_seconds=_coerce_float_clamped(stream_raw.get("silence_seconds"), 0.7, 0.1, 10.0),
                noise_db=_coerce_float_clamped(stream_raw.get("noise_db"), -30.0, -100.0, 0.0),
                min_utterance_seconds=_coerce_float_clamped(stream_raw.get("min_utterance_seconds"), 0.4, 0.05, 60.0),
                max_utterance_seconds=_coerce_float_clamped(stream_raw.get("max_utterance_seconds"), 30.0, 1.0, 300.0),
                paste_delay_ms=int(stream_raw.get("paste_delay_ms", 250)),
                language=str(stream_raw.get("language", "it")),
                prompt=str(stream_raw.get("prompt") or DEFAULT_PROMPT),
                hotwords=str(stream_raw.get("hotwords", "")),
                context_enabled=(
                    stream_raw.get("context_enabled", True)
                    if isinstance(stream_raw.get("context_enabled", True), bool)
                    else str(stream_raw.get("context_enabled", "true")).lower() in ("true", "1", "yes")
                ),
                fallback=_parse_fallback_list(stream_raw.get("fallback", [])),
                max_concurrent_chunks=max_chunks_raw,
                max_concurrent_chunks_auto=max_chunks_raw == 0,
                chunk_timeout_seconds=_coerce_float_clamped(stream_raw.get("chunk_timeout_seconds"), 30.0, 1.0, 600.0),
                vad_margin_db=_coerce_float_clamped(stream_raw.get("vad_margin_db"), 6.0, 0.0, 20.0),
                commands=_parse_stream_commands(stream_raw.get("command", [])),
                paste_shortcut=(
                    stream_raw.get("paste_shortcut", "ctrl+v").lower()
                    if isinstance(stream_raw.get("paste_shortcut", "ctrl+v"), str)
                    and stream_raw.get("paste_shortcut", "ctrl+v").lower() in ("ctrl+v", "ctrl+shift+v")
                    else "ctrl+v"
                ),
                blacklist=str(stream_raw.get("blacklist", "")),
                endpoint_cooldown_seconds=_coerce_float_clamped(
                    stream_raw.get("endpoint_cooldown_seconds"), 3600.0, 0.0, 86400.0
                ),
                paste_channel=(
                    stream_raw.get("paste_channel", "clipboard").lower()
                    if isinstance(stream_raw.get("paste_channel", "clipboard"), str)
                    and stream_raw.get("paste_channel", "clipboard").lower() in ("clipboard", "type")
                    else "clipboard"
                ),
                dispatch_mode=dispatch_mode,
                chunk_log_max_lines=_coerce_int(
                    stream_raw.get("chunk_log_max_lines"), 0, 0, 1_000_000
                ),
                prompt_max_chars=_coerce_int(stream_raw.get("prompt_max_chars"), 800, 100, 4000),
                vad_floor_window_frames=_coerce_int(
                    stream_raw.get("vad_floor_window_frames"), 100, 20, 1000),
                vad_min_floor_frames=_coerce_int(
                    stream_raw.get("vad_min_floor_frames"), 20, 5, 200),
            ),
        )
        blacklist_phrases = parse_blacklist(cfg.stream.blacklist)
        command_phrases = {
            _command_norm(phrase)
            for command in cfg.stream.commands
            for phrase in [command.keyword, *command.aliases]
        }
        overlap = blacklist_phrases & command_phrases
        if overlap:
            raise ValueError(f"blacklist phrase conflicts with command: {min(overlap)!r}")
        # cfg.stt_fallback e' gia' impostato da __post_init__ (righe 374-375),
        # eseguito automaticamente durante Config(...) qui sopra: nessuna riga
        # tra la costruzione e questo punto muta cfg.stt.fallback, quindi una
        # seconda assegnazione qui era una riassegnazione ridondante dello
        # stesso valore, non una risincronizzazione.
        return cfg
    # OverflowError entra nell'elenco perche' TOML accetta `inf`/`-inf` come
    # float e int(inf) solleva OverflowError, che non e' una ValueError:
    # senza, sei campi (misurati: paste_delay_ms, history.max_entries,
    # audio.sample_rate/bitrate_kbps/retry_count e il timeout_seconds di un
    # livello) producevano un traceback grezzo invece di ConfigError. I valori
    # sono gia' protetti alla fonte dove il fallback ha senso (_coerce_int,
    # _parse_fallback_list); qui la rete di sicurezza e' per gli int() che
    # restano scoperti, cosi' l'utente riceve un errore di config leggibile
    # invece di una stack trace.
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
        raise ConfigError(
            _("Invalid value in config: {exc}").format(exc=exc)
        ) from exc


def load_config(path: Path | None = None) -> Config:
    if path is None:
        path = CONFIG_PATH_USER if CONFIG_PATH_USER.exists() else CONFIG_PATH_EXAMPLE
    if not path.exists():
        raise ConfigError(_("Config not found: {path}").format(path=path))

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            _("Invalid TOML in {path}: {exc}").format(path=path, exc=exc)
        ) from exc
    except OSError as exc:
        raise ConfigError(
            _("Cannot read {path}: {exc}").format(path=path, exc=exc)
        ) from exc

    return _build_config(raw)
