"""Editor testuale del config.toml, scoped per blocco, per uso da GUI (prefs.js
via subprocess) o CLI. Le chiavi (endpoint/model/...) si ripetono identiche in
12 blocchi [[*.fallback]]: una regex globale le confonderebbe. Ogni scrittura
è validata con tomllib prima di sostituire il file, altrimenti annullata.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path

from .config import ICON_SLOT_KEYS, STREAM_DISPATCH_MODES, _coerce_int

CONFIG_PATH = Path.home() / ".config" / "bravoric-stt-clipboard" / "config.toml"


@contextlib.contextmanager
def _locked():
    """Serializza read-modify-write tra processi config_editor.py concorrenti
    (ogni comando GUI di prefs.js è un processo separato). Senza lock, due
    scritture su campi diversi lanciate vicine nel tempo leggono lo stesso
    config.toml di partenza e l'ultimo replace() vince, perdendo l'altra
    modifica (race confermata dal vivo, giro 13: 2/8 run perdevano un campo).
    CONFIG_PATH è letto qui (non congelato a livello di modulo) perché i test
    lo riassegnano a runtime."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(CONFIG_PATH.parent / ".config.toml.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

SERVICES = {
    "stt": {"array_header": "[[stt.fallback]]", "section_header": "[stt]"},
    "stt_cleanup": {"array_header": "[[stt_cleanup.fallback]]", "section_header": "[stt_cleanup]"},
    "ocr": {"array_header": "[[ocr.fallback]]", "section_header": "[ocr]"},
    "ocr_cleanup": {"array_header": "[[ocr_cleanup.fallback]]", "section_header": "[ocr_cleanup]"},
    "stream": {"array_header": "[[stream.fallback]]", "section_header": "[stream]"},
}

# `parallel` e `max_concurrency` sono campi stream-only: hanno significato solo
# nel pool parallelo del dicttatore, quindi per gli altri servizi restano
# inerte ma leggibili/salvabili senza errori (SPEC-MAX-CONCURRENCY sez. 3).
LEVEL_FIELDS = ["name", "endpoint", "model", "api_key_env", "api_key", "ca_cert", "timeout_seconds", "hotwords_in_prompt", "parallel", "max_concurrency"]

ICON_SLOTS = list(ICON_SLOT_KEYS)

STREAM_FIELDS = {
    "mode": "mode",
    "dispatch_mode": "string",
    "silence_seconds": "float",
    "noise_db": "float",
    "vad_margin_db": "float",
    "min_utterance_seconds": "float",
    "max_utterance_seconds": "float",
    "paste_delay_ms": "int",
    "paste_shortcut": "string",
    "paste_channel": "string",
    "language": "string",
    "prompt": "string",
    "hotwords": "string",
    "context_enabled": "bool",
    "max_concurrent_chunks": "int",
    "chunk_timeout_seconds": "float",
    "blacklist": "string",
    # Ritenzione del log JSONL dei chunk, in RIGHE (non in orari: il file e'
    # uno strumento di debug, non un archivio). "int" passa dal ramo intero
    # gia' presente, quindi il clamp resta in config.py/_coerce_int.
    "chunk_log_max_lines": "int",
}

# Campi [stream] con clamp numerico esplicito (lo, hi): un valore finito fuori
# range viene clampato, un valore invalido/non finito viene rifiutato.
STREAM_FLOAT_CLAMPS = {
    "vad_margin_db": (0.0, 20.0),
}

STORAGE_SECTIONS = {
    "base": "[storage]",
    "stt_original": "[storage.stt_original]",
    "stt_raw": "[storage.stt_raw]",
    "stt_clean": "[storage.stt_clean]",
    "ocr_original": "[storage.ocr_original]",
    "ocr_raw": "[storage.ocr_raw]",
    "ocr_clean": "[storage.ocr_clean]",
}

# Chiavi booleane ammesse dentro [notifications]. Sono le stesse che
# prefs.js costruiva a mano (TomlBoolEditor.writeBool accettava QUALSIASI
# chiave: una stringa iniettata finiva grezza nel TOML). Qui l'insieme e'
# chiuso e deriva dalla lettura di config.py (service_notif), cosi' le due
# estremita' non possono divergere: ogni chiave accettata qui e' anche
# quella che config.py sa interpretare.
NOTIFICATION_KEYS = frozenset(
    f"{prefix}_on_{event}{suffix}"
    for prefix in ("stt", "ocr", "stream")
    for event in ("processing_start", "raw_ready", "cleanup_ready")
    for suffix in ("", "_content")
    # `stream` non ha la notifica di cleanup: config.py non la legge.
    if not (prefix == "stream" and event == "cleanup_ready")
)


class ConfigEditorError(RuntimeError):
    pass


def _toml_line_value(key: str, value: str) -> str:
    if key in ("timeout_seconds", "retention_hours", "max_entries"):
        try:
            return str(int(value))
        except ValueError as exc:
            raise ConfigEditorError(f"{key} must be an integer, got {value!r}") from exc
    if key in ("enabled", "capture_screenshot"):
        return "true" if value in ("true", "True", "1") else "false"
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _find_block_bounds(lines: list[str], header: str, occurrence: int) -> tuple[int, int]:
    """Trova (start, end) esclusivo del blocco N-esimo (0-based) che inizia
    con `header`, fino alla prossima riga che inizia con '[' o EOF."""
    starts = [i for i, line in enumerate(lines) if line.strip() == header]
    if occurrence >= len(starts):
        raise ConfigEditorError(f"Block '{header}' occurrence {occurrence} not found")
    start = starts[occurrence]
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("["):
            end = i
            break
    return start, end


def _find_or_insert_key_in_block(
    lines: list[str], start: int, end: int, key: str, new_value: str,
    *, key_regex: str | None = None, skip_trailing_blank: bool = True,
) -> None:
    """Sostituisce `key` nel blocco se c'è già, altrimenti la inserisce
    (P13, mandato perfetto: 5 copie byte-quasi-identiche consolidate qui;
    set_storage_field e set_history_max_entries migrate dopo, avevano un
    `_replace_key_in_block` gemello che sollevava se la chiave mancava in
    un blocco già esistente — config più vecchie di un campo si rompevano
    al primo salvataggio da GUI invece di limitarsi a inserirlo).

    La chiave assente non è un errore: i config creati prima
    dell'introduzione di un campo non devono rompersi al primo salvataggio
    da GUI.

    Due varianti PRESERVATE esattamente, non uniformate (cambierebbero il
    TOML prodotto, che 5 punti del gate asseriscono byte per byte):
    - `key_regex`: `set_notification_field` usa `^{key}\\s*=` invece di
      `^{key} = .*$` (nessuna verifica sul resto della riga). Default None =
      usa il pattern standard.
    - `skip_trailing_blank`: `set_icon_field` inserisce sempre a `end`,
      senza guardare se `lines[end-1]` è una riga vuota da saltare (gli
      altri 4 siti lo fanno). Default True = comportamento della maggioranza.
    """
    pattern = re.compile(key_regex if key_regex is not None else rf"^{re.escape(key)} = .*$")
    for i in range(start, end):
        if pattern.match(lines[i]):
            lines[i] = f"{key} = {new_value}"
            return
    if skip_trailing_blank:
        insert_at = end - 1 if end > start and lines[end - 1] == "" else end
    else:
        insert_at = end
    lines.insert(insert_at, f"{key} = {new_value}")


def _atomic_replace(text: str) -> None:
    # P2 (giro 12): path fisso "config.toml.tmp" condiviso da tutti i comandi
    # GUI (ognuno un processo separato) causava una race — due scritture
    # concorrenti sullo stesso tmp potevano far perdere una modifica.
    # tempfile.mkstemp garantisce un path univoco per invocazione, come già
    # fatto in status.py/output_history.py.
    fd, tmp_name = tempfile.mkstemp(dir=CONFIG_PATH.parent, prefix="config.toml.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        tmp_path = Path(tmp_name)
        tmp_path.chmod(0o600)  # contiene api_key: leggibile solo dall'utente
        tmp_path.replace(CONFIG_PATH)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _write_validated(lines: list[str]) -> None:
    new_text = "\n".join(lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigEditorError(f"Write aborted, invalid TOML: {exc}") from exc
    _atomic_replace(new_text)


def _ensure_level_blocks(lines: list[str], header: str, through_index: int) -> None:
    """Aggiunge blocchi fallback vuoti fino all'indice richiesto.

    Serve per migrare in modo incrementale i config creati prima che un
    servizio ottenesse endpoint dedicati (in particolare ``stream``): la GUI
    può mostrare righe sintetiche e materializza il blocco solo al primo edit.
    """
    existing = sum(line.strip() == header for line in lines)
    while existing <= through_index:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend([
            header,
            f'name = "level{existing + 1}"',
            'endpoint = ""',
            'model = ""',
            'api_key_env = ""',
            'api_key = ""',
            'ca_cert = ""',
            'timeout_seconds = 120',
            'hotwords_in_prompt = false',
            'parallel = false',
            'max_concurrency = 3',
            "",
        ])
        existing += 1


def set_level_field(service: str, level_index: int, field: str, value: str) -> None:
    if service not in SERVICES:
        raise ConfigEditorError(f"Unknown service: {service}")
    if field not in LEVEL_FIELDS:
        raise ConfigEditorError(f"Unknown field: {field}")

    with _locked():
        lines = CONFIG_PATH.read_text().split("\n")
        header = SERVICES[service]["array_header"]
        _ensure_level_blocks(lines, header, level_index)
        start, end = _find_block_bounds(lines, header, level_index)
        if field in ("hotwords_in_prompt", "parallel"):
            toml_value = "true" if value.lower() in ("true", "1", "yes") else "false"
        elif field == "max_concurrency":
            # Intero clampato 1..8, mai la verita' di Python: `bool("false")`
            # attiverebbe il dispatcher per un refuso. Testo non numerico ->
            # errore esplicito invece del default silenzioso.
            try:
                toml_value = str(max(1, min(8, int(value))))
            except (TypeError, ValueError) as exc:
                raise ConfigEditorError(
                    f"max_concurrency must be an integer 1..8, got {value!r}"
                ) from exc
        else:
            toml_value = _toml_line_value(field, value)
        _find_or_insert_key_in_block(lines, start, end, field, toml_value)
        _write_validated(lines)


def set_section_field(service: str, field: str, value: str) -> None:
    """field in {'enabled', 'system_prompt', 'language', 'prompt', 'hotwords',
    'capture_screenshot'}, sulla sezione singola [service] (non array-of-tables).

    Se la sezione [service] non esiste nel TOML (config legacy: solo
    [[service.fallback]]), la crea; se esiste ma la chiave manca, la inserisce
    nel blocco invece di fallire. Necessario per i config creati prima
    dell'introduzione di [stt] con language/prompt/hotwords."""
    if service not in SERVICES:
        raise ConfigEditorError(f"Unknown service: {service}")
    header = SERVICES[service]["section_header"]
    if header is None:
        raise ConfigEditorError(f"Service '{service}' has no single editable section")

    toml_value = _toml_line_value(field, value)
    with _locked():
        lines = CONFIG_PATH.read_text().split("\n")
        if not any(line.strip() == header for line in lines):
            # Sezione assente: creala subito prima del primo blocco
            # [[service.fallback]] (o alla fine del file se non c'è).
            array_header = SERVICES[service]["array_header"]
            insert_pos = len(lines)
            for i, line in enumerate(lines):
                if line.strip() == array_header:
                    insert_pos = i
                    break
            block = [header, f"{field} = {toml_value}", ""]
            lines[insert_pos:insert_pos] = block
            _write_validated(lines)
            return
        start, end = _find_block_bounds(lines, header, 0)
        _find_or_insert_key_in_block(lines, start, end, field, toml_value)
        _write_validated(lines)


def reset_to_default() -> None:
    """Sovrascrive config.toml con l'esempio di default (EN o IT secondo la
    lingua di sistema, stessa logica di config.py). Distruttivo: chi chiama
    (GUI) deve confermare con l'utente prima."""
    from .config import _example_config_path

    text = _example_config_path().read_text()
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigEditorError(f"Reset aborted, invalid TOML template: {exc}") from exc

    with _locked():
        _atomic_replace(text)


def set_storage_field(section: str, field: str, value: str) -> None:
    """section in STORAGE_SECTIONS (es. 'stt_raw'), field in
    {'base_dir', 'enabled', 'retention_hours'}."""
    if section not in STORAGE_SECTIONS:
        raise ConfigEditorError(f"Unknown storage section: {section}")

    with _locked():
        lines = CONFIG_PATH.read_text().split("\n")
        header = STORAGE_SECTIONS[section]
        if not any(line.strip() == header for line in lines):
            # P5: tabella assente (config scritta a mano o piu' vecchia):
            # _find_block_bounds sollevava e la scrittura non avveniva, quindi
            # il click sullo switch non salvava NULLA e la GUI mostrava lo
            # stato indietro al riavvio. Stesso rimedio che set_stream_field
            # adotta per [stream]: crea il blocco in fondo al file.
            lines.extend(["", header, f"{field} = {_toml_line_value(field, value)}"])
            _write_validated(lines)
            return
        start, end = _find_block_bounds(lines, header, 0)
        _find_or_insert_key_in_block(lines, start, end, field, _toml_line_value(field, value))
        _write_validated(lines)


def set_stream_commands(commands: list[dict]) -> None:
    from .config import _parse_stream_commands
    try:
        parsed = _parse_stream_commands(commands)
    except (ValueError, TypeError) as exc:
        raise ConfigEditorError(str(exc)) from exc
    # Validate strict bool/type schema above, then atomically replace all command tables.
    with _locked():
        lines = CONFIG_PATH.read_text().splitlines()
        kept = []
        skip = False
        for line in lines:
            if line.strip() == "[[stream.command]]":
                skip = True
                continue
            if skip and line.startswith("["):
                skip = False
            if not skip:
                kept.append(line)
        block = []
        for command in parsed:
            block.extend(["[[stream.command]]", f"keyword = {json.dumps(command.keyword, ensure_ascii=False)}", f'action = "{command.action}"'])
            if command.action == "key":
                block.append(f"key = {json.dumps(command.key)}")
            else:
                block.append(f"scope = {json.dumps(command.scope)}")
            if command.aliases:
                block.append(f"aliases = {json.dumps(command.aliases, ensure_ascii=False)}")
            block.append(f"ends_session = {'true' if command.ends_session else 'false'}")
            block.append("")
        if block:
            kept.extend([""] + block)
        _write_validated(kept)


def set_stream_field(field: str, value: str) -> None:
    """Aggiorna un campo scalare della sezione [stream]."""
    from .config import STREAM_MODES
    if field not in STREAM_FIELDS:
        raise ConfigEditorError(f"Unknown stream field: {field}")
    kind = STREAM_FIELDS[field]
    if field == "mode":
        # Allinea all'enum del parser (config.STREAM_MODES): un valore fuori
        # enum renderebbe la config non caricabile per tutto il plugin.
        if value not in STREAM_MODES:
            raise ConfigEditorError(
                f"invalid mode: {value!r} (expected one of {STREAM_MODES})")
        toml_value = _toml_line_value(field, value)
    elif field == "dispatch_mode":
        # Stessa validazione a ENUM di `mode`, NON il ramo bool qui sotto:
        # `dispatch_mode` non e' un booleano e quel ramo scriverebbe `false`
        # per un valore ignoto, rendendo il TOML illeggibile per config.py al
        # reload dell'intero plugin (l'intera estensione, non solo la GUI).
        from .config import STREAM_DISPATCH_MODES
        if value not in STREAM_DISPATCH_MODES:
            raise ConfigEditorError(
                f"invalid dispatch_mode: {value!r} "
                f"(expected one of {STREAM_DISPATCH_MODES})")
        toml_value = _toml_line_value(field, value)
    elif field == "paste_shortcut":
        normalized = value.strip().lower()
        if normalized not in ("ctrl+v", "ctrl+shift+v"):
            raise ConfigEditorError(f"invalid paste shortcut: {value!r}")
        toml_value = _toml_line_value(field, normalized)
    elif field == "paste_channel":
        normalized = value.strip().lower()
        if normalized not in ("clipboard", "type"):
            raise ConfigEditorError(f"invalid paste channel: {value!r}")
        toml_value = _toml_line_value(field, normalized)
    elif kind == "int":
        try:
            toml_value = str(int(value))
        except ValueError as exc:
            raise ConfigEditorError(f"{field} must be an integer, got {value!r}") from exc
    elif kind == "float":
        try:
            number = float(value)
        except ValueError as exc:
            raise ConfigEditorError(f"{field} must be a number, got {value!r}") from exc
        if not math.isfinite(number):
            raise ConfigEditorError(f"{field} must be finite")
        if field in STREAM_FLOAT_CLAMPS:
            lo, hi = STREAM_FLOAT_CLAMPS[field]
            number = max(lo, min(hi, number))
        toml_value = repr(number)
    elif kind == "bool":
        toml_value = "true" if value.lower() in ("true", "1", "yes") else "false"
    else:
        toml_value = _toml_line_value(field, value)

    with _locked():
        lines = CONFIG_PATH.read_text().split("\n")
        if not any(line.strip() == "[stream]" for line in lines):
            # Sezione assente (config legacy: solo [[stream.fallback]] o niente
            # affatto): creala subito prima del primo [[stream.fallback]] (o in
            # fondo al file). Senza questo, get_state() mostra i controlli
            # stream ma il salvataggio falliva con _find_block_bounds.
            insert_pos = len(lines)
            for i, line in enumerate(lines):
                if line.strip() == "[[stream.fallback]]":
                    insert_pos = i
                    break
            lines[insert_pos:insert_pos] = ["[stream]", f"{field} = {toml_value}", ""]
            _write_validated(lines)
            return
        start, end = _find_block_bounds(lines, "[stream]", 0)
        _find_or_insert_key_in_block(lines, start, end, field, toml_value)
        _write_validated(lines)


def set_notification_field(key: str, value: str) -> None:
    """Scrive una chiave booleana dentro [notifications], esattamente come la
    faceva TomlBoolEditor.writeBool() in prefs.js.

    P5: prefs.js scriveva config.toml con `_readText` + `replace` +
    `replace_contents`, SENZA il lock di config_editor: due scrittori, uno
    solo col lock. La perdita era reale e misurata — un salvataggio di
    streaming appena fatto veniva annullato in silenzio da un click su uno
    switch di notifica, senza errore ne avviso (logError scatterebbe solo su
    IOException).

    Qui la stessa scrittura passa dal lock, ed eredita anche la validazione
    con tomllib che writeBool non aveva. Il nome della chiave e' validato
    contro l'insieme delle chiavi note: la funzione accetta un valore che
    finisce in un file TOML, e una chiave iniettata dall'estensione
    finirebbe per ridefinire un'intera tabella.
    """
    if key not in NOTIFICATION_KEYS:
        raise ConfigEditorError(f"Unknown notification key: {key}")
    normalized = "true" if str(value).strip().lower() in ("true", "1", "yes") else "false"
    with _locked():
        lines = CONFIG_PATH.read_text().split("\n")
        if not any(line.strip() == "[notifications]" for line in lines):
            # Sezione assente (config legacy): creala, come fa
            # set_section_field per le sezioni di servizio.
            lines.extend(["", "[notifications]", f"{key} = {normalized}"])
            _write_validated(lines)
            return
        start, end = _find_block_bounds(lines, "[notifications]", 0)
        # Chiave nuova in una tabella esistente: si inserisce nel blocco, non
        # si solleva (stessa scelta di set_stream_field). Regex propria
        # (`\s*=`, non `\s*=\s.*$`): preservata, vedi docstring dell'helper.
        _find_or_insert_key_in_block(
            lines, start, end, key, normalized,
            key_regex=rf"^{re.escape(key)}\s*=",
        )
        _write_validated(lines)


def set_history_max_entries(value: str) -> None:
    with _locked():
        lines = CONFIG_PATH.read_text().split("\n")
        # P5: stessa creazione di set_storage_field — la tabella [history]
        # assente faceva sollevare _find_block_bounds e la modifica finiva in
        # un errore silenzioso dal punto di vista dell'utente.
        if not any(line.strip() == "[history]" for line in lines):
            lines.extend(["", "[history]",
                          f"max_entries = {_toml_line_value('max_entries', value)}"])
            _write_validated(lines)
            return
        start, end = _find_block_bounds(lines, "[history]", 0)
        _find_or_insert_key_in_block(lines, start, end, "max_entries", _toml_line_value("max_entries", value))
        _write_validated(lines)


def clear_output_history() -> None:
    from . import output_history

    output_history.clear_history()


def set_icon_field(slot: str, value: str) -> None:
    if slot not in ICON_SLOTS:
        raise ConfigEditorError(f"Unknown icon slot: {slot}")
    with _locked():
        if not CONFIG_PATH.is_file():
            raise ConfigEditorError(f"user config does not exist: {CONFIG_PATH}; reset/create it explicitly first")
        lines = CONFIG_PATH.read_text().split("\n")
        if not any(line.strip() == "[icons]" for line in lines):
            lines.extend(["", "[icons]"])
        start, end = _find_block_bounds(lines, "[icons]", 0)
        # skip_trailing_blank=False: unico sito che inserisce sempre a `end`
        # senza saltare una riga vuota finale. Preservato, non uniformato
        # (vedi docstring dell'helper).
        _find_or_insert_key_in_block(
            lines, start, end, slot, _toml_line_value(slot, value),
            skip_trailing_blank=False,
        )
        _write_validated(lines)


def get_state() -> dict:
    try:
        with open(CONFIG_PATH, "rb") as f:
            raw = tomllib.load(f)
    except OSError as exc:
        raise ConfigEditorError(f"cannot read {CONFIG_PATH}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigEditorError(f"invalid TOML in {CONFIG_PATH}: {exc}") from exc

    def levels_of(key: str, minimum: int = 0) -> list[dict]:
        # timeout_seconds mancante (config.toml modificato a mano) deve
        # restituire un numero valido, non "": stesso default di config.py,
        # altrimenti un SpinRow lato prefs.js legge NaN/0 in silenzio.
        levels = [
            {f: str(entry.get(f, 60 if f == "timeout_seconds" else "")) for f in LEVEL_FIELDS}
            for entry in raw.get(key, {}).get("fallback", [])
        ]
        while len(levels) < minimum:
            levels.append({
                f: str(120 if f == "timeout_seconds" else f"level{len(levels) + 1}" if f == "name" else "")
                for f in LEVEL_FIELDS
            })
        return levels

    stt_section = raw.get("stt", {})
    return {
        "stt": {
            "levels": levels_of("stt"),
            "language": stt_section.get("language", "it"),
            "prompt": stt_section.get("prompt", ""),
            "hotwords": stt_section.get("hotwords", ""),
        },
        "stt_cleanup": {
            "enabled": raw.get("stt_cleanup", {}).get("enabled", True),
            "system_prompt": raw.get("stt_cleanup", {}).get("system_prompt", ""),
            "levels": levels_of("stt_cleanup"),
        },
        "ocr": {
            "levels": levels_of("ocr"),
            "system_prompt": raw.get("ocr", {}).get("system_prompt", ""),
            "capture_screenshot": raw.get("ocr", {}).get("capture_screenshot", False),
        },
        "ocr_cleanup": {
            "enabled": raw.get("ocr_cleanup", {}).get("enabled", False),
            "system_prompt": raw.get("ocr_cleanup", {}).get("system_prompt", ""),
            "levels": levels_of("ocr_cleanup"),
        },
        "storage": {
            "base_dir": raw.get("storage", {}).get("base_dir", ""),
            **{
                key: {
                    "enabled": raw.get("storage", {}).get(key, {}).get("enabled", False),
                    "retention_hours": raw.get("storage", {}).get(key, {}).get("retention_hours", 0),
                }
                for key in STORAGE_SECTIONS if key != "base"
            },
        },
        "history": {
            "max_entries": raw.get("history", {}).get("max_entries", 20),
        },
        "stream": {
            "commands": raw.get("stream", {}).get("command", []),
            "blacklist": raw.get("stream", {}).get("blacklist", ""),
            "mode": raw.get("stream", {}).get("mode", "per_chunk"),
            "dispatch_mode": raw.get("stream", {}).get("dispatch_mode", "auto")
                if str(raw.get("stream", {}).get("dispatch_mode", "auto")).strip() in STREAM_DISPATCH_MODES
                else "auto",
            "silence_seconds": raw.get("stream", {}).get("silence_seconds", 0.7),
            "noise_db": raw.get("stream", {}).get("noise_db", -30),
            "vad_margin_db": raw.get("stream", {}).get("vad_margin_db", 6.0),
            "min_utterance_seconds": raw.get("stream", {}).get("min_utterance_seconds", 0.4),
            "max_utterance_seconds": raw.get("stream", {}).get("max_utterance_seconds", 30),
            "paste_delay_ms": raw.get("stream", {}).get("paste_delay_ms", 250),
            "paste_shortcut": raw.get("stream", {}).get("paste_shortcut", "ctrl+v")
                if str(raw.get("stream", {}).get("paste_shortcut", "ctrl+v")).lower() in ("ctrl+v", "ctrl+shift+v") else "ctrl+v",
            "paste_channel": raw.get("stream", {}).get("paste_channel", "clipboard")
                if str(raw.get("stream", {}).get("paste_channel", "clipboard")).lower() in ("clipboard", "type") else "clipboard",
            "language": raw.get("stream", {}).get("language", "it"),
            "prompt": raw.get("stream", {}).get("prompt", ""),
            "hotwords": raw.get("stream", {}).get("hotwords", ""),
            "context_enabled": raw.get("stream", {}).get("context_enabled", True),
            "max_concurrent_chunks": raw.get("stream", {}).get("max_concurrent_chunks", 3),
            # Stessa regola di config.py (StreamConfig.max_concurrent_chunks_auto):
            # 0 o chiave ASSENTE = AUTO, 1..8 = override esplicito, 9+ clampato a
            # 8 (= esplicito, non auto). Nessun secondo criterio: qui si riusa
            # _coerce_int come fa la riga 528 di config.py, altrimenti la GUI
            # dichiarerebbe "fino a 1 worker" mentre il backend calcola 3xN e la
            # nota "il tetto dei worker e' automatico" non comparirebbe mai.
            "max_concurrent_chunks_auto": _coerce_int(raw.get("stream", {}).get("max_concurrent_chunks"), 0, 0, 8) == 0,
            "chunk_timeout_seconds": raw.get("stream", {}).get("chunk_timeout_seconds", 30.0),
            # 0 o chiave ASSENTE = default del modulo chunk_log (2000 righe).
            # Stessa regola di max_concurrent_chunks: il default lo decide chi
            # scrive il file, non un secondo criterio qui.
            "chunk_log_max_lines": _coerce_int(
                raw.get("stream", {}).get("chunk_log_max_lines"), 0, 0, 1_000_000),
            # I config pre-stream non hanno [[stream.fallback]]. Mostra comunque
            # tre righe editabili; set_level_field materializza i blocchi.
            "levels": levels_of("stream", minimum=3),
        },
        "icons": {
            slot: {
                "override": raw.get("icons", {}).get(slot, ""),
                "default": _icon_default_path(slot),
            }
            for slot in ICON_SLOTS
        },
    }


def _icon_default_path(slot: str) -> str:
    from . import notify

    return notify.resolve_icon(slot)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: config_editor.py get | set-level <service> <idx> <field> <value> "
              "| set-section <service> <field> <value> | set-storage <section> <field> <value> "
              "| set-stream <field> <value> | set-stream-commands <json> | set-history-max <value> | clear-history "
              "| set-notification <key> <value> | set-icon <slot> <value> | reset",
              file=sys.stderr)
        return 1

    try:
        if args[0] == "get":
            print(json.dumps(get_state()))
        elif args[0] == "set-level":
            _, service, idx, field, value = args
            if field == "api_key" and value == "-":
                # P2 (giro 14): prefs.js passa '-' e manda l'api_key su stdin
                # invece che come argv, non leggibile via /proc/PID/cmdline.
                value = sys.stdin.readline().rstrip("\n")
            set_level_field(service, int(idx), field, value)
            print("ok")
        elif args[0] == "set-section":
            _, service, field, value = args
            set_section_field(service, field, value)
            print("ok")
        elif args[0] == "set-storage":
            _, section, field, value = args
            set_storage_field(section, field, value)
            print("ok")
        elif args[0] == "set-stream":
            _, field, value = args
            set_stream_field(field, value)
            print("ok")
        elif args[0] == "set-stream-commands":
            set_stream_commands(json.loads(args[1]))
            print("ok")
        elif args[0] == "set-history-max":
            _, value = args
            set_history_max_entries(value)
            print("ok")
        elif args[0] == "set-notification":
            _, key, value = args
            set_notification_field(key, value)
            print("ok")
        elif args[0] == "clear-history":
            clear_output_history()
            print("ok")
        elif args[0] == "set-icon":
            _, slot, value = args
            set_icon_field(slot, value)
            print("ok")
        elif args[0] == "reset":
            reset_to_default()
            print("ok")
        else:
            print(f"unknown command: {args[0]}", file=sys.stderr)
            return 1
    except (ConfigEditorError, ValueError, AttributeError, KeyError, OSError, IndexError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
