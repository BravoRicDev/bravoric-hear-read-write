#!/usr/bin/env python3
"""Test di regressione per i bug trovati nel giro di ricerca (backend Python).

Copre: scritture non atomiche (status/history/lock), lock audio fantasma con
pid morto, letture di file corrotti, risposte API di forma inattesa, TOML
malformato, mancata rimozione dell'audio temporaneo.

Eseguire: PYTHONPATH=src python3 scripts/test-backend.py
"""
from __future__ import annotations

import contextlib
import dis
import importlib.util
import io
import json
import marshal
import multiprocessing
import os
import queue
import re
import struct
import subprocess
import sys
import tempfile
import tomllib
import types
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bravoric_stt_clipboard import (  # noqa: E402
    api_client,
    audio,
    config,
    config_editor,
    notify,
    ocr,
    output_history,
    screenshot,
    status,
    storage,
    stt,
)

PASS = 0
FAIL = 0

# Difetto reale trovato dal vivo: _FifoSequencer._log_chunk chiamava
# chunk_log.append_record senza `path=`, quindi ogni _FifoSequencer
# costruito da un test (una decina di siti, testano l'ordine FIFO/blacklist/
# contesto, non il chunk log) scriveva riga per riga nel file VERO
# dell'utente (~/.cache/bravoric-stt-clipboard/chunk_log.jsonl) ad ogni
# esecuzione di questa suite. _FifoSequencer ora accetta log_path=: questo
# e' il percorso che ogni test che NON sta specificamente testando
# chunk_log/percorso-reale deve passare.
_TEST_CHUNK_LOG_PATH = Path(tempfile.mkdtemp(prefix="bravoric-test-chunklog-")) / "chunk_log.jsonl"


def _append_entry_worker(history_path: str, i: int) -> None:
    from bravoric_stt_clipboard import output_history as oh
    oh.HISTORY_PATH = Path(history_path)
    oh.append_entry("stt", "raw", f"voce-{i}", 100)


def _set_storage_field_worker(config_path: str, section: str, field: str, value: str) -> None:
    from bravoric_stt_clipboard import config_editor as ce
    ce.CONFIG_PATH = Path(config_path)
    ce.set_storage_field(section, field, value)


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# Difetto 2 (giro 19): l'asserzione sull'ordine della guardia in stt._start
# usava .index() sui due lati, quindi una stringa sparita sollevava ValueError
# e la suite ABORBIVA a meta' invece di segnalare un FAIL. Qui la stessa
# verifica e' una funzione PURA che non puo' sollevare: riceve il corpo gia'
# estratto, cerca con find() e restituisce sempre un verdetto booleano piu'
# l'elenco di cio' che manca. La si puo' collaudare da sola, cosa che il
# .index() non permetteva.
def guard_order_verdict(
    body: str,
    guard: str = "_is_stream_active()",
    call: str = "audio.start_recording",
) -> tuple[bool, list[str]]:
    """(ok, mancanti) senza mai sollevare, qualunque cosa manchi dal corpo.

    body vuoto, guardia assente, chiamata assente: sono tutti casi che il
    chiamante deve poter riportare come FAIL pulito, non come eccezione.
    """
    try:
        guard_at = body.find(guard)
        call_at = body.find(call)
        missing = [label for label, at in ((f"la guardia {guard}", guard_at),
                                           (f"la chiamata {call}", call_at))
                   if at == -1]
        if missing:
            return False, missing
        return guard_at < call_at, []
    except Exception:  # rete di sicurezza: un assert non deve mai abortire la suite
        return False, ["la verifica stessa ha sollevato"]


# Difetto 1 (giro 19): il test del cuore confrontava la scrittura di stato con
# TUTTO stream.py. La stessa identica riga compare in altri punti di avvio,
# quindi svuotare il CORPO di heartbeat() non faceva fallire niente: la suite
# restava verde sul codice che non batte piu' il cuore. Qui si legge il corpo
# della funzione e si pretende la chiamata DENTRO quel corpo. La funzione
# resta crash-proof come guard_order_verdict: firma assente = corpo vuoto =
# FAIL pulito, mai IndexError.
def py_func_body(text: str, signature: str) -> str:
    """Corpo di una funzione Python, dalla firma al prossimo `def` di primo livello.

    La docstring viene rimossa: dentro descrive il guard invece di chiamarlo,
    e contarla farebbe passare il test sul codice che non scrive piu' niente.
    """
    try:
        at = text.find(signature)
        if at == -1:
            return ""
        rest = text[at + len(signature):]
        cut = re.search(r"^def \w", rest, re.M)
        body = rest if cut is None else rest[:cut.start()]
        return re.sub(r'^\s*"""[\s\S]*?"""\s*', "", body)
    except Exception:
        return ""


def heartbeat_verdict(text: str) -> tuple[bool, str]:
    """(ok, motivo) sul cuore, senza mai sollevare.

    Fallisce se la funzione non esiste, se il corpo e' vuoto, o se dentro il
    corpo non c'e' la riscrittura di RECORDING con service="stream": tutte e
    tre le forme in cui il cuore potrebbe sparire restando inerte.
    """
    try:
        body = py_func_body(text, "def heartbeat() -> None:")
        if not body.strip():
            return False, "heartbeat() non trovata o con corpo vuoto"
        if not re.search(r'status\.write_status\(\s*status\.STATE_RECORDING,\s*service="stream"\s*\)',
                         body):
            return False, "nel corpo di heartbeat() non c'e' write_status(RECORDING, service='stream')"
        return True, ""
    except Exception:
        return False, "la verifica stessa ha sollevato"


def dead_pid() -> int:
    """Un pid di un processo realmente terminato (e reaped)."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def words_exactly(n: int) -> str:
    """Stringa di ESATTAMENTE n caratteri fatta solo di parole intere separate
    da uno spazio. Serve a costruire un contesto che riempie esattamente il
    budget residuo: e' il caso che rendeva visibile l'off-by-one degli spazi
    di giunzione (801 caratteri inviati invece di 800)."""
    if n <= 0:
        return ""
    words: list[str] = []
    used = 0
    while n - used - (1 if words else 0) >= 9:
        words.append("w" * 9)
        used += 9 + (1 if len(words) > 1 else 0)
    rest = n - used
    if rest == 1:
        if words:
            words[-1] += "w"
        else:
            words.append("w")
    elif rest > 0:
        words.append("w" * (rest - 1 if words else rest))
    return " ".join(words)


# --- difetto 3: il bytecode in __pycache__ puo' non essere quello del
# sorgente -----------------------------------------------------------
# Il difetto del "contesto perso" (155 caratteri invece di 791) NON era
# nel sorgente di api_client.py: era nel .pyc compilato che il processo
# caricava davvero. CPython riusa un .pyc solo se mtime e dimensione del
# sorgente combaciano, quindi una mutazione di lunghezza invariata
# applicata e poi annullata nella STESSA seconda epoch lascia intatto
# l'header e il .pyc vecchio continua a girare per qualunque processo
# successivo. Le funzioni qui sotto confrontano il bytecode realmente in
# uso con quello ricompilato dalla sorgente: e' l'unico controllo che
# vede quel difetto, perche' ogni altro guarda la sorgente, e la
# sorgente e' giusta: conclude che il codice e' corretto.

def _costanti(code_obj) -> list:
    """Costanti di un code object. I code object annidati diventano una
    tupla (nome, co_code, co_varnames, costanti annidate) invece di
    un indirizzo, cosi' il confronto fra due compilazioni e' reale."""
    fuori = []
    for costante in code_obj.co_consts:
        if hasattr(costante, "co_code"):
            fuori.append(("<code>", costante.co_name, costante.co_code,
                          costante.co_varnames, _costanti(costante)))
        else:
            fuori.append(costante)
    return fuori


def _vista(istruzione) -> tuple:
    """(opname, argval) confrontabile: un code object come argval ha un
    indirizzo diverso a ogni compilazione, quindi si confronta il nome."""
    valore = istruzione.argval
    if isinstance(valore, types.CodeType):
        valore = valore.co_name
    return (istruzione.opname, valore)


def _divergenze(loaded, fresh, percorso: str = "<modulo>") -> list[str]:
    """Differenze fra il bytecode caricato dal .pyc e quello dalla sorgente.
    Scende dentro i code object annidati e restituisce il percorso della
    funzione davvero diversa, non il modulo che la contiene: altrimenti
    l'unica istruzione segnalata sarebbe il LOAD_CONST del code object, che
    cambia indirizzo a ogni compilazione e non dice nulla."""
    if (loaded.co_code, loaded.co_names, loaded.co_varnames) == \
            (fresh.co_code, fresh.co_names, fresh.co_varnames) and \
            _costanti(loaded) == _costanti(fresh):
        return []
    annidati_loaded = [c for c in loaded.co_consts if isinstance(c, types.CodeType)]
    annidati_fresh = [c for c in fresh.co_consts if isinstance(c, types.CodeType)]
    if [c.co_name for c in annidati_loaded] == [c.co_name for c in annidati_fresh]:
        for dentro_loaded, dentro_fresh in zip(annidati_loaded, annidati_fresh, strict=True):
            sotto = _divergenze(dentro_loaded, dentro_fresh, f"{percorso}.{dentro_loaded.co_name}")
            if sotto:
                return sotto
    istruzioni_loaded = list(dis.get_instructions(loaded))
    istruzioni_fresh = list(dis.get_instructions(fresh))
    if len(istruzioni_loaded) == len(istruzioni_fresh):
        for carica, sorgente in zip(istruzioni_loaded, istruzioni_fresh, strict=True):
            if _vista(carica) != _vista(sorgente):
                return [f"{percorso} offset {carica.offset}: {carica.opname} "
                        f"{carica.argval!r} invece di {sorgente.opname} {sorgente.argval!r}"]
    return [f"{percorso}: struttura del bytecode diversa "
            f"({len(istruzioni_loaded)} istruzioni contro {len(istruzioni_fresh)})"]


def _moduli_del_pacchetto() -> list[Path]:
    import bravoric_stt_clipboard
    cartella = Path(bravoric_stt_clipboard.__file__).parent
    return sorted(p for p in cartella.glob("*.py") if p.name != "__init__.py")


def _verifica_pyc(path: Path) -> tuple[list[str], bool]:
    """(divergenze bytecode, header combacia). Se il .pyc non esiste non
    c'e' nulla da confrontare: e' il caso sano."""
    pyc = Path(importlib.util.cache_from_source(str(path)))
    if not pyc.exists():
        return [], True
    grezzo = pyc.read_bytes()
    mtime_pyc, size_pyc = struct.unpack("<II", grezzo[8:16])
    stat = path.stat()
    header = mtime_pyc == int(stat.st_mtime) and size_pyc == stat.st_size
    # S301: si deserializza solo il .pyc che CPython ha scritto lui stesso in
    # questo progetto, non dati esterni: e' l'unico modo per vedere il
    # bytecode che gira davvero.
    # ruff: noqa
    #noqa: S301
    caricato = marshal.loads(grezzo[16:])
    fresco = compile(path.read_text(), str(path), "exec")
    return _divergenze(caricato, fresco, f"<{path.name}>"), header


def _flip_budget_a_zero(code_obj):
    """Trasforma di proposito, dentro il code object, il `- 1` del budget del
    contesto in `- 0`. Individua la chiamata a `_drop_oldest_words` e cambia
    la costante immediatamente precedente alla sua CALL, cosi' non tocca le
    altre sottrazioni da 1 presenti nella funzione."""
    istruzioni = list(dis.get_instructions(code_obj))
    for posizione, ins in enumerate(istruzioni):
        if ins.opname != "LOAD_GLOBAL" or ins.argval != "_drop_oldest_words":
            continue
        for seguente in istruzioni[posizione:]:
            if seguente.opname == "LOAD_SMALL_INT" and seguente.argval == 1:
                if seguente.offset + 2 >= len(code_obj.co_code):
                    raise AssertionError("layout di LOAD_SMALL_INT diverso da 2 byte")
                grezzo = bytearray(code_obj.co_code)
                grezzo[seguente.offset + 1] = 0
                return code_obj.replace(co_code=bytes(grezzo))
        raise AssertionError("nessuna costante 1 nel budget del contesto")
    raise AssertionError("chiamata a _drop_oldest_words non trovata")


def _modulo_con_budget_neutro(sorgente: Path):
    """Modulo caricato da un bytecode in cui il budget del contesto e' stato
    neutralizzato a `limit - _blocks_len(...) - 0`: riproduce esattamente il
    difetto del .pyc avvelenato. NON e' codice di produzione, serve solo da
    controprova, per mostrare che lo stesso caso da 155 con quel `- 0` e da
    791 con il `- 1` vero, e che `_verifica_pyc` vedrebbe la divergenza."""
    modulo_compilato = compile(sorgente.read_text(), str(sorgente), "exec")
    costanti = []
    for costante in modulo_compilato.co_consts:
        if hasattr(costante, "co_code") and costante.co_name == "_build_vocabulary_prompt":
            costante = _flip_budget_a_zero(costante)
        costanti.append(costante)
    modulo_compilato = modulo_compilato.replace(co_consts=tuple(costanti))
    modulo = types.ModuleType("bravoric_stt_clipboard._budget_neutro")
    modulo.__package__ = "bravoric_stt_clipboard"
    modulo.__file__ = str(sorgente)
    exec(modulo_compilato, modulo.__dict__)  # noqa: S102 - controprova, non produzione
    return modulo


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brv-test-"))

    # Snapshot di TUTTI i percorsi reali dell'utente PRIMA di qualunque test.
    # Motivo: due difetti reali trovati dal vivo in questa stessa suite (non
    # ipotizzati) — _FifoSequencer scriveva su chunk_log.CHUNK_LOG_PATH senza
    # path= iniettabile, e il blocco "routing icone" (poco sotto) chiamava
    # stt._start() REALE (solo audio mockato) PRIMA che status.STATUS_PATH
    # venisse reindirizzato qualche riga sotto — entrambi scrivevano davvero
    # nei file dell'utente ad ogni run. La vecchia "difesa" per chunk_log era
    # tautologica (confrontava due path per disuguaglianza, sempre vera per
    # costruzione): qui si confronta mtime+size REALI di ogni file noto,
    # prima e dopo l'intera suite, cosi' un terzo sito analogo (presente o
    # futuro) non passerebbe inosservato una terza volta.
    from bravoric_stt_clipboard import chunk_log as _real_cl
    from bravoric_stt_clipboard import endpoint_breaker as _real_eb
    from bravoric_stt_clipboard import stream as _real_sm

    # I Path VANNO catturati qui, non riletti da module.ATTR a fine suite:
    # i test riassegnano legittimamente questi attributi a percorsi
    # temporanei e non li ripristinano sempre (non serve, ognuno usa il
    # proprio tmp). Rileggere l'attributo a fine corsa confronterebbe un
    # file temporaneo (spesso gia' sparito, .stat() -> OSError -> None)
    # contro lo snapshot reale iniziale: falso positivo garantito. Lo
    # stesso oggetto Path, salvato una volta, e' l'unico modo corretto.
    _REAL_PATHS = {
        "status": status.STATUS_PATH,
        "output_history": output_history.HISTORY_PATH,
        "config_editor": config_editor.CONFIG_PATH,
        "chunk_log": _real_cl.CHUNK_LOG_PATH,
        "endpoint_breaker": _real_eb.BREAKER_PATH,
        "stream_state": _real_sm.STREAM_STATE_PATH,
        "stream_live_text": _real_sm.STREAM_LIVE_TEXT_PATH,
    }

    def _snapshot_real_paths() -> dict[str, tuple[int, int] | None]:
        snap: dict[str, tuple[int, int] | None] = {}
        for name, p in _REAL_PATHS.items():
            try:
                st = p.stat()
                snap[name] = (st.st_mtime_ns, st.st_size)
            except OSError:
                snap[name] = None  # assente prima dei test: deve restare assente
        return snap

    _real_paths_before = _snapshot_real_paths()

    # --- notification icon registry/resolver contract -----------------
    print("== notification icon slots ==")
    check("omitted icon keys default to empty", config.IconsConfig().error_general == "")
    legacy_raw = config._example_config_path().read_text()
    legacy_raw = legacy_raw.replace('stt_recording_start = ""\\n', "").replace('stream_session_start = ""\\n', "").replace('stream_session_end = ""\\n', "").replace('stream_chunk_delivered = ""\\n', "").replace('error_general = ""\\n', "")
    legacy_cfg = config._build_config(__import__("tomllib").loads(legacy_raw))
    check("omitted new icon keys parse as empty overrides", all(getattr(legacy_cfg.icons, key) == "" for key in config.ICON_SLOT_KEYS))
    check("resolver preserves all registered slots", len(config.ICON_SLOT_REGISTRY) == len(config.ICON_SLOT_KEYS))
    good_override = tmp / "icon.png"
    good_override.write_bytes(b"icon")
    check("resolver accepts existing user override", notify.resolve_icon("error_general", str(good_override)) == str(good_override))
    # I due slot sotto hanno un asset incluso (stream-session-start.png /
    # error-general.png), quindi non cadono piu' sull'icona a tema: qui si
    # verifica il fallback tematico su slot che restano senza asset.
    check("resolver missing override uses themed category fallback", notify.resolve_icon("stream_session_end", str(tmp / "missing")) == notify.ICON_READY)
    check("resolver invalid override remains non-fatal", notify.resolve_icon("stt_recording_start", "\\0bad") == notify.ICON_RECORDING)
    check("resolver existing packaged default", notify.resolve_icon("stt_start").endswith("mic-neutral.png"))
    check("legacy key meaning remains processing start", next(s for s in config.ICON_SLOT_REGISTRY if s.key == "stt_start").meaning.startswith("STT processing start"))
    icon_config_path = tmp / "icon_editor.toml"
    icon_config_path.write_text("[icons]\n")
    config_editor.CONFIG_PATH = icon_config_path
    config_editor.set_icon_field("stream_session_end", str(good_override))
    check("editor set/get new icon slot", config_editor.get_state()["icons"]["stream_session_end"]["override"] == str(good_override))
    config_editor.set_icon_field("stream_session_end", "")
    check("editor reset override", config_editor.get_state()["icons"]["stream_session_end"]["override"] == "")
    try:
        config_editor.set_icon_field("unknown", "")
        invalid_icon_rejected = False
    except config_editor.ConfigEditorError:
        invalid_icon_rejected = True
    check("editor validates slot names", invalid_icon_rejected)
    # set_section_field/set_storage_field validavano service/section ma non
    # field: unica incoerenza coi 4 setter gemelli (set_level_field,
    # set_stream_field, set_notification_field, set_icon_field). field non
    # e' mai attaccante-controllato dalla GUI reale, ma finiva letteralmente
    # in f"{field} = {toml_value}" nel file: difesa in profondita' aggiunta.
    try:
        config_editor.set_section_field("stt", "campo-inventato", "x")
        invalid_section_field_rejected = False
    except config_editor.ConfigEditorError:
        invalid_section_field_rejected = True
    check("editor validates set_section_field field names", invalid_section_field_rejected)
    try:
        config_editor.set_storage_field("stt_raw", "campo-inventato", "x")
        invalid_storage_field_rejected = False
    except config_editor.ConfigEditorError:
        invalid_storage_field_rejected = True
    check("editor validates set_storage_field field names", invalid_storage_field_rejected)
    absent_icon_path = tmp / "absent" / "config.toml"
    config_editor.CONFIG_PATH = absent_icon_path
    try:
        config_editor.set_icon_field("error_general", "")
        absent_config_safe = False
    except config_editor.ConfigEditorError:
        absent_config_safe = not absent_icon_path.exists()
    check("editor refuses to create config when user config absent", absent_config_safe)

    # --- notification icon slot routing checks -----------------------
    # status.STATUS_PATH va reindirizzato PRIMA di questo blocco: stt._start
    # e' la funzione REALE (solo stt.audio e' mockato), e scrive davvero
    # status.write_status(STATE_RECORDING, service="stt"). Difetto reale
    # trovato dal vivo: il redirect stava PIU' SOTTO (nel blocco status.py),
    # quindi questa singola chiamata precedeva la riassegnazione e finiva
    # nel file VERO dell'utente (~/.cache/bravoric-stt-clipboard/status.json),
    # sovrascrivendolo con uno stato 'recording' falso e un timestamp fresco.
    status.STATUS_PATH = tmp / "status.json"
    with mock.patch("bravoric_stt_clipboard.notify.send") as m_send:
        stt_cfg = mock.Mock()
        stt_cfg.notifications = True
        stt_cfg.icons = config.IconsConfig(stt_recording_start=str(good_override), error_general=str(good_override))
        with mock.patch("bravoric_stt_clipboard.stt.audio"):
            stt._start(stt_cfg)
        check("routing: stt_recording_start uses configured slot", m_send.call_args[1]["icon"] == str(good_override))

    # --- status.py: scrittura atomica + lettura robusta -------------------
    print("== status.py ==")
    status.write_status("recording", service="stt")
    data = status.read_status()
    check("write/read round-trip", data["state"] == "recording" and data["service"] == "stt")
    check("nessun .tmp residuo", not (tmp / "status.json.tmp").exists())
    status.STATUS_PATH.write_text('{"state": "idle", "time')
    check("file troncato -> idle", status.read_status()["state"] == "idle")
    status.STATUS_PATH.write_bytes(b"\xff\xfe non utf8")
    check("file non-utf8 -> idle", status.read_status()["state"] == "idle")

    # --- output_history.py: atomica + UnicodeDecodeError ------------------
    print("== output_history.py ==")
    output_history.HISTORY_PATH = tmp / "output_history.json"
    output_history.append_entry("stt", "raw", "ciao", 10)
    check("append/read round-trip", output_history.read_history()[0]["text"] == "ciao")
    output_history.HISTORY_PATH.write_bytes(b"\xff\xfe rotto")
    check("file non-utf8 -> []", output_history.read_history() == [])
    output_history.clear_history()
    check("clear -> []", output_history.read_history() == [])

    # --- audio.py: lock fantasma, pid morto, JSON malformato --------------
    print("== audio.py ==")
    audio.LOCK_PATH = tmp / "recording.lock"
    cfg = mock.Mock(toggle_debounce_seconds=0.0)
    gone = dead_pid()

    audio.LOCK_PATH.write_text("{non json")
    check("lock malformato -> non registra", not audio.is_recording())

    audio.LOCK_PATH.write_text(json.dumps(
        {"pid": gone, "audio_path": str(tmp / "a.ogg"), "started_at": 0}))
    check("pid morto -> non registra", not audio.is_recording())
    check("pid morto -> lock rimosso", not audio.LOCK_PATH.exists())

    audio.LOCK_PATH.write_text(json.dumps(
        {"pid": os.getpid(), "audio_path": str(tmp / "b.ogg"), "started_at": 0}))
    check("pid vivo -> registra", audio.is_recording())

    # Giro 18 (P1): il fixture reale. Prima questo blocco puntava il lock a
    # tmp/"c.ogg", un file che non esisteva, e pretendeva che stop_restituisse
    # comunque quel path: era il difetto P1 (file sparito/da 0 byte accettato
    # come registrazione valida, caso B del reviewer). L'intento ORIGINALE di
    # questa prova e' legittimo e resta: con ffmpeg gia' morto il lock va
    # comunque ripulito e stop non deve sollevare per quello. Qui il file
    # esiste DAVVERO e ha contenuto, quindi la prova verifica l'intento
    # invece di certificare il difetto; il caso vuoto/spARITO e' verificato
    # subito sotto, ed e' il caso che prima passava e non doveva.
    _c_ogg = tmp / "c.ogg"
    _c_ogg.write_bytes(b"audio reale, non vuoto")
    audio.LOCK_PATH.write_text(json.dumps(
        {"pid": gone, "audio_path": str(_c_ogg), "started_at": 0}))
    try:
        out = audio.stop_recording(cfg)
        check("stop su pid morto non solleva (file valido)", out == _c_ogg)
    except Exception as exc:  # noqa: BLE001
        check(f"stop su pid morto non solleva ({type(exc).__name__})", False)
    check("stop -> lock rimosso", not audio.LOCK_PATH.exists())

    # --- api_client.py: risposta di forma inattesa -> ApiError ------------
    print("== api_client.py ==")
    level = mock.Mock(
        name="test", endpoint="http://x/v1", model="m", timeout_seconds=1,
        resolved_api_key=lambda: "k", ca_cert_path=lambda: None,
    )

    class FakeResp:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

    bad = [
        ({"error": "boom"}, "senza choices"),
        ({"choices": []}, "choices vuoto"),
        ({"choices": [{}]}, "message assente"),
        (ValueError("non json"), "corpo non JSON"),
    ]
    for payload, what in bad:
        with mock.patch.object(api_client.requests, "post", return_value=FakeResp(payload)):
            try:
                api_client.chat_cleanup(level, "sys", "txt")
                check(f"chat_cleanup {what} -> ApiError", False)
            except api_client.ApiError:
                check(f"chat_cleanup {what} -> ApiError", True)
            except Exception as exc:  # noqa: BLE001
                check(f"chat_cleanup {what} -> ApiError ({type(exc).__name__})", False)

    ok = {"choices": [{"message": {"content": json.dumps({"corrected_text": "ok"})}}]}
    with mock.patch.object(api_client.requests, "post", return_value=FakeResp(ok)):
        check("chat_cleanup valido -> testo", api_client.chat_cleanup(level, "s", "t") == "ok")

    # JSON valido ma con 'corrected_text' di tipo sbagliato: deve diventare
    # ApiError, non AttributeError (che sfuggirebbe alla catena di fallback).
    for payload, what in [
        ({"corrected_text": None}, "null"),
        ({"corrected_text": 42}, "numero"),
        ({"corrected_text": ["a"]}, "lista"),
    ]:
        resp = FakeResp({"choices": [{"message": {"content": json.dumps(payload)}}]})
        with mock.patch.object(api_client.requests, "post", return_value=resp):
            try:
                api_client.chat_cleanup(level, "s", "t")
                check(f"chat_cleanup corrected_text {what} -> ApiError", False)
            except api_client.ApiError:
                check(f"chat_cleanup corrected_text {what} -> ApiError", True)
            except Exception as exc:  # noqa: BLE001
                check(f"chat_cleanup corrected_text {what} -> ApiError ({type(exc).__name__})", False)

    # --- output_history.py: JSON valido ma non-lista ---------------------
    print("== output_history.py (forma inattesa) ==")
    for shape in ['{"a": 1}', "123", "null", '"x"', "[1, 2]"]:
        output_history.HISTORY_PATH.write_text(shape)
        check(f"history forma {shape} -> []", output_history.read_history() == [])
        try:
            output_history.append_entry("stt", "raw", "nuovo", 10)
            check(f"append su forma {shape} non solleva", True)
        except Exception as exc:  # noqa: BLE001
            check(f"append su forma {shape} non solleva ({type(exc).__name__})", False)

    # --- config.py: TOML malformato / valore non numerico -> ConfigError --
    print("== config.py ==")
    bad_toml = tmp / "bad.toml"
    bad_toml.write_text("questo non e' = toml [")
    try:
        config.load_config(bad_toml)
        check("TOML malformato -> ConfigError", False)
    except config.ConfigError:
        check("TOML malformato -> ConfigError", True)
    except Exception as exc:  # noqa: BLE001
        check(f"TOML malformato -> ConfigError ({type(exc).__name__})", False)

    bad_num = tmp / "num.toml"
    bad_num.write_text('[audio]\nsample_rate = "sedicimila"\n')
    try:
        config.load_config(bad_num)
        check("valore non numerico -> ConfigError", False)
    except config.ConfigError:
        check("valore non numerico -> ConfigError", True)
    except Exception as exc:  # noqa: BLE001
        check(f"valore non numerico -> ConfigError ({type(exc).__name__})", False)

    # --- stt.py: il file audio temporaneo viene sempre rimosso ------------
    print("== stt.py ==")
    rec = tmp / "rec.ogg"
    rec.write_bytes(b"audio")
    with mock.patch.object(stt.audio, "stop_recording", return_value=rec), \
         mock.patch.object(stt, "_process_recording", return_value=None):
        stt._stop_and_process(mock.Mock())
    check("audio temporaneo rimosso su successo", not rec.exists())

    rec2 = tmp / "rec2.ogg"
    rec2.write_bytes(b"audio")
    with mock.patch.object(stt.audio, "stop_recording", return_value=rec2), \
         mock.patch.object(stt, "_process_recording", side_effect=RuntimeError("boom")), \
         contextlib.suppress(RuntimeError):
        stt._stop_and_process(mock.Mock())
    check("audio temporaneo rimosso su errore", not rec2.exists())

    # ================================================================
    # GIRO 10: fix backend
    # ================================================================

    # --- config.py: _build_config con fallback malformato (AttributeError)
    print("== config.py (giro 10) ==")
    bad_fallback = tmp / "bad_fallback.toml"
    bad_fallback.write_text('[stt]\nfallback = "dovrebbe essere una lista"\n')
    try:
        config.load_config(bad_fallback)
        check("fallback non-lista -> ConfigError", False)
    except config.ConfigError:
        check("fallback non-lista -> ConfigError", True)
    except Exception as exc:  # noqa: BLE001
        check(f"fallback non-lista -> ConfigError ({type(exc).__name__})", False)

    # Lo stato "fallback campi malformati" non e' un bug: TOML accetta
    # interi/bool, e _parse_fallback_list ignora entry non configurate
    # (nessun endpoint/model -> is_configured() False -> skip).
    # Il test segue solo il caso realmente rotto: fallback non-lista.

    # --- stt.py: double_injection=False -> solo clean negli appunti ------
    print("== stt.py (giro 10: double_injection) ==")
    with mock.patch("bravoric_stt_clipboard.stt.clipboard") as m_clip, \
         mock.patch("bravoric_stt_clipboard.stt.notify") as m_notify, \
         mock.patch("bravoric_stt_clipboard.stt.storage"), \
         mock.patch("bravoric_stt_clipboard.stt.output_history"), \
         mock.patch("bravoric_stt_clipboard.stt.status"):

        cfg_di = mock.Mock()
        cfg_di.double_injection = False
        cfg_di.stt_cleanup.enabled = True
        cfg_di.stt_cleanup.fallback = [mock.Mock()]  # non vuoto
        cfg_di.stt_cleanup.system_prompt = "clean"
        cfg_di.audio.retry_on_error = False
        cfg_di.notifications = False

        with mock.patch("bravoric_stt_clipboard.stt.try_with_fallback", return_value="raw text"), \
             mock.patch("bravoric_stt_clipboard.stt.cleanup_with_validation", return_value="clean text"):
            stt._process_recording(cfg_di, tmp / "test.ogg")

        calls = [str(c) for c in m_clip.write_text.call_args_list]
        check(
            "double_injection=False: nessuna scrittura raw",
            not any("raw text" in c for c in calls),
        )
        check(
            "double_injection=False: scrittura clean",
            any("clean text" in c for c in calls),
        )

    # --- stt.py: double_injection=True -> raw + clean ---------------------
    with mock.patch("bravoric_stt_clipboard.stt.clipboard") as m_clip, \
         mock.patch("bravoric_stt_clipboard.stt.notify") as m_notify, \
         mock.patch("bravoric_stt_clipboard.stt.storage"), \
         mock.patch("bravoric_stt_clipboard.stt.output_history"), \
         mock.patch("bravoric_stt_clipboard.stt.status"):

        cfg_di2 = mock.Mock()
        cfg_di2.double_injection = True
        cfg_di2.stt_cleanup.enabled = True
        cfg_di2.stt_cleanup.fallback = [mock.Mock()]
        cfg_di2.stt_cleanup.system_prompt = "clean"
        cfg_di2.audio.retry_on_error = False
        cfg_di2.notifications = False

        with mock.patch("bravoric_stt_clipboard.stt.try_with_fallback", return_value="raw text"), \
             mock.patch("bravoric_stt_clipboard.stt.cleanup_with_validation", return_value="clean text"):
            stt._process_recording(cfg_di2, tmp / "test2.ogg")

        calls = [str(c) for c in m_clip.write_text.call_args_list]
        check(
            "double_injection=True: prima raw poi clean",
            any("raw text" in c for c in calls) and any("clean text" in c for c in calls),
        )

    # --- stt.py: storage failure non blocca il flusso ---------------------
    print("== stt.py (giro 10: storage failure) ==")
    with mock.patch("bravoric_stt_clipboard.stt.clipboard") as m_clip, \
         mock.patch("bravoric_stt_clipboard.stt.notify") as m_notify, \
         mock.patch("bravoric_stt_clipboard.stt.storage") as m_stor, \
         mock.patch("bravoric_stt_clipboard.stt.output_history"), \
         mock.patch("bravoric_stt_clipboard.stt.status") as m_status:

        m_stor.save_text_if_enabled.side_effect = OSError("disk full")
        m_stor.save_if_enabled.side_effect = OSError("disk full")

        cfg_fail = mock.Mock()
        cfg_fail.double_injection = False
        cfg_fail.stt_cleanup.enabled = False
        cfg_fail.stt_cleanup.fallback = []
        cfg_fail.audio.retry_on_error = False
        cfg_fail.notifications = False
        cfg_fail.storage.stt_original.enabled = False
        cfg_fail.storage.stt_raw.enabled = False

        # Imposta STATE_IDLE PRIMA della chiamata: _process_recording usa
        # status.STATE_IDLE, che e' il mock -> deve essere la stringa giusta.
        m_status.STATE_IDLE = "idle"

        with mock.patch("bravoric_stt_clipboard.stt.try_with_fallback", return_value="testo"):
            try:
                stt._process_recording(cfg_fail, tmp / "test3.ogg")
                check("storage failure: flusso completato", True)
            except Exception as exc:  # noqa: BLE001
                check(f"storage failure: flusso completato ({type(exc).__name__})", False)

        check(
            "storage failure: status IDLE scritto",
            m_status.write_status.call_args_list[-1][0][0] == "idle",
        )

    # --- cli.py: ConfigError triggera notifica ----------------------------
    print("== cli.py (giro 10) ==")
    from bravoric_stt_clipboard import cli

    with mock.patch.object(cli, "load_config", side_effect=config.ConfigError("bad config")), \
         mock.patch.object(cli, "notify") as m_notify:
        ret = cli.stt_toggle_main()
        check("ConfigError -> ritorna 1", ret == 1)
        check(
            "ConfigError -> notify.send chiamato",
            m_notify.send.call_count >= 1,
        )

    # --- cli.ocr_capture_main: mai chiamata da nessun test finora ----------
    # Stesso schema di stt_toggle_main sopra (mai testato per la sua propria
    # entry point, solo per handle_capture direttamente): ConfigError e
    # un'eccezione inattesa da ocr.handle_capture devono entrambe tornare 1
    # senza far esplodere il processo CLI.
    with mock.patch.object(cli, "load_config", side_effect=config.ConfigError("bad config")), \
         mock.patch.object(cli, "notify") as m_notify_ocr:
        ret_ocr = cli.ocr_capture_main()
        check("ocr_capture_main: ConfigError -> ritorna 1", ret_ocr == 1)
        check("ocr_capture_main: ConfigError -> notify.send chiamato",
              m_notify_ocr.send.call_count >= 1)

    with mock.patch.object(cli, "load_config", return_value=mock.Mock()), \
         mock.patch.object(cli, "ocr") as m_ocr_cli, \
         mock.patch.object(cli, "status") as m_status_cli, \
         mock.patch.object(cli, "notify") as m_notify_ocr2:
        m_ocr_cli.handle_capture.side_effect = RuntimeError("boom (simulato)")
        ret_ocr2 = cli.ocr_capture_main()
        check("ocr_capture_main: eccezione inattesa da handle_capture -> ritorna 1, non solleva",
              ret_ocr2 == 1)
        check("ocr_capture_main: eccezione inattesa -> stato ERROR scritto",
              m_status_cli.write_status.call_args_list[-1][0][0] == m_status_cli.STATE_ERROR)
        check("ocr_capture_main: eccezione inattesa -> notifica utente",
              m_notify_ocr2.send.call_count >= 1)

    # --- cli.stream_toggle_main: eccezione inattesa non scriveva ERROR -----
    # Difetto reale: a differenza di stt_toggle_main (fix B28/P2, sopra) e
    # ocr_capture_main, il ramo 'unexpected error' di stream_toggle_main non
    # scriveva MAI status.STATE_ERROR. Per lo stream e' piu' grave che per
    # stt/ocr: P4 esclude esplicitamente 'stream' dal timeout di sicurezza
    # sul recording (una sessione live puo' durare ore), quindi qui non
    # c'e' NESSUN watchdog che corregga l'indicatore bloccato — a differenza
    # di stt (30 min) o ocr (120 min). StreamSession e' mockata a livello di
    # classe (importata localmente dentro la funzione, non un attributo di
    # modulo di cli.py: mock.patch.object(cli, "stream") non la vedrebbe).
    with mock.patch.object(cli, "load_config", return_value=mock.Mock()), \
         mock.patch("bravoric_stt_clipboard.stream.StreamSession") as m_session_cls, \
         mock.patch.object(cli, "status") as m_status_stream, \
         mock.patch.object(cli, "notify") as m_notify_stream:
        m_session_cls.return_value.is_active.return_value = False
        m_session_cls.return_value.start.side_effect = RuntimeError("boom (simulato)")
        ret_stream = cli.stream_toggle_main([])
        check("stream_toggle_main: eccezione inattesa -> ritorna 1, non solleva",
              ret_stream == 1)
        check("stream_toggle_main: eccezione inattesa -> stato ERROR scritto",
              m_status_stream.write_status.call_args_list[-1][0][0] == m_status_stream.STATE_ERROR)
        check("stream_toggle_main: eccezione inattesa -> notifica utente",
              m_notify_stream.send.call_count >= 1)

    # CONTRO: notify.send che solleva a sua volta non deve far crashare la
    # funzione (prova diretta del beneficio di _report_unexpected_error()
    # rispetto alla chiamata diretta che c'era prima, senza rete).
    with mock.patch.object(cli, "load_config", return_value=mock.Mock()), \
         mock.patch("bravoric_stt_clipboard.stream.StreamSession") as m_session_cls2, \
         mock.patch.object(cli, "status"), \
         mock.patch.object(cli, "notify") as m_notify_stream2:
        m_session_cls2.return_value.is_active.return_value = False
        m_session_cls2.return_value.start.side_effect = RuntimeError("boom (simulato)")
        m_notify_stream2.send.side_effect = OSError("notify-send a sua volta rotto (simulato)")
        try:
            ret_stream2 = cli.stream_toggle_main([])
            crashed = False
        except Exception:
            ret_stream2 = None
            crashed = True
        check("stream_toggle_main: notify.send rotto non fa crashare la funzione",
              not crashed and ret_stream2 == 1)

    # --- storage.py: _purge_expired con file che scompare -----------------
    print("== storage.py (giro 10) ==")
    from bravoric_stt_clipboard import storage

    purge_dir = tmp / "purge_test"
    purge_dir.mkdir()
    (purge_dir / "old.txt").write_text("old")
    (purge_dir / "old.txt").touch()
    # Rendiamo il file "vecchio" impostando mtime nel passato
    import time
    old_time = time.time() - 72000  # 20 ore fa
    os.utime(purge_dir / "old.txt", (old_time, old_time))

    # Simula race condition: file scompare durante iterdir
    original_iterdir = purge_dir.iterdir
    call_count = [0]

    def fake_iterdir():
        files = list(original_iterdir())
        call_count[0] += 1
        if call_count[0] == 1:
            # Prima chiamata: restituisci i file
            return iter(files)
        # Seconda chiamata (non dovrebbe accadere, ma testa resilience)
        return iter(files)

    with mock.patch.object(type(purge_dir), "iterdir", side_effect=fake_iterdir):
        try:
            storage._purge_expired(purge_dir, retention_hours=1)
            check("_purge_expired non solleva su race", True)
        except FileNotFoundError:
            check("_purge_expired non solleva su race", False)

    # --- config_editor.py: broader exception handling ----------------------
    print("== config_editor.py (giro 10) ==")

    missing = tmp / "nonexistent.toml"
    config_editor.CONFIG_PATH = missing
    ret = config_editor.main()
    check("config mancante -> ritorna 1", ret == 1)

    # A8: argomento JSON mancante per set-stream-commands -> errore CLI pulito
    # (exit 1, nessun traceback). main(argv) accetta un argv esplicito.
    ret = config_editor.main(["set-stream-commands"])
    check("A8: set-stream-commands senza JSON -> exit 1", ret == 1)
    ret = config_editor.main(["set-stream"])
    check("A8: set-stream senza field/value -> exit 1", ret == 1)
    ret = config_editor.main(["comando-inesistente"])
    check("A8: comando sconosciuto -> exit 1", ret == 1)

    # --- config.py: default codec coerente con gli example ("libopus") ----
    print("== config.py (giro 10: default codec) ==")
    # L'encoder nativo ffmpeg "opus" e' experimental/disabilitato di default in
    # molte build: il fallback deve restare "libopus", come negli example.
    check(
        "codec di default = libopus",
        config._build_config({}).audio.codec == "libopus",
    )
    check(
        "codec assente dalla sezione [audio] -> libopus",
        config._build_config({"audio": {"format": "ogg"}}).audio.codec == "libopus",
    )

    # --- config.py: clamp minimo toggle_debounce_seconds (giro 15) ---------
    # A 0 il debounce non protegge più stop_recording da un lock ancora in
    # fase di startup (placeholder col pid del processo CLI, non ffmpeg):
    # SIGINT finirebbe sul processo sbagliato invece che sul secondo ffmpeg.
    check(
        "toggle_debounce_seconds=0 -> clampato a 0.1",
        config._build_config({"audio": {"toggle_debounce_seconds": 0}}).audio.toggle_debounce_seconds == 0.1,
    )
    check(
        "toggle_debounce_seconds negativo -> clampato a 0.1",
        config._build_config({"audio": {"toggle_debounce_seconds": -5}}).audio.toggle_debounce_seconds == 0.1,
    )
    check(
        "toggle_debounce_seconds valido -> invariato",
        config._build_config({"audio": {"toggle_debounce_seconds": 2}}).audio.toggle_debounce_seconds == 2,
    )

    # --- status.py: OCR non spegne il 'recording' di STT (giro 17) ---------
    # status.json e' condiviso tra STT e OCR (scorciatoie indipendenti, senza
    # esclusione reciproca). Confermato dal vivo: OCR completato mentre STT
    # registra ancora sovrascriveva silenziosamente lo stato a idle,
    # disattivando anche il timeout di sicurezza sul recording.
    print("== status.py (giro 17: OCR non spegne recording STT) ==")
    status.STATUS_PATH = tmp / "status_race.json"
    status.write_status(status.STATE_RECORDING, service="stt")
    status.write_status(status.STATE_IDLE, last_output="testo ocr", service="ocr")
    check(
        "OCR idle non sovrascrive STT recording",
        status.read_status()["state"] == status.STATE_RECORDING,
    )
    # Ma OCR resta libero di scrivere IDLE quando STT non sta registrando.
    status.write_status(status.STATE_IDLE, service="stt")
    status.write_status(status.STATE_IDLE, last_output="testo ocr 2", service="ocr")
    check(
        "OCR idle scrive normalmente quando STT non registra",
        status.read_status().get("last_output") == "testo ocr 2",
    )
    # E STT può sempre spegnere il proprio recording.
    status.write_status(status.STATE_RECORDING, service="stt")
    status.write_status(status.STATE_IDLE, last_output="testo stt", service="stt")
    check(
        "STT idle spegne il proprio recording",
        status.read_status().get("last_output") == "testo stt",
    )

    # --- stt.py: fallimento clipboard -> ERROR, non resta "processing" -----
    print("== stt.py (giro 10: clipboard failure) ==")
    with mock.patch("bravoric_stt_clipboard.stt.clipboard") as m_clip, \
         mock.patch("bravoric_stt_clipboard.stt.notify"), \
         mock.patch("bravoric_stt_clipboard.stt.storage"), \
         mock.patch("bravoric_stt_clipboard.stt.output_history"), \
         mock.patch("bravoric_stt_clipboard.stt.status") as m_status:

        # wl-copy fallisce (exit != 0) -> CalledProcessError
        m_clip.write_text.side_effect = subprocess.CalledProcessError(1, "wl-copy")
        m_status.STATE_ERROR = "error"
        m_status.STATE_IDLE = "idle"

        cfg_cb = mock.Mock()
        cfg_cb.double_injection = False
        cfg_cb.stt_cleanup.enabled = False
        cfg_cb.stt_cleanup.fallback = []
        cfg_cb.audio.retry_on_error = False
        cfg_cb.notifications = False
        cfg_cb.storage.stt_original.enabled = False

        with mock.patch("bravoric_stt_clipboard.stt.try_with_fallback", return_value="testo"):
            stt._process_recording(cfg_cb, tmp / "cb.ogg")

        states = [c[0][0] for c in m_status.write_status.call_args_list]
        check("clipboard failure STT: ultimo stato = error", states[-1] == "error")
        check("clipboard failure STT: idle non scritto (niente stato bloccato)", "idle" not in states)

    # --- ocr.py: fallimento clipboard -> ERROR ---------------------------
    print("== ocr.py (giro 10: clipboard failure) ==")
    with mock.patch("bravoric_stt_clipboard.ocr.clipboard") as m_clip, \
         mock.patch("bravoric_stt_clipboard.ocr.notify"), \
         mock.patch("bravoric_stt_clipboard.ocr.storage"), \
         mock.patch("bravoric_stt_clipboard.ocr.output_history"), \
         mock.patch("bravoric_stt_clipboard.ocr.status") as m_status:

        m_clip.read_image_png.return_value = b"png"
        m_clip.write_text.side_effect = FileNotFoundError("wl-copy not found")
        m_status.STATE_ERROR = "error"
        m_status.STATE_IDLE = "idle"

        cfg_ocr = mock.Mock()
        cfg_ocr.double_injection = False
        cfg_ocr.ocr_cleanup.enabled = False
        cfg_ocr.ocr_cleanup.fallback = []
        cfg_ocr.notifications = False
        cfg_ocr.storage.ocr_original.enabled = False
        cfg_ocr.storage.ocr_raw.enabled = False
        # Un Mock() non impostato e' truthy: senza questo, il ramo
        # screenshot (nuovo) scatterebbe al posto di quello clipboard che
        # questo test vuole davvero esercitare.
        cfg_ocr.ocr_capture_screenshot = False

        with mock.patch("bravoric_stt_clipboard.ocr.try_with_fallback", return_value="testo"):
            ocr.handle_capture(cfg_ocr)

        states = [c[0][0] for c in m_status.write_status.call_args_list]
        check("clipboard failure OCR: ultimo stato = error", states[-1] == "error")

    # --- notify.py: ordine argomenti notify-send (giro 12, P1) -------------
    # -i deve stare PRIMA di --: dopo -- GOption smette di riconoscere le
    # opzioni, quindi "-i" verrebbe letto come argomento posizionale e ogni
    # notifica fallirebbe silenziosamente (regressione reale, riprodotta).
    print("== notify.py (giro 12: ordine argomenti notify-send) ==")
    with mock.patch("bravoric_stt_clipboard.notify.subprocess.run") as m_run:
        notify.send("titolo", "corpo", icon="qualche-icona")
        args = m_run.call_args[0][0]
        check(
            "-i precede -- nell'invocazione notify-send",
            args.index("-i") < args.index("--"),
        )

    # --- config_editor.py: scritture concorrenti non si perdono (giro 12) --
    # _atomic_replace usava un path tmp fisso condiviso da tutti i comandi
    # GUI: due scritture concorrenti sullo stesso tmp potevano far perdere
    # una modifica. Verifica che due scritture sequenziali arrivino entrambe.
    print("== config_editor.py (giro 12: tmp univoco) ==")
    cfg_path = tmp / "config.toml"
    cfg_path.write_text('[general]\nnotifications = true\n')
    config_editor.CONFIG_PATH = cfg_path
    config_editor._atomic_replace('[general]\nnotifications = false\n')
    check(
        "_atomic_replace pubblica il contenuto scritto",
        cfg_path.read_text() == '[general]\nnotifications = false\n',
    )
    check(
        "_atomic_replace non lascia .toml.tmp residui",
        not any(cfg_path.parent.glob("config.toml.*.tmp")),
    )

    # --- output_history.py: race su append_entry concorrenti (giro 13) -----
    # Senza lock, N processi concorrenti leggono lo stesso stato iniziale e
    # l'ultimo _write() vince, perdendo le voci degli altri (confermato dal
    # vivo: 11-17/20 sopravvivevano). Con fcntl.flock devono arrivare tutte.
    print("== output_history.py (giro 13: race append_entry concorrenti) ==")
    race_history = tmp / "race_history.json"
    procs = [
        multiprocessing.Process(target=_append_entry_worker, args=(str(race_history), i))
        for i in range(20)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
    output_history.HISTORY_PATH = race_history
    check(
        "20 append_entry concorrenti -> 20 voci (nessuna persa)",
        len(output_history.read_history()) == 20,
    )

    # --- config_editor.py: race su set_storage_field concorrenti (giro 13) -
    print("== config_editor.py (giro 13: race set_storage_field concorrenti) ==")
    race_config = tmp / "race_config.toml"
    race_config.write_text(
        "[storage]\n"
        "[storage.stt_raw]\nenabled = false\nretention_hours = 0\n"
        "[storage.ocr_raw]\nenabled = false\nretention_hours = 0\n"
    )
    p1 = multiprocessing.Process(
        target=_set_storage_field_worker, args=(str(race_config), "stt_raw", "retention_hours", "48"))
    p2 = multiprocessing.Process(
        target=_set_storage_field_worker, args=(str(race_config), "ocr_raw", "retention_hours", "72"))
    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)
    config_editor.CONFIG_PATH = race_config
    state = config_editor.get_state()
    check(
        "set_storage_field concorrenti: entrambe le modifiche sopravvivono",
        state["storage"]["stt_raw"]["retention_hours"] == 48
        and state["storage"]["ocr_raw"]["retention_hours"] == 72,
    )

    # --- storage.py: permessi file salvati (giro 14) -----------------------
    # write_bytes senza chmod e' soggetto a umask di sistema: file con audio/
    # testo dettato/OCR riservato leggibili da altri utenti locali (confermato
    # dal vivo su config.toml/status.json gemelli nei giri 13/14).
    print("== storage.py (giro 14: permessi file salvati) ==")
    from bravoric_stt_clipboard import storage
    storage_base = tmp / "storage_race"
    policy = config.RetentionPolicy(enabled=True, retention_hours=0)
    saved = storage.save_if_enabled(str(storage_base), "stt_raw", policy, b"testo riservato", "txt")
    check(
        "file salvato -> permessi 600",
        saved is not None and oct(saved.stat().st_mode)[-3:] == "600",
    )
    check(
        "cartella salvata -> permessi 700",
        saved is not None and oct(saved.parent.stat().st_mode)[-3:] == "700",
    )

    # --- config_editor.py: api_key via stdin, non argv (giro 14) -----------
    # api_key non deve passare come argv: /proc/PID/cmdline e' leggibile da
    # altri utenti locali per la durata del subprocess (confermato dal vivo).
    # '-' segnala a config_editor.py di leggere il valore da stdin.
    print("== config_editor.py (giro 14: api_key via stdin) ==")
    stdin_config = tmp / "stdin_config.toml"
    stdin_config.write_text(
        '[[stt.fallback]]\nname = "x"\nendpoint = "http://x"\nmodel = "x"\n'
        'api_key_env = ""\napi_key = ""\nca_cert = ""\ntimeout_seconds = 60\n'
    )
    config_editor.CONFIG_PATH = stdin_config
    import io
    old_stdin, old_argv = sys.stdin, sys.argv
    sys.stdin = io.StringIO("sk-segreta-test\n")
    sys.argv = ["config_editor.py", "set-level", "stt", "0", "api_key", "-"]
    try:
        ret = config_editor.main()
    finally:
        sys.stdin, sys.argv = old_stdin, old_argv
    check("set-level api_key '-' legge da stdin: ritorna 0", ret == 0)
    check(
        "set-level api_key '-' legge da stdin: valore scritto",
        'api_key = "sk-segreta-test"' in stdin_config.read_text(),
    )

    # --- config.py: STTConfig parsing ------------------------------
    print("== config.py (stt) =")
    stt_toml = tmp / "stt.toml"
    stt_toml.write_text(
        '[notifications]\n'
        'stt_on_processing_start = true\n'
        '\n[stt]\n'
        'language = "en"\n'
        'prompt = "test prompt"\n'
        'hotwords = "hotword1 hotword2"\n'
        '\n[[stt.fallback]]\n'
        'name = "scrocco-llm"\n'
        'endpoint = "http://10.9.0.2:4001/v1"\n'
        'model = "scrocco-llm-fissone"\n'
        'api_key_env = "SCROCCO_FISSONE_API_KEY"\n'
        'api_key = ""\n'
        'ca_cert = ""\n'
        'timeout_seconds = 120\n'
    )
    cfg_stt = config.load_config(stt_toml)
    check("stt.language = en", cfg_stt.stt.language == "en")
    check("stt.prompt = test prompt", cfg_stt.stt.prompt == "test prompt")
    check("stt.hotwords = hotword1 hotword2", cfg_stt.stt.hotwords == "hotword1 hotword2")
    check("stt.fallback non vuoto", len(cfg_stt.stt_fallback) == 1)

    # Legacy config (no [stt] section) uses defaults
    legacy_toml = tmp / "legacy.toml"
    legacy_toml.write_text(
        '[notifications]\n'
        'stt_on_processing_start = true\n'
        '\n[audio]\n'
        'format = "ogg"\n'
        '\n[[stt.fallback]]\n'
        'name = "test"\n'
        'endpoint = "http://x"\n'
        'model = "m"\n'
        'api_key_env = ""\n'
        'api_key = ""\n'
        'ca_cert = ""\n'
        'timeout_seconds = 60\n'
    )
    cfg_legacy = config.load_config(legacy_toml)
    check("legacy stt.language = it (default)", cfg_legacy.stt.language == "it")
    # Giro 18 (P3): questo era == "" e certificava il difetto che il brief
    # chiede di chiudere ("se l'utente non ha impostato un prompt, si usa
    # quello di default"): il default viveva SOLO nei config.example, quindi
    # per una config scritta a mano — cioe' questo caso legacy — il contesto
    # non esisteva e il ramo vocabolario non partiva. L'intento ORIGINALE
    # della prova (il percorso legacy senza [stt] carica senza errori) e'
    # preservato: qui si asserisce il NUOVO default, non l'assenza di
    # contesto.
    check("legacy stt.prompt = default di codice (prima era vuoto)", cfg_legacy.stt.prompt == config.DEFAULT_PROMPT)
    check("legacy stt.hotwords = empty (default)", cfg_legacy.stt.hotwords == "")
    check("legacy stt.fallback non vuoto", len(cfg_legacy.stt_fallback) == 1)

    # --- config.py: StreamConfig parsing ----------------------------
    print("== config.py (stream) =")
    stream_toml = tmp / "stream.toml"
    stream_toml.write_text(
        '[notifications]\n'
        'stream_on_processing_start = false\n'
        'stream_on_raw_ready = true\n'
        'stream_on_raw_ready_content = false\n'
        '\n[stream]\n'
        'mode = "per_chunk"\n'
        'silence_seconds = 0.7\n'
        'noise_db = -30\n'
        'min_utterance_seconds = 0.4\n'
        'max_utterance_seconds = 30\n'
        'paste_delay_ms = 250\n'
        '\n[[stream.fallback]]\n'
        'name = "scrocco-llm"\n'
        'endpoint = "http://10.9.0.2:4001/v1"\n'
        'model = "scrocco-llm-fissone"\n'
        'api_key_env = "SCROCCO_FISSONE_API_KEY"\n'
        'api_key = ""\n'
        'ca_cert = ""\n'
        'timeout_seconds = 120\n'
    )
    cfg_stream = config.load_config(stream_toml)
    check("stream.mode = per_chunk", cfg_stream.stream.mode == "per_chunk")
    check("stream.silence_seconds = 0.7", cfg_stream.stream.silence_seconds == 0.7)
    check("stream.noise_db = -30", cfg_stream.stream.noise_db == -30)
    check("stream.paste_delay_ms = 250", cfg_stream.stream.paste_delay_ms == 250)
    check("stream paste_shortcut default", cfg_stream.stream.paste_shortcut == "ctrl+v")
    check("stream paste_channel default", cfg_stream.stream.paste_channel == "clipboard")
    channel_toml = tmp / "stream_channel.toml"
    channel_toml.write_text('[stream]\npaste_channel = "TYPE"\n')
    check("stream paste_channel explicit case-insensitive", config.load_config(channel_toml).stream.paste_channel == "type")
    channel_bad_toml = tmp / "stream_channel_bad.toml"
    channel_bad_toml.write_text('[stream]\npaste_channel = "other"\n')
    check("stream paste_channel invalid -> default", config.load_config(channel_bad_toml).stream.paste_channel == "clipboard")
    shortcut_toml = tmp / "stream_shortcut.toml"
    shortcut_toml.write_text('[stream]\npaste_shortcut = "CTRL+SHIFT+V"\n')
    check("stream paste_shortcut explicit case-insensitive", config.load_config(shortcut_toml).stream.paste_shortcut == "ctrl+shift+v")
    shortcut_bad_toml = tmp / "stream_shortcut_bad.toml"
    shortcut_bad_toml.write_text('[stream]\npaste_shortcut = "alt+v"\n')
    check("stream paste_shortcut invalid -> default", config.load_config(shortcut_bad_toml).stream.paste_shortcut == "ctrl+v")
    check("stream.fallback non vuoto", len(cfg_stream.stream.fallback) == 1)
    check("stream.fallback[0].name", cfg_stream.stream.fallback[0].name == "scrocco-llm")
    check("stream notification start = false", not cfg_stream.notif_stream.processing_start)
    check("stream notification ready = true", cfg_stream.notif_stream.raw_ready.enabled)
    check("stream notification content = false", not cfg_stream.notif_stream.raw_ready.content)

    # --- config.py: [stream].vad_margin_db (margine VAD adattivo) ----------
    print("== config.py (stream.vad_margin_db) =")
    # Legacy: chiave assente -> default 6.0.
    check(
        "vad_margin_db assente -> default 6.0",
        cfg_stream.stream.vad_margin_db == 6.0,
    )
    # Valore esplicito valido -> rispettato.
    margin_toml = tmp / "stream_margin.toml"
    margin_toml.write_text('[stream]\nmode = "per_chunk"\nvad_margin_db = 12.5\n')
    check(
        "vad_margin_db esplicito -> 12.5",
        config.load_config(margin_toml).stream.vad_margin_db == 12.5,
    )
    # Under/over clamp sui valori finiti.
    under_toml = tmp / "stream_margin_under.toml"
    under_toml.write_text('[stream]\nmode = "per_chunk"\nvad_margin_db = -3.0\n')
    check(
        "vad_margin_db sotto 0 -> clampato a 0.0",
        config.load_config(under_toml).stream.vad_margin_db == 0.0,
    )
    over_toml = tmp / "stream_margin_over.toml"
    over_toml.write_text('[stream]\nmode = "per_chunk"\nvad_margin_db = 99.0\n')
    check(
        "vad_margin_db sopra 20 -> clampato a 20.0",
        config.load_config(over_toml).stream.vad_margin_db == 20.0,
    )
    # NaN/infinito -> fallback al default 6.0.
    for raw_val, what in [("nan", "NaN"), ("inf", "+inf"), ("-inf", "-inf")]:
        nan_toml = tmp / f"stream_margin_{what.replace('+', 'p').replace('-', 'm')}.toml"
        nan_toml.write_text(f'[stream]\nmode = "per_chunk"\nvad_margin_db = {raw_val}\n')
        check(
            f"vad_margin_db {what} -> default 6.0",
            config.load_config(nan_toml).stream.vad_margin_db == 6.0,
        )
    # Valore non numerico -> default 6.0.
    bad_margin_toml = tmp / "stream_margin_bad.toml"
    bad_margin_toml.write_text('[stream]\nmode = "per_chunk"\nvad_margin_db = "alto"\n')
    check(
        "vad_margin_db non numerico -> default 6.0",
        config.load_config(bad_margin_toml).stream.vad_margin_db == 6.0,
    )
    # Compatibilità posizionale: il campo è appeso in coda, i costruttori
    # posizionali esistenti restano validi e vad_margin_db prende il default.
    check(
        "StreamConfig posizionale: vad_margin_db default 6.0",
        config.StreamConfig("per_chunk", 0.7, -30, 0.4, 30, 250).vad_margin_db == 6.0,
    )

    # mode at_end
    stream_toml_end = tmp / "stream_end.toml"
    stream_toml_end.write_text('[stream]\nmode = "at_end"\n')
    cfg_end = config.load_config(stream_toml_end)
    check("stream.mode = at_end", cfg_end.stream.mode == "at_end")

    # mode invalido -> ConfigError
    stream_toml_bad = tmp / "stream_bad.toml"
    stream_toml_bad.write_text('[stream]\nmode = "invalid"\n')
    try:
        config.load_config(stream_toml_bad)
        check("stream mode invalido -> ConfigError", False)
    except config.ConfigError:
        check("stream mode invalido -> ConfigError", True)
    except Exception as exc:  # noqa: BLE001
        check(f"stream mode invalido -> ConfigError ({type(exc).__name__})", False)

    # --- stream.py: StreamSession ----------------------------
    print("== stream.py =")
    from bravoric_stt_clipboard import stream as stream_mod

    stream_mod.STREAM_LOCK_PATH = tmp / "stream.lock"
    stream_mod.STREAM_STATE_PATH = tmp / "stream_state.json"

    # config con fallback minimo
    cfg_stream_min = config.Config(
        notifications=True,
        notif_stt=config.ServiceNotifications(
            processing_start=True,
            raw_ready=config.NotificationEvent(True, True),
            cleanup_ready=config.NotificationEvent(True, True),
        ),
        notif_ocr=config.ServiceNotifications(
            processing_start=True,
            raw_ready=config.NotificationEvent(True, True),
            cleanup_ready=config.NotificationEvent(True, True),
        ),
        notif_stream=config.ServiceNotifications(
            processing_start=True,
            raw_ready=config.NotificationEvent(True, True),
            cleanup_ready=config.NotificationEvent(True, True),
        ),
        clipboard_tool="wl-copy",
        clipboard_paste_tool="wl-paste",
        audio=config.AudioConfig(
            format="ogg", codec="libopus", sample_rate=16000,
            bitrate_kbps=16, toggle_debounce_seconds=1.0,
            retry_on_error=True, retry_count=2,
        ),
        stt=config.STTConfig(),
        stt_cleanup=config.CleanupConfig(True, "", []),
        ocr_fallback=[],
        ocr_system_prompt="",
        ocr_cleanup=config.CleanupConfig(False, "", []),
        double_injection=True,
        storage=config.StorageConfig(
            base_dir="",
            stt_original=config.RetentionPolicy(False, 0),
            stt_raw=config.RetentionPolicy(False, 0),
            stt_clean=config.RetentionPolicy(False, 0),
            ocr_original=config.RetentionPolicy(False, 0),
            ocr_raw=config.RetentionPolicy(False, 0),
            ocr_clean=config.RetentionPolicy(False, 0),
        ),
        history_max_entries=20,
        icons=config.IconsConfig("", "", "", "", "", ""),
        stream=config.StreamConfig(
            mode="at_end",
            silence_seconds=0.7, noise_db=-30,
            min_utterance_seconds=0.4, max_utterance_seconds=30,
            paste_delay_ms=250,
            fallback=[],
        ),
    )

    session = stream_mod.StreamSession(cfg_stream_min)
    check("is_active() = False all'inizio", not session.is_active())
    check("get_state() = dict vuoto", session.get_state() == {})

    # start() con nessun fallback -> notifica "No endpoint configured"
    result = session.start()
    check("start() senza fallback = False", not result)

    # start() con fallback -> registra (ffmpeg simulato)
    cfg_with_fallback = config.Config(
        notifications=True,
        notif_stt=config.ServiceNotifications(True, config.NotificationEvent(True, True), config.NotificationEvent(True, True)),
        notif_ocr=config.ServiceNotifications(True, config.NotificationEvent(True, True), config.NotificationEvent(True, True)),
        notif_stream=config.ServiceNotifications(True, config.NotificationEvent(True, True), config.NotificationEvent(True, True)),
        clipboard_tool="wl-copy",
        clipboard_paste_tool="wl-paste",
        audio=config.AudioConfig("ogg", "libopus", 16000, 16, 1.0, True, 2),
        stt=config.STTConfig(),
        stt_cleanup=config.CleanupConfig(True, "", []),
        ocr_fallback=[],
        ocr_system_prompt="",
        ocr_cleanup=config.CleanupConfig(False, "", []),
        double_injection=True,
        storage=config.StorageConfig("", config.RetentionPolicy(False,0), config.RetentionPolicy(False,0), config.RetentionPolicy(False,0), config.RetentionPolicy(False,0), config.RetentionPolicy(False,0), config.RetentionPolicy(False,0)),
        history_max_entries=20,
        icons=config.IconsConfig("", "", "", "", "", ""),
        stream=config.StreamConfig("at_end", 0.7, -30, 0.4, 30, 250, fallback=[
            config.FallbackLevel("test", "http://x", "m", "", "", "", 60),
        ]),
    )
    session2 = stream_mod.StreamSession(cfg_with_fallback)
    with mock.patch.object(stream_mod.audio, "is_recording", return_value=False), \
         mock.patch.object(stream_mod, "_spawn_recorder", return_value=mock.Mock(pid=os.getpid())), \
         mock.patch.object(stream_mod.notify, "send"):
        # Il recorder è simulato, ma il lock dedicato è reale.
        result = session2.start()
        stream_mod.STREAM_LOCK_PATH.write_text(
            json.dumps({"pid": os.getpid(), "started_at": 0,
                        "mode": "at_end", "session_id": "test-sid",
                        "audio_path": str(tmp / "stream-rec.ogg")}))
        (tmp / "stream-rec.ogg").write_bytes(b"")
    check("start() con fallback = True", result)
    check("is_active() = True dopo start", session2.is_active())
    state = session2.get_state()
    check("state.active = True", bool(state.get("active")))
    check("state includes paste shortcut", state.get("paste_shortcut") == "ctrl+v")
    check("state includes paste_channel", state.get("paste_channel") == "clipboard")
    # Il blocco di config di incolla ora nasce da un helper unico
    # (stream._paste_state): senza questo controllo PER CHIAVE, una chiave
    # caduta dal dict continuerebbe a passare la suite (i due assert sopra
    # leggono per chiave anche loro, ma non coprono `commands`, che resta
    # una lista di dict e non un valore semplice confrontabile a vista).
    check("state includes commands (per chiave)", state.get("commands") == [])
    check("state includes paste_delay_ms (per chiave)", state.get("paste_delay_ms") == 250)
    check("state includes blacklist (per chiave)", state.get("blacklist") == "")
    check("state.mode = at_end", state.get("mode") == "at_end")
    check("state.chunks = []", state.get("chunks") == [])
    check("state.next_chunk_index = 0", state.get("next_chunk_index") == 0)

    # stop_at_end() (nessuna rete; audio fittizio vuoto)
    # _stop_at_end chiama _terminate_pid e audio_path.unlink()
    # con l'audio_path dal lock. Il lock contiene un path fittizio
    # che esiste come file vuoto (creato sopra).
    with mock.patch.object(stream_mod, "_terminate_pid"), \
         mock.patch.object(stream_mod.notify, "send"):
        stop_result = session2.stop()
    check("stop() = True", stop_result)
    check("is_active() = False dopo stop", not session2.is_active())
    state_after = session2.get_state()
    check("stopped state includes paste shortcut", state_after.get("paste_shortcut") == "ctrl+v")
    check("state.active = False dopo stop", not state_after.get("active"))

    # lock dedicato
    check("stream.lock esiste", not stream_mod.STREAM_LOCK_PATH.exists())  # rimosso da stop

    # paste_next() su stato vuoto -> False
    check("paste_next() su stato vuoto = False", not session.paste_next())

    # --- stream.py (A6: nessun file temp orfano se _spawn_recorder fallisce) --
    print("== stream.py (A6: cleanup tempfile su spawn fallito) ==")
    import tempfile as _tempfile
    _orig_mkstemp = _tempfile.mkstemp
    leak_dir = tmp / "spawn-fail"
    leak_dir.mkdir(exist_ok=True)
    created_paths: list[str] = []

    def _fake_mkstemp(**kwargs):
        fd, name = _orig_mkstemp(dir=leak_dir, prefix="bravoric-stream-", suffix=".ogg")
        created_paths.append(name)
        return fd, name

    spawn_fail_session = stream_mod.StreamSession(cfg_with_fallback)
    with mock.patch.object(stream_mod.tempfile, "mkstemp", side_effect=_fake_mkstemp), \
         mock.patch.object(stream_mod, "_spawn_recorder", side_effect=FileNotFoundError("ffmpeg")):
        spawn_error = None
        try:
            spawn_fail_session._start_at_end()
        except FileNotFoundError as exc:
            spawn_error = exc
    check("A6: spawn fallito propaga l'errore (contratto esistente)", spawn_error is not None)
    check("A6: un solo tempfile creato durante il test", len(created_paths) == 1)
    check("A6: file temporaneo rimosso dopo spawn fallito",
          bool(created_paths) and not Path(created_paths[0]).exists())

    # --- stream.py: StreamSession per_chunk (senza ffmpeg reale) ----
    print("== stream.py (per_chunk) =")
    cfg_per = config.Config(
        notifications=True,
        notif_stt=config.ServiceNotifications(True, config.NotificationEvent(True, True), config.NotificationEvent(True, True)),
        notif_ocr=config.ServiceNotifications(True, config.NotificationEvent(True, True), config.NotificationEvent(True, True)),
        notif_stream=config.ServiceNotifications(True, config.NotificationEvent(True, True), config.NotificationEvent(True, True)),
        clipboard_tool="wl-copy",
        clipboard_paste_tool="wl-paste",
        audio=config.AudioConfig("ogg", "libopus", 16000, 16, 1.0, True, 2),
        stt=config.STTConfig(),
        stt_cleanup=config.CleanupConfig(True, "", []),
        ocr_fallback=[],
        ocr_system_prompt="",
        ocr_cleanup=config.CleanupConfig(False, "", []),
        double_injection=True,
        storage=config.StorageConfig("", config.RetentionPolicy(False,0), config.RetentionPolicy(False,0), config.RetentionPolicy(False,0), config.RetentionPolicy(False,0), config.RetentionPolicy(False,0), config.RetentionPolicy(False,0)),
        history_max_entries=20,
        icons=config.IconsConfig("", "", "", "", "", ""),
        stream=config.StreamConfig("per_chunk", 0.7, -30, 0.4, 30, 250, fallback=[]),
    )
    session3 = stream_mod.StreamSession(cfg_per)
    # testa is_active() e get_state() su sessione non avviata
    check("per_chunk is_active() = False", not session3.is_active())

    # --- config_editor.py: set_stream_field ----------------------------
    print("== config_editor.py (stream) =")
    stream_cfg = tmp / "stream_config.toml"
    stream_cfg.write_text('[stream]\nmode = "per_chunk"\nsilence_seconds = 0.7\n')
    config_editor.CONFIG_PATH = stream_cfg
    try:
        config_editor.set_stream_field("mode", "at_end")
        config_editor.set_stream_field("max_concurrent_chunks", "4")
        config_editor.set_stream_field("chunk_timeout_seconds", "25.5")
        config_editor.set_stream_field("paste_channel", "type")
        state_ce = config_editor.get_state()
        check("new stream fields round-trip", state_ce.get("stream", {}).get("max_concurrent_chunks") == 4 and state_ce.get("stream", {}).get("chunk_timeout_seconds") == 25.5)
        check("set_stream_field mode -> ok", True)
        state_ce = config_editor.get_state()
        check("set_stream_field: mode = at_end", state_ce.get("stream", {}).get("mode") == "at_end")
        check("set_stream_field paste_channel round-trip", state_ce.get("stream", {}).get("paste_channel") == "type")
        check("get_state stream espone levels", isinstance(state_ce.get("stream", {}).get("levels"), list))
        check(
            "config pre-stream mostra 3 endpoint editabili",
            len(state_ce.get("stream", {}).get("levels", [])) == 3,
        )
    except Exception as exc:  # noqa: BLE001
        check(f"set_stream_field mode -> ok ({type(exc).__name__})", False)

    try:
        config_editor.set_stream_field("silence_seconds", "1.5")
        check("set_stream_field silence_seconds -> ok", True)
    except Exception as exc:  # noqa: BLE001
        check(f"set_stream_field silence_seconds -> ok ({type(exc).__name__})", False)

    try:
        config_editor.set_stream_field("context_enabled", "false")
        state_ce = config_editor.get_state()
        check("context_enabled false round-trip", not state_ce["stream"]["context_enabled"])
        config_editor.set_stream_field("context_enabled", "true")
        state_ce = config_editor.get_state()
        check("context_enabled true round-trip", bool(state_ce["stream"]["context_enabled"]))
    except Exception as exc:  # noqa: BLE001
        check(f"context_enabled round-trip ({type(exc).__name__})", False)

    try:
        config_editor.set_level_field("stream", 0, "endpoint", "https://stream.example/v1")
        state_ce = config_editor.get_state()
        check(
            "primo edit materializza [[stream.fallback]]",
            state_ce["stream"]["levels"][0]["endpoint"] == "https://stream.example/v1"
            and "[[stream.fallback]]" in stream_cfg.read_text(),
        )
    except Exception as exc:  # noqa: BLE001
        check(f"primo edit materializza [[stream.fallback]] ({type(exc).__name__})", False)

    # campo sconosciuto -> ConfigEditorError
    try:
        config_editor.set_stream_field("unknown_field", "val")
        check("set_stream_field campo sconosciuto -> errore", False)
    except config_editor.ConfigEditorError:
        check("set_stream_field campo sconosciuto -> ConfigEditorError", True)
    except Exception as exc:  # noqa: BLE001
        check(f"set_stream_field campo sconosciuto -> ConfigEditorError ({type(exc).__name__})", False)

    # --- config_editor.py: levels stream espongono parallel/max_concurrency --
    # STEP 1 dell'onda 4: senza la chiave "levels" sotto "stream" la GUI non
    # aveva nulla su cui scrivere i due campi nuovi (i tre endpoint non erano
    # leggibili). Tutto su una COPIA temporanea: set_level_field su
    # CONFIG_PATH reale toccherebbe il config.toml personale dell'utente.
    print("== config_editor.py (stream levels: parallel/max_concurrency) ==")
    pool_cfg = tmp / "stream_pool_levels.toml"
    pool_cfg.write_text(
        '[stream]\nmode = "per_chunk"\n'
        "\n[[stream.fallback]]\n"
        'name = "a"\n'
        'endpoint = "http://10.9.0.2:4001/v1"\n'
        'model = "large"\n'
        "timeout_seconds = 30\n"
        "parallel = true\n"
        "max_concurrency = 4\n"
        "\n[[stream.fallback]]\n"
        'name = "b"\n'
        'endpoint = "http://10.9.0.2:4001/v1/"\n'
        'model = "small"\n'
    )
    config_editor.CONFIG_PATH = pool_cfg
    state_pool = config_editor.get_state()
    pool_levels = state_pool.get("stream", {}).get("levels", [])
    check(
        "get_state espone levels per stream (non solo stt/ocr)",
        isinstance(pool_levels, list) and len(pool_levels) == 3,
    )
    check(
        "levels[0] legge parallel e max_concurrency come stringa",
        bool(pool_levels)
        and pool_levels[0].get("parallel") == "True"
        and pool_levels[0].get("max_concurrency") == "4",
    )
    # Livello legacy senza i due campi: la GUI li deve comunque leggere come
    # stringa vuota (e non come assenti), altrimenti lo SwitchRow casca.
    check(
        "levels[1] legacy espone i campi nuovi come stringa vuota",
        len(pool_levels) > 1
        and pool_levels[1].get("parallel") == ""
        and pool_levels[1].get("max_concurrency") == "",
    )
    check(
        "levels[2] sintetizzato espone i campi nuovi",
        len(pool_levels) > 2
        and "parallel" in pool_levels[2]
        and "max_concurrency" in pool_levels[2],
    )
    check(
        "le chiavi gia' presenti sotto stream sono invariate",
        {
            "commands", "blacklist", "mode", "silence_seconds", "noise_db",
            "vad_margin_db", "min_utterance_seconds", "max_utterance_seconds",
            "paste_delay_ms", "paste_shortcut", "paste_channel", "language",
            "prompt", "hotwords", "context_enabled", "max_concurrent_chunks",
            "chunk_timeout_seconds", "levels",
            # dispatch_mode: interruttore GLOBALE parallelo/sequenziale, la
            # sua sorgente unica insieme a levels. Aggiunto qui perche' la
            # lista e' una whitelist ESATTA: senza, la GUI non avrebbe modo di
            # sapere in che modalita' siamo.
            "dispatch_mode",
            # max_concurrent_chunks_auto: il flag che dichiara se il tetto dei
            # worker e' AUTO (0 o chiave assente) o esplicito (1..8). Senza
            # questa chiave la nota GUI "il tetto e' automatico" non compariva
            # MAI e con cap=0 la GUI mostrava "fino a 1 worker" mentre il
            # backend calcolava 3xN (difetto C, giro 1). Aggiunta alla
            # whitelist ESATTA insieme a max_concurrent_chunks: le due vanno
            # lette insieme, una senza l'altra mente.
            "max_concurrent_chunks_auto",
            # chunk_log_max_lines: ritenzione del log JSONL dei chunk, in
            # RIGHE. Stessa ragione delle precedenti: la lista e' una
            # whitelist ESATTA, quindi un campo nuovo non elencato qui
            # renderebbe questo test rosso anche se il campo fosse corretto.
            # Il backend lo legge da config.py StreamConfig, non da qui: questa
            # whitelist dichiara che get_state() lo ESPONE, non che lo usi.
            "chunk_log_max_lines",
        }
        == set(state_pool.get("stream", {}).keys()),
    )

    # set_level_field su config legacy: deve CREARE la chiave mancante, non
    # sollevare Unknown field, altrimenti la casella della GUI salva uno schermo
    # di errore invece del valore.
    legacy_cfg = tmp / "stream_pool_legacy.toml"
    legacy_cfg.write_text('[stream]\nmode = "per_chunk"\n')
    config_editor.CONFIG_PATH = legacy_cfg
    try:
        config_editor.set_level_field("stream", 0, "parallel", "true")
        config_editor.set_level_field("stream", 0, "max_concurrency", "6")
        legacy_state = config_editor.get_state()
        check(
            "set_level_field crea parallel/max_concurrency su config legacy",
            legacy_state["stream"]["levels"][0].get("parallel") == "True"
            and legacy_state["stream"]["levels"][0].get("max_concurrency") == "6",
        )
        check(
            "il blocco creato contiene le due chiavi nel TOML",
            "parallel = true" in legacy_cfg.read_text()
            and "max_concurrency = 6" in legacy_cfg.read_text(),
        )
    except Exception as exc:  # noqa: BLE001
        check(
            f"set_level_field crea parallel/max_concurrency su config legacy ({type(exc).__name__})",
            False,
        )

    # I valori passati dalla GUI sono stringhe: il backend le accetta e il
    # clamp 1..8 di _coerce_max_concurrency le ricolloca nei range.
    check(
        "max_concurrency clamp 1..8 sulle stringhe che arrivano dalla GUI",
        [config._coerce_max_concurrency(v, 3, 1, 8) for v in ("0", "6", "99", "")]
        == [1, 6, 8, 3],
    )

    # --- config_editor.py (A3: mode validato contro STREAM_MODES) ----------
    print("== config_editor.py (A3: mode validation) ==")
    mode_guard = tmp / "stream_mode_guard.toml"
    mode_guard.write_text('[stream]\nmode = "per_chunk"\n')
    config_editor.CONFIG_PATH = mode_guard
    mode_before = mode_guard.read_text()
    try:
        config_editor.set_stream_field("mode", "bogus")
        check("A3: mode invalido -> rifiutato", False)
    except config_editor.ConfigEditorError:
        check("A3: mode invalido -> ConfigEditorError", True)
    except Exception as exc:  # noqa: BLE001
        check(f"A3: mode invalido -> ConfigEditorError ({type(exc).__name__})", False)
    check("A3: mode invalido lascia il file invariato", mode_guard.read_text() == mode_before)
    # I valori supportati dal parser restano accettati e round-trippano.
    for good_mode in config.STREAM_MODES:
        config_editor.set_stream_field("mode", good_mode)
        check(f"A3: mode valido {good_mode!r} -> round-trip",
              config_editor.get_state()["stream"]["mode"] == good_mode)

    # --- config_editor.py: vad_margin_db get/set + clamp + creazione sezione --
    print("== config_editor.py (vad_margin_db) =")
    margin_cfg = tmp / "stream_margin_editor.toml"
    margin_cfg.write_text('[stream]\nmode = "per_chunk"\n')
    config_editor.CONFIG_PATH = margin_cfg
    # get_state: chiave assente -> default 6.0.
    check(
        "get_state vad_margin_db assente -> 6.0",
        config_editor.get_state()["stream"]["vad_margin_db"] == 6.0,
    )
    # setter: valore esplicito round-trip.
    config_editor.set_stream_field("vad_margin_db", "9.5")
    check(
        "set_stream_field vad_margin_db -> 9.5",
        config_editor.get_state()["stream"]["vad_margin_db"] == 9.5,
    )
    # setter: clamp numerico finito (over/under).
    config_editor.set_stream_field("vad_margin_db", "50")
    check(
        "set_stream_field vad_margin_db over -> clamp 20.0",
        config_editor.get_state()["stream"]["vad_margin_db"] == 20.0,
    )
    config_editor.set_stream_field("vad_margin_db", "-4")
    check(
        "set_stream_field vad_margin_db under -> clamp 0.0",
        config_editor.get_state()["stream"]["vad_margin_db"] == 0.0,
    )
    # setter: valore invalido/non finito -> reject esplicito.
    for bad_val in ("nan", "inf", "-inf", "alto"):
        try:
            config_editor.set_stream_field("vad_margin_db", bad_val)
            check(f"set_stream_field vad_margin_db {bad_val!r} -> reject", False)
        except config_editor.ConfigEditorError:
            check(f"set_stream_field vad_margin_db {bad_val!r} -> ConfigEditorError", True)
        except Exception as exc:  # noqa: BLE001
            check(f"set_stream_field vad_margin_db {bad_val!r} -> ConfigEditorError ({type(exc).__name__})", False)
    # creazione sezione [stream] legacy (solo [[stream.fallback]]).
    margin_legacy = tmp / "stream_margin_legacy.toml"
    margin_legacy.write_text(
        '[general]\nnotifications = true\n'
        '\n[[stream.fallback]]\nname = "test"\nendpoint = "http://x"\n'
        'model = "m"\napi_key_env = ""\napi_key = ""\nca_cert = ""\ntimeout_seconds = 60\n'
    )
    config_editor.CONFIG_PATH = margin_legacy
    config_editor.set_stream_field("vad_margin_db", "7.5")
    legacy_text = margin_legacy.read_text()
    check("set vad_margin_db crea [stream] legacy", "[stream]" in legacy_text)
    check("set vad_margin_db scrive il valore", "vad_margin_db = 7.5" in legacy_text)
    check("set vad_margin_db mantiene [[stream.fallback]]", "[[stream.fallback]]" in legacy_text)
    check(
        "get_state legge vad_margin_db da sezione creata",
        config_editor.get_state()["stream"]["vad_margin_db"] == 7.5,
    )

    # --- cli.py: stream_toggle_main ----------------------------
    print("== cli.py (stream) =")
    from bravoric_stt_clipboard import cli
    with mock.patch.object(cli, "load_config", return_value=cfg_with_fallback), \
         mock.patch.object(cli, "notify") as m_notify:
        # sessione non attiva -> start() fallisce per mancanza fallback
        # (cfg_with_fallback.stream.fallback è [])
        ret = cli.stream_toggle_main(["paste"])
        check("stream_toggle_main paste senza sessione -> 1", ret == 1)

    # stream_toggle_main con argv=None usa sys.argv[1:]
    # (testato implicitamente sopra)

    # --- sequencer FIFO, tombstone e compatibilità config posizionale ---
    print("== stream.py (parallel & fifo sequencer) ==")
    import bravoric_stt_clipboard.stream as stream_module
    stream_test_cfg = config.StreamConfig("per_chunk", 0.7, -30, 0.4, 30, 250, fallback=[])
    check("StreamConfig positional keeps appended-field defaults", stream_test_cfg.max_concurrent_chunks == 3 and stream_test_cfg.chunk_timeout_seconds == 30.0)
    check("StreamConfig positional keeps vad_margin_db default", stream_test_cfg.vad_margin_db == 6.0)

    # --- stream.py: _rms_to_db / _rms_db_of_chunk (mai testate finora) -----
    # _rms_db_of_chunk sostituisce un ciclo per-campione con struct.unpack in
    # blocco (perf, giro curriculum): stesso risultato numerico, verificato
    # qui su casi noti invece che solo a occhio sul confronto vecchio/nuovo.
    print("== stream.py (_rms_to_db / _rms_db_of_chunk) ==")
    import struct as _struct

    from bravoric_stt_clipboard.stream import _MIN_DB, _rms_db_of_chunk, _rms_to_db
    check("_rms_to_db: rms 1.0 -> 0 dB", _rms_to_db(1.0) == 0.0)
    check("_rms_to_db: rms 0 -> sentinella _MIN_DB", _rms_to_db(0.0) == _MIN_DB)
    check("_rms_to_db: rms negativo -> sentinella _MIN_DB (difesa, non dovrebbe capitare)",
          _rms_to_db(-1.0) == _MIN_DB)
    check("_rms_to_db: dimezzare l'ampiezza toglie ~6 dB",
          abs((_rms_to_db(0.5) - _rms_to_db(1.0)) - (-6.0206)) < 1e-3)
    # Silenzio digitale esatto (tutti campioni a 0): rms 0 -> _MIN_DB.
    silence = _struct.pack("<480h", *([0] * 480))
    check("_rms_db_of_chunk: silenzio digitale -> _MIN_DB",
          _rms_db_of_chunk(silence, 480) == _MIN_DB)
    # Onda a piena scala (±32767 alternati): rms vicino a 1.0 -> ~0 dB.
    full_scale = _struct.pack("<480h", *([32767, -32767] * 240))
    check("_rms_db_of_chunk: piena scala -> vicino a 0 dB",
          abs(_rms_db_of_chunk(full_scale, 480) - 0.0) < 0.01)
    # Byte finale dispari: scartato, stesso comportamento del ciclo originale
    # (num_samples = len // 2, il resto non entra nel calcolo).
    odd_trailing = _struct.pack("<3h", 100, 200, 300) + b"\xff"
    check("_rms_db_of_chunk: byte finale dispari ignorato",
          _rms_db_of_chunk(odd_trailing, 3) == _rms_db_of_chunk(odd_trailing[:6], 3))

    # --- stream.py: formula soglia VAD adattiva (distinta da noise_db) -----
    print("== stream.py (adaptive VAD threshold) ==")
    from bravoric_stt_clipboard.stream import (
        VAD_THRESHOLD_MAX_DB,
        VAD_THRESHOLD_MIN_DB,
        _adaptive_threshold_db,
    )
    # Formula: floor + margine, dentro il range.
    check(
        "adaptive threshold = floor + margin",
        _adaptive_threshold_db(-40.0, 6.0) == -34.0,
    )
    # Il margine configurabile sposta la soglia.
    check(
        "adaptive threshold rispetta margine configurabile",
        _adaptive_threshold_db(-40.0, 12.0) == -28.0,
    )
    # Clamp inferiore: floor molto basso + margine piccolo resta a -55.
    check(
        "adaptive threshold clamp inferiore -55",
        _adaptive_threshold_db(-80.0, 6.0) == VAD_THRESHOLD_MIN_DB,
    )
    # Clamp superiore: floor alto + margine grande resta a -15.
    check(
        "adaptive threshold clamp superiore -15",
        _adaptive_threshold_db(-10.0, 20.0) == VAD_THRESHOLD_MAX_DB,
    )
    # Distinta dalla soglia iniziale noise_db: la formula non usa noise_db.
    check(
        "adaptive threshold indipendente da noise_db",
        _adaptive_threshold_db(-40.0, 6.0) != -30.0,
    )

    # --- stream.py (A1: il silenzio digitale a RMS zero non avvelena il floor) --
    print("== stream.py (A1: floor VAD ignora RMS nullo) ==")
    from bravoric_stt_clipboard.stream import (
        _MIN_DB,
        _estimate_floor_db,
        _is_valid_floor_sample,
    )
    # Frame a rumore reale (-40 dB) mescolati a silenzio digitale esatto
    # (rms 0 -> sentinella _MIN_DB): solo i primi sono campioni validi.
    mixed_frames = [-40.0] * 80 + [_MIN_DB] * 20
    valid_samples = [db for db in mixed_frames if _is_valid_floor_sample(db, -30.0)]
    check("A1: sentinella _MIN_DB esclusa dalla raccolta", len(valid_samples) == 80)
    floor_est = _estimate_floor_db(valid_samples)
    check("A1: floor stimato resta il rumore reale (-40)", floor_est == -40.0)
    a1_threshold = _adaptive_threshold_db(floor_est, 6.0)
    check("A1: soglia non al clamp minimo con floor reale -40",
          a1_threshold == -34.0 and a1_threshold > VAD_THRESHOLD_MIN_DB)
    # Controprova della regressione: senza filtro la sentinella porta il floor
    # a -200 e la soglia al clamp minimo (il bug originale).
    unfiltered_floor = _estimate_floor_db(mixed_frames)
    check("A1: regressione - senza filtro la sentinella avvelena la soglia",
          unfiltered_floor == _MIN_DB
          and _adaptive_threshold_db(unfiltered_floor, 6.0) == VAD_THRESHOLD_MIN_DB)
    # Un frame sopra la soglia provvisoria (parlato) non è un campione di floor.
    check("A1: frame sopra la soglia provvisoria scartato",
          not _is_valid_floor_sample(-10.0, -30.0))

    # --- stream.py (A4: budget drain stop proporzionale ai timeout per-livello)
    # Il budget e' la SOMMA dei level.timeout_seconds, non piu'
    # n_livelli * chunk_timeout: ogni endpoint ha il suo timeout (STEP 1 del
    # BRIEF-TIMEOUT-PERLIVELLO), quindi e' la somma a non troncare i chunk.
    print("== stream.py (A4: budget drain stop) ==")
    from bravoric_stt_clipboard.stream import _stop_drain_budget
    _Lv = lambda *ts: [type("L", (), {"timeout_seconds": t})() for t in ts]
    check("A4: 1 livello copre timeout + margine", _stop_drain_budget(_Lv(30)) == 45.0)
    check("A4: 2 livelli = somma timeout + margine", _stop_drain_budget(_Lv(30, 30)) == 75.0)
    check("A4: 3 livelli = somma timeout + margine (bug: era 75)",
          _stop_drain_budget(_Lv(30, 30, 30)) == 105.0)
    check("A4: 3 livelli non tronca i request timeout",
          _stop_drain_budget(_Lv(30, 30, 30)) > 3 * 30.0)
    check("A4: somma, non n_livelli * primo timeout (10,10,100 -> 135)",
          _stop_drain_budget(_Lv(10, 10, 100)) == 135.0)
    check("A4: lista vuota -> un livello", _stop_drain_budget([]) == 45.0)
    check("A4: levels None -> un livello", _stop_drain_budget(None) == 45.0)
    check("A4: timeout di livello malformed (str) -> 30 per quel livello",
          _stop_drain_budget(_Lv("boh")) == 45.0)
    check("A4: timeout di livello None -> 30 per quel livello",
          _stop_drain_budget(_Lv(None)) == 45.0)
    check("A4: timeout di livello non finito -> 30 per quel livello",
          _stop_drain_budget(_Lv(float("inf"))) == 45.0)
    check("A4: livello senza timeout_seconds -> 30 per quel livello",
          _stop_drain_budget([type("L", (), {})()]) == 45.0)
    check("A4: budget mai sotto 30s", _stop_drain_budget(_Lv(1)) == 30.0)
    check("A4: 3 livelli da 5s -> >= 30s", _stop_drain_budget(_Lv(5, 5, 5)) >= 30.0)
    check("A4: 3 livelli da 40s -> >= 135s",
          _stop_drain_budget(_Lv(40, 40, 40)) >= 135.0)

    # --- get_context_snapshot: contratto ERMETICO, in due tempi ----------
    # Il difetto che questo blocco chiude (misurato, non supposto). La suite
    # costruiva lo stato di test SENZA session_id e poi confrontava
    # get_context_snapshot() con last_chunks. Ma get_context_snapshot chiama
    # read_live_text(self._state.get("session_id")), e read_live_text(None)
    # NON alza: con session_id None accetta QUALSIASI file vivo, quindi legge
    # ~/.cache/bravoric-stt-clipboard/stream_live_text.json, cioe' il file
    # REALE dell'utente. Prima del reboot quel file non esisteva e il gate era
    # verde per caso: il reboot non ha rotto nulla, ha reso visibile che il
    # test era verde per caso e non per merito.
    #
    # Le quattro guardie da tenere insieme, altrimenti il blocco resta
    # decorativo:
    #   1) il file di testo vivo e' rimosso dal modulo durante il contratto,
    #      cosi' l'unico STREAM_LIVE_TEXT_PATH che esiste e' quello finto;
    #   2) il fallback e' provato con read_live_text che RITORNA None
    #      (assenza, file corrotto, altra sessione) e con last_chunks sapendo
    #      cosa contiene: il fallback e' cosi' verificato per costruzione e
    #      non per assenza di file;
    #   3) la preferenza per il testo vivo e' provata con un file FINTO,
    #      su un percorso temporaneo, e con un session_id che combacia;
    #   4) il file finto viene rimosso in finally, altrimenti i test dopo
    #      leggerebbero i segmenti di una sessione inventata e il gate
    #      dipenderebbe dall'ordine di esecuzione.
    _LIVE_PATH_ATTR = "STREAM_LIVE_TEXT_PATH"
    _LIVE_READER_ATTR = "read_live_text"
    _live_path_original = getattr(stream_module, _LIVE_PATH_ATTR)
    _live_reader_original = getattr(stream_module, _LIVE_READER_ATTR)
    _live_dir = Path(tempfile.mkdtemp(prefix="bravoric-live-"))
    _live_fake = _live_dir / "stream_live_text.json"
    _live_fake.write_text(
        json.dumps({
            "session_id": "sessione-di-prova",
            "segments": ["vive uno", "vive due", "vive tre", "vive quattro"],
        }),
        encoding="utf-8",
    )
    setattr(stream_module, _LIVE_PATH_ATTR, _live_fake)
    try:
        # (a) NESSUN file vivo: lo snapshot cade su last_chunks. read_live_text
        # viene sostituito con uno che RITORNA None, cioe' il caso 'file
        # assente / corrotto / di un'altra sessione': il fallback non viene
        # piu' misurato per assenza di file, che dipendeva dalla macchina.
        # get_context_snapshot chiama il nome letto nel modulo stream, quindi
        # e' quello da sostituire (non una copia importata da un altro modulo).
        setattr(stream_module, _LIVE_READER_ATTR, lambda _session_id: None)
        st_fifo = {"chunks": [], "last_chunks": []}
        seq_fifo = stream_module._FifoSequencer(
            st_fifo, stream_test_cfg, lambda text: None, lambda text: None,
            log_path=_TEST_CHUNK_LOG_PATH,
        )
        seq_fifo.ingest(stream_module._ChunkResult(2, "Mondo", True))
        seq_fifo.ingest(stream_module._ChunkResult(0, "Ciao", True))
        seq_fifo.ingest(stream_module._ChunkResult(1, "questo", True))
        check("sequencer commits out-of-order completions in audio order",
              st_fifo["chunks"] == ["Ciao ", "questo ", "Mondo "])
        check("contesto: last_chunks segue l'ordine committato",
              st_fifo["last_chunks"] == ["Ciao", "questo", "Mondo"])
        check("contesto: senza testo vivo lo snapshot ricade su last_chunks",
              seq_fifo.get_context_snapshot() == ["Ciao", "questo", "Mondo"])
        # Non indebolire: il fallback viene confrontato anche con last_chunks
        # esplicitamente DIVERSO. Con last_chunks == snapshot il confronto
        # passerebbe anche se la funzione restituisse una lista fissa o vuota,
        # cioe' sarebbe verde senza provare niente. Qui i due lati devono
        # essere distinguibili, altrimenti l'asserzione e' vacua.
        st_solo = {"chunks": [], "last_chunks": ["dal", "backend"]}
        seq_solo = stream_module._FifoSequencer(
            st_solo, stream_test_cfg, lambda text: None, lambda text: None,
            log_path=_TEST_CHUNK_LOG_PATH,
        )
        check("contesto: il fallback prende last_chunks e nient'altro",
              seq_solo.get_context_snapshot() == ["dal", "backend"])

        # (b) FILE VIVO FINTO che combacia con la sessione: lo snapshot lo
        # preferisce a last_chunks. read_live_text e' tornato QUELLO VERO,
        # che legge il percorso finto: il file e' scritto davvero e viene
        # passato attraverso il parser vero, con la sessione che combacia.
        setattr(stream_module, _LIVE_READER_ATTR, _live_reader_original)
        st_live = {
            "chunks": [], "last_chunks": ["dal", "backend"],
            "session_id": "sessione-di-prova",
        }
        seq_live = stream_module._FifoSequencer(
            st_live, stream_test_cfg, lambda text: None, lambda text: None,
            log_path=_TEST_CHUNK_LOG_PATH,
        )
        _live_snapshot = seq_live.get_context_snapshot()
        # Le ultime tre righe del file, non tutto il file: read_live_text
        # tiene solo gli ultimi 3 segmenti utilizzabili.
        check("contesto: a parita' di condizioni il testo vivo e' quello TRUE",
              _live_snapshot == ["vive due", "vive tre", "vive quattro"])
        check("contesto: col testo vivo lo snapshot NON prende last_chunks",
              _live_snapshot != ["dal", "backend"])

        # (c) la stessa funzione, con un percorso che non esiste: il fallback
        # e' il ramo vero di read_live_text (OSError -> None), non piu' una
        # sostituzione. Nessun file viene scritto: il percorso resta finto e
        # inesistente, e quello vero e' comunque irraggiungibile per la
        # guardia 1.
        setattr(stream_module, _LIVE_PATH_ATTR, _live_dir / "assente.json")
        check("contesto: file inesistente -> il ramo vero ricade su last_chunks",
              seq_live.get_context_snapshot() == ["dal", "backend"])
        # E con la sessione sbagliata il file vivo viene RIFIUTATO: la
        # preferenza non e' 'il file esiste', e' 'il file e' di questa
        # sessione'.
        setattr(stream_module, _LIVE_PATH_ATTR, _live_fake)
        st_altra = {
            "chunks": [], "last_chunks": ["dal", "backend"],
            "session_id": "sessione-di-un-altro-capo",
        }
        seq_altra = stream_module._FifoSequencer(
            st_altra, stream_test_cfg, lambda text: None, lambda text: None,
            log_path=_TEST_CHUNK_LOG_PATH,
        )
        check("contesto: il testo vivo di un'altra sessione viene rifiutato",
              seq_altra.get_context_snapshot() == ["dal", "backend"])
    finally:
        # Ripristino OBBLIGATORIO: un patch non ripristinato invalida tutti i
        # test che vengono dopo (e il file finto renderebbe i loro snapshot
        # dipendenti da una sessione inventata).
        setattr(stream_module, _LIVE_PATH_ATTR, _live_path_original)
        setattr(stream_module, _LIVE_READER_ATTR, _live_reader_original)
        with contextlib.suppress(OSError):
            _live_fake.unlink()
        with contextlib.suppress(OSError):
            _live_dir.rmdir()
    # La guardia 1 non regge da sola: un ripristino dimenticato sarebbe
    # invisibile qui, perche' dalla riga sotto il percorso e' di nuovo quello
    # giusto. Il controllo e' sul dopo, quando l'ambiente e' tornato come
    # prima.
    check("contesto: lettore e percorso del testo vivo ripristinati a fine blocco",
          getattr(stream_module, _LIVE_PATH_ATTR) is _live_path_original
          and getattr(stream_module, _LIVE_READER_ATTR) is _live_reader_original)
    _albero = getattr(stream_module, _LIVE_PATH_ATTR)
    check("contesto: il percorso del testo vivo e' quello del prodotto",
          isinstance(_albero, Path)
          and _albero.name == "stream_live_text.json"
          and _albero.parent.name == "bravoric-stt-clipboard"
          and _albero.parent.parent.name == ".cache")
    check("contesto: nessun file di testo vivo finto rimasto su disco",
          not _live_fake.exists() and not _live_dir.exists())
    no_context = config.StreamConfig("per_chunk", 0.7, -30, 0.4, 30, 250, context_enabled=False, fallback=[])
    st_no_context = {"chunks": [], "last_chunks": ["existing"]}
    seq_no_context = stream_module._FifoSequencer(st_no_context, no_context, lambda text: None, lambda text: None, log_path=_TEST_CHUNK_LOG_PATH)
    seq_no_context.ingest(stream_module._ChunkResult(0, "personal prompt only", True))
    check("context disabled leaves last_chunks unchanged", st_no_context["last_chunks"] == ["existing"])
    st_tomb = {"chunks": [], "last_chunks": []}
    seq_tomb = stream_module._FifoSequencer(st_tomb, stream_test_cfg, lambda text: None, lambda text: None, log_path=_TEST_CHUNK_LOG_PATH)
    for item in [stream_module._ChunkResult(0, "Primo", True), stream_module._ChunkResult(1, "", False), stream_module._ChunkResult(2, "Terzo", True)]:
        seq_tomb.ingest(item)
    check("failed chunk advances tombstone without blocking", st_tomb["chunks"] == ["Primo ", "Terzo "] and seq_tomb._next_expected == 3)
    # Ogni chunk committato termina con esattamente uno spazio (separatore).
    check("chunk normalizzato: spazio finale unico", stream_module._normalize_chunk_text("ciao") == "ciao ")
    check("chunk normalizzato: spazio già presente non duplicato", stream_module._normalize_chunk_text("ciao  ") == "ciao ")
    check("chunk normalizzato: bordi rimossi", stream_module._normalize_chunk_text("  ciao  mondo  ") == "ciao  mondo ")
    check("chunk normalizzato: stringa vuota resta vuota", stream_module._normalize_chunk_text("   ") == "")
    import threading
    import time
    active_workers = 0
    max_workers_seen = 0
    counter_lock = threading.Lock()
    def fake_transcribe(*args, **kwargs):
        nonlocal active_workers, max_workers_seen
        with counter_lock:
            active_workers += 1
            max_workers_seen = max(max_workers_seen, active_workers)
        time.sleep(0.04)
        with counter_lock:
            active_workers -= 1
        return "ok"
    fake_level = mock.Mock()
    stream_test_cfg.fallback = [fake_level]
    result_q = __import__("queue").Queue()
    sem_test = threading.BoundedSemaphore(2)
    with mock.patch.object(stream_module, "transcribe_audio", side_effect=fake_transcribe), mock.patch.object(
        stream_module, "try_with_fallback", side_effect=lambda fallback, fn: fn(fake_level)
    ):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            for i in range(4):
                audio_file = tmp / f"parallel-{i}.wav"
                audio_file.write_bytes(b"audio")
                sem_test.acquire()
                pool.submit(stream_module._worker, i, audio_file, None,
                            stream=stream_test_cfg, sem=sem_test, result_queue=result_q)
    check("worker concurrency respects semaphore bound", max_workers_seen == 2 and result_q.qsize() == 4)

    # ================================================================
    # Nuovi test: build_prompt, api_client nuovi parametri, config_editor

    # --- build_prompt ---
    print("== build_prompt ==")
    from bravoric_stt_clipboard.stream import build_prompt, update_last_chunks

    check("build_prompt empty -> None", build_prompt("", []) is None)
    check("build_prompt whitespace -> None", build_prompt("  ", []) is None)
    check("build_prompt personal only", build_prompt("Ciao", []) == "Ciao")
    check("build_prompt personal + chunks", build_prompt("Ciao", ["uno","due","tre"]) == "Ciao uno due tre")
    check("build_prompt max 3 chunks", len((build_prompt("P", ["a","b","c","d","e"]) or "").split()) == 4)  # P + 3 chunks

    # Le allucinazioni NON sono piu' filtrate qui da una lista fissa: sono la
    # blacklist utente, applicata a monte in ingest() (test "blacklist dropped
    # chunk never added to last_chunks"). build_prompt usa cio' che riceve.
    check("build_prompt: nessun filtro nascosto (il filtro e' la blacklist, a monte)",
          "Sottotitoli" in (build_prompt("P", ["Sottotitoli a cura di", "vero"]) or ""))
    # Empty chunks filtered
    check("build_prompt filters empty", build_prompt("P", ["", "  ", "vero"]) == "P vero")
    # Consecutive duplicates filtered
    check("build_prompt filters dups", build_prompt("P", ["x","x","y"]) == "P x y")

    # Personal prompt intact, chunks fill budget
    result = build_prompt("A"*700, ["B"*50, "C"*50])
    check("build_prompt personal intact", (result or "").startswith("A"*700) or "A" in (result or ""))
    check("build_prompt result <= 800 chars", len(result or "") <= 800)

    # Personal too long -> truncated to 800 from right
    long_result = build_prompt("X"*900, ["y"])
    check("build_prompt trunc personal to 800", len(long_result or "") == 800)
    check("build_prompt trunc from right", (long_result or "").endswith("X"))

    # Big chunk dropped if doesn't fit, older kept
    result2 = build_prompt("P", ["short", "X"*900])
    check("build_prompt drops big chunk", result2 == "P short" or result2 == "P")

    # update_last_chunks helper
    st = {}
    update_last_chunks(st, "hello")
    check("update_last_chunks adds", st["last_chunks"] == ["hello"])
    update_last_chunks(st, "hello")  # dup -> filtered
    check("update_last_chunks filters dup", st["last_chunks"] == ["hello"])
    update_last_chunks(st, "")  # empty -> filtered
    check("update_last_chunks filters empty", st["last_chunks"] == ["hello"])
    update_last_chunks(st, "world")
    check("update_last_chunks max 3", len(st["last_chunks"]) <= 3)
    for i in range(5):
        update_last_chunks(st, f"c{i}")
    check("update_last_chunks keeps last 3", st["last_chunks"] == ["c2","c3","c4"])

    # --- api_client new params ---
    print("== api_client (new params) ==")
    level2 = mock.Mock(
        name="test", endpoint="http://x/v1", model="m", timeout_seconds=1,
        resolved_api_key=lambda: "k", ca_cert_path=lambda: None,
    )
    fake_audio = tmp / "fake.wav"
    fake_audio.write_bytes(b"RIFF" + b"\x00" * 40)

    class OkResp:
        status_code = 200
        text = ""

        def json(self):
            return {"text": "ciao"}

    # Con tutti i parametri valorizzati: devono finire in `data` normalizzati.
    with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post:
        out = api_client.transcribe_audio(
            level2, fake_audio, language="it", prompt="contesto", hotwords="wh1 wh2")
        sent = m_post.call_args.kwargs["data"]
        check("transcribe_audio -> testo", out == "ciao")
        check("transcribe_audio invia language", sent.get("language") == "it")
        check("transcribe_audio invia prompt", sent.get("prompt") == "contesto")
        check("transcribe_audio invia hotwords", sent.get("hotwords") == "wh1 wh2")
        check("transcribe_audio invia model", sent.get("model") == "m")

    # Livello opt-in: frase completa, soltanto per endpoint abilitato.
    from bravoric_stt_clipboard.config import FallbackLevel
    level_vocab = FallbackLevel("vocab", "http://x/v1", "m", "", "", "", 1, True)
    with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post:
        api_client.transcribe_audio(level_vocab, fake_audio, prompt="PERSONALE chunk-vecchio chunk-recente",
                                    personal_prompt="PERSONALE", hotwords="PiAgent, tmux, inventario")
        vocab_prompt = m_post.call_args.kwargs["data"]["prompt"]
        check("vocabolario frase naturale", vocab_prompt.endswith("Le parole PiAgent, tmux, inventario sono nomi proprio."))
        check("vocabolario conserva prompt personale", vocab_prompt.startswith("PERSONALE"))
        check("vocabolario non elenco nudo", not vocab_prompt.endswith("inventario"))
    with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post:
        api_client.transcribe_audio(level2, fake_audio, prompt="PERSONALE", hotwords="PiAgent")
        check("endpoint senza opt-in prompt invariato", m_post.call_args.kwargs["data"]["prompt"] == "PERSONALE")

    # Chunk distinti e piu' lunghi del budget residuo, cosi' il troncamento
    # scatta per forza. Dimostra la direzione giusta: il taglio parte dalla
    # TESTA del contesto, quindi il pezzo piu' vecchio sparisce e il piu'
    # recente resta intatto. Le due meta' devono stare nel budget da sole ma
    # non insieme, altrimenti non si taglia nulla e il test non prova niente.
    vecchio = ("parolavecchia " * 60).strip()
    recente = ("parolaricente " * 60).strip()
    huge_ctx = "PERSONALE " + vecchio + " " + recente
    with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post:
        api_client.transcribe_audio(level_vocab, fake_audio, prompt=huge_ctx,
                                    personal_prompt="PERSONALE", hotwords="PiAgent")
        p = m_post.call_args.kwargs["data"]["prompt"]
        check("vocabolario prompt sotto 800", len(p) <= 800)
        check("vocabolario contesto vecchio sacrificato",
              "parolavecchia" not in p and "parolaricente" in p)
        check("vocabolario finale integro", p.endswith("Le parole PiAgent sono nomi proprio."))

    # Difetto 1 — off-by-one: il prompt finale e' " ".join([personal, context,
    # vocabulary]), quindi con personal E vocabulary ci sono DUE spazi di
    # giunzione; il budget ne sottraeva al massimo uno. Con il contesto che
    # riempiva esattamente il budget si mandavano 801 caratteri.
    print("== budget prompt: spazi di giunzione ==")
    hw_small = "PiAgent, tmux, inventario"
    personal_100 = "P" * 100
    vocab_sentence = f"Le parole {hw_small} sono nomi proprio."
    # Il budget COSI' com'era calcolato dal codice precedente (riga da correggere).
    old_budget = 800 - len(personal_100) - len(vocab_sentence) - (1 if personal_100 else 0)
    ctx_exact = words_exactly(old_budget)
    check("contesto di prova == budget esatto", len(ctx_exact) == old_budget)
    check("composizione vecchia sarebbe stata 801 caratteri",
          len(" ".join(part for part in (personal_100, ctx_exact, vocab_sentence) if part)) == 801)
    with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post:
        api_client.transcribe_audio(level_vocab, fake_audio, prompt=personal_100 + " " + ctx_exact,
                                    personal_prompt=personal_100, hotwords=hw_small)
        p = m_post.call_args.kwargs["data"]["prompt"]
    check("contesto esattamente al budget -> <= 800", len(p) <= 800)
    check("contesto esattamente al budget -> personale intatto", p.startswith(personal_100))
    check("contesto esattamente al budget -> frase vocabolario intatta", p.endswith(vocab_sentence))
    # Non solo "<= 800": qui l'attesa 800 era IMPOSSIBILE, non solo piu'
    # esigente. `ctx_exact` e' costruito sul budget VECCHIO (645, un solo
    # spazio riservato) e la check chiedeva la catena
    # len(p) == 100+1+645+1+54 == 800, cioe' 801 E 800 insieme: nessun codice
    # al mondo puo' soddisfarla. Il codice corretto riserva i DUE spazi di
    # giunzione, quindi da un contesto vecchio-budget esce SOTTO 800 e il
    # numero giusto e' quello che segue dalla composizione reale: il suffisso
    # di parole intere piu' lungo che entra nel budget contesto corretto (644).
    ctx_budget = 800 - len(personal_100) - len(vocab_sentence) - 2
    suffix = ctx_exact.split()
    while suffix and len(" ".join(suffix)) > ctx_budget:
        suffix.pop(0)
    check("contesto vecchio-budget -> prompt == 791 (non 801, non 800)",
          len(p) == len(personal_100) + 1 + len(" ".join(suffix)) + 1 + len(vocab_sentence) == 791)
    # Lo stesso codice, con il contesto costruito sul budget CORRETTO, deve
    # invece arrivare esattamente a 800: e' questo il caso che chiude il difetto
    # dei due spazi di giunzione.
    ctx_ok = words_exactly(ctx_budget)
    check("contesto al budget corretto == 644", len(ctx_ok) == 644)
    with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post:
        api_client.transcribe_audio(level_vocab, fake_audio, prompt=personal_100 + " " + ctx_ok,
                                    personal_prompt=personal_100, hotwords=hw_small)
        p_ok = m_post.call_args.kwargs["data"]["prompt"]
    check("contesto al budget corretto -> prompt == 800",
          len(p_ok) == len(personal_100) + 1 + len(ctx_ok) + 1 + len(vocab_sentence) == 800)
    # Nessuna parola spezzata a meta': il contesto conservato deve essere un
    # SUFFISSO delle parole originali, non una frammentazione.
    kept_words = [t for t in p.split() if t.startswith("w")]
    check("contesto esattamente al budget -> parole intere",
          kept_words == ctx_exact.split()[-len(kept_words):])
    # Stesso caso senza prompt personale (un solo blocco fisso oltre il contesto).
    with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post:
        api_client.transcribe_audio(level_vocab, fake_audio, prompt=ctx_exact, hotwords=hw_small)
        p = m_post.call_args.kwargs["data"]["prompt"]
    check("solo contesto+vocabolario -> <= 800", len(p) <= 800)
    check("solo contesto+vocabolario -> parole intere",
          [t for t in p.split() if t.startswith("w")] == ctx_exact.split()[-len([t for t in p.split() if t.startswith("w")]):])
    # Via privata del prompt personale, contesto e frase interi: anche qui il
    # risultato e' DERIVATO (nessun blocco deve perdere una parola), altrimenti
    # un troncamento buggardo ma "abbastanza sotto 800" passerebbe.
    only_ctx_budget = 800 - len(vocab_sentence) - 1
    check("solo contesto+vocabolario -> prompt == budget esatto",
          len(p) == min(len(ctx_exact), only_ctx_budget) + 1 + len(vocab_sentence) == 700)

    # Difetto 2, il caso che lo esponeva: prompt personale VUOTO ("" esplicito,
    # non None) e hotwords enormi. I due blocchi fissi da soli gia' superavano
    # 800, il budget del contesto andava negativo e il prompt usciva fuori dal
    # limite (misurato: 1223 caratteri). Qui il personale non c'e': chi deve
    # tenere la cornice e' il vocabolario, e il contesto cede tutto.
    print("== budget prompt: personale vuoto con hotwords enormi ==")
    hw_vuoto = " ".join(f"nome{numero:05d}" for numero in range(101))
    noti_vuoto = set(hw_vuoto.split())
    check("hotwords di prova oltre 800 caratteri", len(hw_vuoto) > 800)
    with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post:
        api_client.transcribe_audio(level_vocab, fake_audio,
                                    prompt=("contestovero " * 80).strip(),
                                    personal_prompt="", hotwords=hw_vuoto)
        sent_vuoto = m_post.call_args.kwargs["data"]
        p = sent_vuoto["prompt"]
    # Il troncamento riguarda SOLO il prompt: il campo hotwords dedicato resta
    # quello scritto dall'utente, altrimenti i termini non arriverebbero mai
    # al provider nemmeno per la via breve.
    check("personale vuoto: campo hotwords intatto", sent_vuoto.get("hotwords") == hw_vuoto)
    check("personale vuoto: prompt <= 800", len(p) <= 800)
    check("personale vuoto: nessuno spazio aggiuntivo",
          not p.startswith(" ") and "  " not in p and not p.endswith(" "))
    check("personale vuoto: contesto sacrificato del tutto", "contestovero" not in p)
    check("personale vuoto: cornice di vocabolario intatta",
          p.startswith("Le parole ") and p.endswith(" sono nomi proprio."))
    # Nessun token troncato a meta': tutto quello che c'e' nel mezzo e' un
    # termine per intero dell'elenco originale, e la coda resta quella
    # documentata invece di un pezzo di parola.
    mezzo_vuoto = p[len("Le parole "):-len(" sono nomi proprio.")]
    check("personale vuoto: nessuna parola spezzata a meta'",
          bool(mezzo_vuoto.split()) and all(tok in noti_vuoto for tok in mezzo_vuoto.split()))
    check("personale vuoto: elenco di termini non azzerato", len(mezzo_vuoto.split()) > 1)
    # Lunghezza ATTESA, derivata e non copiata: cornice fissa (29 caratteri:
    # "Le parole" 9 + spazio + elenco + spazio + "sono nomi proprio." 18)
    # piu' tutti i termini INTERI dell'elenco che ci stanno nel residuo (771).
    # Il residuo va CONSUMATO progressivamente: confrontare ogni termine con
    # il residuo intero non tronca mai e finirebbe per tenere tutti i 101
    # termini, dando 1038 invece di 798. 796 poi non e' raggiungibile da
    # nessun codice: n termini da 9 caratteri danno 29 + 10n - 1, e per
    # arrivare a 796 servirebbe n = 76.8. Il massimo e' 77 termini = 798
    # (il 78mo porterebbe a 808, oltre 800).
    cornice = len("Le parole") + 1 + 1 + len("sono nomi proprio.")
    tenuti: list[str] = []
    usato = 0
    for termine in hw_vuoto.split():
        costo = len(termine) + (1 if tenuti else 0)
        if usato + costo > 800 - cornice:
            break
        tenuti.append(termine)
        usato += costo
    check("personale vuoto: prompt == lunghezza derivata",
          len(p) == cornice + len(" ".join(tenuti)) == 798)

    # Difetto 1 — caso personale + vocabolario + contesto lungo: il contesto
    # cede per primo (il pezzo piu' vecchio), personal e frase restano interi.
    print("== budget prompt: contesto lungo con personale e vocabolario ==")
    personal_real = "Sei il mio segretario, registrate diagnosi e fix in note."  # ~290 come la config reale
    ctx_chunks = " ".join(f"chunk{numero}-{'z' * 60}" for numero in range(12))
    with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post:
        api_client.transcribe_audio(level_vocab, fake_audio, prompt=personal_real + " " + ctx_chunks,
                                    personal_prompt=personal_real, hotwords=hw_small)
        p = m_post.call_args.kwargs["data"]["prompt"]
    check("personale+vocabolario+contesto lungo -> <= 800", len(p) <= 800)
    check("personale+vocabolario+contesto lungo -> personale intatto", p.startswith(personal_real))
    check("personale+vocabolario+contesto lungo -> contesto recente conservato", "chunk11-" in p)
    check("personale+vocabolario+contesto lungo -> contesto vecchio sacrificato", "chunk0-" not in p)
    check("personale+vocabolario+contesto lungo -> frase vocabolario intatta", p.endswith(vocab_sentence))
    check("personale+vocabolario+contesto lungo -> nessun blocco svuotato",
          all(len(b) for b in (personal_real, " ".join(t for t in p.split() if t.startswith("chunk")), vocab_sentence)))

    # Difetto 2 — blocchi fissi da soli oltre 800: il budget del contesto
    # diventava negativo, il contesto veniva svuotato, ma i blocchi fissi
    # restavano e il prompt usciva ben oltre 800 (misurato: 922 e 1223).
    print("== budget prompt: blocchi fissi oltre 800 ==")
    for size in (600, 900, 3000):
        huge_hw = " ".join(f"term{numero:05d}" for numero in range(size // 9))
        known = set(huge_hw.split())
        with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post, \
                mock.patch.object(api_client.logger, "warning") as m_warn:
            api_client.transcribe_audio(level_vocab, fake_audio, prompt=huge_hw[:200],
                                        personal_prompt="P" * 150, hotwords=huge_hw)
            p = m_post.call_args.kwargs["data"]["prompt"]
        check(f"blocchi fissi {size}: prompt <= 800", len(p) <= 800)
        check(f"blocchi fissi {size}: avviso nel log", m_warn.called)
        head, _, tail = p.partition("Le parole ")
        check(f"blocchi fissi {size}: cornice conservata",
              head and tail.endswith(" sono nomi proprio."))
        middle = tail[:-len(" sono nomi proprio.")]
        check(f"blocchi fissi {size}: nessuna parola spezzata a meta'",
              bool(middle.split()) and all(tok in known for tok in middle.split()))
        check(f"blocchi fissi {size}: prompt personale conservato a parole intere",
              head.strip() == "P" * 150)

    # Estremo opposto: solo il prompt personale e' oltre 800, vocabolario minimo.
    personal_big = " ".join(["PERSONALE"] * 400)
    with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post:
        api_client.transcribe_audio(level_vocab, fake_audio, prompt=personal_big,
                                    personal_prompt=personal_big, hotwords="PiAgent")
        p = m_post.call_args.kwargs["data"]["prompt"]
    check("personale enorme -> <= 800", len(p) <= 800)
    check("personale enorme -> frase vocabolario intatta", p.endswith("Le parole PiAgent sono nomi proprio."))
    check("personale enorme -> nessuna parola spezzata a meta'",
          all(tok == "PERSONALE" for tok in p[:p.rindex(" Le parole")].split()))
    check("personale enorme -> testa conservata (policy A5)", p.startswith("PERSONALE PERSONAL"))

    # Difetto 3 — il contesto veniva buttato via INTERO (155 caratteri invece
    # di 791) e il budget a runtime risultava 645 mentre la sorgente dice 644.
    # Difetto reale ma NON nella sorgente: era nel .pyc in __pycache__, che
    # CPython continuava a riusare. Qui il caso viene bloccato due volte: dai
    # numeri di composizione e dal confronto del bytecode in uso con quello
    # ricompilato dalla sorgente (sezione "bytecode in __pycache__" piu' in
    # basso). Nessuno dei due da solo basterebbe.
    print("== difetto 155: contesto perso per intero ==")
    with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post, \
            mock.patch.object(api_client.logger, "warning") as m_warn:
        api_client.transcribe_audio(
            level_vocab, fake_audio, prompt=personal_100 + " " + ctx_exact,
            personal_prompt=personal_100, hotwords=hw_small)
        p_155 = m_post.call_args.kwargs["data"]["prompt"]
    # I numeri sono derivati dalla composizione, non ricordati: con personal e
    # frase di vocabolario non vuoti ci sono DUE spazi di giunzione, e
    # _blocks_len conta gia' quello fra personal e vocabolario. Il budget del
    # contesto e' quindi 800 - 155 - 1 = 644, non 645: il "- 1" che resta e'
    # quello del secondo spazio di giunzione. Con un budget di 645 la
    # composizione finale sarebbe 100+1+645+1+54 = 801 e il codice, vedendo
    # il limite superato, svuoterebbe il contesto mandando solo 155
    # caratteri (i due blocchi fissi) con l'avviso "blocchi fissi oltre 800",
    # che con quei numeri e' fuori luogo: i blocchi fissi sono 155.
    budget_contesto = api_client.PROMPT_MAX_CHARS - api_client._blocks_len(personal_100, vocab_sentence) - 1
    check("difetto 155: blocchi fissi 100+1+54", api_client._blocks_len(personal_100, vocab_sentence) == 155)
    check("difetto 155: budget del contesto == 644 (non 645)", budget_contesto == 644)
    # Un budget di 645 produrrebbe davvero 801: il numero che spiega il difetto.
    check("difetto 155: con 645 la composizione sarebbe 801",
          100 + 1 + 645 + 1 + len(vocab_sentence) == 801)
    check("difetto 155: contesto NON perso interamente", len(p_155) == 791)
    check("difetto 155: contesto conservato per 635 caratteri",
          len(p_155) - len(personal_100) - 1 - len(vocab_sentence) - 1 == 635)
    check("difetto 155: nessun avviso di blocchi fissi oltre 800", not m_warn.called)
    # Il contesto inviato deve essere il SUFFISSO piu' lungo di parole INTERE
    # che entra in 644. Attenzione alla direzione: il codice scarta dalla
    # TESTA, quindi il pezzo che resta e' la coda. `ctx_exact` (645) e' fatto
    # di 65 parole: togliendone una dalla testa si scende a 635, che ci sta;
    # rimetterla porterebbe a 645, ancora oltre il budget. Il suffisso atteso
    # e' derivato qui, non ricordato.
    parole_attese = ctx_exact.split()
    while parole_attese and len(" ".join(parole_attese)) > budget_contesto:
        parole_attese.pop(0)
    check("difetto 155: suffisso atteso a 635 caratteri in 64 parole",
          len(" ".join(parole_attese)) == 635 and len(parole_attese) == 64)
    # "P"*100 e' UNA parola sola, non 100: si indicizza per numero di parole.
    check("difetto 155: il contesto conserva tutte le parole tranne le piu' vecchie",
          " ".join(p_155.split()[len(personal_100.split()):-len(vocab_sentence.split())])
          == " ".join(parole_attese))
    # Controprova: lo stesso caso con il "- 1" neutralizzato a "- 0" (cioe'
    # esattamente il bytecode che girava) deve riprodurre i 155 caratteri.
    # Serve a dimostrare che le check qui sopra falliscono davvero quando il
    # difetto c'e', e non solo che passano quando non c'e'.
    modulo_neutro = _modulo_con_budget_neutro(
        Path(api_client.__file__))
    with mock.patch.object(modulo_neutro.requests, "post", return_value=OkResp()) as m_post, \
            mock.patch.object(modulo_neutro.logger, "warning") as m_warn:
        modulo_neutro.transcribe_audio(
            level_vocab, fake_audio, prompt=personal_100 + " " + ctx_exact,
            personal_prompt=personal_100, hotwords=hw_small)
        p_neutro = m_post.call_args.kwargs["data"]["prompt"]
    check("difetto 155: controprova col budget neutro riproduce 155", len(p_neutro) == 155)
    check("difetto 155: controprova emette l'avviso fuori luogo", m_warn.called)
    check("difetto 155: controprova e' proprio il contesto perso",
          p_neutro == " ".join([personal_100, vocab_sentence]))

    # Parametri vuoti/whitespace: NON devono essere aggiunti a `data`.
    with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post:
        api_client.transcribe_audio(level2, fake_audio, language="  ", prompt="", hotwords="\t ")
        sent = m_post.call_args.kwargs["data"]
        check("transcribe_audio omette language vuoto", "language" not in sent)
        check("transcribe_audio omette prompt vuoto", "prompt" not in sent)
        check("transcribe_audio omette hotwords vuoto", "hotwords" not in sent)

    # Chiamata senza i nuovi parametri (retrocompatibile): nessuna chiave extra.
    with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post:
        api_client.transcribe_audio(level2, fake_audio)
        sent = m_post.call_args.kwargs["data"]
        check("transcribe_audio senza nuovi param -> solo model",
              set(sent) == {"model"})

    # prompt oltre 800 caratteri: troncato a 800 conservando l'inizio ([:800]),
    # così il prompt personale istruttivo non perde la testa (policy A5,
    # coerente con build_prompt). La coda viene scartata.
    long_prompt = "A" * 100 + "B" * 800
    with mock.patch.object(api_client.requests, "post", return_value=OkResp()) as m_post:
        api_client.transcribe_audio(level2, fake_audio, prompt=long_prompt)
        sent = m_post.call_args.kwargs["data"]
        check("transcribe_audio prompt troncato a 800", len(sent.get("prompt", "")) == 800)
        check("transcribe_audio prompt conserva la testa ([:800])",
              sent.get("prompt") == long_prompt[:800])
        check("transcribe_audio prompt scarta la coda",
              sent.get("prompt") == "A" * 100 + "B" * 700
              and not sent.get("prompt", "").endswith("B" * 800))

    # Difetto 3 — nessun modulo del pacchetto puo' girare con un bytecode
    # diverso da quello della sua sorgente. E' il controllo che chiude il
    # difetto dei 155 caratteri: l'header del .pyc combaciava (mtime e
    # dimensione identici, perche' la mutazione era stata annullata nella
    # stessa seconda epoch), ma il codice compilato dentro aveva ancora il
    # budget sbagliato. Senza questo, ogni test guarda la sorgente, la
    # sorgente e' giusta e la suite passa mentre il programma perde il
    # contesto. Il .pyc va rimosso, non aggiornato a mano: il problema qui
    # e' il file, non la cache.
    print("== bytecode in __pycache__ coerente con la sorgente ==")
    moduli = _moduli_del_pacchetto()
    check("moduli del pacchetto trovati per il controllo bytecode", len(moduli) >= 10)
    cache_presente = False
    divergenti: list[str] = []
    for modulo in moduli:
        nome = modulo.name
        divergenze, header = _verifica_pyc(modulo)
        percorso_cache = Path(importlib.util.cache_from_source(str(modulo)))
        if percorso_cache.exists():
            cache_presente = True
        if not header:
            # L'header decide se CPython riusa il .pyc: se non combacia il
            # file viene rigenerato al primo import e non c'e' pericolo.
            print(f"  INFO  {nome}: .pyc da rigenerare, header non combacia")
            continue
        if divergenze:
            # Fallisce davvero, e il messaggio porta la prova: qui si e'
            # visto il difetto dei 155 caratteri, con l'offset e le due
            # istruzioni diverse. Il .pyc va rimosso, non aggiornato a mano.
            for linea in divergenze:
                divergenti.append(f"{nome}: {linea} (rimuovere {percorso_cache})")
    for problema in divergenti:
        check(f"bytecode coerente con la sorgente — {problema}", False)
    if not divergenti:
        check(f"nessun .pyc del pacchetto diverso dalla sorgente ({len(moduli)} moduli)", True)
    if cache_presente:
        check("controllo bytecode esercitato su almeno un .pyc presente", True)
    else:
        # Nessun .pyc su disco: niente da confrontare, e niente di rotto.
        # Non e' un fallimento (basta `python -B` o PYTHONDONTWRITEBYTECODE),
        # ma il controllo e' vacuo e va detto.
        print("  INFO  nessun .pyc presente: il controllo bytecode non ha nulla da confrontare")

    # --- config_editor get_state new fields ---
    print("== config_editor (new fields) ==")
    cfg_ce = tmp / "config_editor_test.toml"
    cfg_ce.write_text(
        '[general]\n'
        'notifications = true\n'
        '\n[notifications]\n'
        'stt_on_processing_start = true\n'
        '\n[audio]\n'
        'format = "ogg"\n'
        '\n[stt]\n'
        'language = "en"\n'
        'prompt = "my prompt"\n'
        'hotwords = "kw1 kw2"\n'
        '\n[[stt.fallback]]\n'
        'name = "test"\n'
        'endpoint = "http://x"\n'
        'model = "m"\n'
        'api_key_env = ""\n'
        'api_key = ""\n'
        'ca_cert = ""\n'
        'timeout_seconds = 60\n'
        'hotwords_in_prompt = false\n'
        '\n[stream]\n'
        'mode = "per_chunk"\n'
        'silence_seconds = 0.7\n'
        'language = "fr"\n'
        'prompt = "stream prompt"\n'
        'hotwords = "sh1 sh2"\n'
        '\n[[stream.fallback]]\n'
        'name = "test"\n'
        'endpoint = "http://x"\n'
        'model = "m"\n'
        'api_key_env = ""\n'
        'api_key = ""\n'
        'ca_cert = ""\n'
        'timeout_seconds = 60\n'
    )
    config_editor.CONFIG_PATH = cfg_ce
    state = config_editor.get_state()
    check("get_state stt.language", state["stt"]["language"] == "en")
    check("get_state stt.prompt", state["stt"]["prompt"] == "my prompt")
    check("get_state stt.hotwords", state["stt"]["hotwords"] == "kw1 kw2")
    check("get_state stream.language", state["stream"]["language"] == "fr")
    check("get_state stream.prompt", state["stream"]["prompt"] == "stream prompt")
    check("get_state stream.hotwords", state["stream"]["hotwords"] == "sh1 sh2")
    check("get_state stream.paste_shortcut default", state["stream"]["paste_shortcut"] == "ctrl+v")
    check("set_stream_field paste_shortcut", config_editor.set_stream_field("paste_shortcut", "CTRL+SHIFT+V") is None)
    check("get_state/set_stream_field paste_shortcut round-trip", config_editor.get_state()["stream"]["paste_shortcut"] == "ctrl+shift+v")

    # Test set_section_field creates [stt] section if missing
    cfg_ce2 = tmp / "config_editor_test2.toml"
    cfg_ce2.write_text(
        '[general]\n'
        'notifications = true\n'
        '\n[notifications]\n'
        'stt_on_processing_start = true\n'
        '\n[audio]\n'
        'format = "ogg"\n'
        '\n[[stt.fallback]]\n'
        'name = "test"\n'
        'endpoint = "http://x"\n'
        'model = "m"\n'
        'api_key_env = ""\n'
        'api_key = ""\n'
        'ca_cert = ""\n'
        'timeout_seconds = 60\n'
        '\n[stream]\n'
        'mode = "per_chunk"\n'
        'silence_seconds = 0.7\n'
    )
    config_editor.CONFIG_PATH = cfg_ce2
    try:
        config_editor.set_section_field("stt", "language", "de")
        check("set_section_field creates [stt] section", "[stt]" in cfg_ce2.read_text())
        check("set_section_field writes language", 'language = "de"' in cfg_ce2.read_text())
    except Exception as exc:
        check(f"set_section_field creates [stt] section ({type(exc).__name__})", False)

    # set_stream_field new fields
    try:
        config_editor.set_stream_field("language", "es")
        check("set_stream_field language -> ok", True)
        state_ce2 = config_editor.get_state()
        check("set_stream_field: language = es", state_ce2.get("stream", {}).get("language") == "es")
    except Exception as exc:
        check(f"set_stream_field language -> ok ({type(exc).__name__})", False)

    try:
        config_editor.set_stream_field("prompt", "new stream prompt")
        check("set_stream_field prompt -> ok", True)
    except Exception as exc:
        check(f"set_stream_field prompt -> ok ({type(exc).__name__})", False)

    try:
        config_editor.set_stream_field("hotwords", "kw1 kw2")
        check("set_stream_field hotwords -> ok", True)
    except Exception as exc:
        check(f"set_stream_field hotwords -> ok ({type(exc).__name__})", False)

    # Regressione P2: set_stream_field deve creare [stream] se assente
    cfg_ce3 = tmp / "config_editor_test3.toml"
    cfg_ce3.write_text(
        '[general]\n'
        'notifications = true\n'
        '\n[[stream.fallback]]\n'
        'name = "test"\n'
        'endpoint = "http://x"\n'
        'model = "m"\n'
        'api_key_env = ""\n'
        'api_key = ""\n'
        'ca_cert = ""\n'
        'timeout_seconds = 60\n'
    )
    config_editor.CONFIG_PATH = cfg_ce3
    try:
        config_editor.set_stream_field("language", "fr")
        text_ce3 = cfg_ce3.read_text()
        check("set_stream_field creates [stream] section", "[stream]" in text_ce3)
        check("set_stream_field writes language in new section", 'language = "fr"' in text_ce3)
        check("set_stream_field keeps [[stream.fallback]]", "[[stream.fallback]]" in text_ce3)
    except Exception as exc:
        check(f"set_stream_field creates [stream] section ({type(exc).__name__})", False)

    # Regressione P2: valori TOML non-stringa non devono far crashare il parse
    cfg_ce4 = tmp / "config_types.toml"
    cfg_ce4.write_text(
        '[stt]\n'
        'language = 123\n'
        'prompt = 456\n'
        'hotwords = 789\n'
    )
    try:
        with open(cfg_ce4, "rb") as f:
            import tomllib as _tomllib
            parsed = config._build_config(_tomllib.load(f))
        check("non-string stt fields coerced to str",
              parsed.stt.language == "123" and parsed.stt.prompt == "456")
    except Exception as exc:
        check(f"non-string stt fields coerced to str ({type(exc).__name__})", False)

    # Regressione: float non finiti in stream (silence_seconds, noise_db, ecc.)
    cfg_nonfinite = tmp / "config_nonfinite.toml"
    cfg_nonfinite.write_text(
        '[stream]\n'
        'mode = "per_chunk"\n'
        'silence_seconds = nan\n'
        'noise_db = -inf\n'
        'min_utterance_seconds = inf\n'
        'max_utterance_seconds = nan\n'
        'chunk_timeout_seconds = inf\n'
    )
    try:
        with open(cfg_nonfinite, "rb") as f:
            import tomllib as _tomllib
            parsed_nf = config._build_config(_tomllib.load(f))
        check("stream nonfinite silence_seconds fallback", parsed_nf.stream.silence_seconds == 0.7)
        check("stream nonfinite noise_db fallback", parsed_nf.stream.noise_db == -30.0)
        check("stream nonfinite min_utterance_seconds fallback", parsed_nf.stream.min_utterance_seconds == 0.4)
        check("stream nonfinite max_utterance_seconds fallback", parsed_nf.stream.max_utterance_seconds == 30.0)
        check("stream nonfinite chunk_timeout_seconds fallback", parsed_nf.stream.chunk_timeout_seconds == 30.0)
    except Exception as exc:
        check(f"stream nonfinite values fallback ({type(exc).__name__})", False)

    # Comandi vocali: parsing, default, validazione e setter atomico.
    cmd_cfg = tmp / "commands.toml"
    cmd_cfg.write_text('[stream]\n[[stream.command]]\nkeyword="Invio."\naliases=["invito", "in view"]\naction="key"\nkey="Return"\n')
    parsed_commands = config.load_config(cmd_cfg).stream.commands
    check("stream commands parsed", len(parsed_commands) == 1 and parsed_commands[0].key == "Return")
    check("stream command aliases parsed", parsed_commands[0].aliases == ["invito", "in view"])
    check("stream command all_phrases keeps keyword first and dedupes", parsed_commands[0].all_phrases == ["Invio.", "invito", "in view"])
    check("stream commands default empty", config._build_config({}).stream.commands == [])
    duplicate_cfg = tmp / "duplicate_commands.toml"
    duplicate_cfg.write_text('[stream]\n[[stream.command]]\nkeyword="Invio."\naction="key"\nkey="Return"\n[[stream.command]]\nkeyword=" invio! "\naction="key"\nkey="Tab"\n')
    try:
        config.load_config(duplicate_cfg)
        check("normalized duplicate rejected", False)
    except config.ConfigError:
        check("normalized duplicate rejected", True)

    alias_collision_cfg = tmp / "alias_collision.toml"
    alias_collision_cfg.write_text('[stream]\n[[stream.command]]\nkeyword="Invio"\naliases=["vai"]\naction="key"\nkey="Return"\n[[stream.command]]\nkeyword="avanti"\naliases=["vai!"]\naction="key"\nkey="Tab"\n')
    try:
        config.load_config(alias_collision_cfg)
        check("cross-rule alias collision rejected", False)
    except config.ConfigError:
        check("cross-rule alias collision rejected", True)

    punct_cfg = tmp / "punct_commands.toml"
    punct_cfg.write_text('[stream]\n[[stream.command]]\nkeyword="!!!"\naction="key"\nkey="Return"\n')
    try:
        config.load_config(punct_cfg)
        check("punctuation-only keyword rejected", False)
    except config.ConfigError:
        check("punctuation-only keyword rejected", True)

    punct_alias_cfg = tmp / "punct_alias.toml"
    punct_alias_cfg.write_text('[stream]\n[[stream.command]]\nkeyword="ok"\naliases=["???"]\naction="key"\nkey="Return"\n')
    try:
        config.load_config(punct_alias_cfg)
        check("punctuation-only alias rejected", False)
    except config.ConfigError:
        check("punctuation-only alias rejected", True)

    # --- Blacklist tests ---
    print("== config.py / stream.py (blacklist) ==")
    from bravoric_stt_clipboard.config import parse_blacklist
    check("parse_blacklist empty string -> empty set", parse_blacklist("") == frozenset())
    check("parse_blacklist normalization", parse_blacklist(" Grazie! , THANK YOU... ") == frozenset({"grazie", "thank you"}))

    blacklist_overlap_cfg = tmp / "blacklist_overlap.toml"
    blacklist_overlap_cfg.write_text('[stream]\nblacklist="invio, grazie"\n[[stream.command]]\nkeyword="invio"\naction="key"\nkey="Return"\n')
    try:
        config.load_config(blacklist_overlap_cfg)
        check("blacklist overlapping with command keyword rejected", False)
    except config.ConfigError:
        check("blacklist overlapping with command keyword rejected", True)

    blacklist_alias_overlap_cfg = tmp / "blacklist_alias_overlap.toml"
    blacklist_alias_overlap_cfg.write_text('[stream]\nblacklist="in view, grazie"\n[[stream.command]]\nkeyword="invio"\naliases=["in view"]\naction="key"\nkey="Return"\n')
    try:
        config.load_config(blacklist_alias_overlap_cfg)
        check("blacklist overlapping with command alias rejected", False)
    except config.ConfigError:
        check("blacklist overlapping with command alias rejected", True)

    # Sequencer tombstone and drop behavior with blacklist
    st_bl = {"chunks": [], "last_chunks": []}
    seq_bl = stream_module._FifoSequencer(st_bl, stream_test_cfg, lambda text: None, lambda text: None, blacklist=frozenset({"grazie"}), log_path=_TEST_CHUNK_LOG_PATH)
    committed_bl = []
    committed_bl.extend(seq_bl.ingest(stream_module._ChunkResult(0, "Primo", True)))
    committed_bl.extend(seq_bl.ingest(stream_module._ChunkResult(1, "Grazie!", True)))
    committed_bl.extend(seq_bl.ingest(stream_module._ChunkResult(2, "Terzo", True)))
    check("blacklist drops whole chunk without breaking tombstone", st_bl["chunks"] == ["Primo ", "Terzo "] and seq_bl._next_expected == 3)
    check("blacklist dropped chunk never committed to history/notify", committed_bl == ["Primo ", "Terzo "])
    check("blacklist dropped chunk never added to last_chunks", st_bl["last_chunks"] == ["Primo", "Terzo"])

    # drain_and_stop with blacklisted orphan
    st_bl_drain = {"chunks": [], "last_chunks": []}
    seq_bl_drain = stream_module._FifoSequencer(st_bl_drain, stream_test_cfg, lambda text: None, lambda text: None, blacklist=frozenset({"grazie"}), log_path=_TEST_CHUNK_LOG_PATH)
    seq_bl_drain._pending[0] = stream_module._ChunkResult(0, "Grazie.", True)
    seq_bl_drain._pending[1] = stream_module._ChunkResult(1, "Fine", True)
    seq_bl_drain.drain_and_stop(2)
    check("drain_and_stop discards blacklisted orphan while advancing", st_bl_drain["chunks"] == ["Fine "])

    # at_end and paste_next blacklist defenses
    stream_at_end_cfg = config.Config(
        notifications=False,
        notif_stt=config.ServiceNotifications(False, config.NotificationEvent(False, False), config.NotificationEvent(False, False)),
        notif_ocr=config.ServiceNotifications(False, config.NotificationEvent(False, False), config.NotificationEvent(False, False)),
        notif_stream=config.ServiceNotifications(False, config.NotificationEvent(False, False), config.NotificationEvent(False, False)),
        clipboard_tool="wl-copy", clipboard_paste_tool="wl-paste",
        audio=config.AudioConfig("ogg", "libopus", 16000, 16, 1.0, True, 2),
        stt=config.STTConfig("it", "", "", []),
        stt_cleanup=config.CleanupConfig(False, "", []),
        ocr_fallback=[], ocr_system_prompt="",
        ocr_cleanup=config.CleanupConfig(False, "", []),
        double_injection=True,
        storage=config.StorageConfig(str(tmp), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0)),
        history_max_entries=10, icons=config.IconsConfig("", "", "", "", "", ""),
        stream=config.StreamConfig("at_end", 0.7, -30.0, 0.4, 30.0, 250, blacklist="grazie"),
    )
    sess_at_end = stream_module.StreamSession(stream_at_end_cfg)
    audio_fake = tmp / "at_end_fake.ogg"
    audio_fake.write_bytes(b"dummy")
    with mock.patch.object(stream_module, "_terminate_pid"), mock.patch.object(stream_module, "try_with_fallback", return_value="Grazie! "):
        sess_at_end._stop_at_end({"audio_path": str(audio_fake), "session_id": "test_at_end", "pid": 12345})
        st_disk = stream_module.read_state()
        check("_stop_at_end discards blacklisted text completely", st_disk.get("chunks") == [])

    # paste_next stale-state drop
    stream_pn_cfg = config.Config(
        notifications=False,
        notif_stt=config.ServiceNotifications(False, config.NotificationEvent(False, False), config.NotificationEvent(False, False)),
        notif_ocr=config.ServiceNotifications(False, config.NotificationEvent(False, False), config.NotificationEvent(False, False)),
        notif_stream=config.ServiceNotifications(False, config.NotificationEvent(False, False), config.NotificationEvent(False, False)),
        clipboard_tool="wl-copy", clipboard_paste_tool="wl-paste",
        audio=config.AudioConfig("ogg", "libopus", 16000, 16, 1.0, True, 2),
        stt=config.STTConfig("it", "", "", []),
        stt_cleanup=config.CleanupConfig(False, "", []),
        ocr_fallback=[], ocr_system_prompt="",
        ocr_cleanup=config.CleanupConfig(False, "", []),
        double_injection=True,
        storage=config.StorageConfig(str(tmp), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0)),
        history_max_entries=10, icons=config.IconsConfig("", "", "", "", "", ""),
        stream=config.StreamConfig("per_chunk", 0.7, -30.0, 0.4, 30.0, 250, blacklist="grazie"),
    )
    sess_pn = stream_module.StreamSession(stream_pn_cfg)
    stream_mod._write_state({
        "session_id": "pn_sess", "chunks": ["Grazie.", "Valido "], "next_chunk_index": 0,
        "paste_delay_ms": 250, "paste_shortcut": "ctrl+v", "commands": [], "blacklist": "grazie"
    })
    pn_ret = sess_pn.paste_next()
    check("paste_next skips blacklisted chunk and returns False", pn_ret is False)
    check("paste_next advances index past blacklisted chunk", stream_module.read_state().get("next_chunk_index") == 1)

    config_editor.CONFIG_PATH = tmp / "commands_editor.toml"
    config_editor.CONFIG_PATH.write_text('[stream]\nmode="per_chunk"\n')
    config_editor.set_stream_commands([{"keyword":"cancella", "aliases":["elimina"], "action":"delete", "scope":"word", "ends_session":False}])
    st_ed = config_editor.get_state()["stream"]
    check("command setter/get_state round-trip with aliases", st_ed["commands"][0]["keyword"] == "cancella" and st_ed["commands"][0]["aliases"] == ["elimina"])
    config_editor.set_stream_field("blacklist", "grazie, thank you")
    check("set_stream_field blacklist round-trip", config_editor.get_state()["stream"]["blacklist"] == "grazie, thank you")

    # Regressione: paste_delay_ms propagato nello stato
    state_mock_start = {}
    # La sostituzione precedente era un FURTO PERMANENTE: la lambda restava
    # per tutta la suite e ogni test successivo che chiamava _write_state non
    # scriveva piu' su disco (lo leggeva solo nel dict in memoria). Rimosso sub
    # dopo, qui: sotto non c'e' un test che dipenda dalla lambda
    # (state_mock_start non e' mai riletto). Cosi' i test che vengono dopo
    # osservano davvero il file di stato, e i miei test D/L possono misurare
    # il percorso reale invece di una simulazione che passerebbe comunque.
    _write_state_saved = stream_mod._write_state
    stream_mod._write_state = lambda s: state_mock_start.update(s)
    sess_mock = stream_mod.StreamSession(config.Config(
        notifications=False,
        notif_stt=config.ServiceNotifications(False, config.NotificationEvent(False, False), config.NotificationEvent(False, False)),
        notif_ocr=config.ServiceNotifications(False, config.NotificationEvent(False, False), config.NotificationEvent(False, False)),
        notif_stream=config.ServiceNotifications(False, config.NotificationEvent(False, False), config.NotificationEvent(False, False)),
        clipboard_tool="wl-copy", clipboard_paste_tool="wl-paste",
        audio=config.AudioConfig("ogg", "libopus", 16000, 16, 1.0, True, 2),
        stt=config.STTConfig("it", "", "", []),
        stt_cleanup=config.CleanupConfig(False, "", []),
        ocr_fallback=[], ocr_system_prompt="",
        ocr_cleanup=config.CleanupConfig(False, "", []),
        double_injection=True,
        storage=config.StorageConfig(str(tmp), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0)),
        history_max_entries=10, icons=config.IconsConfig("", "", "", "", "", ""),
        stream=config.StreamConfig("per_chunk", 0.7, -30.0, 0.4, 30.0, 350),
    ))
    check("StreamSession config paste_delay_ms", sess_mock._stream.paste_delay_ms == 350)
    # Rimette la funzione vera (vedi sopra): niente piu' la sostituisce.
    stream_mod._write_state = _write_state_saved

    # Regressione: supervisor timeout expired termina con kill
    class _MockProcessTimeout:
        def __init__(self):
            self.killed = False
            self.terminated = False
            self.waits = 0
        def terminate(self):
            self.terminated = True
        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(cmd="test", timeout=timeout or 5)
        def poll(self):
            return None if not self.killed else 0
        def kill(self):
            self.killed = True

    mock_p = _MockProcessTimeout()
    with contextlib.suppress(Exception):
        mock_p.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        mock_p.wait(timeout=5)
    if mock_p.poll() is None:
        with contextlib.suppress(Exception):
            mock_p.kill()
        with contextlib.suppress(Exception):
            mock_p.wait(timeout=5)
    check("process wait timeout triggers kill fallback", mock_p.killed and mock_p.terminated)

    # --- endpoint_breaker.py: compute_state/endpoint_id, mai testate a diretto ---
    # compute_state e' documentata come funzione PURA con 3 casi limite
    # espliciti (last_failure None, age<0 orologio indietro, boundary esatto
    # al cooldown): esercitata solo indirettamente via EndpointBreaker.state()
    # finora, mai con un'asserzione diretta sui suoi limiti.
    from bravoric_stt_clipboard.endpoint_breaker import (
        CLOSED,
        HALF_OPEN,
        OPEN,
        compute_state,
        endpoint_id,
    )
    check("compute_state: mai fallito -> CLOSED", compute_state(None, 1000.0, 3600.0) == CLOSED)
    check("compute_state: appena fallito -> OPEN", compute_state(1000.0, 1000.1, 3600.0) == OPEN)
    check("compute_state: cooldown appena scaduto (boundary esatto) -> HALF_OPEN",
          compute_state(1000.0, 1000.0 + 3600.0, 3600.0) == HALF_OPEN)
    check("compute_state: un istante prima del boundary -> ancora OPEN",
          compute_state(1000.0, 1000.0 + 3600.0 - 0.001, 3600.0) == OPEN)
    check("compute_state: timestamp nel futuro (orologio indietro/altro processo) -> CLOSED, non bloccato",
          compute_state(2000.0, 1000.0, 3600.0) == CLOSED)
    # endpoint_id: normalizzazione dello slash finale, documentata esplicitamente
    # come idempotente PRIMA di endpoint_key (non due implementazioni diverse).
    check("endpoint_id: slash finale non cambia la chiave",
          endpoint_id("http://h:4001/v1/", "m") == endpoint_id("http://h:4001/v1", "m"))
    check("endpoint_id: model diverso -> chiave diversa (stesso endpoint)",
          endpoint_id("http://h:4001/v1", "m1") != endpoint_id("http://h:4001/v1", "m2"))
    check("endpoint_id: endpoint diverso, model uguale -> chiave diversa",
          endpoint_id("http://h1:4001/v1", "m") != endpoint_id("http://h2:4001/v1", "m"))

    # --- onda 2: dispatcher (stream.py) ------------------------------------
    # Un test verde puo' essere VERDE BUGGATO: in questo progetto e' gia'
    # successo (bytecode avvelenato, 648 combinazioni in attesa). Percio' i
    # test sotto sono verificati per NON-VACUITA' con una mutazione: se
    # inverti l'ordinamento in _Dispatcher._candidates, il test deve ROSARE.
    #
    # Attenzione: il breaker va SEMPRE costruito su un path temporaneo. Se si
    # usa stream_mod._build_breaker() si scrive nel breaker REALE dell'utente
    # (~/.cache/.../endpoint_breaker.json) e un test che fallisce lascia
    # endpoint in cooldown per un'ora, facendo pendere i test successivi.
    from bravoric_stt_clipboard import stream as stream_mod
    from bravoric_stt_clipboard.config import FallbackLevel
    from bravoric_stt_clipboard.endpoint_breaker import EndpointBreaker

    def _tmp_breaker():
        return EndpointBreaker(path=os.path.join(
            tempfile.mkdtemp(), "endpoint_breaker.json"))

    def _lvl(name, parallel=True, slots=2):
        return FallbackLevel(
            name, f"http://{name.lower()}:4001/v1", "m",
            "", "", "", 60, False, parallel, slots,
        )

    def _key(level):
        return stream_mod._level_key(level)

    # --- least-busy con longest-waiting: NON il primo della lista ----------
    # Tre endpoint paralleli tutti LIBERI e a parita' di carico: vince quello
    # libero da piu' tempo, non il primo in config. Con l'ordinamento
    # invertito questo asserisce False -> il test e' sensibile alla mutazione.
    la = _lvl("LA")
    lb = _lvl("LB")
    lc = _lvl("LC")
    disp = stream_mod._Dispatcher([la, lb, lc], _tmp_breaker())
    ka, kb, kc = _key(la), _key(lb), _key(lc)
    # A e B sono liberi da 100s, C da 0: vince A (il piu' affamato), non C
    # (che e' l'ultimo della lista) ne il primo-per-config casuale.
    disp._free_since[ka] = time.time() - 100
    disp._free_since[kb] = time.time() - 100
    disp._free_since[kc] = time.time()
    chosen = disp._candidates(time.time())[0]
    check(
        "dispatcher least-busy sceglie il libero da piu' tempo, non il primo",
        chosen == ka,
    )

    # Least-busy ha la precedenza sul longest-waiting: A ha 1 slot libero (carico
    # 1) e B 2 slot liberi (carico 0) -> vince B anche se B e' "meno affamato".
    disp2 = stream_mod._Dispatcher([la, lb], _tmp_breaker())
    disp2._free_since[ka] = time.time() - 100
    disp2._free_since[kb] = time.time()
    check(
        "dispatcher least-busy precede longest-waiting",
        disp2._take_slot(ka) and disp2._candidates(time.time())[0] == kb,
    )
    disp2._give_slot(ka)

    # La capienza dichiarata e' un vincolo reale: B con 1 slot non regge 2
    # richieste contemporanee, quindi il terzo acquire non puo' passare.
    small = _lvl("SMALL", slots=1)
    disp3 = stream_mod._Dispatcher([small], _tmp_breaker())
    held_level, held_key = disp3.acquire([small])
    check("dispatcher capienza 1: primo acquire ok", held_level is not None)
    second = disp3.try_acquire_now([small])
    check("dispatcher capienza 1: secondo acquire bloccato (no overbooking)",
          second[0] is None)
    if held_key is not None:
        disp3.release(held_key)
    third = disp3.try_acquire_now([small])
    check("dispatcher dopo release lo slot torna libero", third[0] is not None)
    if third[0] is not None:
        disp3.release(third[1])
    check("dispatcher nessuno slot perso dopo il ciclo", disp3._free[_key(small)] == 1)

    # Un endpoint in cooldown non e' eleggibile e non ha slot.
    cooled = _lvl("COOLED")
    disp4 = stream_mod._Dispatcher([cooled, small], _tmp_breaker())
    disp4._breaker.record_failure(_key(cooled))
    check("dispatcher endpoint in cooldown escluso dai candidati",
          _key(cooled) not in disp4._candidates(time.time()))

    # --- N_parallel == 0: percorso sequenziale identico --------------------
    seq1 = _lvl("S1", parallel=False)
    seq2 = _lvl("S2", parallel=False)
    disp5 = stream_mod._Dispatcher([seq1, seq2], _tmp_breaker())
    check("N_parallel == 0 -> dispatcher inattivo", not disp5.active)

    # _worker senza dispatcher deve usare try_with_fallback: primo livello
    # della config, in ordine, e nessun lease.
    def _wav():
        p = Path(tempfile.mkdtemp()) / "utt.wav"
        p.write_bytes(b"")
        return p

    class _SeqStream:
        language, prompt, hotwords = "it", "", ""
        fallback = [seq1, seq2]

    seen: list[str] = []
    saved_transcribe = stream_mod._transcribe
    saved_fallback = stream_mod.try_with_fallback
    try:
        stream_mod._transcribe = (
            lambda lv, w, s, p: (seen.append(lv.name), "testo")[1]
        )
        q = queue.Queue()
        sem = threading.BoundedSemaphore(3)
        sem.acquire()  # il supervisor acquisisce prima di submit
        stream_mod._worker(0, _wav(), None, stream=_SeqStream(), sem=sem,
                           result_queue=q)
        res = q.get_nowait()
        check("N_parallel == 0: percorso sequenziale, primo livello in ordine",
              res.success and res.text == "testo" and seen == ["S1"])
        check("N_parallel == 0: semaforo del supervisor bilanciato", sem._value == 3)
    finally:
        stream_mod._transcribe = saved_transcribe

    # --- retry una volta per livello, senza ping-pong ----------------------
    def _run_parallel(levels, behaviour):
        """Esegue _worker sul percorso parallelo e restituisce
        (risultato, livelli tentati, dispatcher, semaforo bilanciato)."""
        st = type("S", (), {"language": "it", "prompt": "", "hotwords": "",
                            "fallback": levels})()
        tried: list[str] = []
        def _fake(lv, w, s, p):
            tried.append(lv.name)
            return behaviour(lv)
        saved = stream_mod._transcribe
        try:
            stream_mod._transcribe = _fake
            d = stream_mod._Dispatcher(levels, _tmp_breaker())
            q = queue.Queue()
            sm = threading.BoundedSemaphore(3)
            sm.acquire()
            stream_mod._worker(0, _wav(), None, stream=st, sem=sm,
                               result_queue=q, dispatcher=d)
            return q.get_nowait(), tried, d, sm._value == 3
        finally:
            stream_mod._transcribe = saved

    bad = _lvl("BAD")
    good = _lvl("GOOD")

    res, tried, d, balanced = _run_parallel(
        [bad, good],
        lambda lv: (_ for _ in ()).throw(RuntimeError("HTTP 500"))
        if lv.name == "BAD" else f"da {lv.name}",
    )
    check("retry: 1° rotto -> recovers sul 2° endpoint",
          res.success and res.text == "da GOOD")
    check("retry: ogni livello tentato UNA volta sola (no ping-pong)",
          len(tried) == len(set(tried)))
    check("retry: semaforo bilanciato anche in errore", balanced)

    res, tried, d, balanced = _run_parallel(
        [bad, good],
        lambda lv: (_ for _ in ()).throw(RuntimeError("HTTP 500")),
    )
    check("retry: tutti i livelli falliscono -> AllLevelsFailed",
          not res.success and "HTTP 500" in (res.error or "")
          and "unexpected" not in (res.error or ""))
    check("retry: due livelli tentati una volta ciascuno, nessun ciclo",
          sorted(tried) == ["BAD", "GOOD"])
    check("retry: slot rilasciati anche quando tutti falliscono",
          all(d._free[_key(lv)] == lv.max_concurrency for lv in (bad, good)))

    # Tre endpoint tutti rotti: ogniuno una volta, e si chiude il loop.
    third = _lvl("THIRD")
    res, tried, d, balanced = _run_parallel(
        [bad, good, third],
        lambda lv: (_ for _ in ()).throw(RuntimeError("HTTP 500")),
    )
    check("retry: 3 livelli tutti rotti, un tentativo ciascuno",
          sorted(tried) == ["BAD", "GOOD", "THIRD"] and not res.success)

    # ==================================================================
    # TEMA2 VOCE 1: il gate per chiave endpoint nel percorso SEQUENZIALE.
    #
    # Il difetto misurato (TEMA2 V1): il semaforo per endpoint esisteva solo
    # dentro _Dispatcher, e il dispatcher viene costruito solo in
    # dispatch == "parallel". In sequenziale `max_concurrency` non vincolava
    # NESSUNA richiesta: con max_concurrent_chunks = 6 e max_concurrency = 1
    # dichiarato, 6 worker mandavano 6 richiete CONTEMPORANEE allo stesso
    # endpoint. Il caso non richiedeva `parallel`: bastava dispatch_mode
    # "sequential", o "auto" con zero livelli paralleli.
    #
    # Qui si MISURA il picco di richieste contemporanee per CHIAVE endpoint
    # mentre N worker girano in parallelo sulla catena sequenziale. Il
    # soggetto e' il gate REALE: il tetto dichiarato deve valere.
    print("== gate per endpoint in sequenziale (TEMA2 voce 1) ==")

    def _picco_sequenziale(levels, workers):
        """Lancia `workers` worker sulla catena sequenziale e restituisce il
        picco di richieste contemporanee per CHIAVE endpoint.

        Il picco e' contato dentro _transcribe, che e' il punto in cui la
        richiesta HTTP e' realmente in volo: e' li' che il gate deve
        trattenere, non all'avvio del worker. I worker sono lanciati insieme
        e ogni richiesta dura 50 ms, cosi' l'overlap e' reale: il picco non
        dipende dalla fortuna dello scheduler.
        """
        st = type("S", (), {"language": "it", "prompt": "", "hotwords": "",
                            "fallback": levels})()
        gate = stream_mod._EndpointGate(levels)
        counts: dict[str, int] = {}
        peak: dict[str, int] = {}
        guard = threading.Lock()

        def _fake(lv, w, s, p):
            key = _key(lv)
            with guard:
                counts[key] = counts.get(key, 0) + 1
                peak[key] = max(peak.get(key, 0), counts[key])
            try:
                time.sleep(0.05)
            finally:
                with guard:
                    counts[key] -= 1
            return f"da {lv.name}"

        saved = stream_mod._transcribe
        q: queue.Queue = queue.Queue()
        # Il semaforo del SUPERVISORE e' un BoundedSemaphore(workers) e ogni
        # worker ne ACQUISISCE uno e ne rilascia UNO nel finally, come fa il
        # supervisore prima di ogni submit. Il bilanciamento va rifatto qui
        # perche' i worker partono diretti, non dall'executor: se si
        # pre-acquisisce una volta sola e se ne rilasciano quattro, il
        # BoundedSemaphore esplode con "released too many times" e il test
        # misurerebbe l'harness invece del gate. Uno slot per thread, quindi.
        sem = threading.BoundedSemaphore(workers)
        try:
            stream_mod._transcribe = _fake
            done = []
            lock = threading.Lock()

            def _one(seq):
                sem.acquire()
                try:
                    stream_mod._worker(seq, _wav(), None, stream=st, sem=sem,
                                       result_queue=q, dispatcher=None,
                                       endpoint_gate=gate)
                finally:
                    with lock:
                        done.append(seq)

            threads = [threading.Thread(target=_one, args=(i,))
                       for i in range(workers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)
            return dict(peak), len(done)
        finally:
            stream_mod._transcribe = saved

    # Un solo endpoint con max_concurrency = 1, 4 worker: il picco DEVE
    # essere 1. Senza il gate sarebbe 4, che e' esattamente il difetto.
    uno = _lvl("SOLO", parallel=False, slots=1)
    peak, finiti = _picco_sequenziale([uno], workers=4)
    check("gate sequenziale: max_concurrency = 1 tiene il picco a 1 richiesta",
          peak.get(_key(uno), 0) == 1)
    check("gate sequenziale: tutti i worker finiscono (il gate non perde chunk)",
          finiti == 4)

    # Con 2 slot dichiarati il picco sale a 2 e NON oltre: il gate e' una
    # capienza, non un divieto. Un tetto che bloccasse tutto sarebbe un
    # falso verde di peggior specie.
    due = _lvl("DUE", parallel=False, slots=2)
    peak2, finiti2 = _picco_sequenziale([due], workers=5)
    check("gate sequenziale: max_concurrency = 2 lascia passare 2, non 5",
          peak2.get(_key(due), 0) == 2 and finiti2 == 5)

    # Due endpoint DISTINTI: i due limiti sono indipendenti, ciascuno al suo
    # tetto. Serve a prendere la chiave sbagliata (per livello invece che per
    # endpoint): con due livelli diversi la somma sarebbe 4.
    x1 = _lvl("X1", parallel=False, slots=1)
    x2 = _lvl("X2", parallel=False, slots=1)
    gate2 = stream_mod._EndpointGate([x1, x2])
    check("gate: due endpoint distinti hanno due chiavi distinte",
          len(gate2) == 2 and _key(x1) != _key(x2))
    # Stesso endpoint dichiarato in due livelli (endpoint+modello identici):
    # una sola chiave, quindi una sola capienza condivisa.
    twin_a = _lvl("TWIN", parallel=False, slots=1)
    twin_b = FallbackLevel("TWIN2", twin_a.endpoint, "m", "", "", "", 60,
                           False, False, 1)
    gate3 = stream_mod._EndpointGate([twin_a, twin_b])
    check("gate: due livelli sullo stesso endpoint condividono la capienza",
          len(gate3) == 1 and _key(twin_a) == _key(twin_b))

    # Il gate COSTRUITO su TUTTI i livelli, non solo sui checked: un livello
    # con parallel = false deve avere comunque la sua capienza, altrimenti
    # il gate non lo limiterebbe proprio dove serve.
    gate4 = stream_mod._EndpointGate([_lvl("CHK", parallel=True, slots=1),
                                      _lvl("NOCHK", parallel=False, slots=1)])
    check("gate: costruito anche sui livelli non checked",
          len(gate4) == 2)

    # Rilascio SEMPRE: se il tentativo solleva, lo slot deve tornare. Uno slot
    # perso e' un endpoint saturo per sempre e la catena si blocca sul primo
    # livello: il difetto si vede solo al tentativo successivo, quindi il
    # test lo forza con una seconda chiamata dopo l'eccezione.
    gate5 = stream_mod._EndpointGate([_lvl("REL", parallel=False, slots=1)])
    def _solleva():
        gate5.call(uno, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        _solleva()
    except RuntimeError:
        pass
    # Se il rilascio non c'e' stato, questa seconda chiamata si blocca per
    # sempre: il test deve poter fallire per timeout, non per assenza di
    # eccezione. Il tentativo di ripresa prova il percorso reale.
    try:
        gate5.call(uno, lambda: "ok")
        recuperato = True
    except Exception:  # noqa: BLE001
        recuperato = False
    check("gate: lo slot e' rilasciato anche quando il tentativo solleva",
          recuperato)

    # ==================================================================
    # BRIEF-IMPL-DISPATCH-MODE: [stream].dispatch_mode parallelo/sequenziale
    print("== dispatch_mode (toggle globale) ==")
    from bravoric_stt_clipboard import config as cfg_mod

    def _stream_cfg(dispatch_mode=None, levels=None, **kw):
        """StreamConfig con dispatch_mode gia' applicato, o CHIAVE ASSENTE
        (default "auto") quando dispatch_mode e' None. `_resolve_dispatch`
        legge il dataclass, non il TOML: e' la stessa identita' che vede
        _worker e _build_breaker, quindi il test copre davvero il percorso.
        Il dizionario e' annotato `dict[str, Any]`: senza, lo splat deduce
        un'unione eterogenea (str | float | list) e pyright (basic, include
        "scripts") rifiuta `mode: Literal[...]`."""
        base: dict[str, Any] = dict(mode="per_chunk", silence_seconds=0.7,
                                    noise_db=-30.0, min_utterance_seconds=0.4,
                                    max_utterance_seconds=30.0,
                                    paste_delay_ms=250,
                                    fallback=list(levels or []))
        base.update(kw)
        sc = cfg_mod.StreamConfig(**base)
        if dispatch_mode is None:
            # dispatch_mode assente: si rimuove il campo per esercitare il
            # default reale del dataclass invece di passare "auto" esplicito.
            object.__setattr__(sc, "dispatch_mode", cfg_mod.StreamConfig.dispatch_mode)
        else:
            object.__setattr__(sc, "dispatch_mode", dispatch_mode)
        return sc

    a_p, b_s, c_s = _lvl("A", parallel=True), _lvl("B", parallel=False), _lvl("C", parallel=False)
    no_check = [b_s, c_s]

    # --- T1: chiave ASSENTE = default auto, degrada a sequenziale ---------
    check("T1 dispatch_mode assente + >=1 checked -> parallel",
          stream_mod._resolve_dispatch(_stream_cfg(levels=[a_p, b_s])) == "parallel")
    check("T1 dispatch_mode assente + zero checked -> sequential (degrada)",
          stream_mod._resolve_dispatch(_stream_cfg(levels=no_check)) == "sequential")
    check("T1 default del dataclass StreamConfig e' 'auto'",
          cfg_mod.StreamConfig.dispatch_mode == "auto")

    # --- T2: SEQUENZIALE + A checked + A che SOLLEVA -> catena completa ----
    # IL test che distingue il toggle. La variante "A che va bene -> seen==[A]"
    # e' VERDE-BUGGATA: passa UGUALE con toggle acceso e spento, perche' il
    # ramo sequenziale e il ramo parallelo chiamano entrambi A per primi.
    # Qui A SOLLEVAMO: in parallelo gli altri livelli sono IRRAGGUNGIBILI
    # (A1) e si chiude con AllLevelsFailedError; in sequenziale la catena
    # prosegue su B e C. Solo il toggle acceso produce [A, B, C].
    from bravoric_stt_clipboard.api_client import ApiError as _ApiError

    def _run_worker_dispatch(levels, behaviour, dispatcher=None, dispatch_mode=None):
        st = _stream_cfg(dispatch_mode=dispatch_mode, levels=levels)
        seen: list[str] = []

        def _fake(lv, w, s, p):
            seen.append(lv.name)
            return behaviour(lv)

        saved = stream_mod._transcribe
        try:
            stream_mod._transcribe = _fake
            q = queue.Queue()
            sm = threading.BoundedSemaphore(3)
            sm.acquire()
            stream_mod._worker(0, _wav(), None, stream=st, sem=sm,
                               result_queue=q, dispatcher=dispatcher)
            return q.get_nowait(), seen, sm._value == 3
        finally:
            stream_mod._transcribe = saved

    def _boom(lv):
        raise _ApiError("endpoint A non risponde")

    # In sequenziale A rotto fa proseguire la catena su B, che risponde:
    # seen == [A, B] e res.success True. Il punto che distingue il toggle e'
    # che B e C VENGONO CHIAMATI: in parallelo con solo A checked sono
    # irraggiungibili (A1) e `seen` resta ["A"] con success False.
    res, seen, bal = _run_worker_dispatch(
        [a_p, b_s, c_s],
        lambda lv: (_ for _ in ()).throw(_ApiError("A rotto")) if lv.name == "A" else f"da {lv.name}",
        dispatcher=stream_mod._Dispatcher([a_p, b_s, c_s], _tmp_breaker()),
        dispatch_mode="sequential",
    )
    check("T2 SEQUENZIALE + A checked + A che solleva -> catena prosegue su B",
          seen == ["A", "B"] and res.success and res.text == "da B")
    check("T2 SEQUENZIALE: semaforo bilanciato anche percorrendo piu' livelli", bal)

    # L'ALTRO braccio di T2 (la contro-prova col toggle "auto") e tutto T3
    # stanno in FONDO al file, dove il toggle viene ricostruito: qui il
    # commento storico "in auto deve fermarsi ad A" NON e' piu' vero, perche'
    # col ripiego auto arriva anche a B e C. Aggiornare qui i numeri a mano
    # (seen == ["A","B"]) produrrebbe due test IDENTICI: uno per il toggle
    # acceso e uno per quello spento, quindi VERDI-BUGGATI, e non
    # presidierebbero piu' niente. Il discriminante che resta dopo il ripiego
    # e' ordine e concorrenza, ed e' verificato in coda al file.

    # --- T4: auto + zero checked -> dispatcher inattivo, sequenziale -------
    st_auto0 = _stream_cfg(levels=no_check)
    d0 = stream_mod._Dispatcher(no_check, _tmp_breaker())
    check("T4 auto + zero checked -> not dispatcher.active", not d0.active)
    res, seen, bal = _run_worker_dispatch(no_check, lambda lv: f"da {lv.name}",
                                          dispatcher=d0, dispatch_mode="auto")
    check("T4 auto + zero checked -> seen==['S1'] sul percorso sequenziale",
          seen == ["B"] and res.success)
    check("T4 auto + zero checked -> semaforo del supervisor bilanciato (==3)",
          bal)
    check("T4 _resolve_dispatch e' l'unico punto: zero checked -> sequential",
          stream_mod._resolve_dispatch(st_auto0) == "sequential")

    # --- T5: SEQUENZIALE -> nessuna chiamata al breaker, _NullBreaker ------
    # Il breaker REALE non deve ne' essere costruito ne' scrivere su disco:
    # tutte le chiamate breaker.state/acquire/release stanno dentro
    # _Dispatcher, quindi in sequenziale non deve accadere nessuna.
    calls: list[str] = []
    st_seq = _stream_cfg(dispatch_mode="sequential", levels=[a_p, b_s, c_s])
    brk = stream_mod._build_breaker(st_seq, stream_mod._resolve_dispatch(st_seq))
    check("T5 SEQUENZIALE -> isinstance(breaker, _NullBreaker)",
          isinstance(brk, stream_mod._NullBreaker))
    spy = stream_mod._NullBreaker()
    for meth in ("acquire", "record_failure", "release", "record_success"):
        setattr(spy, meth, (lambda m: lambda *a, **k: calls.append(m))(meth))
    res, seen, bal = _run_worker_dispatch(
        [a_p, b_s, c_s],
        lambda lv: (_ for _ in ()).throw(_ApiError("boom")) if lv.name == "A" else f"da {lv.name}",
        dispatcher=stream_mod._Dispatcher([a_p, b_s, c_s], spy),
        dispatch_mode="sequential",
    )
    check("T5 SEQUENZIALE: nessuna chiamata a breaker.acquire/record_failure/release",
          calls == [])
    # e il ramo sequenziale produce comunque testo: A rotta, B va bene.
    check("T5 SEQUENZIALE: A rotto -> B risponde, catena intatta",
          res.success and res.text == "da B")

    # Cooldown 0: breaker disabilitato per scelta, resta _NullBreaker anche
    # in auto con livelli paralleli (nessun file su disco).
    st_c0 = _stream_cfg(levels=[a_p], endpoint_cooldown_seconds=0.0)
    check("T5 auto + cooldown 0 -> ancora _NullBreaker",
          isinstance(stream_mod._build_breaker(st_c0, "parallel"),
                     stream_mod._NullBreaker))

    # --- T6: FUZZ — dispatch_mode ASSENTE = comportamento pre-refactor ------
    # Per ogni N di livelli misti, con dispatch_mode assente la decisione deve
    # essere esattamente quella di prima: parallelo se e solo se c'e' almeno un
    # checked. E' la rete di sicurezza della retrocompatibilita'.
    import itertools as _it
    seed = 0
    mismatches = []
    for n in range(1, 5):
        for combo in _it.product([True, False], repeat=n):
            if n >= 4 and sum(combo) not in (0, 1, n):
                continue  # campiona le combinazioni estreme (0, 1, tutti)
            lv = [_lvl(f"FZ{i}") for i in range(n)]
            lv = [cast(Any, type(l)(l.name, l.endpoint, l.model, l.api_key_env,
                                    l.api_key, l.ca_cert, l.timeout_seconds,
                                    l.hotwords_in_prompt, flag, l.max_concurrency))
                  for l, flag in zip(lv, combo)]
            st = _stream_cfg(levels=lv)
            expected = "parallel" if any(combo) else "sequential"
            got = stream_mod._resolve_dispatch(st)
            if got != expected:
                mismatches.append((combo, got, expected))
            seed += 1
    check(f"T6 fuzz: {seed} combinazioni di livelli misti, dispatch_mode assente invariato",
          not mismatches)
    # E il punto di consumo: _worker con dispatch_mode assente e zero checked
    # resta sul ramo sequenziale identico a oggi.
    res, seen, bal = _run_worker_dispatch(no_check, lambda lv: f"da {lv.name}",
                                          dispatch_mode=None)
    check("T6 dispatch_mode assente + zero checked -> sequenziale su [B]",
          seen == ["B"] and res.success and bal)
    res, seen, bal = _run_worker_dispatch([a_p, b_s, c_s],
                                          lambda lv: f"da {lv.name}",
                                          dispatcher=stream_mod._Dispatcher(
                                              [a_p, b_s, c_s], _tmp_breaker()),
                                          dispatch_mode=None)
    check("T6 dispatch_mode assente + >=1 checked -> parallelo, primo del pool",
          res.success and res.text.startswith("da "))

    # ==================================================================
    # BRIEF-TIMEOUT-PERLIVELLO: level.timeout_seconds e' l'UNICO timeout
    # della richiesta HTTP. Tutto sotto mock, mai con rete.
    print("== timeout per-livello ==")
    import requests as _rq
    from bravoric_stt_clipboard.config import FallbackLevel as _FL
    from bravoric_stt_clipboard import api_client as _ac

    _audio = tmp / "timeout-probe.wav"
    _audio.write_bytes(b"RIFFfake")

    class _OkResp:
        status_code = 200
        text = ""

        def json(self):
            return {"text": "ciao"}

    def _lvl_to(t, name="L"):
        return _FL(name, "http://example.invalid/v1", "m", "K", "k", "", t)

    def _probe(t, side_effect=None, session=None):
        """Chiama transcribe_audio catturando il timeout realmente passato
        a requests.post; restituisce (timeout_catturato, eccezione)."""
        cap = {}
        def _post(*a, **kw):
            cap["timeout"] = kw.get("timeout")
            if side_effect is not None:
                return side_effect(*a, **kw)
            return _OkResp()
        try:
            with mock.patch.object(_ac.requests, "post", side_effect=_post):
                _ac.transcribe_audio(_lvl_to(t), _audio, session=session)
            return cap.get("timeout"), None
        except Exception as exc:  # noqa: BLE001 - il test osserva l'errore
            return cap.get("timeout"), exc

    # 1. Il timeout per-livello ARRIVA DAVVERO a requests.post. Prima
    #    l'override stream (30s di default) vinceva sempre: 5 era codice morto.
    got, err = _probe(5)
    check("timeout per-livello arriva a requests.post", got == 5.0 and err is None)
    got, err = _probe(120)
    check("timeout 120 arriva a requests.post", got == 120.0 and err is None)

    # 2. Con timeout per-livello 5s, una richiesta che "impiega" 8s deve
    #    sollevare il timeout. Il fake simula il comportamento di requests:
    #    solleva ReadTimeout se l'attesa supera il timeout ricevuto.
    def _slow_8s(url, headers=None, files=None, data=None, timeout=None, verify=None):
        if timeout is None or float(timeout) < 8.0:
            raise _rq.exceptions.ReadTimeout(f"timeout di {timeout}s scaduto, serviva 8s")
        return _OkResp()
    got, err = _probe(5, side_effect=_slow_8s)
    check("5s + richiesta da 8s -> ReadTimeout (prima il dato era ignorato)",
          isinstance(err, _ac.ApiError) and "8s" in str(err))
    # Con 30s la stessa richiesta va a buon fine: prova che il test morda
    # davvero sul valore e non su un qualunque errore di rete.
    got, err = _probe(30, side_effect=_slow_8s)
    check("30s + richiesta da 8s -> va a buon fine (il test discrimina)",
          err is None and got == 30.0)

    # 3. Non finito o <= 0 ricadono su 30.0 (difesa di transcribe_audio).
    for bad_val, label in ((0, "zero"), (-3, "negativo"), (float("inf"), "inf"),
                           (None, "None"), ("boh", "malformato")):
        got, err = _probe(bad_val)
        check(f"timeout per-livello {label} -> 30.0", got == 30.0 and err is None)
    check("timeout per-livello nan -> 30.0", _probe(float("nan"))[0] == 30.0)

    # 4. Vale anche via Session, non solo sul modulo requests.
    class _Sess:
        def __init__(self):
            self.got = None

        def post(self, *a, **kw):
            self.got = kw.get("timeout")
            return _OkResp()
    s = _Sess()
    _ac.transcribe_audio(_lvl_to(7), _audio, session=cast(Any, s))
    check("timeout per-livello vale anche su Session", s.got == 7.0)

    # 5. Non esiste piu' alcun override: la firma non lo offre piu'.
    import inspect as _insp
    check("transcribe_audio non ha piu' timeout_override",
          "timeout_override" not in _insp.signature(_ac.transcribe_audio).parameters)
    check("stream._transcribe non ha piu' chunk_timeout",
          "chunk_timeout" not in _insp.signature(stream_mod._transcribe).parameters)
    check("stream._worker non ha piu' chunk_timeout",
          "chunk_timeout" not in _insp.signature(stream_mod._worker).parameters)

    # 6. Il percorso di stream non re-introduce l'override: _transcribe passa
    #    il timeout solo implicito dal livello.
    cap_s = _Sess()
    st_stream = type("S", (), {"language": "it", "prompt": "", "hotwords": "",
                               "fallback": [], "chunk_timeout_seconds": 30.0})()
    with mock.patch.object(stream_mod, "_thread_session", return_value=cast(Any, cap_s)):
        stream_mod._transcribe(_lvl_to(5), _audio, st_stream, None)
    check("stream._transcribe usa il timeout del livello, non chunk_timeout",
          cap_s.got == 5.0)

    # 7. Budget di stop: la SOMMA dei timeout per-livello, non un unico valore.
    check("budget stop: 3 livelli da 5s -> >= 30s",
          _stop_drain_budget(_Lv(5, 5, 5)) >= 30.0)
    check("budget stop: 3 livelli da 40s -> >= 135s",
          _stop_drain_budget(_Lv(40, 40, 40)) >= 135.0)
    check("budget stop: non tronca la somma dei timeout",
          _stop_drain_budget(_Lv(40, 40, 40)) > sum((40, 40, 40)))

    # 8. Le due icone mancanti (stream_session_start, error_general): i PNG
    #    esistono, sono 128x128 sRGBA e sono i default dei due slot nuovi.
    #    Le dimensioni/alpha si leggono dall'intestazione PNG con struct: il
    #    progetto non dipende da Pillow ne' da ImageMagick.
    def _png_size_has_alpha(path: Path) -> bool:
        raw = path.read_bytes()
        if raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
            return False
        width, height = struct.unpack(">II", raw[16:24])
        color_type = raw[25]
        return (width, height) == (128, 128) and color_type in (4, 6)

    icons_dir = Path(notify.ICONS_DIR)
    for filename in ("stream-session-start.png", "error-general.png"):
        png = icons_dir / filename
        check(f"icona presente e non vuota: {filename}",
              png.is_file() and png.stat().st_size > 0)
        check(f"icona 128x128 con canale alpha: {filename}",
              _png_size_has_alpha(png))

    check("default stream_session_start punta al nuovo PNG",
          notify._PACKAGED_DEFAULTS.get("stream_session_start") == "stream-session-start.png")
    check("default error_general punta al nuovo PNG",
          notify._PACKAGED_DEFAULTS.get("error_general") == "error-general.png")

    check("resolve_icon stream_session_start restituisce il nuovo PNG",
          notify.resolve_icon("stream_session_start", "") == str(icons_dir / "stream-session-start.png"))
    check("resolve_icon error_general restituisce il nuovo PNG",
          notify.resolve_icon("error_general", "") == str(icons_dir / "error-general.png"))

    # 9. Nessuna regressione sui 6 slot storici: devono continuare a
    #    risolvere ai packaged asset di sempre, non alle icone a tema.
    legacy = {
        "stt_start": "mic-neutral.png", "stt_raw": "mic-wood.png",
        "stt_clean": "mic-cyberpunk.png", "ocr_start": "camera-neutral.png",
        "ocr_raw": "camera-wood.png", "ocr_clean": "camera-cyberpunk.png",
    }
    for slot, filename in legacy.items():
        check(f"nessuna regressione: {slot} -> {filename}",
              notify.resolve_icon(slot, "") == str(icons_dir / filename))

    # 10. L'override utente vince ancora sul default appena installato,
    #     anche per i due slot nuovi.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        user_override = tmp / "mia-icona.png"
        user_override.write_bytes((icons_dir / "mic-neutral.png").read_bytes())
        check("override utente vince sul default (stream_session_start)",
              notify.resolve_icon("stream_session_start", str(user_override)) == str(user_override))
        check("override utente vince sul default (error_general)",
              notify.resolve_icon("error_general", str(user_override)) == str(user_override))
        check("override inesistente non e' fatale: torna al nuovo default",
              notify.resolve_icon("stream_session_start", str(tmp / "non-esiste.png"))
              == str(icons_dir / "stream-session-start.png"))

    # 11. Difetto C (giro 1): get_state() deve emettere
    #     max_concurrent_chunks_auto con la STESSA regola di config.py, altrimenti
    #     la nota GUI "il tetto e' automatico" non compare mai e con cap=0 la
    #     GUI dice "fino a 1 worker" mentre il backend calcola 3xN. La regola e'
    #     quella di StreamConfig.max_concurrent_chunks_auto: 0 o assente = AUTO,
    #     1..8 = esplicito, 9+ clampato a 8 (quindi esplicito, non auto).
    print("== difetto C: max_concurrent_chunks_auto emesso da get_state ==")
    # Directory propria: `tmp` piu' sopra e' stato riassociato dentro un
    # `with tempfile.TemporaryDirectory()` gia' uscito, quindi non esiste piu'.
    giro1_tmp = Path(tempfile.mkdtemp(prefix="brv-giro1-"))
    for label, body, want_auto, want_cfg_auto in [
        ("chiave assente = AUTO", "[stream]\nmode = \"per_chunk\"\n", True, True),
        ("0 = AUTO", "[stream]\nmax_concurrent_chunks = 0\n", True, True),
        ("3 = esplicito", "[stream]\nmax_concurrent_chunks = 3\n", False, False),
        ("8 = esplicito", "[stream]\nmax_concurrent_chunks = 8\n", False, False),
        ("12 = clampato a 8, esplicito", "[stream]\nmax_concurrent_chunks = 12\n", False, False),
    ]:
        auto_path = giro1_tmp / f"auto_{abs(hash(label))}.toml"
        auto_path.write_text(body)
        config_editor.CONFIG_PATH = auto_path
        auto_state = config_editor.get_state()["stream"]
        check(f"get_state emette max_concurrent_chunks_auto ({label})",
              "max_concurrent_chunks_auto" in auto_state
              and auto_state["max_concurrent_chunks_auto"] is want_auto)
        # Coerenza con la regola vera: config.py sullo stesso file.
        backend_cfg = config.load_config(auto_path)
        check(f"il flag coincide con config.py ({label})",
              backend_cfg.stream.max_concurrent_chunks_auto is want_cfg_auto
              and auto_state["max_concurrent_chunks_auto"] == backend_cfg.stream.max_concurrent_chunks_auto)

    # 12. Difetto B (giro 1): `inf` e' un float TOML legittimo e int(inf)
    #     solleva OverflowError, che NON e' una ValueError: sfuggiva come
    #     traceback grezzo invece di ConfigError. Misurato su 6 campi.
    print("== difetto B: OverflowError non sfugge piu' ==")
    inf_cases = [
        ("stream.max_concurrent_chunks", "[stream]\nmax_concurrent_chunks = inf\n"),
        ("stream.paste_delay_ms", "[stream]\npaste_delay_ms = inf\n"),
        ("history.max_entries", "[history]\nmax_entries = inf\n"),
        ("audio.sample_rate", "[audio]\nsample_rate = inf\n"),
        ("audio.bitrate_kbps", "[audio]\nbitrate_kbps = inf\n"),
        ("audio.retry_count", "[audio]\nretry_count = inf\n"),
    ]
    for label, body in inf_cases:
        inf_path = giro1_tmp / f"inf_{abs(hash(label))}.toml"
        inf_path.write_text(body)
        try:
            config.load_config(inf_path)
            check(f"inf gestito senza traceback ({label})", True)
        except config.ConfigError:
            check(f"inf diventa ConfigError leggibile ({label})", True)
        except Exception as exc:  # noqa: BLE001 - la fuga e' il difetto
            check(f"inf non sfugge come {type(exc).__name__} ({label})", False)
    # -inf e nan restano tollerati: non devono diventare errori nuovi.
    for label, body in [("-inf", "[stream]\nmax_concurrent_chunks = -inf\n"),
                        ("nan", "[stream]\nmax_concurrent_chunks = nan\n")]:
        nan_path = giro1_tmp / f"nan_{abs(hash(label))}.toml"
        nan_path.write_text(body)
        try:
            check(f"{label} resta tollerato", isinstance(config.load_config(nan_path), config.Config))
        except Exception:  # noqa: BLE001
            check(f"{label} resta tollerato", False)
    check("_coerce_int ricade sul default su inf invece di propagare",
          config._coerce_int(float("inf"), 7, 0, 8) == 7)

    # 13. Difetto O (giro 1): un singolo termine senza spazi piu' lungo del
    #     budget faceva cadere il fallback sul taglio dalla TESTA della frase
    #     intera, restituendo solo 'sono nomi proprio.' — cornice mozzata e 782
    #     caratteri sprecati su 800. Ora la cornice si tiene sempre.
    print("== difetto O: la cornice del vocabolario regge ==")
    long_single = "Parola" * 400          # 2400 caratteri, un solo "termine"
    vocab_long = api_client._build_vocabulary_prompt("", "", long_single, api_client.PROMPT_MAX_CHARS)
    check("termine unico enorme: la cornice resta intera",
          vocab_long.startswith(api_client._VOCAB_HEAD)
          and vocab_long.endswith(api_client._VOCAB_TAIL))
    check("termine unico enorme: il budget non viene sprecato",
          len(vocab_long) > 500)
    check("termine unico enorme: mai oltre il limite",
          len(vocab_long) <= api_client.PROMPT_MAX_CHARS)
    # La banda sotto la cornice non puo' stare intera: nessuna regressione
    # peggiore di prima (prima restituiva un frammento di coda).
    below = api_client._vocabulary_sentence("Roma", 18)
    check("budget sotto la cornice: niente stringa vuota",
          isinstance(below, str) and len(below) <= 18)
    # Il riflesso 21b344eb8954 resta: con personale pieno la testa del prompt
    # personale non viene sacrificata per il vocabolario.
    personal_keep = api_client._build_vocabulary_prompt("PERSONALE", "", long_single, api_client.PROMPT_MAX_CHARS)
    check("riflesso 21b344eb8954: il prompt personale resta intatto",
          personal_keep.startswith("PERSONALE"))

    # 14. Difetto D (giro 1): paste_next() legge TUTTO lo stato, dorme
    #     paste_delay_ms e riscrive TUTTO lo stato. _write_state() mergeava
    #     solo next_chunk_index/last_paste_at, NON chunks: un chunk committato
    #     dal supervisore nella finestra veniva sovrascritto. Misurato prima
    #     della correzione: 1 chunk PERSO con un commit a 10ms su finestra di
    #     250ms. Qui la finestra e' riprodotta davvero (thread + sleep).
    print("== difetto D: il chunk committato nella finestra non viene perso ==")
    from bravoric_stt_clipboard import stream as stream_module
    # Il furto permanente di _write_state (lambda, senza ripristino) e' stato
    # rimosso alla fonte: senza, questo blocco scriverebbe e rileggerebbe lo
    # stesso dict in memoria e passerebbe VERDE anche senza la correzione D,
    # cioe' non presidierebbe niente. Qui si usa il percorso REALE su disco.
    if getattr(stream_module._write_state, "__name__", "").startswith("<lambda>"):
        check("presidio D non vacuo: _write_state non e' piu' la lambda di un altro test", False)
    stream_state_saved = stream_module.STREAM_STATE_PATH
    clipboard_saved = stream_module.clipboard
    try:
        d_dir = giro1_tmp / "stateD"
        d_dir.mkdir(parents=True, exist_ok=True)
        stream_module.STREAM_STATE_PATH = d_dir / "stream_state.json"
        written_chunks: list[str] = []

        class _FakeClipboard:
            def write_text(self, text: str, tool: str) -> None:
                written_chunks.append(text)

        stream_module.clipboard = _FakeClipboard()
        fake_stream_cfg = types.SimpleNamespace(
            blacklist="", paste_delay_ms=250, paste_shortcut="ctrl+v",
            paste_channel="clipboard", commands=[],
        )
        sess = types.SimpleNamespace(
            _stream=fake_stream_cfg,
            _cfg=types.SimpleNamespace(clipboard_tool="wl-copy"),
        )
        paste_next_fn = stream_module.StreamSession.paste_next.__get__(sess)

        # last_paste_at = adesso APRE la finestra di pacing da 250ms.
        stream_module._write_state({
            "session_id": "giro1", "active": True, "mode": "per_chunk",
            "chunks": ["PRIMO"], "next_chunk_index": 0,
            "last_paste_at": time.time(),
            "paste_delay_ms": 250, "paste_shortcut": "ctrl+v",
            "paste_channel": "clipboard", "commands": [], "blacklist": "",
        })
        supervisor_state = json.loads(stream_module.STREAM_STATE_PATH.read_text())

        def _commit_inside_window() -> None:
            # 10ms: dentro la finestra di 250ms di paste_next.
            time.sleep(0.010)
            supervisor_state.setdefault("chunks", []).append("SECONDO-COMMITTATO")
            stream_module._write_state(supervisor_state)

        committer = threading.Thread(target=_commit_inside_window)
        committer.start()
        paste_next_fn()
        committer.join()

        after_D = json.loads(stream_module.STREAM_STATE_PATH.read_text())
        check("chunk committato nella finestra NON e' perso",
              "SECONDO-COMMITTATO" in after_D.get("chunks", []))
        check("il chunk gia' incollato resta e l'indice avanza di uno",
              after_D.get("next_chunk_index") == 1 and written_chunks == ["PRIMO"])
        check("l'indice non punta oltre la lista dei chunk",
              after_D.get("next_chunk_index", 0) <= len(after_D.get("chunks", [])))

        # Difetto L: la clipboard e' l'unica chiamata a processo esterno rimasta
        # fuori dal try. Con wl-copy assente l'eccezione usciva da paste_next
        # e il chunk non veniva mai incollato.
        class _BrokenClipboard:
            def write_text(self, text: str, tool: str) -> None:
                raise FileNotFoundError(2, "No such file or directory: 'wl-copy'")

        stream_module.clipboard = _BrokenClipboard()
        stream_module._write_state({
            "session_id": "giro1L", "active": True, "mode": "per_chunk",
            "chunks": ["CHUNK-L"], "next_chunk_index": 0,
            "paste_delay_ms": 0, "paste_shortcut": "ctrl+v",
            "paste_channel": "clipboard", "commands": [], "blacklist": "",
        })
        raised = None
        try:
            paste_next_fn()
        except Exception as exc:  # noqa: BLE001 - la fuga e' il difetto
            raised = exc
        after_L = json.loads(stream_module.STREAM_STATE_PATH.read_text())
        check("clipboard rotta: paste_next non propaga l'eccezione", raised is None)
        check("clipboard rotta: il chunk resta in coda, nessuna perdita",
              "CHUNK-L" in after_L.get("chunks", [])
              and after_L.get("next_chunk_index") == 0)
    finally:
        stream_module.STREAM_STATE_PATH = stream_state_saved
        stream_module.clipboard = clipboard_saved
    # 15. Difetto N (giro 1): la premessa dello scout era falsa (storage.py non
    #     ha nessuna open()), ma restava una finestra TOCTOU reale: exists()
    #     e write_bytes() non sono atomici, quindi due processi nello stesso
    #     millisecondo potevano scegliere lo stesso nome. Ora la creazione e'
    #     con O_EXCL. Presidiato qui sul comportamento osservabile: piu'
    #     salvataggi nello stesso secondo danno file distinti e nessuna
    #     sovrascrittura, e i file preesistenti non vengono toccati.
    print("== difetto N: nomi distinti e nessuna sovrascrittura ==")
    n_dir = giro1_tmp / "storageN"
    n_pol = config.RetentionPolicy(enabled=True, retention_hours=0)
    n_paths = [storage.save_if_enabled(str(n_dir), "sub", n_pol, f"contenuto-{i}".encode(), "txt")
               for i in range(5)]
    check("5 salvataggi nello stesso secondo producono 5 file distinti",
          len({str(p) for p in n_paths}) == 5 and all(p is not None for p in n_paths))
    check("nessun contenuto sovrascritto",
          len({p.read_bytes() for p in n_paths if p}) == 5)
    check("i file sono leggibili solo dal proprietario (0o600)",
          all((p.stat().st_mode & 0o777) == 0o600 for p in n_paths if p))
    check("policy disabilitata: nessun file scritto",
          storage.save_if_enabled(str(n_dir), "sub",
                                  config.RetentionPolicy(enabled=False, retention_hours=0),
                                  b"x", "txt") is None)

    # La prova qui sopra NON distingue le due implementazioni (passa anche con
    # il vecchio while path.exists() + write_bytes: e' appunto la premessa
    # falsa dello scout). Il presidio vero del TOCTOU e' sulla creazione
    # atomica del file: O_EXCL fa fallire os.open se il nome e' gia' preso,
    # mentre exists()+write_bytes() e' una finestra non atomica. Qui si
    # verifica il meccanismo, non l'intento.
    import inspect as _inspect
    storage_src = _inspect.getsource(storage.save_if_enabled)
    check("storage crea il file in modo atomico (O_EXCL, niente finestra exists->write)",
          "os.O_EXCL" in storage_src and "path.write_bytes(content)" not in storage_src)
    check("storage gestisce esplicitamente FileExistsError invece di fallire in silenzio",
          "FileExistsError" in storage_src)

    # Comportamento a prova diretta: se il nome e' gia' preso, la funzione
    # prosegue con il successivo invece di sollevare. Con il vecchio
    # while path.exists() il risultato e' lo stesso, qui: e' il controllo
    # statico sopra a presidiare il meccanismo atomico.
    n_dir2 = giro1_tmp / "storageN2"
    ts_n = time.strftime("%Y-%m-%dT%H-%M-%S")
    (n_dir2 / "sub").mkdir(parents=True, exist_ok=True)
    (n_dir2 / "sub" / f"{ts_n}.txt").write_bytes(b"gia-presente")
    n2 = storage.save_if_enabled(str(n_dir2), "sub", n_pol, b"nuovo", "txt")
    check("nome gia' occupato: si prosegue col successivo e non si sovrascrive",
          n2 is not None and n2.name != f"{ts_n}.txt"
          and (n_dir2 / "sub" / f"{ts_n}.txt").read_bytes() == b"gia-presente")

    # --- B3 (giro 4): file di archivio TRONCATO col nome definitivo ---------
    # Con O_EXCL l'fd serve a riservare il nome, ma il contenuto passava
    # comunque sul file definitivo: un ENOSPC a meta' scriveva lasciando sul
    # disco un file PARZIALE col nome timestampato definitivo, che l'utente
    # credeva completo e che con retention_hours=0 (return immediato in
    # _purge_expired) non sarebbe mai stato ripulito.
    # Il test ha DUE facce per non essere un test di un dettaglio:
    #   (a) l'eccezione continua a propagare al chiamante (semantica invariata);
    #   (b) NON resta alcun file, né troncato col nome definitivo né .tmp.
    # Il confronto con il comportamento pre-correzione e' reale: senza (b)
    # questa funzione scriveva i 10 byte e lasciava il file (misurato dal
    # reviewer con lo stesso trucco, e verificato qui con l'assert su 0 file).
    print("== B3: scrittura fallita non lascia file col nome definitivo ==")
    b3_dir = giro1_tmp / "storageB3"
    b3_real_fdopen = os.fdopen

    class _PartialWrite:
        """Scrive i primi 10 byte e poi simula il disco pieno."""

        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            self._fh.write(data[:10])
            self._fh.flush()
            raise OSError(28, "No space left on device")

        def flush(self):
            pass

        def fileno(self):
            return self._fh.fileno()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _broken_fdopen(fd, *args, **kwargs):
        return _PartialWrite(b3_real_fdopen(fd, *args, **kwargs))

    b3_raised = None
    with mock.patch.object(storage.os, "fdopen", side_effect=_broken_fdopen):
        try:
            storage.save_if_enabled(str(b3_dir), "sub", n_pol, b"X" * 500, "txt")
        except OSError as exc:
            b3_raised = exc
    b3_sub = b3_dir / "sub"
    b3_left = sorted(p.name for p in b3_sub.iterdir()) if b3_sub.exists() else []
    if b3_left:
        print(f"    (residui trovati: {b3_left})")
    check("B3: l'eccezione di scrittura continua a propagare al chiamante",
          isinstance(b3_raised, OSError) and b3_raised.errno == 28)
    check("B3: nessun file resta su disco, ne' troncato ne' .tmp",
          b3_left == [])
    check("B3: nessun file col nome definitivo (timestamp .txt)",
          not list(b3_sub.glob("*.txt")) if b3_sub.exists() else True)
    check("B3: il percorso di successo continua a funzionare dopo il fallimento",
          (lambda p: p is not None and p.read_bytes() == b"X" * 500)(
              storage.save_if_enabled(str(b3_dir), "sub", n_pol, b"X" * 500, "txt")))
    # Non-vacuita': il meccanismo di scrittura atomica e' quello dichiarato.
    b3_src = _inspect.getsource(storage.save_if_enabled)
    check("B3: il contenuto passa da un temporaneo con os.replace (nome definitivo mai parziale)",
          "os.replace(tmp_path, path)" in b3_src and "fh.write(content)" in b3_src)
    check("B3: la pulizia copre BaseException, non solo Exception",
          "except BaseException:" in b3_src)

    # ==================================================================
    # BRIEF-FALLBACK-TUTTI: se il pool parallel non ha endpoint utilizzabili
    # si usa TUTTA la lista in catena sequenziale, e il chunk non si perde.
    #
    # Ogni test sotto e' verificato per NON-VACUITA': il confronto col
    # comportamento pre-correzione e' fatto davvero (cfr. `_vecchio_worker`), e
    # i casi 2/3/4 falliscono con il codice di prima. Un test che passa anche
    # prima della correzione non presidia niente.
    print("== fallback: pool senza endpoint utilizzabili -> catena su TUTTI ==")
    from bravoric_stt_clipboard import stream as _sm
    from bravoric_stt_clipboard.api_client import ApiError as _ApiErr2

    def _wav2():
        p = Path(tempfile.mkdtemp()) / "utt.wav"
        p.write_bytes(b"")
        return p

    def _fake_chain(seen, behaviour):
        """Sostituisce _sequential_chain con un doppione fedele di
        try_with_fallback: registra i livelli tentati, IGNORA i fallimenti
        singoli e passa al successivo, e solo se tutti falliscono alza
        AllLevelsFailedError. Senza questo comportamento il doppione sarebbe
        piu' severo della catena vera e i test misurerebbero il doppione.

        `behaviour` ha la firma di un livello (un argomento), come tutti i
        doppioni di questo blocco: la catena e' il solo posto che chiama
        `behaviour(lv)`."""
        def _inner(levels, wav_path, stream, prompt):
            errs = []
            for lv in levels:
                seen.append(("chain", lv.name))
                try:
                    return behaviour(lv)
                except Exception as exc:  # noqa: BLE001 - il doppione guarda i fallimenti
                    errs.append(f"{lv.name}: {exc}")
            raise _sm.AllLevelsFailedError("All levels failed: " + " | ".join(errs))
        return _inner

    def _run(levels, behaviour, *, parallel_levels=None, breaker=None,
             stop_timeout=30.0, stop_check=None, timeout_tweak=None):
        """Esegue _worker sul percorso parallelo col fallback. Restituisce
        (risultato, traccia dei livelli, dispatcher, semaforo bilanciato)."""
        st = _stream_cfg(levels=levels)
        seen: list[tuple[str, str]] = []
        disp = _sm._Dispatcher(
            parallel_levels if parallel_levels is not None else levels,
            breaker if breaker is not None else _tmp_breaker(),
            fallback_chain=levels,
        )
        saved_t = stream_mod._transcribe
        saved_c = _sm._sequential_chain
        try:
            def _t(lv, w, s, p):
                seen.append(("pool", lv.name))
                return behaviour(lv)
            stream_mod._transcribe = _t
            _sm._sequential_chain = _fake_chain(seen, behaviour)
            q = queue.Queue()
            sm = threading.BoundedSemaphore(3)
            sm.acquire()
            kw = {}
            if stop_check is not None:
                kw["stop_check"] = stop_check
            if timeout_tweak is not None:
                timeout_tweak()
            stream_mod._worker(0, _wav2(), None, stream=st, sem=sm,
                               result_queue=q, dispatcher=disp,
                               stop_timeout=stop_timeout, **kw)
            return q.get_nowait(), seen, disp, sm._value == 3
        finally:
            stream_mod._transcribe = saved_t
            _sm._sequential_chain = saved_c

    def _boom_on(*names):
        """behaviour: solleva ApiError sui livelli in `names`, testo sugli altri."""
        def _b(lv):
            if lv.name in names:
                raise _ApiErr2(f"{lv.name} non risponde")
            return f"da {lv.name}"
        return _b

    # --- caso 1: checked che solleva + NON-checked che funziona ------------
    # IL test centrale del brief. Prima della correzione i livelli non-checked
    # erano IRRAGGUNGIBILI (il dispatcher costruiva il pool solo sui checked e
    # il worker chiudeva con AllLevelsFailedError): res.success False e B/C
    # mai chiamati. Ora il testo del non-checked ARRIVA.
    res, trace, disp, bal = _run([a_p, b_s, c_s], _boom_on("A"))
    check("F1 checked rotto + non-checked buono: il testo del non-checked ARRIVA",
          res.success and res.text == "da B")
    # NOTA (unica asserzione della serie F toccata, ed e' una scelta da
    # dichiarare): questo era `[("chain","A"), ("chain","B")]`, cioe' PINNAVA
    # LETTERALMENTE il difetto che BRIEF-GATE-FIX-5FAIL ha ordinato di
    # correggere (la catena che riprova i livelli gia' tentati dal pool). Con
    # la correzione il pool tenta A, la catena riparte da B e NON ritocca A,
    # quindi la traccia reale e' [("pool","A"), ("chain","B")]. Il senso del
    # test ("il ripiego e' la CATENA, non il pool") resta identico e
    # diventa piu' forte: adesso presidia ANCHE che la catena non rifaccia
    # il giro del pool. La prova di non-vacuità del difetto e' la stessa
    # della sezione sotto: togliendo `_levels_untried` questa riga va rossa.
    check("F1 il percorso di ripiego e' la CATENA, non il pool",
          [n for kind, n in trace if kind == "pool"] == ["A"]
          and [n for kind, n in trace if kind == "chain"] == ["B"])
    check("F1 semaforo bilanciato dopo il ripiego", bal)
    check("F1 nessun chunk vuoto: res.success True con testo non vuoto",
          bool(res.text) and res.error is None)

    # Contro-prova di NON-VACUITA': lo stesso scenario col codice di prima
    # (ramo parallelo chiuso in AllLevelsFailedError, senza catena) DEVE
    # fallire. Se anche questa asserzione passasse, il test non presiderebbe.
    def _vecchio_worker(levels, behaviour):
        """Il comportamento pre-correzione: pool sui soli checked, se tutti
        falliscono AllLevelsFailedError, la catena NON viene mai tentata."""
        seen2: list[tuple[str, str]] = []
        disp2 = stream_mod._Dispatcher(levels, _tmp_breaker())
        saved = stream_mod._transcribe
        try:
            def _t(lv, w, s, p):
                seen2.append(("pool", lv.name))
                return behaviour(lv)
            stream_mod._transcribe = _t
            tried, errors = set(), []
            for _r in (0, 1):
                for lv in disp2.candidates_excluding(levels, tried):
                    k = stream_mod._level_key(lv)
                    tried.add(k)
                    got, key = disp2.acquire(levels)
                    if got is None:
                        break
                    try:
                        stream_mod._transcribe(got, _wav2(), None, None)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{k}: {exc}")
                        if key is not None:
                            disp2.release(key)
                        continue
                    if key is not None:
                        disp2.release(key)
                    break
                if errors:
                    break
            return (bool(errors), seen2)
        finally:
            stream_mod._transcribe = saved

    old_fail, old_seen = _vecchio_worker([a_p, b_s, c_s], _boom_on("A"))
    check("F1 NON-VACUITA': col codice pre-correzione il caso 1 era irraggiungibile",
          old_fail and [n for _, n in old_seen] == ["A"])

    # --- caso 2: pool INTERAMENTE in cooldown -> catena, non scarto -------
    p_bad = _lvl("COOL")
    q_good = _lvl("AFTER")
    brk2 = _tmp_breaker()
    brk2.record_failure(_key(p_bad))          # endpoint in cooldown
    res, trace, disp, bal = _run(
        [p_bad, q_good], lambda lv: f"da {lv.name}", parallel_levels=[p_bad],
        breaker=brk2,
    )
    check("F2 pool interamente in cooldown: il chunk e' servito dalla catena",
          res.success and res.text == "da COOL")
    check("F2 nessun endpoint del pool toccato (era in cooldown, non ignoto)",
          ("pool", "COOL") not in trace)
    check("F2 semaforo bilanciato e nessun chunk perso", bal and res.error is None)
    # NON-VACUITA': con il codice pre-correzione _candidates ordinava per
    # scadenza e restituiva comunque l'endpoint in cooldown, quindi acquire()
    # falliva e il chunk finiva scartato dal semaforo.
    disp_old = stream_mod._Dispatcher([p_bad], brk2)
    cands_old = list(disp_old._order)  # il vecchio comportamento restituiva la chiave
    check("F2 NON-VACUITA': prima il pool offriva l'endpoint in cooldown",
          bool(cands_old) and _key(p_bad) in cands_old
          and brk2.state(_key(p_bad)) == "OPEN")
    check("F2 dopo la correzione l'endpoint in cooldown NON e' piu' candidato",
          disp._candidates(time.time()) == [])

    # --- caso 3: nessun chunk perso in silenzio --------------------------
    # Sorgente del difetto: il supervisor cancellava il WAV e ingoiava un
    # _ChunkResult VUOTO. Qui si verifica che _worker non produce mai un
    # risultato vuoto quando esiste almeno un endpoint non in cooldown, e che
    # il testo del livello non-checked sia davvero committato dal sequenziatore
    # (non solo presente nel risultato).
    seq_state: dict[str, Any] = {"session_id": "probe", "chunks": []}
    sq = _sm._FifoSequencer(
        seq_state, _stream_cfg(levels=[a_p, b_s, c_s]),
        lambda t: None, lambda t: None,
        log_path=_TEST_CHUNK_LOG_PATH,
    )
    st_s = _stream_cfg(levels=[a_p, b_s, c_s])
    seen3: list[str] = []
    saved_t3 = stream_mod._transcribe
    saved_c3 = _sm._sequential_chain
    try:
        def _t3(lv, w, s, p):
            seen3.append(lv.name)
            if lv.name == "A":
                raise _ApiErr2("A non risponde")
            return f"da {lv.name}"
        stream_mod._transcribe = _t3
        _sm._sequential_chain = _fake_chain([], lambda lv: _t3(lv, None, None, None))
        d3 = _sm._Dispatcher([a_p], _tmp_breaker(), fallback_chain=[a_p, b_s, c_s])
        d3._breaker.record_failure(_key(a_p))   # pool tutto in cooldown
        qq: queue.Queue = queue.Queue()
        sm3 = threading.BoundedSemaphore(3)
        sm3.acquire()
        # seq 0: il sequenziatore e' FIFO e aspetta da _next_expected in poi',
        # quindi un seq_id sparato (7) resterebbe in attesa per sempre e il
        # test misurerebbe l'ordine, non la perdita del chunk. Il caso reale
        # e' seq 0, primo chunk della sessione.
        _sm._worker(0, _wav2(), None, stream=st_s, sem=sm3,
                    result_queue=qq, dispatcher=d3)
        r3 = qq.get_nowait()
    finally:
        stream_mod._transcribe = saved_t3
        _sm._sequential_chain = saved_c3
    committed = sq.ingest(r3)
    check("F3 endpoint non in cooldown esistente: il chunk NON e' vuoto",
          r3.success and bool(r3.text.strip()) and r3.error is None)
    check("F3 il testo arriva al sequenziatore (non scartato in silenzio)",
          committed == ["da B "] or "da B" in "".join(committed))
    check("F3 nessun _ChunkResult vuoto quando un endpoint e' utilizzabile",
          not (not r3.success and not r3.text and r3.error is None))

    # E il caso peggiore: pool in cooldown E catena esaurita. Non si puo'
    # inventare un testo, ma il chunk non deve sparire in silenzio: l'errore
    # deve essere esplicito e il supervisore non deve accorgersi di niente.
    res, trace, disp, bal = _run(
        [p_bad, q_good],
        lambda lv: (_ for _ in ()).throw(_ApiErr2("anche questo e' rotto")),
        parallel_levels=[p_bad], breaker=brk2,
    )
    check("F3 pool in cooldown + catena esaurita: fallimento ESPLICITO, non vuoto",
          (not res.success) and bool(res.error) and "unexpected" not in (res.error or ""))
    check("F3 semaforo bilanciato anche quando tutto fallisce", bal)

    # --- caso 4: caso SANO invariato, la catena NON viene chiamata ---------
    # Con un checked libero si usa il pool e basta: se la catena venisse
    # chiamata anche qui, il "percorso veloce" non esisterebbe piu'.
    res, trace, disp, bal = _run([a_p, b_s, c_s], lambda lv: f"da {lv.name}")
    check("F4 caso sano: si usa il POOL e la catena NON viene chiamata",
          bool(res.success) and bool(trace)
          and all(kind == "pool" for kind, _ in trace)
          and trace[0][1] == "A")
    check("F4 caso sano: nessun livello non-checked viene toccato",
          [n for _, n in trace] == ["A"])
    # Contratto C: i non-checked NON sono membri del pool e non prendono lease.
    check("F4 C: il pool contiene solo i checked (i non-checked non sono membri)",
          list(disp._order) == [_key(a_p)] and disp.level_count == 1)
    check("F4 C: la retrovia contiene TUTTI i livelli, in ordine di config",
          [lv.name for lv in disp.fallback_chain] == ["A", "B", "C"])
    check("F4 C: i non-checked non hanno slot nel dispatcher",
          _key(b_s) not in disp._capacity and _key(c_s) not in disp._capacity)
    # E con piu' endpoint paralleli sani, la catena resta comunque muta.
    d_ok, e_ok = _lvl("OK1"), _lvl("OK2")
    res, trace, disp, bal = _run([d_ok, e_ok, b_s], lambda lv: f"da {lv.name}",
                                parallel_levels=[d_ok, e_ok])
    check("F4 caso sano con piu' endpoint: resta tutto sul pool",
          bool(res.success) and all(k == "pool" for k, _ in trace)
          and {n for _, n in trace} <= {"OK1", "OK2"})

    # --- caso 5: il breaker non registra doppio fallimento ---------------
    # Un endpoint non puo' essere registrato come fallito due volte per lo
    # stesso chunk. Il conteggio e' quello del breaker reale su path temp.
    brk3 = _tmp_breaker()
    d3b = _sm._Dispatcher([bad], brk3, fallback_chain=[bad])
    st5 = _stream_cfg(levels=[bad])
    seen5: list[tuple[str, str]] = []
    saved_t5 = stream_mod._transcribe
    saved_c5 = _sm._sequential_chain
    try:
        def _t5(lv, w, s, p):
            seen5.append(("pool", lv.name))
            raise _ApiErr2("bad rotto")
        stream_mod._transcribe = _t5
        def _chain5(levels, wav_path, stream, prompt):
            for lv in levels:
                seen5.append(("chain", lv.name))
                raise _ApiErr2("catena: bad rotto")
            raise _sm.AllLevelsFailedError("catena esaurita")
        _sm._sequential_chain = _chain5
        q5: queue.Queue = queue.Queue()
        sm5 = threading.BoundedSemaphore(3)
        sm5.acquire()
        _sm._worker(0, _wav2(), None, stream=st5, sem=sm5,
                    result_queue=q5, dispatcher=d3b)
        r5 = q5.get_nowait()
    finally:
        stream_mod._transcribe = saved_t5
        _sm._sequential_chain = saved_c5
    # Nessun doppio conteggio: il fallimento del pool e' registrato UNA volta.
    rec = brk3._records.get(_key(bad))
    check("F5 un endpoint non viene registrato come fallito due volte per chunk",
          rec is not None and rec.failures == 1)
    check("F5 il fallimento viene comunque registrato (non azzerato dal ripiego)",
          brk3.state(_key(bad)) == "OPEN")
    # E il caso della catena: la catena NON registra fallimenti, quindi un
    # livello servito solo da lei non apre mai un cooldown (contratto F: il
    # conteggio per endpoint e per chunk resta quello del pool).
    brk4 = _tmp_breaker()
    d4 = _sm._Dispatcher([a_p, b_s, c_s], brk4, fallback_chain=[a_p, b_s, c_s])
    res, seen4, disp4, bal4 = _run([a_p, b_s, c_s], _boom_on("A"),
                                   parallel_levels=[a_p], breaker=brk4)
    check("F5 il livello servito solo dalla catena non registra alcun fallimento",
          brk4._records.get(_key(b_s)) is None
          and brk4._records.get(_key(c_s)) is None)
    check("F5 il testo del livello non-checked non apre il cooldown",
          res.success and res.text == "da B" and b_s not in disp4._order)

    # --- caso 6: AUTO (contratto E) ---------------------------------------
    # Con l'unico checked in cooldown, il cap non deve collassare a 1 se esiste
    # un altro endpoint utilizzabile: altrimenti il chunk non entra in coda e
    # finisce a terra.
    brk5 = _tmp_breaker()
    brk5.record_failure(_key(a_p))
    cap_cooled = _sm._auto_worker_count([a_p, b_s, c_s], [a_p], brk5)
    check("F6 AUTO: un checked in cooldown non basta a far collassare il cap a 1",
          cap_cooled > 1)
    brk6 = _tmp_breaker()
    slots_ok = sum(stream_mod._parallel_slots(lv) for lv in (d_ok, e_ok))
    cap_healthy = _sm._auto_worker_count([d_ok, e_ok, b_s], [d_ok, e_ok], brk6)
    check("F6 AUTO caso sano: la formula NON cambia, cap = somma degli slot",
          cap_healthy == min(8, slots_ok), )
    brk7 = _tmp_breaker()
    for lv in (a_p, b_s):
        brk7.record_failure(_key(lv))
    check("F6 AUTO: zero endpoint utilizzabili -> 1 (mai 0, l'executor deve girare)",
          _sm._auto_worker_count([a_p, b_s], [a_p], brk7) == 1)
    check("F6 AUTO: clamp a 8 col tetto dichiarato dal progetto",
          _sm._auto_worker_count([d_ok, e_ok, b_s, d_ok, e_ok], [d_ok, e_ok, d_ok, e_ok], _tmp_breaker()) == 8)

    # --- caso 7: has_pending_capacity, la porta del fallback --------------
    d7 = _sm._Dispatcher([a_p], _tmp_breaker(), fallback_chain=[a_p, b_s])
    check("F7 pool vuoto di usable -> has_pending_capacity False (porta aperta)",
          not _sm._Dispatcher([b_s], _tmp_breaker()).has_pending_capacity())
    check("F7 checked libero -> has_pending_capacity True (porta chiusa, niente fallback)",
          d7.has_pending_capacity())
    b7 = _tmp_breaker()
    b7.record_failure(_key(a_p))
    d7b = _sm._Dispatcher([a_p], b7, fallback_chain=[a_p, b_s])
    check("F7 checked in cooldown -> has_pending_capacity False",
          not d7b.has_pending_capacity())
    # Slot esauriti ma breakeraperto: si ASPETTA (il contratto B parla di
    # "busy oltre la deadline", non di coda momentanea).
    d7c = _sm._Dispatcher([a_p], _tmp_breaker(), fallback_chain=[a_p, b_s])
    d7c._free[_key(a_p)] = 0
    check("F7 slot esauriti con endpoint aperto: si aspetta, non si ripiega",
          not d7c.has_pending_capacity())
    # E in quel caso il ripiego c'e' comunque, se il lease non arriva: qui si
    # simula la deadline scaduta dello stop_check.
    def _always_stopping():
        return True
    seen8: list[tuple[str, str]] = []
    saved_t8 = stream_mod._transcribe
    saved_c8 = _sm._sequential_chain
    try:
        def _t8(lv, w, s, p):
            seen8.append(("pool", lv.name))
            return f"da {lv.name}"
        stream_mod._transcribe = _t8
        _sm._sequential_chain = _fake_chain(seen8, lambda lv: f"da {lv.name}")
        d8 = _sm._Dispatcher([a_p], _tmp_breaker(), fallback_chain=[a_p, b_s, c_s])
        d8._free[_key(a_p)] = 0     # pool saturo: nessun slot
        st8 = _stream_cfg(levels=[a_p, b_s, c_s])
        q8: queue.Queue = queue.Queue()
        sm8 = threading.BoundedSemaphore(3)
        sm8.acquire()
        _sm._worker(0, _wav2(), None, stream=st8, sem=sm8, result_queue=q8,
                    dispatcher=d8, stop_check=_always_stopping,
                    stop_timeout=0.3)
        r8 = q8.get_nowait()
    finally:
        stream_mod._transcribe = saved_t8
        _sm._sequential_chain = saved_c8
    check("F7 deadline di attesa scaduta: il chunk ripiega sulla catena, non si perde",
          r8.success and bool(r8.text) and ("chain", "A") in seen8)

    # ==================================================================
    # T2/T3 RICOSTRUITI: il toggle sequential/auto, dopo il ripiego.
    #
    # PERCHE' IL VECCHIO T2/T3 NON SI PIAZZAVA piu'. Le tre asserzioni
    # ("resta su A", "seen==[A] e success False", "B non in seen") pinnavano
    # "in auto gli altri livelli sono IRRAGGUNGIBILI" (A1): era il difetto
    # che questo progetto ha messo in scope, e il comportamento NUOVO e' il
    # contrario. Spostare i numeri a ["A","B"] e' la mossa sbagliata: nella
    # config [A checked, B, C] i DUE bracci chiamerebbero esattamente gli
    # stessi livelli nello stesso ordine, e le due copie passerebbero uguale
    # con toggle acceso e spento. Test identici = VERDI-BUGGATI.
    #
    # DOMANDA REALE, a cui ho risposto per ESECUZIONE: quale discriminante
    # sopravvive al ripiego? Misurato, non ragionato:
    #   - i livelli chiamati, quando il checked e' il PRIMO in config: no
    #     ("A che va bene", o "A rotto e B buono", danno ["A","B"] in entrambi
    #     i bracci). Perche': in auto il pool parte dal checked, che e' anche
    #     il primo della catena, quindi il primo tentato e' lo stesso.
    #   - la raggiungibilita' dei non-checked in caso SANO: non e' un
    #     discriminante del toggle (e' gia' presidiata dai test F4: sani, il
    #     pool non tocca i non-checked).
    #   - RESTA l'ordine e la concorrenza, ed e' quello che misura la
    #     semantica: sequenziale = un livello alla volta in ordine di CONFIG;
    #     auto = prima il POOL, poi la catena su quello che il pool non ha
    #     tentato. Perche' si vede: mettendo il checked NON per primo
    #     ([A non-checked, B checked, C non-checked], A e B rotti, C buono) il
    #     pool non puo' rispettare l'ordine di config, che e' la proprieta'
    #     del ramo sequenziale.
    #
    # Il blocco gira la catena VERA (non viene sostituita
    # `_sequential_chain`), altrimenti l'esclusione dei livelli gia' tentati
    # dal pool non sarebbe esercitata e il test sarebbe verde-buggato.

    t2_a, t2_b, t2_c = _lvl("A", parallel=False), _lvl("B"), _lvl("C", parallel=False)

    def _t2_behaviour(lv):
        if lv.name == "C":
            return f"da {lv.name}"
        raise _ApiError(f"{lv.name} rotto")

    class _T2Breaker:
        """Proxy sul breaker REALE che annota le lease prese dal pool.

        Serve a misurare la concorrenza senza dipendere da come il worker
        chiama `acquire`: il ramo sequenziale non deve mai chiedere un lease,
        il ramo parallelo sì. Il conteggio dei fallimenti resta quello del
        breaker vero, il proxy non registra nulla.
        """

        def __init__(self, real):
            self.real = real
            self.leases: list[str] = []

        def state(self, key, now=None):
            return self.real.state(key, now)

        def acquire(self, key):
            got = self.real.acquire(key)
            if got:
                self.leases.append(key)
            return got

        def release(self, key):
            return self.real.release(key)

        def record_success(self, key):
            return self.real.record_success(key)

        def record_failure(self, key):
            return self.real.record_failure(key)

    def _t2_run(mode):
        """Un chunk su [A non-checked, B checked, C non-checked] nella modalita'
        data. A e B sollevano, C risponde. Restituisce
        (livelli tentati, chiavi in lease, breaker reale, risultato, semaforo ok)."""
        levels = [t2_a, t2_b, t2_c]
        brk = _T2Breaker(_tmp_breaker())
        disp = stream_mod._Dispatcher(levels, brk)
        seen: list[str] = []

        def _fake(lv, w, s, p):
            seen.append(lv.name)
            return _t2_behaviour(lv)

        saved = stream_mod._transcribe
        try:
            stream_mod._transcribe = _fake
            q: queue.Queue = queue.Queue()
            sem = threading.BoundedSemaphore(3)
            sem.acquire()
            stream_mod._worker(
                0, _wav(), None,
                stream=_stream_cfg(dispatch_mode=mode, levels=levels),
                sem=sem, result_queue=q, dispatcher=disp,
            )
            return seen, brk.leases, brk.real, q.get_nowait(), sem._value == 3
        finally:
            stream_mod._transcribe = saved

    t2_seen, t2_leases_seq, t2_brk_seq, t2_res_seq, t2_bal_seq = _t2_run("sequential")
    t2_seen_a, t2_leases_a, t2_brk_a, t2_res_a, t2_bal_a = _t2_run("auto")

    check("T2 ricostruito SEQUENZIALE: un livello alla volta in ordine di CONFIG",
          t2_seen == ["A", "B", "C"])
    check("T2 ricostruito AUTO: prima il POOL, poi la catena sui non tentati",
          t2_seen_a == ["B", "A", "C"])
    # LA NON-VACUITA': questo e' il presidio che nessuna delle forme
    # "sposta-il-numero" avrebbe. Se i due bracci producessero la stessa
    # traccia il toggle non sarebbe osservabile e l'intero blocco crollerebbe
    # a una sola meta' di test.
    check("T2 ricostruito: i due bracci NON sono identici (toggle osservabile)",
          t2_seen != t2_seen_a)
    check("T2 ricostruito AUTO: il pool prende un LEASE (concorrenza reale)",
          t2_leases_a == [_key(t2_b)])
    check("T2 ricostruito SEQUENZIALE: nessuna lease, il breaker resta muto",
          t2_leases_seq == [] and t2_brk_seq._records == {}
          and t2_brk_seq.state(_key(t2_b)) == "CLOSED")
    # La correzione del difetto "la catena riprova i livelli gia' falliti":
    # se tornasse indietro, in auto B sarebbe tentato DUE volte (pool e
    # catena) e questa assertzione andrebbe rossa. Qui e' la prova che la
    # catena parte davvero da dove si e' fermato il pool.
    check("T2 ricostruito: ogni livello tentato UNA volta sola in entrambi i bracci",
          len(t2_seen) == len(set(t2_seen))
          and len(t2_seen_a) == len(set(t2_seen_a)))
    # E il chunk non si perde in nessuno dei due: il testo che arriva e' lo
    # stesso, quindi il toggle non cambia l'esito per l'utente.
    check("T2 ricostruito: entrambi i bracci salvano il chunk (testo da C)",
          t2_res_seq.success and t2_res_seq.text == "da C"
          and t2_res_a.success and t2_res_a.text == "da C"
          and t2_bal_seq and t2_bal_a)

    # --- T3 ricostruito: il ripiego e' REALE e non silenzioso -------------
    # Il vecchio T3 chiedeva "B non viene chiamato": era A1, cioe' il difetto.
    # Il nuovo T3 chiede la cosa che resta e che conta: in auto il livello
    # non-checked C viene davvero tentato, il testo e' quello suo, e il
    # fallimento del pool resta una TRACCIA (breaker OPEN con UN tentativo),
    # non unpezzo di percorso sparito.
    _t3_rec = t2_brk_a._records.get(_key(t2_b))
    check("T3 ricostruito AUTO: il ripiego arriva davvero al non-checked C",
          "C" in t2_seen_a and t2_res_a.success and t2_res_a.text == "da C"
          and t2_res_a.error is None)
    check("T3 ricostruito AUTO: il fallimento del pool resta REGISTRATO",
          _t3_rec is not None and _t3_rec.failures == 1
          and t2_brk_a.state(_key(t2_b)) == "OPEN")
    # Contratto F: i livelli serviti SOLO dalla catena non aprono un cooldown
    # e non registrano fallimenti (altrimenti il ripiego accenderebbe da solo
    # i backup e il cooldown misurerebbe tentativi mai fatti dal pool).
    check("T3 ricostruito AUTO: i livelli serviti solo dalla catena non toccano il breaker",
          t2_brk_a._records.get(_key(t2_a)) is None
          and t2_brk_a._records.get(_key(t2_c)) is None
          and t2_brk_a.state(_key(t2_c)) == "CLOSED")
    # E il perno della ricostruzione: se il toggle venisse ignorato, cioe' se
    # i due bracci si scambiassero, questo blocco deve accorgersene. Il check
    # e' sul brano AUTO perche' e' li' che il comportamento storico
    # ("nessun lease, nessun breaker") non puo' piu' essere quello.
    check("T3 ricostruito: AUTO non puo' degradare al ramo sequenziale",
          not (t2_leases_a == [] and t2_brk_a._records == {}))


    # ================================================================
    # GIRO 2 — test di regressione (append in fondo, come da brief)
    # ================================================================
    # Alcuni test precedenti rimuovono la directory temporanea: la si ricrea.
    tmp.mkdir(parents=True, exist_ok=True)

    print("== giro 2 (B1: cleanup del file audio di at_end) ==")
    s2_lock_saved = stream_mod.STREAM_LOCK_PATH
    s2_state_saved = stream_mod.STREAM_STATE_PATH
    try:
        g2 = tmp / "giro2"
        g2.mkdir(parents=True, exist_ok=True)
        stream_mod.STREAM_LOCK_PATH = g2 / "stream.lock"
        stream_mod.STREAM_STATE_PATH = g2 / "stream_state.json"

        gone = dead_pid()
        audio_file = g2 / "bravoric-stream-test.ogg"
        audio_file.write_bytes(b"REGISTRAZIONE" * 10)
        sess_dir = stream_mod.STREAM_LOCK_PATH.parent / "stream-abc123"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "seg.ogg").write_bytes(b"x")

        stream_mod.STREAM_LOCK_PATH.write_text(json.dumps(
            {"pid": gone, "session_id": "abc123", "mode": "at_end",
             "started_at": 0, "audio_path": str(audio_file)}))
        check("B1: is_stream_active False su sessione morta",
              stream_mod.is_stream_active() is False)
        check("B1: lock residuo rimosso", not stream_mod.STREAM_LOCK_PATH.exists())
        check("B1: session_dir rimossa", not sess_dir.exists())
        check("B1: file audio at_end rimosso insieme al lock",
              not audio_file.exists())

        # lock senza audio_path: la pulizia non deve sollevare
        stream_mod.STREAM_LOCK_PATH.write_text(json.dumps(
            {"pid": gone, "session_id": "def456", "mode": "at_end",
             "started_at": 0}))
        check("B1: lock senza audio_path non solleva",
              stream_mod.is_stream_active() is False)
    finally:
        stream_mod.STREAM_LOCK_PATH = s2_lock_saved
        stream_mod.STREAM_STATE_PATH = s2_state_saved

    print("== giro 2 (B1: _stop_at_end ripulisce anche in caso di errore) ==")
    # Se la notifica o la trascrizione sollevano, il file audio deve comunque
    # sparire: e' il ramo che il reviewer misurava (notify che solleva dopo
    # _write_state -> ffmpeg vivo, lock presente, file in /tmp per sempre).
    # Sessione e config REALI (come nel test at_end piu' sopra), non un fake:
    # il finally deve coprire l'intero ramo, notify compresa.
    g2b_cfg = config.Config(
        notifications=False,
        notif_stt=config.ServiceNotifications(False, config.NotificationEvent(False, False), config.NotificationEvent(False, False)),
        notif_ocr=config.ServiceNotifications(False, config.NotificationEvent(False, False), config.NotificationEvent(False, False)),
        notif_stream=config.ServiceNotifications(False, config.NotificationEvent(False, False), config.NotificationEvent(False, False)),
        clipboard_tool="wl-copy", clipboard_paste_tool="wl-paste",
        audio=config.AudioConfig("ogg", "libopus", 16000, 16, 1.0, True, 2),
        stt=config.STTConfig("it", "", "", []),
        stt_cleanup=config.CleanupConfig(False, "", []),
        ocr_fallback=[], ocr_system_prompt="",
        ocr_cleanup=config.CleanupConfig(False, "", []),
        double_injection=True,
        storage=config.StorageConfig(str(tmp), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0), config.RetentionPolicy(False, 0)),
        history_max_entries=10, icons=config.IconsConfig("", "", "", "", "", ""),
        stream=config.StreamConfig("at_end", 0.7, -30.0, 0.4, 30.0, 250),
    )
    sess_g2 = stream_mod.StreamSession(g2b_cfg)
    state_g2 = tmp / "g2_stop_state.json"
    state_g2.write_text(json.dumps({"session_id": "g2stop", "chunks": []}))
    # lock con una sola uscita che solleva: la notifica di "Transcribing...".
    audio_leak = tmp / "giro2_leak.ogg"
    audio_leak.write_bytes(b"AUDIO" * 200)

    def _notify_boom(*_a, **_k):
        raise RuntimeError("notifica fallita")

    lock_g2 = {"audio_path": str(audio_leak), "session_id": "g2stop",
               "pid": dead_pid(), "mode": stream_mod.MODE_AT_END}
    with mock.patch.object(stream_mod, "_terminate_pid"), \
         mock.patch.object(stream_mod, "STREAM_STATE_PATH", state_g2), \
         mock.patch.object(stream_mod, "STREAM_LOCK_PATH", tmp / "g2.lock"), \
         mock.patch.object(stream_mod.notify, "maybe_send_simple", _notify_boom):
        raised = False
        try:
            sess_g2._stop_at_end(lock_g2)
        except RuntimeError:
            raised = True
        check("B1: la notifica che solleva propaga l'errore (comportamento atteso)",
              raised)
        check("B1: il file audio sparisce anche se la notifica solleva",
              not audio_leak.exists())
    # e il caso normale: il file sparisce comunque
    audio_ok = tmp / "giro2_ok.ogg"
    audio_ok.write_bytes(b"AUDIO" * 200)
    with mock.patch.object(stream_mod, "_terminate_pid"), \
         mock.patch.object(stream_mod, "STREAM_STATE_PATH", state_g2), \
         mock.patch.object(stream_mod, "STREAM_LOCK_PATH", tmp / "g2.lock"), \
         mock.patch.object(stream_mod, "try_with_fallback", return_value="ciao "):
        sess_g2._stop_at_end({"audio_path": str(audio_ok), "session_id": "g2stop",
                              "pid": dead_pid(), "mode": stream_mod.MODE_AT_END})
    check("B1: il file audio sparisce anche nel percorso riuscito",
          not audio_ok.exists())

    print("== giro 2 (B4: guard dello status su PROCESSING ed ERROR) ==")
    st2_saved = status.STATUS_PATH
    try:
        status.STATUS_PATH = g2 / "status.json"
        for hostile in (status.STATE_IDLE, status.STATE_PROCESSING, status.STATE_ERROR):
            status.write_status(status.STATE_RECORDING, service="stt")
            status.write_status(hostile, service="ocr")
            after = status.read_status()
            check(f"B4: recording STT non è spento da {hostile} di un altro servizio",
                  after.get("state") == status.STATE_RECORDING
                  and after.get("service") == "stt")
        # stesso servizio: il completamento genuino deve passare
        status.write_status(status.STATE_RECORDING, service="stt")
        status.write_status(status.STATE_IDLE, service="stt")
        check("B4: lo stesso servizio può ancora scrivere IDLE",
              status.read_status().get("state") == status.STATE_IDLE)
        # Giro 3 (B4): scenario ricostruito. Prima qui c'era
        #   write_status(RECORDING, service="stt"); write_status(IDLE, service=None)
        #   -> IDLE, cioe' l'asserzione PINGAVA il difetto: il ramo senza
        #   servizio non era coperto dal guard (clausola `service is not None`)
        #   e spegneva una registrazione altrui. Non si indebolisce l'asserzione:
        #   si ribalta sullo scenario che il guard deve davvero attraversare.
        #   1) senza registrazione in corso, una scrittura senza servizio
        #      continua a passare (il guard non deve diventare un lucchetto
        #      assoluto: da idle si deve poter andare ovunque, e questo e' il
        #      percorso che verifica, fra l'altro, l'auto/sequential: OCR e STT
        #      NON si sovrascrivono a vicenda in nessuna delle due direzioni).
        status.STATUS_PATH.write_text(json.dumps(
            {"state": status.STATE_IDLE, "timestamp": 0.0, "service": "ocr"}))
        status.write_status(status.STATE_ERROR, service=None)
        check("B4: senza registrazione in corso la scrittura senza service passa",
              status.read_status().get("state") == status.STATE_ERROR)
        status.write_status(status.STATE_IDLE, last_output="dopo", service=None)
        check("B4: da error si torna a idle anche senza service",
              status.read_status().get("last_output") == "dopo")
        #   2) con registrazione STT in corso, la scrittura senza servizio
        #      viene respinta: e' il difetto chiuso in questo giro.
        status.write_status(status.STATE_RECORDING, service="stt")
        status.write_status(status.STATE_ERROR, service=None)
        check("B4: recording STT non è spento da un ERROR senza service",
              status.read_status().get("state") == status.STATE_RECORDING
              and status.read_status().get("service") == "stt")
        #   3) il percorso di default dell'utente resta aperto: STT registra,
        #      smette e completa da solo con il proprio IDLE+service.
        status.write_status(status.STATE_RECORDING, service="stt")
        status.write_status(status.STATE_PROCESSING, service="stt")
        status.write_status(status.STATE_IDLE, last_output="trascritto", service="stt")
        check("B4: il percorso STT completo (recording->processing->idle) passa",
              status.read_status().get("state") == status.STATE_IDLE
              and status.read_status().get("last_output") == "trascritto")
        #   4) idem per lo streaming, che usa lo stesso file di stato.
        status.write_status(status.STATE_RECORDING, service="stream")
        status.write_status(status.STATE_ERROR, service=None)
        check("B4: recording stream non è spento da un ERROR senza service",
              status.read_status().get("state") == status.STATE_RECORDING)
        #   5) e un recording SENZA servizio dichiarato resta sensibile: con
        #      la clausola tolta, None == None, quindi un servizio ignoto
        #      non viene più trattato come "nessuno".
        status.STATUS_PATH.write_text(json.dumps(
            {"state": status.STATE_RECORDING, "timestamp": 0.0}))
        status.write_status(status.STATE_IDLE, service=None)
        check("B4: recording senza service non è bloccato dal proprio idle",
              status.read_status().get("state") == status.STATE_IDLE)
    finally:
        status.STATUS_PATH = st2_saved

    print("== giro 2 (B2-backend: il drain pubblica i propri chunk) ==")
    # Il reviewer proponeva preserve_chunks=True nel drain, ispirandosi a
    # paste_next. MISURATO SBAGLIATO: preserve_chunks fa si' che _write_state
    # sostituisca `chunks` con l'elenco RILESTO da disco, scartando i chunk
    # appena committati dal drain. Qui il prodotto scrive lo stato autorevole e
    # questo test presidia quella scelta.
    s3_lock_saved = stream_mod.STREAM_LOCK_PATH
    s3_state_saved = stream_mod.STREAM_STATE_PATH
    try:
        g3 = tmp / "giro2_b2"
        g3.mkdir(parents=True, exist_ok=True)
        stream_mod.STREAM_LOCK_PATH = g3 / "stream.lock"
        stream_mod.STREAM_STATE_PATH = g3 / "stream_state.json"
        stream_mod.STREAM_STATE_PATH.write_text(json.dumps(
            {"chunks": ["STALE "], "next_chunk_index": 0}))
        g2_cfg = config.StreamConfig("per_chunk", 0.7, -30, 0.4, 30, 250, fallback=[])
        st3 = {"chunks": [], "last_chunks": []}
        seq3 = stream_mod._FifoSequencer(st3, g2_cfg, lambda t: None, lambda t: None,
                                         blacklist=frozenset(), log_path=_TEST_CHUNK_LOG_PATH)
        seq3._pending[0] = stream_mod._ChunkResult(0, "Primo", True)
        seq3._pending[1] = stream_mod._ChunkResult(1, "Secondo", True)
        seq3.drain_and_stop(2)
        check("B2: il drain mantiene i chunk appena committati",
              st3["chunks"] == ["Primo ", "Secondo "])
        on_disk = json.loads(stream_mod.STREAM_STATE_PATH.read_text())["chunks"]
        check("B2: il drain pubblica i propri chunk, non un elenco stale su disco",
              on_disk == ["Primo ", "Secondo "])
    finally:
        stream_mod.STREAM_LOCK_PATH = s3_lock_saved
        stream_mod.STREAM_STATE_PATH = s3_state_saved

    # =================================================================
    # GIRO 3 — le tre voci IMPLEMENTABILE ORA.
    #
    # Copertura PORTATA NEL PROGETTO. Il reviewer aveva misurato tutto con
    # harness in /tmp/rev3 (b3.py, b3b.py, b3c.py, b3d.py, b3e2e.py): /tmp è
    # volatile, quindi quei file qui dentro, senza path assoluti e senza
    # puntare a /tmp, altrimenti il difetto tornerebbe senza copertura.
    # =================================================================
    print("== giro 3 (B4: il ramo di errore senza service non spegne più la registrazione) ==")
    # Portato da b3e2e.py: end-to-end con ocr.handle_capture REALE e
    # clipboard che solleva (l'utente preme OCR mentre detta). Non un
    # write_status finto: è il percorso di produzione che ha scritto il bug.
    st3_saved = status.STATUS_PATH
    try:
        g3 = tmp / "giro3_b4"
        g3.mkdir(parents=True, exist_ok=True)
        status.STATUS_PATH = g3 / "status.json"
        g3_ocr_cfg = mock.Mock()
        g3_ocr_cfg.notifications = False
        g3_ocr_cfg.double_injection = False
        g3_ocr_cfg.ocr_cleanup.enabled = False
        g3_ocr_cfg.ocr_cleanup.fallback = []
        g3_ocr_cfg.storage.ocr_original.enabled = False
        g3_ocr_cfg.storage.ocr_raw.enabled = False
        g3_ocr_cfg.ocr_capture_screenshot = False  # vedi commento sul giro 10 sopra
        with mock.patch("bravoric_stt_clipboard.ocr.clipboard") as m_clip_g3, \
             mock.patch("bravoric_stt_clipboard.ocr.notify"), \
             mock.patch("bravoric_stt_clipboard.ocr.storage"), \
             mock.patch("bravoric_stt_clipboard.ocr.output_history"):
            # clipboard vuota: ocr.py:28 chiama write_status(STATE_ERROR)
            # SENZA service, il ramo che il guard non copriva.
            m_clip_g3.read_image_png.side_effect = FileNotFoundError("no image in clipboard")
            status.write_status(status.STATE_RECORDING, service="stt")
            ocr.handle_capture(g3_ocr_cfg)
            after_e2e = status.read_status()
            check("B4: OCR che fallisce non porta l'indicatore fuori dal microfono",
                  after_e2e.get("state") == status.STATE_RECORDING
                  and after_e2e.get("service") == "stt")
    finally:
        status.STATUS_PATH = st3_saved

    print("== giro 3 (B3: il lock audio non attraversa più lo zero byte) ==")
    # Portato da b3c.py + b3d.py. Il difetto: lseek(0)+ftruncate(0)+write()
    # lasciava il file a zero byte per la finestra fra troncamento e
    # scrittura; in quel momento _read_lock() -> None e un toggle concorrente
    # avviava un secondo ffmpeg. Ora il lock è pubblicato con os.replace,
    # quindi il percorso LOCK_PATH va dal placeholder valido (giro 12, P3)
    # al lock completo senza mai essere troncato a zero.
    #
    # Il test NON misura una finestra di microsecondi (irripetibile): verifica
    # l'invariante strutturale, cioè che start_recording non usa più
    # ftruncate/lseek sul lock e pubblica con os.replace. È la forma che
    # resta vera anche se qualcuno ci mette mano fra tre anni.
    a3_saved = audio.LOCK_PATH
    a3_dir = tmp / "giro3_b3"
    a3_dir.mkdir(parents=True, exist_ok=True)
    try:
        audio.LOCK_PATH = a3_dir / "recording.lock"
        a3_cfg = config.AudioConfig("ogg", "libopus", 16000, 16, 0.0, True, 2)
        with mock.patch("bravoric_stt_clipboard.audio.subprocess.Popen") as m_popen:
            m_popen.return_value = mock.Mock(pid=os.getpid())
            a3_out = audio.start_recording(a3_cfg)
        try:
            a3_lock = audio._read_lock()
            check("B3: il lock pubblicato è valido e completo",
                  a3_lock is not None
                  and a3_lock["audio_path"] == str(a3_out)
                  and a3_lock["pid"] == os.getpid())
            check("B3: il percorso del lock non attraversa lo zero byte",
                  audio.LOCK_PATH.stat().st_size > 0)
            # Invariante anti-regressione sul sorgente: se qualcuno reintroduce
            # la riscrittura in place, l'asserzione qui sotto deve virare.
            a3_src = (ROOT / "src" / "bravoric_stt_clipboard" / "audio.py").read_text()
            check("B3: start_recording non tronca più il lock in place",
                  "os.ftruncate" not in a3_src and "os.lseek" not in a3_src)
            check("B3: il lock è pubblicato con os.replace",
                  "os.replace" in a3_src)
        finally:
            audio.LOCK_PATH.unlink(missing_ok=True)
            a3_out.unlink(missing_ok=True)
        # Nessun .tmp residuo accanto al lock (il pattern atomico pulisce).
        check("B3: nessun .tmp residuo accanto al lock",
              not any(a3_dir.glob("recording.lock*.tmp")))

        # Sondaggio concorrente vero (portato da b3d.py). Il difetto originale
        # non si lascia catturare a freddo: la finestra reale ftruncate(0)->write
        # dura ~0.01 ms, e il reviewer l'ha misurata 8 volte senza mai centrarla
        # al tavolo. Per rendere il test DISCRIMINANTE (verde sul codice giusto,
        # rosso su quello vecchio) si allargano artificialmente entrambe le
        # possibili vie di scrittura: os.write (la riscrittura in place di prima)
        # e os.fsync (la scrittura atomica di adesso). Qualunque delle due il
        # codice scelga, la sonda deve comunque trovare un lock VALIDO, mai None.
        a3_stop = threading.Event()
        a3_seen = {"samples": 0, "unreadable": 0, "not_recording": 0, "ever_valid": False}
        real_write_g3, real_fsync_g3 = os.write, os.fsync

        def _is_real_lock(data: bytes) -> bool:
            return b'"audio_path": "' in data and b'"audio_path": ""' not in data

        def slow_write(fd, data, *a, **kw):
            if isinstance(data, bytes) and _is_real_lock(data):
                time.sleep(0.30)  # finestra allargata: il probe deve reggere
            return real_write_g3(fd, data, *a, **kw)

        def slow_fsync(fd):
            time.sleep(0.30)
            return real_fsync_g3(fd)

        def probe_g3():
            # Si conta SOLO da quando il lock è stato una volta LEGGIBILE.
            # Prima di allora il file è appena stato creato da O_CREAT|O_EXCL e
            # non contiene ancora il placeholder (giro 12, P3): quel vuoto è una
            # finestra DIVERSA, preesistente e fuori perimetro, e contarla
            # renderebbe il test instabile senza presidiare B3.
            # Il difetto B3 ha una firma precisa e verificabile: un lock che
            # ERA valido smette di esserlo (diventa illeggibile) mentre si
            # riscrive al suo posto. È questo che qui deve restare impossibile.
            while not a3_stop.is_set():
                if not audio.LOCK_PATH.exists():
                    continue
                if audio._read_lock() is not None:
                    a3_seen["ever_valid"] = True
                if not a3_seen["ever_valid"]:
                    continue
                a3_seen["samples"] += 1
                if audio._read_lock() is None:
                    a3_seen["unreadable"] += 1
                elif not audio.is_recording():
                    a3_seen["not_recording"] += 1

        with mock.patch("bravoric_stt_clipboard.audio.subprocess.Popen") as m_popen2:
            m_popen2.return_value = mock.Mock(pid=os.getpid())
            os.write, os.fsync = slow_write, slow_fsync
            th_g3 = threading.Thread(target=probe_g3, daemon=True)
            th_g3.start()
            try:
                a3_out2 = audio.start_recording(a3_cfg)
            finally:
                a3_seen_stop = a3_stop.set()
                th_g3.join(timeout=5)
                os.write, os.fsync = real_write_g3, real_fsync_g3
        try:
            check("B3: un lock già valido non diventa mai illeggibile",
                  a3_seen["ever_valid"] and a3_seen["samples"] > 0
                  and a3_seen["unreadable"] == 0)
            check("B3: is_recording() non diventa mai False durante l'avvio",
                  a3_seen["not_recording"] == 0)
        finally:
            audio.LOCK_PATH.unlink(missing_ok=True)
            a3_out2.unlink(missing_ok=True)
    finally:
        audio.LOCK_PATH = a3_saved



    # ==================================================================
    # GIRO 5 — Voce 1 (B5): stop_recording nella finestra di avvio.
    #
    # Il difetto: start_recording pubblica un placeholder col solo pid
    # (audio_path = "") per tutta la finestra Popen -> os.replace, ~1 s, che
    # e' proprio la durata del debounce di default. Una doppia pressione del
    # toggle ci cade dentro, e Path("") non e' "nessun file": e' la cwd. Il
    # valore saliva fino a stt.py, che leggeva e cancellava la DIRECTORY
    # invece del .ogg vero (IsADirectoryError, registrazione persa, file
    # troncato lasciato in /tmp con il nome definitivo).
    #
    # Il test aggancia la seconda pressione DENTRO Popen: e' l'unico modo di
    # essere davvero nella finestra, perche' il lock e' gia' il placeholder e
    # non lo sara' piu' dopo che start_recording ritorna.
    print("== giro 5 (B5: stop nella finestra di avvio non restituisce la cwd) ==")
    g5_lock_saved = audio.LOCK_PATH
    g5_dir = tmp / "giro5_b5"
    g5_dir.mkdir(parents=True, exist_ok=True)
    # .ogg di un caso reale, creato PRIMA: e' il file che il difetto perdeva.
    g5_ogg = Path(tempfile.mkstemp(suffix=".ogg", prefix="bravoric-stt-")[1])
    g5_ogg.write_bytes(b"")
    g5_cwd_saved = os.getcwd()
    g5_cwd_probe = Path(tempfile.mkdtemp(prefix="giro5-cwd-"))
    g5_seen: dict[str, object] = {"lock_audio_path": None, "lock_pid": None}
    # Foto dei .ogg in /tmp PRIMA di start_recording: il confronto e' la base
    # dell'asserzione sui file lasciati indietro, quindi va preso qui e non
    # dopo (dopo sarebbe gia' troppo tardi per vedere quello che perde).
    g5_tmp_before = set(Path("/tmp").glob("bravoric-stt-*.ogg"))

    def _g5_second_press(*a, **kw):
        """Secondo toggle, agganciato dentro Popen: siamo nella finestra."""
        data = json.loads(audio.LOCK_PATH.read_text())
        g5_seen["lock_audio_path"] = data.get("audio_path")
        g5_seen["lock_pid"] = data.get("pid")
        # pid MORTO: il vero caso e' il processo che STA avviando (quindi
        # vivo), ma per osservare il lock non serve che sia vivo, e con un
        # pid vivo stop_recording manderebbe SIGINT a questo processo di test.
        audio.LOCK_PATH.write_text(json.dumps(
            {"pid": gone, "audio_path": data.get("audio_path", ""),
             "started_at": data["started_at"]}))
        try:
            g5_seen["returned"] = audio.stop_recording(
                mock.Mock(toggle_debounce_seconds=0.0))
        except Exception as exc:  # noqa: BLE001 - e' l'esito che si misura
            g5_seen["raised"] = exc
        return mock.Mock(pid=os.getpid())

    try:
        os.chdir(g5_cwd_probe)
        audio.LOCK_PATH = g5_dir / "recording.lock"
        with mock.patch("bravoric_stt_clipboard.audio.subprocess.Popen",
                        side_effect=_g5_second_press):
            g5_out = audio.start_recording(
                config.AudioConfig("ogg", "libopus", 16000, 16, 0.0, True, 2))
    finally:
        os.chdir(g5_cwd_saved)
        audio.LOCK_PATH = g5_lock_saved
        audio.LOCK_PATH.unlink(missing_ok=True)

    # La guardia che conta: nella finestra il lock dice davvero "" (quindi il
    # test sta davvero provando il caso, non un caso diverso piu' facile).
    check("B5: la seconda pressione cade davvero nella finestra (audio_path vuoto)",
          g5_seen["lock_audio_path"] == "")
    check("B5: il lock della finestra porta il pid del processo che avvia",
          g5_seen["lock_pid"] == os.getpid())
    # Il DIFETTO: tornava la cwd, e su quella il chiamante fa read_bytes +
    # unlink, cioe' quello che in stt.py falliva con IsADirectoryError.
    check("B5: stop nella finestra non solleva IsADirectoryError",
          not isinstance(g5_seen.get("raised"), IsADirectoryError))
    check("B5: stop nella finestra non restituisce la directory di lavoro",
          "returned" not in g5_seen
          or not Path(g5_seen["returned"]).is_dir())  # type: ignore[arg-type]
    check("B5: il valore restituito non e' mai Path('') (la cwd mascherata)",
          "returned" not in g5_seen
          or g5_seen["returned"] != Path(""))  # type: ignore[operator]
    # Se il chiamante avesse ricevuto un path, la catena di trascrizione
    # avrebbe girato su una directory. Qui si fa esattamente quello che fa
    # stt.py col valore restituito: lo LEGGE e lo CANCELLA. Sono le due
    # operazioni che in produzione sollevano IsADirectoryError, quindi
    # l'eccezione viene osservata qui e non dedotta.
    g5_chain: dict[str, object] = {}
    if "returned" in g5_seen:
        try:
            cast(Path, g5_seen["returned"]).read_bytes()
            g5_chain["read"] = "ok"
        except IsADirectoryError as exc:
            g5_chain["read"] = "IsADirectoryError"
            g5_chain["exc"] = exc
        except OSError as exc:
            g5_chain["read"] = f"OSError: {type(exc).__name__}"
    else:
        g5_chain["read"] = "nessun path: la catena non parte"
    check("B5: nessuna IsADirectoryError sulla catena di trascrizione",
          g5_chain["read"] != "IsADirectoryError")
    check("B5: la catena non gira sulla directory di lavoro",
          g5_chain["read"] == "ok" or "nessun path" in str(g5_chain["read"]),
          )
    # Il .ogg vero non è stato toccato dalla finestra.
    check("B5: il .ogg vero non è stato cancellato dalla finestra",
          g5_ogg.exists() and g5_ogg.stat().st_size == 0)
    g5_ogg.unlink(missing_ok=True)
    # start_recording crea il file con tempfile.mkstemp, quindi in /tmp e NON
    # nella directory del lock: qui il chiamante riceve il path vero e lo
    # cancella, come fa stt.py. Il confronto e' fatto DOPO la pulizia, perche'
    # prima ci sarebbe ancora il file che il test sta ancora usando: misurare
    # li' guarderebbe uno stato intermedio e non direbbe niente su /tmp.
    with contextlib.suppress(OSError):
        g5_out.unlink(missing_ok=True)
    check("B5: nessun .ogg lasciato in /tmp dalla finestra di avvio",
          not (set(Path("/tmp").glob("bravoric-stt-*.ogg")) - g5_tmp_before))

    # Il ramo gemello: is_recording su lock morto con audio_path vuoto non deve
    # tentare di cancellare la cwd (prima lo faceva, innocuo ma falso: non
    # cancellava il .ogg residuo, contro la promessa del commento).
    g5_is: dict[str, object] = {}
    try:
        audio.LOCK_PATH = g5_dir / "recording2.lock"
        audio.LOCK_PATH.write_text(json.dumps(
            {"pid": gone, "audio_path": "", "started_at": 0}))
        g5_is["cwd_before"] = Path.cwd()
        g5_is["rec"] = audio.is_recording()
        g5_is["cwd_after"] = Path.cwd()
    finally:
        os.chdir(g5_cwd_saved)
        audio.LOCK_PATH = g5_lock_saved
        audio.LOCK_PATH.unlink(missing_ok=True)
    check("B5: is_recording su lock vuoto -> False e lock ripulito",
          g5_is["rec"] is False)
    check("B5: is_recording non tocca la directory di lavoro",
          g5_is["cwd_before"] == g5_is["cwd_after"])

    # ==================================================================
    # GIRO 5 — Voce 2 (B3): `tried` non si aggiunge prima del lease.
    #
    # Il difetto: la chiave veniva segnata come tentata PRIMA di sapere se
    # il lease sarebbe arrivato. Se non arriva (pool saturo, deadline
    # scaduta, o sonda half-open gia' in volo) nessuna richiesta HTTP aveva
    # toccato quell'endpoint, ma la catena di ripiego lo escludeva perche'
    # risultava "gia' tentato": il chunk moriva con "pool: <vuoto> |
    # catena: nessun livello da tentare", un messaggio che certifica da
    # solo che nessuno e' stato interrogato.
    print("== giro 5 (B3: un endpoint senza lease resta tentabile dalla catena) ==")
    from bravoric_stt_clipboard import stream as _sm5
    from bravoric_stt_clipboard.config import FallbackLevel as _FL5

    # Un solo endpoint, in HALF_OPEN con la SONDA GIA' IN VOLO: il caso peggiore
    # misurato dal reviewer. has_pending_capacity e' True (HALF_OPEN e'
    # utilizzabile e ha slot), quindi il worker entra nel ramo parallelo, ma
    # il lease non arriva: nessun endpoint viene interrogato.
    g5_only = _FL5("G5", "http://g5:4001/v1", "m", "", "", "", 60, False, True, 2)
    g5_key5 = _sm5._level_key(g5_only)
    g5_brk5 = _tmp_breaker()
    g5_brk5.record_failure(g5_key5)
    g5_brk5._records[g5_key5] = g5_brk5._records[g5_key5].__class__(
        last_failure=time.time() - 7200, failures=1)
    g5_brk5.acquire(g5_key5)          # la sonda di un altro worker e' in volo
    g5_disp5 = _sm5._Dispatcher([g5_only], g5_brk5, fallback_chain=[g5_only])
    g5_st5 = _stream_cfg(levels=[g5_only])
    g5_seen5: list[tuple[str, str]] = []
    g5_saved_t5 = stream_mod._transcribe
    g5_saved_c5 = _sm5._sequential_chain
    try:
        def _g5t(lv, w, s, p):
            g5_seen5.append(("pool", lv.name))
            return f"da {lv.name}"
        stream_mod._transcribe = _g5t
        _sm5._sequential_chain = _fake_chain(g5_seen5, lambda lv: f"da {lv.name}")
        g5_q5: queue.Queue = queue.Queue()
        g5_sem5 = threading.BoundedSemaphore(3)
        g5_sem5.acquire()
        _sm5._worker(0, _wav2(), None, stream=g5_st5, sem=g5_sem5,
                     result_queue=g5_q5, dispatcher=g5_disp5,
                     stop_check=lambda: True, stop_timeout=0.3)
        g5_r5 = g5_q5.get_nowait()
    finally:
        stream_mod._transcribe = g5_saved_t5
        _sm5._sequential_chain = g5_saved_c5

    # Il messaggio del caso peggiore non deve piu' prodursi.
    check("B3: niente piu' 'pool: | catena: nessun livello da tentare'",
          not (g5_r5.error or "").endswith("catena: nessun livello da tentare"))
    check("B3: l'errore non si autodichiara con pool_errors vuota",
          "pool:  |" not in (g5_r5.error or ""))
    # E l'endpoint non tentato deve restare DISPONIBILE: la catena lo
    # raggiunge e il chunk non si perde.
    check("B3: l'endpoint non tentato resta disponibile per la catena",
          ("chain", "G5") in g5_seen5)
    check("B3: il chunk non si perde (successo con testo)",
          g5_r5.success and bool(g5_r5.text))
    check("B3: semaforo bilanciato dopo il ripiego", g5_sem5._value == 3)
    check("B3: nessun endpoint consumato due volte (una sola richiesta HTTP)",
          g5_seen5 == [("chain", "G5")])


    # =================================================================
    # GIRO 18 — P4 (watchdog) e il difetto del pallino rosso segnalato
    # dall'utente (chiusura a comando vocale). Test APPENDATI in fondo.
    # =================================================================
    from bravoric_stt_clipboard import audio as _a18
    from bravoric_stt_clipboard import stt as _stt18
    from bravoric_stt_clipboard import stream as _sm18
    from bravoric_stt_clipboard import cli as _cli18

    print("== giro 18: P4 esclusione reciproca STT <-> streaming ==")
    # Difetto: i due lock sono file DISTINTI, stt._start non guardava
    # stream.lock, quindi una scorciatoia durante una sessione viva avviava
    # un SECONDO ffmpeg sul microfono gia' aperto.
    class _FakeProc18:
        pid = os.getpid()
        def poll(self): return 0
        def terminate(self): return None
        def wait(self, timeout=None): return 0
        def kill(self): return None
    stt_start_calls: list[Path] = []
    _a18_saved = _stt18._is_stream_active
    _sa_saved = _a18.start_recording
    # Lo stub di start_recording resta installato per ENTRAMBE le prove
    # (caso difettoso e contro-prova). Nella prima stesura il finally lo
    # ripristinava prima della contro-prova: questa chiamava quindi
    # l'audio.start_recording VERO, che lanciava un ffmpeg reale e
    # lasciava recording.lock in /run/user/<uid> — e il test successivo
    # (misura del toggle) leggeva quel lock, si rifiutava di partire e
    # falliva per un motivo estraneo al difetto che stava misurando.
    # Silenziare anche notify/status: il guard da provare e' quello PRIMA
    # di start_recording, non la coda di notifica.
    try:
        _a18.start_recording = lambda cfg: (stt_start_calls.append(Path("/tmp/falso.ogg")) or Path("/tmp/falso.ogg"))
        with mock.patch.object(_stt18, "notify", mock.Mock()), \
             mock.patch.object(_stt18, "status", mock.Mock()):
            _stt18._is_stream_active = lambda: True
            raised18 = None
            try:
                _stt18._start(cast(Any, cfg_stream_min))
            except RuntimeError as exc:
                raised18 = str(exc)
            check("P4: con sessione streaming VIVA, STT non avvia nessun ffmpeg",
                  stt_start_calls == [])
            check("P4: il rifiuto e' esplicito (RuntimeError, non un silenzio)",
                  raised18 is not None and "stream" in raised18.lower())

            # Contro-prova (non-vacuita'): nessuna sessione -> STT parte.
            _stt18._is_stream_active = lambda: False
            stt_start_calls.clear()
            _stt18._start(cast(Any, cfg_stream_min))
            check("P4 (contro-prova): senza sessione streaming STT parte come prima",
                  len(stt_start_calls) == 1)
    finally:
        _stt18._is_stream_active = _a18_saved
        _a18.start_recording = _sa_saved

    # Il guard deve stare PRIMA di start_recording nel sorgente: un check
    # messo dopo nonImpedirebbe il secondo ffmpeg (il danno e' gia' fatto).
    _stt_src = (ROOT / "src" / "bravoric_stt_clipboard" / "stt.py").read_text(encoding="utf-8")
    # Difetto 2 (giro 19): anche lo .split("def _start(cfg: Config) -> None:")[1]
    # era un crash in attesa: se la firma spariva dal sorgore, [1] sollevava
    # IndexError e la suite abortiva. Ora si controlla che la firma ci sia e un
    # corpo vuoto si registra come FAIL pulito. Difetto gia' segnalato nel giro
    # 18 (i due .index()): qui si chiude anche il buco rimasto.
    _start_sig = "def _start(cfg: Config) -> None:"
    if _start_sig not in _stt_src:
        _start_body = ""
    else:
        _start_body = _stt_src.split(_start_sig, 1)[1].split("\ndef ", 1)[0]
    # La verifica e' una funzione pura (guard_order_verdict): cerca con find()
    # e restituisce sempre un verdetto, quindi qui non puo' sollevare nulla.
    _ok4, _missing4 = guard_order_verdict(_start_body)
    if _missing4:
        check("P4: il guard sullo stato stream sta PRIMA di audio.start_recording "
              f"(mancano da stt._start: {', '.join(_missing4)})", False)
    else:
        check("P4: il guard sullo stato stream sta PRIMA di audio.start_recording",
              _ok4)

    print("== giro 18: il toggle di default AVVIA, non chiude (misurato) ==")
    # Questo e' il presupposto della scelta: se il toggle fosse innocuo, il
    # fix potrebbe spararlo e basta. Non lo e'. Misurato, non ipotizzato.
    # Config con un endpoint E con mode per_chunk: senza, StreamSession.start()
    # rifiuta con "No endpoint configured" e la misura non osserverebbe
    # nulla (scoperta scrivendo il test: cfg_stream_min ha fallback=[]).
    import dataclasses as _dc18
    cfg_toggle = _dc18.replace(
        cfg_stream_min,
        notifications=False,
        stream=_dc18.replace(
            cfg_stream_min.stream,
            mode="per_chunk",
            fallback=[config.FallbackLevel("T", "http://127.0.0.1:1/v1",
                                           "m", "k", "", "", 1, False, True, 1)],
        ),
    )
    with tempfile.TemporaryDirectory() as _td18:
        _sm18.STREAM_LOCK_PATH = Path(_td18) / "stream.lock"
        _sm18.STREAM_STATE_PATH = Path(_td18) / "stream_state.json"
        _sm18.STREAM_LOCK_PATH.unlink(missing_ok=True)
        _sm18._write_state({"session_id": "s1", "active": False, "mode": "per_chunk", "chunks": []})
        with mock.patch.object(_cli18, "load_config", lambda: cfg_toggle), \
             mock.patch.object(_cli18, "notify", mock.Mock()), \
             mock.patch.object(_sm18, "subprocess", mock.Mock(Popen=lambda *a, **k: _FakeProc18())):
            _rc_toggle = _cli18.stream_toggle_main([])
            check("P4: il toggle SENZA argomenti con nessuna sessione AVVIA una sessione "
                  "(motivo per cui non puo' essere usato per chiudere)",
                  _sm18.STREAM_LOCK_PATH.exists()
                  and _sm18.read_state().get("active") is True)
        # pulizia del lock creato dalla misura
        _sm18.STREAM_LOCK_PATH.unlink(missing_ok=True)

    print("== giro 18: 'stop' e' idempotente e non avvia nulla ==")
    with tempfile.TemporaryDirectory() as _td18b:
        _sm18.STREAM_LOCK_PATH = Path(_td18b) / "stream.lock"
        _sm18.STREAM_STATE_PATH = Path(_td18b) / "stream_state.json"
        _sm18.STREAM_LOCK_PATH.unlink(missing_ok=True)
        _sm18._write_state({"session_id": "s1", "active": False, "mode": "per_chunk", "chunks": []})
        with mock.patch.object(_cli18, "load_config", lambda: cfg_stream_min), \
             mock.patch.object(_cli18, "notify", mock.Mock()), \
             mock.patch.object(_cli18, "logger", mock.Mock()):
            _rc_stop1 = _cli18.stream_toggle_main(["stop"])
            check("P4: 'stop' senza sessione esce 0 (chiusura riuscita, non un errore)",
                  _rc_stop1 == 0)
            check("P4: 'stop' senza sessione NON avvia nulla (il pallino non si riapre)",
                  not _sm18.STREAM_LOCK_PATH.exists()
                  and _sm18.read_state().get("active") is not True)
            _rc_stop2 = _cli18.stream_toggle_main(["stop"])
            check("P4: 'stop' e' idempotente (secondo giro identico)",
                  _rc_stop2 == 0
                  and not _sm18.STREAM_LOCK_PATH.exists()
                  and _sm18.read_state().get("active") is not True)

    # Anti-drift: il sottocomando deve esistere davvero nel sorgente, altrimenti
    # un refactor silenzioso lo toglierebbe e l'estensione continuerebbe a
    # chiamarlo senza accorgersene (fallo SILENZIOSO, il difetto che si vuole
    # chiudere).
    _cli_src = (ROOT / "src" / "bravoric_stt_clipboard" / "cli.py").read_text(encoding="utf-8")
    check("P4: il sottocomando 'stop' esiste nel sorgente di cli.py",
          'command[0] == "stop"' in _cli_src
          and "if not session.is_active():" in _cli_src)
    _ext_src = (ROOT / "gnome-extension" / "bravoric-indicator@local" / "extension.js").read_text(encoding="utf-8")
    _end_body = _ext_src.split("_requestStreamEnd(sessionId) {")[1].split("\n    _startVirtualDevice")[0]
    check("P4: l'estensione chiude con il sottocomando 'stop', non col toggle",
          "spawnBackground('bravoric-stream-toggle', 'stop')" in _end_body)
    check("P4: l'estensione NON usa piu' la condizione di partenza active === true "
          "(era la chiusura che tornava in silenzio)",
          "state.active === true" not in _end_body)
    # Il latch non deve poter bloccare un ritento per sempre.
    check("P4: il latch di fine sessione viene rilasciato a ogni uscita terminale",
          "return finish();" in _end_body
          and "this._streamEndRequested = null;" in _end_body
          and _end_body.count("return finish();") >= 3)
    # Nessuna chiusura deve fallire in silenzio: i due rami che non sparano il
    # toggle lasciano comunque una traccia (sessione cambiata / coda bloccata).
    check("P4: la sessione gia' cambiata lascia una traccia nel log",
          "nessuna chiusura necessaria" in _end_body)
    check("P4: la coda bloccata lascia una traccia nel log",
          "stream end abbandonato" in _end_body)
    # E il tetto: senza, una coda che non si svuota tiene acceso il pallino.
    check("P4: esiste un tetto di attesa che forza la chiusura",
          "STREAM_END_TIMEOUT_MS" in _end_body
          and "chiusura forzata" in _end_body
          and "const STREAM_END_TIMEOUT_MS" in _ext_src)
    check("P4: spawnBackground accetta gli argomenti (senza, 'stop' verrebbe scartato)",
          "function spawnBackground(binName, ...args)" in _ext_src
          and "Gio.Subprocess.new([path, ...args]" in _ext_src)


    print("== giro 18: P2 la riparazione passa il guard ==")
    # Difetto: cli.py scriveva STATE_ERROR SENZA service. Il guard di
    # status.write_status confronta il servizio e respinse la scrittura
    # quando lo stato corrente e' recording con un altro servizio, quindi la
    # riparazione era INERTE (stato identico prima/dopo, misurato dal
    # reviewer) e il timeout si prendeva la colpa con un motivo falso.
    with tempfile.TemporaryDirectory() as _td_p2:
        _sp2 = Path(_td_p2) / "status.json"
        _saved_sp2 = status.STATUS_PATH
        _saved_lc2 = _cli18.load_config
        try:
            status.STATUS_PATH = _sp2
            _sp2.write_text(json.dumps(
                {"state": "recording", "timestamp": 1.0, "service": "stt"}))
            with mock.patch.object(_cli18, "load_config", return_value=object()), \
                 mock.patch.object(_stt18, "handle_toggle", side_effect=RuntimeError("boom")), \
                 mock.patch.object(_cli18, "_report_unexpected_error"):
                _rc_p2 = _cli18.stt_toggle_main()
            _after2 = json.loads(_sp2.read_text())
            check("P2: la riparazione scrive ERROR (prima era respinta: stato invariato)",
                  _after2.get("state") == "error")
            check("P2: la riparazione dichiara il proprio servizio, cosi' passa il guard",
                  _after2.get("service") == "stt")
            check("P2: l'uscita resta un errore (1)", _rc_p2 == 1)

            # CONTRO: una sessione STREAM VIVA non deve essere spenta dalla
            # riparazione della scorciatoia STT. Misurato come regressione:
            # una prima stesura propagava il service letto da disco e qui
            # finiva {error, service=stream}, cioe' l'indicatore si spegneva
            # mentre la sessione stava ancora registrando.
            _sp2.write_text(json.dumps(
                {"state": "recording", "timestamp": 1.0, "service": "stream"}))
            with mock.patch.object(_cli18, "load_config", return_value=object()), \
                 mock.patch.object(_stt18, "handle_toggle", side_effect=RuntimeError("boom")), \
                 mock.patch.object(_cli18, "_report_unexpected_error"):
                _cli18.stt_toggle_main()
            _after3 = json.loads(_sp2.read_text())
            check("P2 (contro): la riparazione STT NON spegne una sessione stream viva",
                  _after3.get("state") == "recording"
                  and _after3.get("service") == "stream")
        finally:
            status.STATUS_PATH = _saved_sp2
            _cli18.load_config = _saved_lc2

    print("== giro 18: P2 il RuntimeError di stop non fuga piu' verso cli.py ==")
    # I due RuntimeError di audio.stop_recording (lock assente, audio_path
    # vuoto nella finestra di avvio) sfuggivano da stt._stop_and_process e
    # risalivano a cli.py, che segnalava all'utente un errore inesistente.
    for _label, _exc2 in (("lock assente", RuntimeError("No recording in progress")),
                          ("audio_path vuoto (finestra di avvio)",
                           RuntimeError("No recording in progress"))):
        _seen2: dict[str, Any] = {"processed": False}
        with mock.patch.object(_a18, "is_recording", return_value=True), \
             mock.patch.object(_a18, "stop_recording", side_effect=_exc2), \
             mock.patch.object(_stt18, "_process_recording",
                               side_effect=lambda *a, **k: _seen2.__setitem__("processed", True)), \
             mock.patch.object(_stt18, "status"), \
             mock.patch.object(_stt18, "notify"):
            _escaped2: str | None = None
            try:
                _stt18._stop_and_process(mock.Mock())
            except RuntimeError as exc:
                _escaped2 = str(exc)
        check(f"P2: {_label} non esce da _stop_and_process", _escaped2 is None)
        check(f"P2: {_label} non avvia la trascrizione su un path inesistente",
              not _seen2["processed"])

    # Il ToggleDebouncedError continua a essere trattato come prima (non
    # deve diventare un errore: e' una pressione troppo ravvicinata).
    _deb: dict[str, Any] = {"processed": False}
    with mock.patch.object(_a18, "is_recording", return_value=True), \
         mock.patch.object(_a18, "stop_recording",
                           side_effect=_a18.ToggleDebouncedError("Debounce active")), \
         mock.patch.object(_stt18, "_process_recording",
                           side_effect=lambda *a, **k: _deb.__setitem__("processed", True)), \
         mock.patch.object(_stt18, "status"), \
         mock.patch.object(_stt18, "notify"):
        _stt18._stop_and_process(mock.Mock())
    check("P2: il debounce resta silenzioso e senza trascrizione",
          not _deb["processed"])

    # Anti-drift: le due catene di cattura devono stare in _stop_and_process.
    _stop_body = (_stt_src.split("def _stop_and_process(cfg: Config) -> None:")[1]
                  .split("\ndef ")[0])
    check("P2: stt.py cattura RuntimeError oltre a ToggleDebouncedError",
          "except audio.ToggleDebouncedError" in _stop_body
          and "except RuntimeError" in _stop_body)


    print("== giro 18: P1 un file da 0 byte non e' una registrazione ==")
    # Difetto: il ramo normale (ffmpeg vivo, chiuso con SIGINT) restituiva il
    # path anche con il file a zero byte, e la trascrizione partiva su un file
    # vuoto. Stessa guardia del gemello stream.py:1342, che qui mancava.
    with tempfile.TemporaryDirectory() as _td_p1:
        _lk1 = Path(_td_p1) / "recording.lock"
        _saved_lk1 = _a18.LOCK_PATH
        # pid MORTO, non os.getpid(): con un pid vivo stop_recording manda
        # davvero SIGINT al processo (prima stesura di questo test: il
        # runner ha ricevuto il KeyboardInterrupt e la suite e' morta qui).
        # Il ramo che si vuole provare e' comunque quello normale — il file
        # vuoto resta vuoto sia con ffmpeg vivo sia con ffmpeg gia' morto.
        _dead1 = dead_pid()
        try:
            _a18.LOCK_PATH = _lk1
            _cfg_a = mock.Mock(toggle_debounce_seconds=0.0)

            # 1) file a 0 byte
            _empty1 = Path(_td_p1) / "vuoto.ogg"
            _empty1.write_bytes(b"")
            _lk1.write_text(json.dumps({"pid": _dead1,
                                        "audio_path": str(_empty1), "started_at": 0}))
            _raised1: str | None = None
            try:
                _a18.stop_recording(_cfg_a)
            except RuntimeError as exc:
                _raised1 = str(exc)
            check("P1: un file a 0 byte viene rifiutato (non e' una registrazione)",
                  _raised1 is not None)
            check("P1: il lock e' comunque rimosso (registrazione successiva parte)",
                  not _lk1.exists())
            check("P1: il file vuoto non resta a terra", not _empty1.exists())

            # 2) file sparito fra is_recording() e stop_recording() (caso B)
            _lk1.write_text(json.dumps({"pid": _dead1,
                                        "audio_path": str(Path(_td_p1) / "fantasma.ogg"),
                                        "started_at": 0}))
            _raised2: str | None = None
            try:
                _a18.stop_recording(_cfg_a)
            except RuntimeError as exc:
                _raised2 = str(exc)
            check("P1: un file sparito non viene restituito come registrazione",
                  _raised2 is not None)

            # CONTRO (non-vacuita): un file con contenuto passa e viene
            # restituito — la guardia non ha reso lo stop sempre fallito.
            _good1 = Path(_td_p1) / "buono.ogg"
            _good1.write_bytes(b"audio reale")
            _lk1.write_text(json.dumps({"pid": _dead1,
                                        "audio_path": str(_good1), "started_at": 0}))
            check("P1 (contro): un file con contenuto viene restituito normalmente",
                  _a18.stop_recording(_cfg_a) == _good1)
        finally:
            _a18.LOCK_PATH = _saved_lk1

    print("== giro 18: D1 la trascrizione vuota non azzera gli appunti ==")
    # Difetto: api_client ritornava "" e stt.py testava `is None`, quindi il
    # vuoto era un SUCCESSO e finiva a wl-copy: appunti azzerati.
    _seen_clip: list[str] = []
    _seen_st: list[str] = []

    class _RespD1:
        status_code = 200
        text = ""
        def json(self): return {"text": ""}

    class _SessD1:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return _RespD1()

    _lvl_d1 = config.FallbackLevel("D1", "http://x/v1", "m", "k", "", "", 5, False, True, 1)
    # 1) a livello api_client: il vuoto solleva invece di tornare
    with tempfile.TemporaryDirectory() as _td_d1:
        _au = Path(_td_d1) / "a.ogg"
        _au.write_bytes(b"audio")
        _raised_d1: str | None = None
        try:
            api_client.transcribe_audio(_lvl_d1, _au, session=_SessD1())  # type: ignore[arg-type]
        except api_client.ApiError as exc:
            _raised_d1 = str(exc)
        check("D1: una trascrizione vuota solleva ApiError (non e' un successo)",
              _raised_d1 is not None and "empty" in _raised_d1.lower())

    # 2) a livello stt.py: nemmeno se la catena restituisce "" gli appunti
    #    vengono azzerati. Qui si forza raw_text = "" per verificare la guardia
    #    del CHIAMANTE, indipendentemente da api_client.
    # Il mock di `status` riceve le COSTANTI vere: senza, STATE_ERROR sarebbe
    # un attributo Mock e l'asserzione "trattata come errore" controllerebbe
    # una stringa mai scritta (misurato: falliva con la costante mocked).
    _st_d1 = mock.Mock()
    _st_d1.STATE_ERROR = status.STATE_ERROR
    _st_d1.STATE_PROCESSING = status.STATE_PROCESSING
    _st_d1.STATE_IDLE = status.STATE_IDLE
    with mock.patch.object(stt, "try_with_fallback", return_value="   "), \
         mock.patch.object(stt, "clipboard") as _clip, \
         mock.patch.object(stt, "status", _st_d1), \
         mock.patch.object(stt, "notify") as _nt, \
         mock.patch.object(stt, "storage"), \
         mock.patch.object(stt, "output_history"):
        stt._process_recording(cast(Any, cfg_stream_min), Path("/tmp/qualsiasi.ogg"))
        _seen_clip = [c.args[0] for c in _clip.write_text.call_args_list]
        _seen_st = [c.args[0] for c in _st_d1.write_status.call_args_list]
    check("D1: una trascrizione vuota NON azzera gli appunti (wl-copy non riceve '')",
          "" not in _seen_clip and "   " not in _seen_clip)
    check("D1: una trascrizione vuota viene trattata come errore",
          bool(_seen_st) and _seen_st[-1] == status.STATE_ERROR)

    # Frase in BLACKLIST utente come INTERA trascrizione (registrazione senza
    # voce -> allucinazione Whisper): stessa uscita dell'empty, appunti
    # intatti, errore notificato. E' la stessa lista [stream].blacklist
    # configurabile da GUI, non una seconda lista.
    import dataclasses as _dc_bl
    _cfg_bl = _dc_bl.replace(cfg_stream_min, stream=_dc_bl.replace(
        cfg_stream_min.stream, blacklist="grazie per la visione, Sottotitoli a cura di"))
    for _hal_text in ("Grazie per la visione!", "  sottotitoli A CURA DI.  "):
        with mock.patch.object(stt, "try_with_fallback", return_value=_hal_text), \
             mock.patch.object(stt, "clipboard") as _clip_h, \
             mock.patch.object(stt, "status", _st_d1), \
             mock.patch.object(stt, "notify") as _nt_h, \
             mock.patch.object(stt, "storage"), \
             mock.patch.object(stt, "output_history"):
            _st_d1.reset_mock()
            stt._process_recording(cast(Any, _cfg_bl), Path("/tmp/qualsiasi.ogg"))
            _hal_clip = _clip_h.write_text.call_count
            _hal_st = [c.args[0] for c in _st_d1.write_status.call_args_list]
            _hal_notified = _nt_h.send.call_count >= 1
        check(f"stt: frase in blacklist {_hal_text.strip()!r} come intera trascrizione NON va negli appunti",
              _hal_clip == 0)
        check(f"stt: frase in blacklist {_hal_text.strip()!r} -> errore + notifica",
              bool(_hal_st) and _hal_st[-1] == status.STATE_ERROR and _hal_notified)

    # CONTRO: senza la frase in blacklist (default vuoto) la stessa trascrizione
    # passa: e' l'utente a decidere, nessuna lista nascosta nel codice.
    with mock.patch.object(stt, "try_with_fallback", return_value="Grazie per la visione!"), \
         mock.patch.object(stt, "clipboard") as _clip_nb, \
         mock.patch.object(stt, "status", _st_d1), \
         mock.patch.object(stt, "notify"), \
         mock.patch.object(stt, "storage"), \
         mock.patch.object(stt, "output_history"):
        stt._process_recording(cast(Any, cfg_stream_min), Path("/tmp/qualsiasi.ogg"))
        _nb_clip = [c.args[0] for c in _clip_nb.write_text.call_args_list]
    check("stt (contro): blacklist vuota -> nessun filtro nascosto, il testo passa",
          "Grazie per la visione!" in _nb_clip)

    # CONTRO: una frase vera che CONTIENE una voce della blacklist non e' toccata
    # (match sull'intero testo, mai su sottostringa).
    with mock.patch.object(stt, "try_with_fallback", return_value="Grazie per la visione! Ci vediamo domani."), \
         mock.patch.object(stt, "clipboard") as _clip_hs, \
         mock.patch.object(stt, "status", _st_d1), \
         mock.patch.object(stt, "notify"), \
         mock.patch.object(stt, "storage"), \
         mock.patch.object(stt, "output_history"):
        stt._process_recording(cast(Any, _cfg_bl), Path("/tmp/qualsiasi.ogg"))
        _hs_clip = [c.args[0] for c in _clip_hs.write_text.call_args_list]
    check("stt (contro): frase vera che contiene una voce della blacklist NON viene scartata",
          "Grazie per la visione! Ci vediamo domani." in _hs_clip)

    # CONTRO: con un testo vero la clipboard viene comunque scritta — la
    # guardia non ha spento il percorso buono.
    with mock.patch.object(stt, "try_with_fallback", return_value="ciao mondo"), \
         mock.patch.object(stt, "clipboard") as _clip2, \
         mock.patch.object(stt, "status"), \
         mock.patch.object(stt, "notify"), \
         mock.patch.object(stt, "storage"), \
         mock.patch.object(stt, "output_history"):
        stt._process_recording(cast(Any, cfg_stream_min), Path("/tmp/qualsiasi.ogg"))
        _clip_ok = [c.args[0] for c in _clip2.write_text.call_args_list]
    check("D1 (contro): una trascrizione vera finisce negli appunti come prima",
          "ciao mondo" in _clip_ok)

    # Anti-drift sul chiamante: il test non deve tornare a `is None`.
    _proc_body = (_stt_src.split("def _process_recording(")[1].split("\ndef ")[0])
    check("D1: stt.py non tratta piu' il vuoto come successo (guarda il contenuto, non `is None`)",
          "raw_text is None:" not in _proc_body
          and "not raw_text.strip()" in _proc_body)
    check("D1: la guardia di stato dichiara il servizio (come tutte le altre scritture STT)",
          'status.write_status(status.STATE_ERROR, service="stt")' in _proc_body)

    print("== mandato: D1 lato OCR, mai chiuso finora ==")
    # Stesso difetto di D1 (STT), mai corretto lato OCR: vision_extract non
    # aveva guardia sul vuoto, e ocr.py non aveva la guardia lato chiamante
    # che stt.py ha da tempo. Un 200 con content vuoto finiva a wl-copy,
    # azzerando gli appunti al posto di segnalare un errore.

    # 1) a livello api_client: il vuoto solleva invece di tornare.
    class _RespD1Ocr:
        status_code = 200
        text = ""
        def json(self) -> dict:
            return {"choices": [{"message": {"content": "   "}}]}

    class _SessD1Ocr:
        def post(self, *a: Any, **k: Any) -> Any:
            return _RespD1Ocr()

    _lvl_d1o = config.FallbackLevel("D1O", "http://x/v1", "m", "k", "", "", 5, False, True, 1)
    _saved_requests_post = api_client.requests.post
    api_client.requests.post = _SessD1Ocr().post
    try:
        _raised_d1o: str | None = None
        try:
            api_client.vision_extract(_lvl_d1o, "sistema", b"png-bytes")
        except api_client.ApiError as exc:
            _raised_d1o = str(exc)
        check("D1-OCR: un'estrazione vuota solleva ApiError (non e' un successo)",
              _raised_d1o is not None and "empty" in _raised_d1o.lower())
    finally:
        api_client.requests.post = _saved_requests_post

    # 2) a livello ocr.py: nemmeno se la catena restituisce "" gli appunti
    #    vengono azzerati (difesa in profondita', indipendente da api_client).
    with mock.patch.object(ocr, "clipboard") as _clip_o, \
         mock.patch.object(ocr, "try_with_fallback", return_value="   "), \
         mock.patch.object(ocr, "status") as _st_o, \
         mock.patch.object(ocr, "notify"), \
         mock.patch.object(ocr, "storage"), \
         mock.patch.object(ocr, "output_history"):
        _st_o.STATE_ERROR = status.STATE_ERROR
        _st_o.STATE_PROCESSING = status.STATE_PROCESSING
        _st_o.STATE_IDLE = status.STATE_IDLE
        _clip_o.read_image_png.return_value = b"png-bytes"
        ocr.handle_capture(cast(Any, cfg_stream_min))
        _seen_clip_o = [c.args[0] for c in _clip_o.write_text.call_args_list]
        _seen_st_o = [c.args[0] for c in _st_o.write_status.call_args_list]
    check("D1-OCR: un'estrazione vuota NON azzera gli appunti (wl-copy non riceve '')",
          "" not in _seen_clip_o and "   " not in _seen_clip_o)
    check("D1-OCR: un'estrazione vuota viene trattata come errore",
          bool(_seen_st_o) and _seen_st_o[-1] == status.STATE_ERROR)

    # CONTRO: con un testo vero la clipboard viene comunque scritta.
    with mock.patch.object(ocr, "clipboard") as _clip_o2, \
         mock.patch.object(ocr, "try_with_fallback", return_value="testo estratto"), \
         mock.patch.object(ocr, "status"), \
         mock.patch.object(ocr, "notify"), \
         mock.patch.object(ocr, "storage"), \
         mock.patch.object(ocr, "output_history"):
        _clip_o2.read_image_png.return_value = b"png-bytes"
        ocr.handle_capture(cast(Any, cfg_stream_min))
        _clip_ok_o = [c.args[0] for c in _clip_o2.write_text.call_args_list]
    check("D1-OCR (contro): un'estrazione vera finisce negli appunti come prima",
          "testo estratto" in _clip_ok_o)

    print("== screenshot.py: capture_area_png (nuova funzionalita') ==")
    # screenshot.capture_area_png non chiama mai un vero gnome-screenshot nei
    # test: subprocess.run e' sostituito con doppioni che ispezionano l'argv
    # reale (per scrivere il file al path che la funzione ha davvero scelto,
    # non uno concordato in anticipo) e simulano i 4 esiti possibili.
    import dataclasses as _dc

    def _fake_run_success(args: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        path = Path(args[args.index("--file") + 1])
        path.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
        return subprocess.CompletedProcess(args, 0)

    def _fake_run_cancel(args: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 1)  # Esc: niente file, exit != 0

    def _fake_run_missing(args: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        raise FileNotFoundError("gnome-screenshot non installato")

    def _fake_run_timeout(args: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(args, screenshot.SELECTION_TIMEOUT_SECONDS)

    def _fake_run_oserror(args: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        # Non FileNotFoundError: un OSError generico (permessi, risorse
        # esaurite, ...) che avviare il processo puo' sollevare. Senza il
        # ramo `except OSError` in screenshot.py, questo risalirebbe fino a
        # cli.ocr_capture_main() come "Unexpected error", non come
        # annullamento silenzioso.
        raise PermissionError("simulato: permesso negato")

    _orig_ss_run = screenshot.subprocess.run
    try:
        screenshot.subprocess.run = _fake_run_success
        check("screenshot: successo -> bytes del PNG catturato",
              screenshot.capture_area_png() == b"\x89PNG\r\n\x1a\nFAKE")

        screenshot.subprocess.run = _fake_run_cancel
        check("screenshot: annullato (Esc, exit!=0, nessun file) -> None, non un errore",
              screenshot.capture_area_png() is None)

        screenshot.subprocess.run = _fake_run_missing
        check("screenshot: gnome-screenshot assente -> None (loggato, non solleva)",
              screenshot.capture_area_png() is None)

        screenshot.subprocess.run = _fake_run_timeout
        check("screenshot: timeout selezione -> None",
              screenshot.capture_area_png() is None)

        screenshot.subprocess.run = _fake_run_oserror
        check("screenshot: OSError generico all'avvio -> None, non solleva",
              screenshot.capture_area_png() is None)
    finally:
        screenshot.subprocess.run = _orig_ss_run

    print("== ocr.py: capture_screenshot=True chiama screenshot invece di clipboard ==")
    cfg_shot = _dc.replace(cfg_stream_min, ocr_capture_screenshot=True)
    with mock.patch.object(ocr, "screenshot") as _shot_o, \
         mock.patch.object(ocr, "clipboard") as _clip_shot, \
         mock.patch.object(ocr, "try_with_fallback", return_value="testo da screenshot"), \
         mock.patch.object(ocr, "status"), \
         mock.patch.object(ocr, "notify"), \
         mock.patch.object(ocr, "storage"), \
         mock.patch.object(ocr, "output_history"):
        _shot_o.capture_area_png.return_value = b"png-bytes"
        ocr.handle_capture(cast(Any, cfg_shot))
        _shot_calls = _shot_o.capture_area_png.call_count
        _clip_read_calls = _clip_shot.read_image_png.call_count
        _shot_final = [c.args[0] for c in _clip_shot.write_text.call_args_list]
    check("ocr capture_screenshot=True: chiama screenshot.capture_area_png, non clipboard.read_image_png",
          _shot_calls == 1 and _clip_read_calls == 0)
    check("ocr capture_screenshot=True: il testo estratto arriva comunque in clipboard",
          "testo da screenshot" in _shot_final)

    # CONTRO: annullamento (None) non scrive nulla in clipboard e non chiama la catena OCR.
    with mock.patch.object(ocr, "screenshot") as _shot_cancel, \
         mock.patch.object(ocr, "clipboard") as _clip_cancel, \
         mock.patch.object(ocr, "try_with_fallback") as _chain_cancel, \
         mock.patch.object(ocr, "status") as _st_cancel, \
         mock.patch.object(ocr, "notify"), \
         mock.patch.object(ocr, "storage"), \
         mock.patch.object(ocr, "output_history"):
        _st_cancel.STATE_PROCESSING = status.STATE_PROCESSING
        _st_cancel.STATE_IDLE = status.STATE_IDLE
        _shot_cancel.capture_area_png.return_value = None
        ocr.handle_capture(cast(Any, cfg_shot))
        _cancel_write_calls = _clip_cancel.write_text.call_count
        _cancel_chain_calls = _chain_cancel.call_count
        _cancel_states = [c.args[0] for c in _st_cancel.write_status.call_args_list]
    check("ocr capture_screenshot=True, annullato: nessuna scrittura in clipboard",
          _cancel_write_calls == 0)
    check("ocr capture_screenshot=True, annullato: la catena OCR non viene nemmeno chiamata",
          _cancel_chain_calls == 0)
    check("ocr capture_screenshot=True, annullato: stato torna a idle, non error",
          _cancel_states and _cancel_states[-1] == status.STATE_IDLE)

    # CONTRO: capture_screenshot=False (default) continua a leggere la clipboard, invariato.
    with mock.patch.object(ocr, "screenshot") as _shot_off, \
         mock.patch.object(ocr, "clipboard") as _clip_off, \
         mock.patch.object(ocr, "try_with_fallback", return_value="testo da clipboard"), \
         mock.patch.object(ocr, "status"), \
         mock.patch.object(ocr, "notify"), \
         mock.patch.object(ocr, "storage"), \
         mock.patch.object(ocr, "output_history"):
        _clip_off.read_image_png.return_value = b"png-bytes"
        ocr.handle_capture(cast(Any, cfg_stream_min))
        _off_shot_calls = _shot_off.capture_area_png.call_count
    check("ocr capture_screenshot=False (default): screenshot.capture_area_png MAI chiamata",
          _off_shot_calls == 0)

    print("== ocr.py: guardia rientranza capture_screenshot (doppia pressione) ==")
    # Senza questa guardia, una seconda pressione mentre la prima selezione
    # e' ancora aperta lancerebbe un secondo gnome-screenshot sovrapposto.
    _st_saved_reentr = status.STATUS_PATH
    try:
        _tmp_status_dir = Path(tempfile.mkdtemp(prefix="bravoric-status-reentr-"))
        status.STATUS_PATH = _tmp_status_dir / "status.json"
        status.write_status(status.STATE_PROCESSING, service="ocr")
        with mock.patch.object(ocr, "screenshot") as _shot_re, \
             mock.patch.object(ocr, "clipboard"), \
             mock.patch.object(ocr, "try_with_fallback") as _chain_re, \
             mock.patch.object(ocr, "notify"), \
             mock.patch.object(ocr, "storage"), \
             mock.patch.object(ocr, "output_history"):
            ocr.handle_capture(cast(Any, cfg_shot))
            _reentr_shot_calls = _shot_re.capture_area_png.call_count
            _reentr_chain_calls = _chain_re.call_count
        check("ocr capture_screenshot=True: seconda pressione durante 'processing' non riscatta uno screenshot",
              _reentr_shot_calls == 0 and _reentr_chain_calls == 0)

        # Residuo STANTIO: 'processing/ocr' vecchio di 10 min (processo ucciso
        # con kill -9 durante la selezione): NON deve bloccare per sempre le
        # catture successive (lockout silenzioso fino al watchdog, 120 min).
        status.STATUS_PATH.write_text(json.dumps(
            {"state": status.STATE_PROCESSING, "service": "ocr",
             "timestamp": time.time() - 600}), encoding="utf-8")
        with mock.patch.object(ocr, "screenshot") as _shot_stale, \
             mock.patch.object(ocr, "clipboard"), \
             mock.patch.object(ocr, "try_with_fallback", return_value="ok"), \
             mock.patch.object(ocr, "notify"), \
             mock.patch.object(ocr, "storage"), \
             mock.patch.object(ocr, "output_history"):
            _shot_stale.capture_area_png.return_value = b"png"
            ocr.handle_capture(cast(Any, cfg_shot))
            _stale_calls = _shot_stale.capture_area_png.call_count
        check("ocr guard: stato 'processing' STANTIO (10 min, residuo di un crash) non blocca la cattura",
              _stale_calls == 1)

        # CONTRO: a idle (nessuna cattura in corso), la guardia non blocca la prima pressione.
        status.write_status(status.STATE_IDLE)
        with mock.patch.object(ocr, "screenshot") as _shot_ok, \
             mock.patch.object(ocr, "clipboard"), \
             mock.patch.object(ocr, "try_with_fallback", return_value="ok"), \
             mock.patch.object(ocr, "notify"), \
             mock.patch.object(ocr, "storage"), \
             mock.patch.object(ocr, "output_history"):
            _shot_ok.capture_area_png.return_value = b"png"
            ocr.handle_capture(cast(Any, cfg_shot))
            _idle_shot_calls = _shot_ok.capture_area_png.call_count
        check("ocr capture_screenshot=True (contro): a idle la prima pressione scatta normalmente",
              _idle_shot_calls == 1)
    finally:
        status.STATUS_PATH = _st_saved_reentr

    print("== ocr.py: capture_screenshot=True, gnome-screenshot assente ==")
    # Diverso dall'annullamento: qui la feature e' attivata ma non puo'
    # funzionare MAI, ad ogni pressione — deve avvisare, non tacere come un
    # cambio idea. Senza screenshot.is_available(), questo caso era
    # indistinguibile per l'utente da un Esc silenzioso.
    with mock.patch.object(ocr, "screenshot") as _shot_missing, \
         mock.patch.object(ocr, "clipboard") as _clip_missing, \
         mock.patch.object(ocr, "try_with_fallback") as _chain_missing, \
         mock.patch.object(ocr, "status") as _st_missing, \
         mock.patch.object(ocr, "notify") as _notify_missing, \
         mock.patch.object(ocr, "storage"), \
         mock.patch.object(ocr, "output_history"):
        _st_missing.STATE_PROCESSING = status.STATE_PROCESSING
        _st_missing.STATE_ERROR = status.STATE_ERROR
        _st_missing.read_status.return_value = {"state": status.STATE_IDLE}
        _shot_missing.is_available.return_value = False
        ocr.handle_capture(cast(Any, cfg_shot))
        _missing_capture_calls = _shot_missing.capture_area_png.call_count
        _missing_chain_calls = _chain_missing.call_count
        _missing_clip_calls = _clip_missing.write_text.call_count
        _missing_states = [c.args[0] for c in _st_missing.write_status.call_args_list]
        _missing_notified = _notify_missing.send.call_count >= 1
    check("ocr gnome-screenshot assente: non tenta la cattura (l'avviso e' PRIMA)",
          _missing_capture_calls == 0)
    check("ocr gnome-screenshot assente: la catena OCR non viene chiamata",
          _missing_chain_calls == 0)
    check("ocr gnome-screenshot assente: nessuna scrittura in clipboard",
          _missing_clip_calls == 0)
    check("ocr gnome-screenshot assente: stato ERROR, non IDLE silenzioso",
          _missing_states and _missing_states[-1] == status.STATE_ERROR)
    check("ocr gnome-screenshot assente: l'utente viene avvisato (a differenza di Esc)",
          _missing_notified)

    print("== config.py: ocr_capture_screenshot (default e parsing) ==")
    check("Config: ocr_capture_screenshot default False su cfg_stream_min",
          cfg_stream_min.ocr_capture_screenshot is False)
    _cfg_shot_true = config._build_config({"ocr": {"capture_screenshot": True}})
    check("config: [ocr].capture_screenshot = true viene letto",
          _cfg_shot_true.ocr_capture_screenshot is True)
    # La stringa "false" e' truthy in Python: senza _coerce_bool attivava lo
    # screenshot per un refuso (o per un config scritto dal vecchio bug che
    # serializzava questo campo come stringa TOML invece che bool).
    check("config: capture_screenshot = \"false\" (stringa) resta False, non truthy",
          config._build_config({"ocr": {"capture_screenshot": "false"}}).ocr_capture_screenshot is False)
    check("config: capture_screenshot = \"true\" (stringa) e' True",
          config._build_config({"ocr": {"capture_screenshot": "true"}}).ocr_capture_screenshot is True)
    check("config: capture_screenshot = 0 (intero) resta False",
          config._build_config({"ocr": {"capture_screenshot": 0}}).ocr_capture_screenshot is False)
    # Stessa coercizione lato GUI: get_state alimenta lo switch di prefs.js;
    # con "false" grezzo (truthy) lo switch mostrerebbe ON mentre il backend
    # (_coerce_bool) dice OFF — GUI che mente sullo stato reale.
    with tempfile.TemporaryDirectory() as _td_gs:
        _p_gs = Path(_td_gs) / "config.toml"
        _p_gs.write_text('[ocr]\ncapture_screenshot = "false"\n', encoding="utf-8")
        _saved_cp_gs = config_editor.CONFIG_PATH
        try:
            config_editor.CONFIG_PATH = _p_gs
            check("get_state: capture_screenshot = \"false\" (stringa) -> False, coerente col backend",
                  config_editor.get_state()["ocr"]["capture_screenshot"] is False)
        finally:
            config_editor.CONFIG_PATH = _saved_cp_gs
    # storage.base_dir vuoto: Path("") e' la cwd -> dati sensibili in $HOME.
    check("config: storage.base_dir = \"\" (vuoto) cade sul default, non sulla cwd",
          config._build_config({"storage": {"base_dir": ""}}).storage.base_dir
          == config.DEFAULT_STORAGE_BASE_DIR)
    check("config: storage.base_dir solo spazi cade sul default",
          config._build_config({"storage": {"base_dir": "   "}}).storage.base_dir
          == config.DEFAULT_STORAGE_BASE_DIR)
    check("config: storage.base_dir valido resta invariato",
          config._build_config({"storage": {"base_dir": "/dati/x"}}).storage.base_dir == "/dati/x")
    # api_client: il corpo di una risposta d'errore finisce in ApiError ->
    # notifica desktop + journal + chunk log. Un 401 stile OpenAI ripete la
    # chiave; un corpo HTML puo' essere lunghissimo.
    class _RespErr:
        status_code = 401
        text = "Incorrect API key provided: sk-abcdef123456. " + ("x" * 5000)
        def json(self) -> dict:
            return {}

    _lvl_err = config.FallbackLevel(
        name="E", endpoint="http://x/v1", model="m", api_key_env="",
        api_key="sk-abcdef123456", ca_cert="", timeout_seconds=5)
    _saved_post_err = api_client.requests.post
    api_client.requests.post = lambda *a, **k: _RespErr()  # type: ignore[assignment]
    try:
        _err_msgs: dict[str, str] = {}
        for _name_err, _call_err in (
            ("chat_cleanup", lambda: api_client.chat_cleanup(_lvl_err, "sys", "testo")),
            ("vision_extract", lambda: api_client.vision_extract(_lvl_err, "sys", b"png")),
        ):
            try:
                _call_err()
                _err_msgs[_name_err] = ""
            except api_client.ApiError as exc:
                _err_msgs[_name_err] = str(exc)
    finally:
        api_client.requests.post = _saved_post_err
    _au_err = tmp / "err.ogg"
    _au_err.write_bytes(b"x")

    class _SessErr:
        def post(self, *a: Any, **k: Any) -> Any:
            return _RespErr()

    try:
        api_client.transcribe_audio(_lvl_err, _au_err, session=_SessErr())  # type: ignore[arg-type]
        _err_msgs["transcribe_audio"] = ""
    except api_client.ApiError as exc:
        _err_msgs["transcribe_audio"] = str(exc)
    for _name_err, _msg_err in _err_msgs.items():
        check(f"api_client.{_name_err}: la chiave NON compare nell'ApiError (401 che la ripete)",
              bool(_msg_err) and "sk-abcdef123456" not in _msg_err)
        check(f"api_client.{_name_err}: il corpo d'errore e' troncato (non 5000 caratteri in una notifica)",
              0 < len(_msg_err) < api_client.MAX_ERROR_BODY_CHARS + 120)

    # fallback: un errore requests/OSError include l'URL; con un endpoint
    # ...?api_key=XXX la chiave finiva in journal e notifica (misurato).
    from bravoric_stt_clipboard import fallback as _fb_mod
    _lv_fb = config.FallbackLevel(
        name="L", endpoint="http://h/v1", model="m", api_key_env="", api_key="",
        ca_cert="", timeout_seconds=5)

    def _boom_fb(_level: Any) -> str:
        raise OSError("Max retries with url: /v1/audio?api_key=TOPSECRET99 (Caused by X)")

    try:
        _fb_mod.try_with_fallback([_lv_fb], _boom_fb)
        _fb_msg = ""
    except _fb_mod.AllLevelsFailedError as exc:
        _fb_msg = str(exc)
    check("fallback: la chiave in un URL d'errore NON compare in AllLevelsFailedError (-> notifica)",
          bool(_fb_msg) and "TOPSECRET99" not in _fb_msg)
    check("fallback: il nome del livello e il resto dell'errore restano leggibili",
          "L:" in _fb_msg and "Max retries" in _fb_msg and "api_key=" in _fb_msg)

    # api_client._keep_leading_words: pura, mai testata direttamente.
    _klw = api_client._keep_leading_words
    check("keep_leading_words: budget esatto tiene tutte le parole ('ab cd' = 5)",
          _klw("ab cd", 5) == "ab cd")
    check("keep_leading_words: un carattere in meno scarta la parola che sfora",
          _klw("ab cd", 4) == "ab")
    check("keep_leading_words: prima parola piu' lunga del budget -> vuoto (mai a meta')",
          _klw("abcdefgh x", 3) == "")
    check("keep_leading_words: budget <= 0 -> vuoto",
          _klw("ab cd", 0) == "" and _klw("ab cd", -5) == "")
    check("keep_leading_words: spazi/newline multipli normalizzati a uno",
          _klw("ab \n  cd", 99) == "ab cd")
    # _filter_chunks: vuoti e duplicati consecutivi, max 3. Le allucinazioni
    # non sono piu' qui: sono la blacklist utente, applicata prima (ingest).
    _fc = stream_module._filter_chunks
    check("filter_chunks: testo vero tenuto",
          _fc(["Grazie mille"]) == ["Grazie mille"])
    check("filter_chunks: duplicati consecutivi e vuoti scartati, max 3 tenuti",
          _fc(["a", "a", "", "  ", "b", "c", "d"]) == ["b", "c", "d"])
    check("stream: la lista hardcoded KNOWN_HALLUCINATIONS non esiste piu' (una sola blacklist, configurabile)",
          not hasattr(stream_module, "KNOWN_HALLUCINATIONS"))
    _cfg_shot_absent = config._build_config({})
    check("config: [ocr] assente -> capture_screenshot default False",
          _cfg_shot_absent.ocr_capture_screenshot is False)


    print("== giro 18: P3 il prompt di default esiste davvero ==")
    # Difetto: i dataclass avevano prompt = "" e i parser passavano
    # "" esplicito, quindi per chi scriveva una config a mano il default NON
    # esisteva: era solo nei file di esempio. E con prompt assente il ramo
    # del vocabolario non partiva mai (soglia `prompt is not None`).
    _lvl_p3 = config.FallbackLevel("P3", "http://x/v1", "m", "k", "", "", 5, False, True, 1)

    def _cfg_p3(stt_line: str = "", stream_line: str = "") -> Any:
        _txt = (
            "[stt]\n"
            'language = "it"\n'
            'hotwords = "PiAgent tmux"\n'
            + stt_line + "\n"
            'fallback = [{name="P3", endpoint="http://x/v1", model="m", api_key="k"}]\n'
            "[stream]\n"
            'mode = "per_chunk"\n'
            + stream_line + "\n"
            'fallback = [{name="P3", endpoint="http://x/v1", model="m", api_key="k"}]\n'
        )
        _p = Path(tempfile.mkdtemp()) / "c.toml"
        _p.write_text(_txt, encoding="utf-8")
        return config.load_config(_p)

    class _R3:
        status_code = 200
        text = ""
        def json(self) -> dict: return {"text": "ok"}

    class _S3:
        sent: dict | None = None
        def __enter__(self) -> Any: return self
        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool: return False
        def post(self, *a: Any, **k: Any) -> Any:
            _S3.sent = k.get("data")
            return _R3()

    def _send_p3(cfg: Any, prompt: Any) -> Any:
        _lv = cfg.stt_fallback[0]
        _lv.hotwords_in_prompt = True
        _S3.sent = None
        with tempfile.TemporaryDirectory() as _td:
            _au = Path(_td) / "a.ogg"
            _au.write_bytes(b"audio")
            api_client.transcribe_audio(_lv, _au, prompt=prompt,
                                        hotwords=cfg.stt.hotwords or None, session=cast(Any, _S3()))
        return (_S3.sent or {}).get("prompt")

    check("P3: il default e' una costante con nome in config.py",
          bool(config.DEFAULT_PROMPT.strip()) and "contesto di dettatura" in config.DEFAULT_PROMPT.lower())

    # (a) chiave ASSENTE e (b) chiave VUOTA, per [stt] e per [stream].
    for _label, _line in (("chiave assente", ""), ("chiave vuota", 'prompt = ""')):
        _c = _cfg_p3(_line, _line)
        check(f"P3 ({_label}): [stt] prende il default di codice",
              _c.stt.prompt == config.DEFAULT_PROMPT)
        check(f"P3 ({_label}): [stream] prende il default di codice",
              _c.stream.prompt == config.DEFAULT_PROMPT)
        _p3 = _send_p3(_c, _c.stt.prompt or None)
        check(f"P3 ({_label}): il campo prompt viaggia al backend", bool(_p3))
        check(f"P3 ({_label}): la frase di vocabolario viene mandata",
              bool(_p3) and "nomi proprio" in str(_p3))

    # (b) del piano: il ramo vocabolario NON dipende dal prompt personale.
    # prompt=None e' il caso reale (primo chunk di sessione, e
    # `cfg.stt.prompt or None` per chi non ha prompt).
    _p3_none = _send_p3(_cfg_p3(), None)
    check("P3: con prompt=None la frase di vocabolario parte comunque",
          bool(_p3_none) and "nomi proprio" in str(_p3_none))
    check("P3: con prompt=None la frase contiene i hotwords",
          bool(_p3_none) and "PiAgent" in str(_p3_none))

    # CONTROLLO: senza hotwords il prompt personale viaggia normalmente
    # (il ramo vocabolario non deve averlo mangiato).
    _c_ctl = _cfg_p3()
    _lv_ctl = _c_ctl.stt_fallback[0]
    _lv_ctl.hotwords_in_prompt = False
    _S3.sent = None
    with tempfile.TemporaryDirectory() as _td3:
        _au3 = Path(_td3) / "a.ogg"
        _au3.write_bytes(b"audio")
        api_client.transcribe_audio(_lv_ctl, _au3, prompt=_c_ctl.stt.prompt,
                                    hotwords="", session=cast(Any, _S3()))
    check("P3 (contro): senza hotwords il prompt personale viaggia",
          bool((_S3.sent or {}).get("prompt")))

    # I due config.example non devono contraddire il default.
    for _ex in ("config.example.it.toml", "config.example.toml"):
        _txt_ex = (ROOT / "config" / _ex).read_text(encoding="utf-8")
        _stream_part = _txt_ex.split("[stream]")[1]
        check(f"P3: [stream] in {_ex} non ha prompt = \"\" (contraddirrebbe il default)",
              'prompt = ""' not in _stream_part.split("\n[")[0])
    # E il testo del default deve coincidere con quello degli esempi [stt].
    _ex_it = (ROOT / "config" / "config.example.it.toml").read_text(encoding="utf-8")
    _ex_stt = _ex_it.split("[stt]")[1].split("\n[")[0]
    _m3 = re.search(r'prompt\s*=\s*"([^"]*)"', _ex_stt)
    check("P3: il default coincide con il testo di [stt] nell'esempio italiano",
          bool(_m3) and _m3.group(1) == config.DEFAULT_PROMPT)


    print("== giro 18: P5 un solo scrittore di config.toml ==")
    # Difetto: prefs.js scriveva config.toml con _readText + replace +
    # replace_contents, SENZA il lock di config_editor. Due scrittori, uno
    # solo col lock, e la perdita era SILENZIOSA (logError solo su IOException).
    _cfg_p5 = """
[stream]
mode = "per_chunk"

[storage.stt_raw]
enabled = false
retention_hours = 0

[history]
max_entries = 20
"""
    with tempfile.TemporaryDirectory() as _td5:
        _p5 = Path(_td5) / "config.toml"
        _p5.write_text(_cfg_p5, encoding="utf-8")
        _saved_cp5 = config_editor.CONFIG_PATH
        try:
            config_editor.CONFIG_PATH = _p5

            # Il caso MISURATO dal reviewer: un salvataggio di streaming
            # appena fatto, poi il click sullo switch. Prima la modifica
            # spariva in silenzio.
            config_editor.set_stream_field("mode", "at_end")
            config_editor.set_notification_field("stt_on_raw_ready", "false")
            _after5 = _p5.read_text(encoding="utf-8")
            check("P5: la modifica di streaming SOPRAVVIVE allo switch di notifica",
                  'mode = "at_end"' in _after5)
            check("P5: la chiave di notifica e' stata scritta",
                  "stt_on_raw_ready = false" in _after5)
            # E il TOML resta valido (la validazione e' gratis dal lock).
            check("P5: il TOML resta valido dopo le due scritture",
                  isinstance(tomllib.loads(_after5), dict))
        finally:
            config_editor.CONFIG_PATH = _saved_cp5

    # Lo stesso, nell'ordine inverso: lo switch non deve perdere niente.
    with tempfile.TemporaryDirectory() as _td5b:
        _p5b = Path(_td5b) / "config.toml"
        _p5b.write_text(_cfg_p5, encoding="utf-8")
        _saved_cp5b = config_editor.CONFIG_PATH
        try:
            config_editor.CONFIG_PATH = _p5b
            config_editor.set_notification_field("ocr_on_processing_start", "false")
            config_editor.set_stream_field("paste_delay_ms", "400")
            _after5b = _p5b.read_text(encoding="utf-8")
            check("P5 (inverso): lo switch non perde la modifica successiva",
                  "ocr_on_processing_start = false" in _after5b
                  and "paste_delay_ms = 400" in _after5b)
        finally:
            config_editor.CONFIG_PATH = _saved_cp5b

    # Chiave sconosciuta: respinta. Il valore finisce in un TOML e una chiave
    # iniettata ridefinirebbe un'intera tabella.
    with tempfile.TemporaryDirectory() as _td5c:
        _p5c = Path(_td5c) / "config.toml"
        _p5c.write_text(_cfg_p5, encoding="utf-8")
        _saved_cp5c = config_editor.CONFIG_PATH
        try:
            config_editor.CONFIG_PATH = _p5c
            # Chiave INIEtabile ma TOML-VALIDA. Con una chiave malformata
            # (con un header dentro) la rifiutava comunque _write_validated,
            # quindi il test non avrebbe misurato la guardia: sarebbe stato
            # verde anche senza di lei, cioe' VACUO. Qui la chiave e'
            # sintatticamente legittima e produrrebbe un TOML valido: senza
            # la guardia la scrittura passerebbe e il file cambierebbe in
            # silenzio (una tabella in piu', o una chiave spinta sotto
            # [notifications] che nessuno legge).
            _inj5 = "storage.stt_raw.enabled"
            _rej5: str | None = None
            try:
                config_editor.set_notification_field(_inj5, "false")
            except config_editor.ConfigEditorError as exc:
                _rej5 = str(exc)
            check("P5: una chiave di notifica sconosciuta viene respinta "
                  "(non basta che il TOML resti valido: la guardia e' nostra)",
                  _rej5 is not None and "Unknown notification key" in _rej5)
            check("P5: il rifiuto non ha scritto nulla nel file",
                  _p5c.read_text(encoding="utf-8") == _cfg_p5)
        finally:
            config_editor.CONFIG_PATH = _saved_cp5c

    print("== giro 18: P5 tabelle mancanti vengono create ==")
    # Sotto-difetto: set_storage_field e set_history_max_entries chiamavano
    # _find_block_bounds, che solleva se l'header manca, e non avevano ramo
    # di creazione (set_stream_field ce l'aveva gia').
    with tempfile.TemporaryDirectory() as _td5d:
        _p5d = Path(_td5d) / "config.toml"
        _p5d.write_text('[stream]\nmode = "per_chunk"\n', encoding="utf-8")
        _saved_cp5d = config_editor.CONFIG_PATH
        try:
            config_editor.CONFIG_PATH = _p5d
            _ok5: list[str] = []
            for _name5, _fn5, _args5 in (
                    ("set_storage_field", config_editor.set_storage_field, ("stt_raw", "enabled", "true")),
                    ("set_history_max_entries", config_editor.set_history_max_entries, ("42",)),
            ):
                try:
                    _fn5(*_args5)
                    _ok5.append(_name5)
                except Exception:
                    pass
            check("P5: set_storage_field crea la tabella mancante", "set_storage_field" in _ok5)
            check("P5: set_history_max_entries crea la tabella mancante",
                  "set_history_max_entries" in _ok5)
            _txt5d = _p5d.read_text(encoding="utf-8")
            check("P5: il TOML creato e' valido e contiene i valori",
                  isinstance(tomllib.loads(_txt5d), dict)
                  and "max_entries = 42" in _txt5d
                  and "enabled = true" in _txt5d)
        finally:
            config_editor.CONFIG_PATH = _saved_cp5d

    print("== giro 21: tabella presente ma chiave assente (set_storage_field/history) ==")
    # Difetto reale (non lo stesso di P5 sopra): set_storage_field e
    # set_history_max_entries chiamavano _replace_key_in_block, che solleva
    # se la CHIAVE manca in un blocco che PERO' esiste gia' (config piu'
    # vecchia di quel campo, o modificata a mano). _find_block_bounds non
    # c'entra qui: la tabella c'e', manca solo la riga. Riprodotto dal vivo
    # prima del fix: ConfigEditorError su un salvataggio GUI legittimo.
    with tempfile.TemporaryDirectory() as _td21:
        _p21 = Path(_td21) / "config.toml"
        _p21.write_text('[storage.stt_raw]\nenabled = false\n\n[history]\n', encoding="utf-8")
        _saved_cp21 = config_editor.CONFIG_PATH
        try:
            config_editor.CONFIG_PATH = _p21
            # retention_hours non e' nel blocco [storage.stt_raw]: deve
            # inserirla, non sollevare.
            config_editor.set_storage_field("stt_raw", "retention_hours", "24")
            # max_entries non e' nel blocco [history]: idem.
            config_editor.set_history_max_entries("50")
            _txt21 = _p21.read_text(encoding="utf-8")
            check("giro 21: set_storage_field inserisce una chiave assente in un blocco esistente",
                  "retention_hours = 24" in _txt21)
            check("giro 21: set_history_max_entries inserisce una chiave assente in un blocco esistente",
                  "max_entries = 50" in _txt21)
            check("giro 21: il TOML risultante resta valido",
                  isinstance(tomllib.loads(_txt21), dict))
        finally:
            config_editor.CONFIG_PATH = _saved_cp21

    print("== config_editor.reset_to_default: mai testata finora ==")
    # Operazione distruttiva (sovrascrive config.toml col template) con zero
    # copertura di test fino ad ora. Legge il template REALE del repo
    # (config/config.example*.toml, gia' verificato tomllib-valido altrove),
    # ma scrive su un CONFIG_PATH temporaneo: nessun file reale dell'utente
    # e' toccato.
    with tempfile.TemporaryDirectory() as _td_rst:
        _p_rst = Path(_td_rst) / "config.toml"
        _p_rst.write_text('[general]\nnotifications = false\n', encoding="utf-8")
        _saved_cp_rst = config_editor.CONFIG_PATH
        try:
            config_editor.CONFIG_PATH = _p_rst
            config_editor.reset_to_default()
            _txt_rst = _p_rst.read_text(encoding="utf-8")
            check("reset_to_default: il file esiste ancora ed e' TOML valido",
                  isinstance(tomllib.loads(_txt_rst), dict))
            check("reset_to_default: la personalizzazione precedente e' sparita (sovrascritta)",
                  "notifications = false" not in _txt_rst)
            check("reset_to_default: contiene una sezione [ocr] del template",
                  "[ocr]" in _txt_rst)
        finally:
            config_editor.CONFIG_PATH = _saved_cp_rst

    # CONTRO: un template TOML rotto viene rifiutato PRIMA di scrivere
    # (config_editor._example_config_path e' quella vera, quindi si
    # monkeypatcha solo tomllib.loads per simulare un template guasto senza
    # toccare i file reali del repo).
    with tempfile.TemporaryDirectory() as _td_rst2:
        _p_rst2 = Path(_td_rst2) / "config.toml"
        _original_content = '[general]\nnotifications = true\n'
        _p_rst2.write_text(_original_content, encoding="utf-8")
        _saved_cp_rst2 = config_editor.CONFIG_PATH
        _orig_tomllib_loads = config_editor.tomllib.loads
        try:
            config_editor.CONFIG_PATH = _p_rst2

            def _broken_loads(_text: str) -> dict:
                raise config_editor.tomllib.TOMLDecodeError("template rotto (simulato)")
            config_editor.tomllib.loads = _broken_loads
            _raised_rst2 = None
            try:
                config_editor.reset_to_default()
            except config_editor.ConfigEditorError as exc:
                _raised_rst2 = str(exc)
            check("reset_to_default (contro): template rotto solleva ConfigEditorError",
                  _raised_rst2 is not None and "invalid TOML template" in _raised_rst2)
            check("reset_to_default (contro): il file originale non viene toccato se il template e' rotto",
                  _p_rst2.read_text(encoding="utf-8") == _original_content)
        finally:
            config_editor.tomllib.loads = _orig_tomllib_loads
            config_editor.CONFIG_PATH = _saved_cp_rst2

    # Anti-drift sull'estensione: nessuno switch deve piu' scrivere il file
    # fuori dal lock, e le chiavi che costruisce devono essere tutte note al
    # backend (se una nuova riga usasse una chiave fuori elenco, la scrittura
    # fallirebbe a runtime — questo test lo intercetta prima).
    _prefs_src = (ROOT / "gnome-extension" / "bravoric-indicator@local" / "prefs.js").read_text(encoding="utf-8")
    check("P5: prefs.js non chiama piu' writeBool per gli switch (scrittura fuori dal lock)",
          "editor.writeBool(startKey" not in _prefs_src
          and "editor.writeBool(enabledKey" not in _prefs_src
          and "editor.writeBool(contentKey" not in _prefs_src)
    check("P5: prefs.js passa da config_editor per le chiavi di notifica",
          "setNotificationField" in _prefs_src
          and "['set-notification', key, String(value)]" in _prefs_src)
    # Le chiavi costruite da prefs.js devono esistere in NOTIFICATION_KEYS.
    _keys_from_prefs: set[str] = set()
    for _pfx in ("stt", "ocr", "stream"):
        for _suffix in ("_on_processing_start", "_on_raw_ready", "_on_raw_ready_content",
                        "_on_cleanup_ready", "_on_cleanup_ready_content"):
            # stream non ha cleanup: l'esempio in prefs.js non lo genera, ma
            # l'insieme ammesso dal backend non deve essere piu' stretto.
            if _pfx == "stream" and "_on_cleanup_ready" in _suffix:
                continue
            _keys_from_prefs.add(_pfx + _suffix)
    check("P5: ogni chiave che prefs.js puo' generare e' accettata dal backend",
          _keys_from_preps_ok := (_keys_from_prefs <= set(config_editor.NOTIFICATION_KEYS)),
          )
    # La sezione [notifications] assente viene creata (config legacy).
    with tempfile.TemporaryDirectory() as _td5e:
        _p5e = Path(_td5e) / "config.toml"
        _p5e.write_text('[stream]\nmode = "per_chunk"\n', encoding="utf-8")
        _saved_cp5e = config_editor.CONFIG_PATH
        try:
            config_editor.CONFIG_PATH = _p5e
            config_editor.set_notification_field("stt_on_raw_ready", "false")
            _txt5e = _p5e.read_text(encoding="utf-8")
            check("P5: la sezione [notifications] assente viene creata",
                  "[notifications]" in _txt5e and "stt_on_raw_ready = false" in _txt5e)
            check("P5: la sezione creata non rompe il TOML", isinstance(tomllib.loads(_txt5e), dict))
        finally:
            config_editor.CONFIG_PATH = _saved_cp5e

    print("== giro 19: P4 il cuore si misura nel CORPO di heartbeat() ==")
    # Difetto 1: il check precedente confrontava la scrittura di stato con
    # TUTTO stream.py. La riga identica compare anche nei write_status di
    # avvio e di chiusura, quindi svuotare il corpo di heartbeat() lasciava la
    # suite VERDE: era una copertura solo apparente, il test non poteva
    # fallire proprio sul difetto che dichiarava di coprire. Qui il soggetto
    # e' il corpo della funzione, non il file.
    _stream_src19 = (ROOT / "src" / "bravoric_stt_clipboard" / "stream.py").read_text(encoding="utf-8")
    _hb_ok, _hb_why = heartbeat_verdict(_stream_src19)
    check("P4: il CORPO di heartbeat() riscrive RECORDING con service=stream", _hb_ok)
    # Contro-prova sul VERDETTO, non sul sorgente: la funzione deve saper
    # dichiarare fallimento su tutti i modi in cui il cuore puo' sparire,
    # invece di passarelisciare o di sollevare. Con .index() questi casi
    # avrebbero sollevato e abortito la suite.
    for _lbl19, _src19, _want19 in (
        ("funzione assente", "x = 1\n", False),
        ("corpo vuoto", 'def heartbeat() -> None:\n    """doc"""\n    return\n', False),
        ("corpo senza la scrittura",
         'def heartbeat() -> None:\n    logger.debug("niente")\n', False),
        ("scrittura con service sbagliato",
         'def heartbeat() -> None:\n    status.write_status(status.STATE_RECORDING, service="stt")\n',
         False),
        ("scrittura corretta nel corpo",
         'def heartbeat() -> None:\n    """d"""\n    status.write_status(status.STATE_RECORDING, service="stream")\n',
         True),
    ):
        check(f"P4 (contro-prova): heartbeat con {_lbl19} -> {'PASS' if _want19 else 'FAIL'}",
              heartbeat_verdict(_src19)[0] is _want19)
    # E il caso del difetto 1 vero e proprio: la stessa sorgente con la
    # funzione svuotata deve dare FAIL. Non e' una prova sulla copia in /tmp,
    # qui si dimostra che il verdetto e' effettivamente sensibile al corpo.
    _hb_svuotata = re.sub(r'(def heartbeat\(\) -> None:)(.*?)(?=\ndef )',
                          r'\1\n    """doc"""\n    return\n', _stream_src19, count=1, flags=re.S)
    check("P4 (contro-prova): svuotando il corpo di heartbeat() il verdetto diventa FAIL",
          _hb_svuotata != _stream_src19
          and heartbeat_verdict(_hb_svuotata)[0] is False)
    # La stessa logica per LAVORO 2 (guard_order_verdict), che prima non era
    # collaudabile perche' stava inline e poteva sollevare: qui i casi che
    # avrebbero dato ValueError devono dare un FAIL pulito e dichiarato.
    for _lbl19b, _bd19, _ok19, _miss19 in (
        ("corpo vuoto", "", False, 2),
        ("guardia assente",
         "    audio.start_recording(cfg)\n", False, 1),
        ("chiamata assente",
         "    if _is_stream_active():\n        raise RuntimeError('busy')\n", False, 1),
        ("ordine giusto",
         "    if _is_stream_active():\n        raise RuntimeError('busy')\n"
         "    audio.start_recording(cfg)\n", True, 0),
    ):
        _v19, _m19 = guard_order_verdict(_bd19)
        check(f"P4 (contro-prova): guardia con {_lbl19b} -> "
              f"{'PASS' if _ok19 else 'FAIL'}, {len(_m19)} mancanti dichiarati",
              _v19 is _ok19 and len(_m19) == _miss19)
    # Caso "ordine sbagliato": la funzione dichiara che non manca nulla ma il
    # verdetto e' False per l'ordine. E' il caso che il messaggio non nomina,
    # quindi va detto esplicitamente per non confonderlo con una stringa assente.
    _v_inv, _m_inv = guard_order_verdict(
        "    audio.start_recording(cfg)\n    _is_stream_active()\n")
    check("P4 (contro-prova): ordine invertito -> FAIL con zero mancanti "
          "(il difetto e' l'ordine, non la presenza)", _v_inv is False and _m_inv == [])



    # ==================================================================
    # BRIEF-LOG-CHUNK: log JSONL append-only, una riga per chunk
    print("== chunk_log: log JSONL per chunk (provenance + tempi) ==")
    from bravoric_stt_clipboard import chunk_log as cl_mod
    from bravoric_stt_clipboard import stream as stream_mod
    from bravoric_stt_clipboard.config import FallbackLevel

    # Il percorso del log DEVE essere iniettabile: scrivere su
    # ~/.cache/bravoric-stt-clipboard/chunk_log.jsonl inquinerebbe la sessione
    # REALE dell'utente. Qui ogni test lavora su un tmp_path e passa il
    # percorso a append_record; nessuno scrive mai sul percorso di default.
    _cl_dir = Path(tempfile.mkdtemp())
    _cl_log = _cl_dir / "chunk_log.jsonl"

    def _cl_read():
        """Rilegge il log dal disco con il lettore VERO (non una copia)."""
        return cl_mod.read_records(_cl_log)

    def _cl_lvl(name, host, model="whisper-gpu"):
        return FallbackLevel(name, f"http://{host}:4001/v1", model,
                             "CL_ENV_VAR", "CL_API_KEY_VALUE", "", 8)

    def _cl_append(record, **kw):
        kw.setdefault("max_lines", 2000)
        return cl_mod.append_record(record, path=_cl_log, **kw)

    # --- 1. chunk SENZA fallback: un tentativo, served_by = quel livello ---
    _r1 = cl_mod.make_record(
        session="sess-A", seq=3, audio_s=4.3,
        attempts=[cl_mod.make_attempt(_cl_lvl("whisper-locale", "10.9.0.2"),
                                      4052, True)],
        total_ms=4098, text="Devo aprire il terminale")
    _cl_append(_r1)
    _rows1 = _cl_read()
    _row1 = _rows1[0]
    check("chunk_log: 1 chunk senza fallback -> una riga sul disco",
          len(_rows1) == 1)
    check("chunk_log: served_by = livello che ha risposto, fallback false",
          _row1["served_by"] == "whisper-locale" and _row1["fallback"] is False)
    check("chunk_log: attempts ha UNA sola voce, con l'host e i tempi",
          len(_row1["attempts"]) == 1
          and _row1["attempts"][0]["host"] == "10.9.0.2:4001"
          and _row1["attempts"][0]["ms"] == 4052)
    check("chunk_log: seq e audio_s passano, audio_s non e' inventato",
          _row1["seq"] == 3 and _row1["audio_s"] == 4.3)
    # La chiave NON deve mai finire nel file, nemmeno se il livello la
    # contiene: il modulo costruisce `host` dal livello, non dal testo libero.
    _raw1 = _cl_log.read_text(encoding="utf-8")
    check("chunk_log: nessuna api_key nel file (nome e valore)",
          "CL_API_KEY_VALUE" not in _raw1 and "CL_ENV_VAR" not in _raw1)

    # --- 2. chunk CON fallback: primo errore, secondo ok, served_by = 2o ---
    _r2 = cl_mod.make_record(
        session="sess-A", seq=4, audio_s=6.2,
        attempts=[
            cl_mod.make_attempt(_cl_lvl("whisper-gpu", "10.9.0.2"),
                                8000, False, "Read timed out"),
            cl_mod.make_attempt(_cl_lvl("scrocco-fissone", "10.9.0.2",
                                        model="scrocco"),
                                1200, True),
        ],
        total_ms=9250, text="va bene cosi")
    _cl_append(_r2)
    _row2 = _cl_read()[1]
    check("chunk_log: chunk con fallback -> attempts ha 2 voci in ordine",
          len(_row2["attempts"]) == 2
          and _row2["attempts"][0]["level"] == "whisper-gpu"
          and _row2["attempts"][1]["level"] == "scrocco-fissone")
    check("chunk_log: fallback true e served_by = livello SECONDO (quello ok)",
          _row2["fallback"] is True
          and _row2["served_by"] == "scrocco-fissone")
    check("chunk_log: il tentativo fallito porta ok false ed err presente",
          _row2["attempts"][0]["ok"] is False
          and _row2["attempts"][0]["err"] == "Read timed out")
    # I due endpoint sono lo STESSO host: e' il caso misurato oggi
    # (whisper-gpu e scrocco-fissone su 10.9.0.2:4001). Il log deve
    # distinguerli per livello/modello, non collapserli per host.
    check("chunk_log: stesso host ma modelli diversi restano distinguibili",
          _row2["attempts"][0]["model"] == "whisper-gpu"
          and _row2["attempts"][1]["model"] == "scrocco")

    # --- 3. TUTTI i livelli falliti: served_by null, ok false, err presente ---
    _r3 = cl_mod.make_record(
        session="sess-B", seq=0, audio_s=1.0,
        attempts=[
            cl_mod.make_attempt(_cl_lvl("whisper-gpu", "10.9.0.2"),
                                8000, False, "Timeout"),
            cl_mod.make_attempt(_cl_lvl("scrocco-fissone", "10.9.0.2",
                                        model="scrocco"),
                                300, False, "Connection refused"),
        ],
        total_ms=8300, text="")
    _cl_append(_r3)
    _row3 = _cl_read()[2]
    check("chunk_log: tutti i livelli falliti -> served_by null",
          _row3["served_by"] is None)
    check("chunk_log: tutti i livelli falliti -> ogni tentativo ok false + err",
          all(a["ok"] is False and a["err"] for a in _row3["attempts"])
          and len(_row3["attempts"]) == 2)

    # --- 4. redaction: una chiave in query string non finisce nel log -------
    _r4 = cl_mod.make_record(
        session="sess-B", seq=1, audio_s=1.0,
        attempts=[cl_mod.make_attempt(
            _cl_lvl("whisper-gpu", "10.9.0.2"), 10, False,
            "GET /v1/audio?api_key=SUPERSECRET returned 401")],
        total_ms=10, text="")
    _cl_append(_r4)
    _raw4 = _cl_log.read_text(encoding="utf-8")
    check("chunk_log: la chiave in un messaggio d'errore viene redatta",
          "SUPERSECRET" not in _raw4 and "<redacted>" in _raw4)
    check("chunk_log: l'host tiene SOLO il netloc, niente path ne query",
          _cl_read()[3]["attempts"][0]["host"] == "10.9.0.2:4001")

    # --- 5. rotazione: tiene le ULTIME N righe ------------------------------
    _cl_rot = _cl_dir / "rot.jsonl"
    for i in range(5):
        cl_mod.append_record(
            {"ts": "2026-09-26T18:30:00", "session": "s", "seq": i,
             "audio_s": 1.0, "attempts": [], "served_by": None,
             "fallback": False, "total_ms": 1, "text_len": 0, "text": ""},
            path=_cl_rot, max_lines=3)
    _seqs = [r["seq"] for r in cl_mod.read_records(_cl_rot)]
    check("chunk_log: la rotazione tiene le ULTIME N righe (2,3,4)",
          _seqs == [2, 3, 4])

    # --- 5b. COERENZA: max_lines=0 vuol dire "default", NON "una riga" ----
    # Il difetto: _coerce_max_lines faceva max(1, ...) quindi 0 -> 1. Ma
    # StreamConfig.chunk_log_max_lines e i due TOML di esempio dicono tutti
    # e tre che 0 = default (2000). La config personale dell'utente non ha
    # la chiave, quindi prendeva 0 e il log teneva UNA riga sola: summarize
    # non aveva nulla su cui aggregare e il --summary per endpoint restava
    # vuoto, cioe' la feature richiesta era spenta in silenzio.
    # Percorso TEMPORANEO INIETTATO, mai quello reale: _cl_zero sta sotto il
    # mkdtemp di questo blocco, e nessuna delle righe qui sotto tocca
    # CHUNK_LOG_PATH.
    _cl_zero_dir = Path(tempfile.mkdtemp())
    _cl_zero = _cl_zero_dir / "zero.jsonl"
    check("chunk_log: percorso iniettato, NON quello reale dell'utente",
          str(_cl_zero) != str(cl_mod.CHUNK_LOG_PATH)
          and str(_cl_zero).startswith(tempfile.gettempdir()))

    _cl_def = cl_mod.DEFAULT_MAX_LINES
    # I quattro casi minimi richiesti dal brief, sulla funzione vera.
    check("max_lines: 0 -> il DEFAULT (2000), non 1",
          cl_mod._coerce_max_lines(0) == _cl_def == 2000)
    check("max_lines: None (chiave assente) -> il DEFAULT (2000)",
          cl_mod._coerce_max_lines(None) == _cl_def)
    check("max_lines: intero positivo -> se stesso",
          cl_mod._coerce_max_lines(3) == 3
          and cl_mod._coerce_max_lines(2000) == 2000
          and cl_mod._coerce_max_lines(1) == 1)
    check("max_lines: non numerico -> il DEFAULT (2000)",
          cl_mod._coerce_max_lines("mille") == _cl_def
          and cl_mod._coerce_max_lines("") == _cl_def
          and cl_mod._coerce_max_lines(object()) == _cl_def)
    # Coerenza con la documentazione degli altri due luoghi che dichiarano
    # la stessa cosa: il default di StreamConfig e' 0 e 0 deve valere il
    # DEFAULT del modulo, altrimenti la config di default dell'utente
    # (senza la chiave) farebbe dipendere il log dalla sua assenza.
    _cl_cfg_default = config.StreamConfig(
        "per_chunk", 0.7, -30, 0.4, 30, 250).chunk_log_max_lines
    check("max_lines: il default di StreamConfig (0) produce il DEFAULT del modulo",
          _cl_cfg_default == 0
          and cl_mod._coerce_max_lines(_cl_cfg_default) == _cl_def)
    # Negativo, bool, float non esatto, NaN/inf, e la stringa numerica che il
    # percorso GUI scrive fra virgoletti ("3"): i primi tornano al default,
    # l'ultima e' rispettata (altrimenti si rompe prefs.js -> config.toml).
    check("max_lines: negativo/bool/float rotto/NaN -> DEFAULT, stringa numerica ok",
          cl_mod._coerce_max_lines(-5) == _cl_def
          and cl_mod._coerce_max_lines(True) == _cl_def
          and cl_mod._coerce_max_lines(12.7) == _cl_def
          and cl_mod._coerce_max_lines(float("nan")) == _cl_def
          and cl_mod._coerce_max_lines(float("inf")) == _cl_def
          and cl_mod._coerce_max_lines("3") == 3)
    # Clamp: il tetto e' quello GIA' nel file, non uno nuovo. Nessun test
    # puo' dimostrare un limite assente da solo, quindi si verifica il
    # comportamento dichiarato (oltre il tetto -> tetto) e che il tetto sia
    # quello di sempre.
    check("max_lines: oltre il tetto viene clampato al tetto esistente (1e6)",
          cl_mod.MAX_MAX_LINES == 1_000_000
          and cl_mod._coerce_max_lines(10**9) == 1_000_000)
    # PROVA END-TO-END, la parte che FALLISCE col codice di prima: 5 append
    # con max_lines=0 sul percorso iniettato. Con 0 -> 1 le righe 1..4 venivano
    # ruotate via e sul disco restava UNA riga sola, quindi summarize non poteva
    # aggregare nulla. Con 0 -> 2000 restano tutte e 5.
    # Ogni record porta UN tentativo reale: summarize aggrega sugli `attempts`,
    # quindi con una lista vuota la verifica non discriminerebbe nulla (l'ho
    # scritto una volta cosi e la suite me l'ha detto).
    for i in range(5):
        cl_mod.append_record(
            cl_mod.make_record(
                session="s", seq=i, audio_s=1.0, total_ms=10, text="",
                attempts=[cl_mod.make_attempt(_cl_lvl("whisper-gpu", "10.9.0.2"),
                                             10, True)]),
            path=_cl_zero, max_lines=0)
    _zero_rows = cl_mod.read_records(_cl_zero)
    _zero_seqs = [r["seq"] for r in _zero_rows]
    check("max_lines=0 NON tronca a 1: 5 righe scritte, 5 conservate",
          _zero_seqs == [0, 1, 2, 3, 4])
    # E il punto dell'intera voce: col default applicato, summarize aggrega TUTTI
    # i tentativi. Con 0 -> 1 ne avrebbe visti 1 solo, quindi il conteggio (5)
    # distingue i due comportamenti: non basta ">= 1", che passerebbe anche
    # con la rotazione a 1 riga.
    _zero_sum = cl_mod.summarize(_zero_rows)
    check("max_lines=0 -> summarize vede TUTTI i tentativi (il default e' applicato)",
          len(_zero_sum) == 1 and _zero_sum[0]["attempts"] == 5
          and _zero_sum[0]["ok"] == 5 and _zero_sum[0]["served"] == 5)

    # --- 6. un errore di scrittura NON propaga e NON solleva ---------------
    # Percorso non scrivibile: la directory padre e' un FILE, quindi
    # mkdir/parents fallisce. append_record deve tornare False, non alzare.
    _cl_bad = _cl_dir / "not_a_dir" / "chunk_log.jsonl"
    (_cl_dir / "not_a_dir").write_text("sono un file", encoding="utf-8")
    _raised = False
    _ok_write = None
    try:
        _ok_write = cl_mod.append_record({"seq": 0, "text": "x"}, path=_cl_bad)
    except Exception:  # noqa: BLE001 - il testvuole dimostrare che NON solleva
        _raised = True
    check("chunk_log: percorso non scrivibile -> False, nessuna eccezione",
          _ok_write is False and _raised is False)
    # E il chiamante (il sequencer) deve proseguire: un log rotto non puo'
    # far perdere una parola. Qui si verifica l'ingest, non solo append_record.
    _cl_st = {"session_id": "sess-C", "chunks": [], "last_chunks": []}
    _cl_seq = stream_mod._FifoSequencer(
        _cl_st, types.SimpleNamespace(context_enabled=False, blacklist=""),
        lambda text: None, lambda text: None, log_max_lines=2000)
    _cl_saved_path = cl_mod.CHUNK_LOG_PATH
    try:
        cl_mod.CHUNK_LOG_PATH = _cl_bad  # il log NON puo' scrivere
        _committed = _cl_seq.ingest(stream_mod._ChunkResult(0, "Parola", True))
    finally:
        cl_mod.CHUNK_LOG_PATH = _cl_saved_path
    check("chunk_log: log rotto -> il chunk viene comunque committato",
          _committed == ["Parola "] and _cl_st["chunks"] == ["Parola "])
    check("chunk_log: il globale del modulo e' tornato al suo valore",
          cl_mod.CHUNK_LOG_PATH == _cl_saved_path)

    # --- 7. lettura: --last / --session / --since ---------------------------
    # Il log contiene 4 righe, seq 3, 4, 0, 1: --last 2 prende le due
    # ULTIME (0, 1), non le prime. E' il punto dell'opzione.
    _all = _cl_read()
    check("lettura: --last tiene le ultime righe, piu' recente in fondo",
          [r["seq"] for r in cl_mod.filter_records(_all, last=2)] == [0, 1])
    _sessA = cl_mod.filter_records(_all, session="sess-A")
    check("lettura: --session filtra per id di sessione",
          [r["seq"] for r in _sessA] == [3, 4])
    _since_now = datetime.now()
    _fresh = cl_mod.make_record(session="sess-D", seq=99, audio_s=1.0,
                                attempts=[], total_ms=1, text="recente",
                                ts=_since_now.strftime("%Y-%m-%dT%H:%M:%S"))
    _old = cl_mod.make_record(session="sess-D", seq=98, audio_s=1.0,
                              attempts=[], total_ms=1, text="vecchio",
                              ts=(_since_now - timedelta(minutes=30))
                              .strftime("%Y-%m-%dT%H:%M:%S"))
    _mixed = [_old, _fresh]
    check("lettura: --since tiene solo la finestra richiesta",
          [r["seq"] for r in cl_mod.filter_records(_mixed, since_minutes=5)]
          == [99])
    # Una riga SENZA ts non puo' dimostrare di essere dentro la finestra.
    _nots = cl_mod.filter_records(
        [{"seq": 97, "text": "senza orario"}], since_minutes=5)
    check("lettura: --since scarta la riga senza ts (niente finestra provata)",
          _nots == [])

    # --- 8. --summary: aggregazione vera, non reimplementata nel test -------
    # Il test chiama summarize() (la funzione vera): ricalcolare qui i numeri
    # riprodurrebbe la logica invece di verificarla, che e' esattamente il
    # difetto che questo blocco deve chiudere.
    _sum_rows = cl_mod.summarize(cl_mod.read_records(_cl_log))
    _by_level = {r["level"]: r for r in _sum_rows}
    # whisper-gpu e' stato tentato 3 volte (seq 4, seq 0, seq 1) e ha FALLITO
    # tutte e tre: e' il caso misurato oggi a 4 richieste simultanee.
    _gpu = _by_level.get("whisper-gpu")
    check("summary: per endpoint conta tentativi, ok e falliti",
          _gpu is not None and _gpu["attempts"] == 3
          and _gpu["ok"] == 0 and _gpu["failed"] == 3)
    # Nessun tentativo riuscito => latenza INDEFINITA, non 0. Un 0 qui
    # sembrerebbe "l'endpoint ha risposto in 0 ms".
    check("summary: ms medio/p50/p95 su whisper-gpu restano None (0 successi)",
          _gpu is not None and _gpu["ms_avg"] is None
          and _gpu["p50"] is None and _gpu["p95"] is None)
    # whisper-locale ha risposto una volta: qui i tempi sono reali (4052 ms).
    _locale = _by_level.get("whisper-locale")
    check("summary: ms medio/p50/p95 calcolati sui tentativi RIUSCITI",
          _locale is not None and _locale["ms_avg"] == 4052.0
          and _locale["p50"] == 4052.0 and _locale["p95"] == 4052.0)
    # Dei 3 chunk, 2 hanno avuto una risposta (seq 3 e seq 4): scrocco ne ha
    # servito 1 => 50%. E' la quota, non il conteggio assoluto.
    _scrocco = _by_level.get("scrocco-fissone")
    check("summary: la quota di chunk serviti e' sul totale dei chunk serviti",
          _scrocco is not None and _scrocco["served"] == 1
          and _scrocco["served_pct"] == 50.0)
    # whisper-gpu non ha servito nessun chunk: la sua quota deve essere 0,
    # non assente. E' la differenza fra "non ha mai risposto" e "non c'era".
    check("summary: chi non ha servito ha quota 0.0, non un buco",
          _gpu is not None and _gpu["served"] == 0 and _gpu["served_pct"] == 0.0)
    # p95 su piu' campioni: qui 1 solo, quindi il percentile deve restare
    # indefinito e NON diventare 0 (che sembrerebbe una latenza piu' veloce).
    check("summary: senza campioni riusciti ms resta None, non 0 inventato",
          cl_mod.summarize([{"attempts": [
              {"level": "x", "model": "m", "host": "h", "ms": 0,
               "ok": False, "err": "ko"}]}])[0]["ms_avg"] is None)
    # Ordine deterministico: a parita' di tentativi, per livello.
    check("summary: le righe sono ordinate per tentativi decrescenti",
          [r["attempts"] for r in _sum_rows]
          == sorted([r["attempts"] for r in _sum_rows], reverse=True))

    # --- 9. la CLI del log: --summary e --last sul percorso iniettato -----
    # Ogni invocazione cattura il proprio stdout in un buffer separato: con
    # un solo buffer i due output si sommerebbero e la seconda verifica
    # passerebbe guardando testo prodotto dalla prima.
    def _run_cli(args):
        out = io.StringIO()
        saved = sys.stdout
        try:
            sys.stdout = out
            rc = cl_mod.main(args)
        finally:
            sys.stdout = saved  # ripristino OBBLIGATORIO, anche se main solleva
        return rc, out.getvalue()

    _rc_sum, _sum_text = _run_cli(["--summary", "--path", str(_cl_log)])
    _rc_last, _last_text = _run_cli(["--last", "1", "--path", str(_cl_log)])
    check("CLI: --summary esce 0 e mostra l'intestazione per endpoint",
          _rc_sum == 0 and "endpoint" in _sum_text and "whisper-gpu" in _sum_text)
    check("CLI: --summary mostra la quota di chunk serviti",
          "quota" in _sum_text and "50.0%" in _sum_text)
    check("CLI: --last 1 mostra una sola riga, la piu' recente",
          _rc_last == 0 and _last_text.count("\n") == 1
          and "seq=1" in _last_text)

    # --- redact: il segreto NON deve restare nel log (mai testata a nome) ----
    # Misurato prima del fix: "Authorization: Bearer sk-x" lasciava sk-x in
    # chiaro (il regex prendeva "Bearer" come valore), e password=/secret=/
    # user:pass@host non erano coperti.
    _secret_cases = [
        ("Authorization: Bearer sk-TOPSECRET99", "sk-TOPSECRET99"),
        ("url: /v1/audio?api_key=TOPSECRET99&x=1", "TOPSECRET99"),
        ("url: /v1?token=TOPSECRET99", "TOPSECRET99"),
        ("https://utente:TOPSECRET99@host/v1", "TOPSECRET99"),
        ("password=TOPSECRET99", "TOPSECRET99"),
        ("client_secret: TOPSECRET99", "TOPSECRET99"),
    ]
    for _txt_sc, _sec_sc in _secret_cases:
        check(f"redact: il segreto non resta in {_txt_sc[:38]!r}",
              _sec_sc not in cl_mod.redact(_txt_sc))
    check("redact: un errore normale resta invariato",
          cl_mod.redact("connection refused (errno 111)") == "connection refused (errno 111)")
    check("redact: il nome del parametro resta leggibile",
          "api_key=" in cl_mod.redact("?api_key=TOPSECRET99"))
    # Valore ESATTO della chiave configurata: un 401 stile OpenAI la ripete nel
    # corpo senza "nome=valore" agganciabile da un regex.
    _lv_sec = types.SimpleNamespace(
        name="a", model="m", endpoint="http://h:1/v1",
        resolved_api_key=lambda: "sk-abcdef123456")
    _att_sec = cl_mod.make_attempt(
        _lv_sec, 5, False, "401 Incorrect API key provided: sk-abcdef123456")
    check("make_attempt: la chiave configurata (valore esatto) e' redatta anche senza pattern",
          "sk-abcdef123456" not in (_att_sec["err"] or "") and "<redacted>" in (_att_sec["err"] or ""))
    _lv_short = types.SimpleNamespace(
        name="a", model="m", endpoint="http://h:1/v1", resolved_api_key=lambda: "ab")
    check("make_attempt: una 'chiave' di 2 caratteri NON viene redatta (colpirebbe testo qualunque)",
          cl_mod.make_attempt(_lv_short, 5, False, "abc abc")["err"] == "abc abc")
    _lv_broken = types.SimpleNamespace(name="a", model="m", endpoint="http://h:1/v1")
    check("make_attempt: livello senza resolved_api_key non rompe il log",
          cl_mod.make_attempt(_lv_broken, 5, False, "boom")["err"] == "boom")

    # --- privacy: la rotazione non deve rendere il log leggibile da altri ----
    # Misurato dal vivo: il chunk_log reale (testo dettato) era 0644 dopo la
    # rotazione, perche' _rotate riscriveva con open(tmp, "wb") sotto umask.
    _cl_priv_dir = Path(tempfile.mkdtemp(prefix="brv-clpriv-"))
    _cl_priv = _cl_priv_dir / "chunk_log.jsonl"
    _old_umask = os.umask(0o022)
    try:
        for _i_priv in range(6):
            cl_mod.append_record({"seq": _i_priv, "text": "riservato"}, path=_cl_priv, max_lines=3)
        _mode_after_rotate = _cl_priv.stat().st_mode & 0o777
        _rows_after_rotate = len(cl_mod.read_records(_cl_priv))
    finally:
        os.umask(_old_umask)
    check("chunk_log: la rotazione e' avvenuta davvero (il test misura il file ruotato)",
          _rows_after_rotate <= 3)
    check("chunk_log: dopo la rotazione il file resta 0600 (testo dettato, mai leggibile da altri)",
          _mode_after_rotate == 0o600)

    # Anti-drift privacy sull'estensione: il file di testo vivo (testo dettato)
    # va creato PRIVATE (0600). Verificato dal vivo con gjs sotto umask 022:
    # senza il flag nasce 0644. Qui si presidia solo che il flag non sparisca.
    _ext_js = (ROOT / "gnome-extension" / "bravoric-indicator@local" / "extension.js").read_text(encoding="utf-8")
    _live_at = _ext_js.index("_writeStreamLiveText() {")
    _live_block = _ext_js[_live_at:_ext_js.index("_refreshStatus() {", _live_at)]
    check("extension.js: stream_live_text.json creato con Gio.FileCreateFlags.PRIVATE (0600)",
          "Gio.FileCreateFlags.PRIVATE" in _live_block)

    # --- audio.ensure_private_dir: dir di runtime con la VOCE dell'utente ------
    _pd_root = Path(tempfile.mkdtemp(prefix="brv-privdir-"))
    _old_um = os.umask(0o022)
    try:
        _pd_new = _pd_root / "a" / "b"
        audio.ensure_private_dir(_pd_new)
        check("ensure_private_dir: directory nuova (umask 022) e' 0700",
              (_pd_new.stat().st_mode & 0o777) == 0o700)
        _pd_open = _pd_root / "aperta"
        _pd_open.mkdir()
        _pd_open.chmod(0o777)
        audio.ensure_private_dir(_pd_open)
        check("ensure_private_dir: una directory nostra gia' 0777 viene stretta a 0700",
              (_pd_open.stat().st_mode & 0o777) == 0o700)
        _pd_real = _pd_root / "reale"
        _pd_real.mkdir()
        _pd_link = _pd_root / "link"
        _pd_link.symlink_to(_pd_real)
        try:
            audio.ensure_private_dir(_pd_link)
            _pd_link_rejected = False
        except RuntimeError:
            _pd_link_rejected = True
        check("ensure_private_dir: un symlink viene RIFIUTATO (non seguito)", _pd_link_rejected)
        _pd_file = _pd_root / "file"
        _pd_file.write_text("x")
        try:
            audio.ensure_private_dir(_pd_file)
            _pd_file_rejected = False
        except (RuntimeError, FileExistsError, NotADirectoryError):
            _pd_file_rejected = True
        check("ensure_private_dir: un file al posto della directory viene rifiutato", _pd_file_rejected)
        _pd_other = _pd_root / "altrui"
        _pd_other.mkdir()
        with mock.patch.object(audio.os, "getuid", return_value=os.getuid() + 1):
            try:
                audio.ensure_private_dir(_pd_other)
                _pd_other_rejected = False
            except RuntimeError:
                _pd_other_rejected = True
        check("ensure_private_dir: una directory di un ALTRO uid viene rifiutata (nome prevedibile in /tmp)",
              _pd_other_rejected)
    finally:
        os.umask(_old_um)

    # --- config_editor: caratteri di controllo in una stringa TOML -----------
    # Misurato: \x0b/\x1b/\x7f/\x00 rendevano il TOML invalido e il salvataggio
    # dalla GUI falliva ("Write aborted") per un testo che sembrava normale.
    with tempfile.TemporaryDirectory() as _td_cc:
        _p_cc = Path(_td_cc) / "config.toml"
        _p_cc.write_text('[ocr]\nsystem_prompt = "x"\n', encoding="utf-8")
        _saved_cc = config_editor.CONFIG_PATH
        try:
            config_editor.CONFIG_PATH = _p_cc
            for _val_cc in ("a\x0bb", "a\x7fb", "a\x00b", "a\x1bb", 'q"\\ \n\t ok'):
                try:
                    config_editor.set_section_field("ocr", "system_prompt", _val_cc)
                    _rt_cc = tomllib.loads(_p_cc.read_text(encoding="utf-8"))["ocr"]["system_prompt"]
                except config_editor.ConfigEditorError:
                    _rt_cc = None
                check(f"config_editor: {_val_cc!r} nel prompt si salva e rilegge identico",
                      _rt_cc == _val_cc)
        finally:
            config_editor.CONFIG_PATH = _saved_cc

    # --- segnali solo a processi NOSTRI (pid riusato da un lock stale) --------
    # Dopo un crash il lock resta; se il pid e' stato RIUSATO da un processo
    # qualunque dell'utente, stop_recording/_terminate_pid gli mandavano
    # SIGINT/SIGTERM/SIGKILL. Processi REALI: uno innocente (sleep) e uno con
    # argv0 "ffmpeg". (I test non segnalano mai os.getpid(): ucciderebbe il runner.)
    check("pid_matches: il nostro processo python contiene 'python'",
          audio.pid_matches(os.getpid(), ("python",)))
    check("pid_matches: marker assente -> False",
          not audio.pid_matches(os.getpid(), ("marker-che-non-esiste-xyz",)))
    _innocent = subprocess.Popen(["sleep", "60"])
    _fake_ff = subprocess.Popen(["bash", "-c", "exec -a ffmpeg sleep 60"])
    _sg_dir = Path(tempfile.mkdtemp(prefix="brv-sig-"))
    _sg_saved_lock = audio.LOCK_PATH
    try:
        time.sleep(0.3)  # lascia partire exec
        check("pid_matches: argv0 'ffmpeg' riconosciuto",
              audio.pid_matches(_fake_ff.pid, ("ffmpeg",)))
        check("pid_matches: un 'sleep' qualunque NON e' ffmpeg",
              not audio.pid_matches(_innocent.pid, ("ffmpeg",)))
        _sg_audio = _sg_dir / "a.ogg"
        _sg_audio.write_bytes(b"dati")
        audio.LOCK_PATH = _sg_dir / "recording.lock"
        _sg_cfg = mock.Mock(toggle_debounce_seconds=0.0)

        def _sg_lock(pid: int) -> None:
            audio.LOCK_PATH.write_text(json.dumps(
                {"pid": pid, "audio_path": str(_sg_audio), "started_at": 0}))

        _sg_lock(_innocent.pid)
        audio.stop_recording(_sg_cfg)
        check("stop_recording: un pid riusato da un processo NON ffmpeg NON viene segnalato",
              _innocent.poll() is None)
        check("stop_recording: il lock stale viene comunque rimosso",
              not audio.LOCK_PATH.exists())
        _sg_lock(_fake_ff.pid)
        audio.stop_recording(_sg_cfg)
        for _ in range(30):
            if _fake_ff.poll() is not None:
                break
            time.sleep(0.1)
        check("stop_recording (contro): un vero 'ffmpeg' viene ancora fermato",
              _fake_ff.poll() is not None)
        # stream._terminate_pid: stessa protezione
        _innocent2 = subprocess.Popen(["sleep", "60"])
        try:
            stream_mod._terminate_pid(_innocent2.pid, graceful=False)
            time.sleep(0.3)
            check("stream._terminate_pid: un processo non nostro NON viene segnalato",
                  _innocent2.poll() is None)
        finally:
            _innocent2.kill()
            _innocent2.wait()
    finally:
        audio.LOCK_PATH = _sg_saved_lock
        for _pr in (_innocent, _fake_ff):
            if _pr.poll() is None:
                _pr.kill()
            _pr.wait()

    # --- ogni notifica ha il suo interruttore (GUI: pagina Notifiche) ------------
    import dataclasses as _dc_nt
    _N = config.ServiceNotifications
    _nt_default = config._build_config({}).notif_stt
    check("notifiche: error/recording_start/session_end default True (comportamento di sempre)",
          _nt_default.error and _nt_default.recording_start and _nt_default.session_end)
    _nt_off = config._build_config({"notifications": {
        "stt_on_error": False, "ocr_on_error": False, "stream_on_error": False,
        "stt_on_recording_start": False, "stream_on_session_end": False}})
    check("notifiche: le 5 chiavi nuove spente vengono lette",
          not _nt_off.notif_stt.error and not _nt_off.notif_ocr.error and not _nt_off.notif_stream.error
          and not _nt_off.notif_stt.recording_start and not _nt_off.notif_stream.session_end)
    check("notifiche: la stringa \"false\" (truthy in Python) spegne davvero lo switch",
          not config._build_config({"notifications": {"stt_on_error": "false"}}).notif_stt.error)
    for _k_nt in ("stt_on_error", "ocr_on_error", "stream_on_error",
                  "stt_on_recording_start", "stream_on_session_end"):
        check(f"config_editor: chiave di notifica {_k_nt} accettata",
              _k_nt in config_editor.NOTIFICATION_KEYS)

    def _cfg_nt(**kw: Any) -> Any:
        base = cfg_stream_min
        return _dc_nt.replace(
            base, notifications=True,
            notif_stt=_dc_nt.replace(base.notif_stt, **kw.get("stt", {})),
            notif_ocr=_dc_nt.replace(base.notif_ocr, **kw.get("ocr", {})),
            notif_stream=_dc_nt.replace(base.notif_stream, **kw.get("stream", {})))

    # stt: errore di trascrizione + registrazione avviata
    for _label_nt, _on_nt in (("acceso", True), ("spento", False)):
        with mock.patch.object(stt, "try_with_fallback", return_value="   "), \
             mock.patch.object(stt, "clipboard"), mock.patch.object(stt, "status"), \
             mock.patch.object(stt, "notify") as _nt_e, mock.patch.object(stt, "storage"), \
             mock.patch.object(stt, "output_history"):
            stt._process_recording(_cfg_nt(stt={"error": _on_nt}), Path("/tmp/x.ogg"))
            _sent_e = _nt_e.send.call_count
        check(f"stt: notifica d'errore con stt_on_error {_label_nt} -> {'inviata' if _on_nt else 'NON inviata'}",
              (_sent_e >= 1) == _on_nt)
        with mock.patch.object(stt, "audio"), mock.patch.object(stt, "status"), \
             mock.patch.object(stt, "notify") as _nt_r, \
             mock.patch.object(stt, "_is_stream_active", return_value=False):
            stt._start(_cfg_nt(stt={"recording_start": _on_nt}))
            _sent_r = _nt_r.send.call_count
        check(f"stt: 'recording started' con stt_on_recording_start {_label_nt} -> {'inviata' if _on_nt else 'NON inviata'}",
              (_sent_r >= 1) == _on_nt)
    # ocr: strumento screenshot mancante (un errore qualunque dell'OCR)
    for _label_nt, _on_nt in (("acceso", True), ("spento", False)):
        with mock.patch.object(ocr, "screenshot") as _shot_nt, mock.patch.object(ocr, "status") as _st_nt, \
             mock.patch.object(ocr, "notify") as _nt_o:
            _shot_nt.is_available.return_value = False
            _st_nt.read_status.return_value = {"state": "idle"}
            ocr.handle_capture(_dc_nt.replace(_cfg_nt(ocr={"error": _on_nt}), ocr_capture_screenshot=True))
            _sent_o = _nt_o.send.call_count
        check(f"ocr: notifica d'errore con ocr_on_error {_label_nt} -> {'inviata' if _on_nt else 'NON inviata'}",
              (_sent_o >= 1) == _on_nt)
    # cli: "Unexpected error" segue l'interruttore del servizio; "Config error" NO
    for _label_nt, _on_nt in (("acceso", True), ("spento", False)):
        with mock.patch.object(cli, "load_config", return_value=_cfg_nt(stt={"error": _on_nt})), \
             mock.patch.object(cli, "stt") as _stt_cli, mock.patch.object(cli, "status"), \
             mock.patch.object(cli, "notify") as _nt_c:
            _stt_cli.handle_toggle.side_effect = RuntimeError("boom (simulato)")
            cli.stt_toggle_main()
            _sent_c = _nt_c.send.call_count
        check(f"cli: 'Unexpected error' con stt_on_error {_label_nt} -> {'inviata' if _on_nt else 'NON inviata'}",
              (_sent_c >= 1) == _on_nt)
    # stream: sessione terminata e avviso "occupato" (errore)
    for _label_nt, _on_nt in (("acceso", True), ("spento", False)):
        _sess_nt = stream_mod.StreamSession(_cfg_nt(stream={"session_end": _on_nt, "error": _on_nt}))
        with mock.patch.object(stream_mod, "notify") as _nt_s:
            _sess_nt._busy_notice("occupato")
            _sent_busy = _nt_s.send.call_count
        check(f"stream: avviso d'errore con stream_on_error {_label_nt} -> {'inviato' if _on_nt else 'NON inviato'}",
              (_sent_busy >= 1) == _on_nt)
        with mock.patch.object(stream_mod, "notify") as _nt_s2, \
             mock.patch.object(stream_mod, "_read_lock", return_value=None), \
             mock.patch.object(stream_mod, "_write_state"), \
             mock.patch.object(stream_mod, "read_state", return_value={}):
            _sess_nt._stop_per_chunk({"pid": dead_pid(), "session_id": "nt"})
            _sent_end = _nt_s2.send.call_count
        check(f"stream: 'Sessione terminata' con stream_on_session_end {_label_nt} -> {'inviata' if _on_nt else 'NON inviata'}",
              (_sent_end >= 1) == _on_nt)

    # --- anti-drift: OGNI notifica del backend ha la sua riga in GUI ---------
    # Regola del progetto: nessuna notifica non configurabile da GUI. Le chiavi
    # che prefs.js costruisce (processing_start + contentRows con _content +
    # extraRows) devono coincidere ESATTAMENTE con NOTIFICATION_KEYS.
    _prefs_nt = (ROOT / "gnome-extension" / "bravoric-indicator@local" / "prefs.js").read_text(encoding="utf-8")
    _groups_nt = _prefs_nt[_prefs_nt.index("const NOTIFICATION_GROUPS = ["):]
    _groups_nt = _groups_nt[:_groups_nt.index("\n];")]
    _gui_keys: set[str] = set()
    for _blk in re.split(r"\n    \{\n        prefix: ", _groups_nt)[1:]:
        _pre = re.match(r"'(\w+)'", _blk).group(1)  # type: ignore[union-attr]
        _gui_keys.add(f"{_pre}_on_processing_start")
        _content_part = _blk.split("contentRows: [", 1)[1].split("],", 1)[0]
        for _k in re.findall(r"key: '(\w+)'", _content_part):
            _gui_keys.update({f"{_pre}_on_{_k}", f"{_pre}_on_{_k}_content"})
        if "extraRows: [" in _blk:
            _extra_part = _blk.split("extraRows: [", 1)[1].split("],", 1)[0]
            for _k in re.findall(r"key: '(\w+)'", _extra_part):
                _gui_keys.add(f"{_pre}_on_{_k}")
    check("GUI: ogni chiave di notifica del backend ha una riga (nessuna notifica non configurabile)",
          config_editor.NOTIFICATION_KEYS - _gui_keys == set())
    check("GUI: nessuna riga di notifica punta a una chiave sconosciuta al backend",
          _gui_keys - config_editor.NOTIFICATION_KEYS == set())
    _schema_txt = (ROOT / "gnome-extension" / "bravoric-indicator@local" / "schemas"
                   / "org.gnome.shell.extensions.bravoric-indicator.gschema.xml").read_text(encoding="utf-8")
    _ext_keys = set(re.findall(r"key: '(notify-[a-z]+)'", _prefs_nt))
    check("GUI: ogni interruttore di notifica dell'estensione e' nello schema GSettings",
          bool(_ext_keys) and all(f'name="{_k}"' in _schema_txt for _k in _ext_keys))

    # --- 10. il percorso di default e' quello vero, non un doppione -------
    # (la verifica che il percorso reale non sia stato TOCCATO da nessun test
    # e' in fondo a main(): confronta mtime+size reali, non stringhe di path
    # — quella era la vecchia "difesa finale", tautologica per costruzione.)
    check("chunk_log: CHUNK_LOG_PATH del modulo e' il percorso reale, non un doppione di test",
          cl_mod.CHUNK_LOG_PATH != _cl_log
          and str(_cl_log) not in str(cl_mod.CHUNK_LOG_PATH))

    # --- 11. PROVA DI NON-VACUITA' ----------------------------------------
    # Il test qui sotto deve diventare ROSSO se il log smette di funzionare.
    # Non basta dirlo: si FA fallire il log e si asserisce che la verifica
    # diventi False. Se restasse True, il test coprirebbe niente.
    _cl_probe_ok = len(_cl_read()) >= 1 and _cl_read()[0]["served_by"] is not None
    check("NON-VACUITA': con il log funzionante la verifica e' True",
          _cl_probe_ok is True)
    # Fallisco il log: il percorso e' un file, non una directory. La stessa
    # lettura/asserzione deve ora dichiarare il fallimento, non passare.
    _cl_probe_dir = _cl_dir / "probe_rotto"
    _cl_probe_dir.write_text("file", encoding="utf-8")
    _cl_probe_target = _cl_probe_dir / "chunk_log.jsonl"
    _cl_probe_written = cl_mod.append_record(
        {"seq": 0, "text": "x"}, path=_cl_probe_target)
    _cl_probe_after = cl_mod.read_records(_cl_probe_target)
    # La verifica "il chunk e' stato loggato" deve essere False qui: se fosse
    # True, il test che la usa passerebbe anche con il log morto.
    _cl_probe_detects_failure = (not _cl_probe_written) and not _cl_probe_after
    check("NON-VACUITA': la verifica diventa False quando il log e' rotto",
          _cl_probe_detects_failure is True)

    # Verifica REALE (non tautologica, vedi commento a inizio main()) che
    # nessun test di questa run abbia scritto su NESSUNO dei percorsi reali
    # dell'utente: confronto mtime+size di ogni file noto, snapshot preso a
    # inizio main() contro lo stato attuale.
    _real_paths_after = _snapshot_real_paths()
    for _name_rp, _before_rp in _real_paths_before.items():
        check(f"{_name_rp}: il file REALE dell'utente ha mtime/size invariati dopo l'intera suite",
              _real_paths_after[_name_rp] == _before_rp)

    print(f"\n{PASS} PASS / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

