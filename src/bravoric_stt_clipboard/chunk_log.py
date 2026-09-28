"""Log JSONL append-only: UNA riga per chunk di dettatura.

PERCHE' (misurato, non ipotizzato)
---------------------------------
In ~/.cache/bravoric-stt-clipboard c'erano solo output_history.json (il testo)
e stream_state.json (i chunk): nessuno diceva QUALE endpoint aveva risposto,
SE aveva fatto fallback e IN QUANTO TEMPO. I logger.warning di fallback.py
finiscono su stderr, e il supervisore gira staccato: quei warning non
arrivavano da nessuna parte. Quindi non c'era modo di misurare se
whisper-gpu reggeva e se scrocco-fissone perdeva parole.

Il log risponde a una domanda sola, per chunk: chi ha risposto, in ordine di
tentativo, e con quali tempi. Non e' un archivio: e' uno strumento di debug,
quindi la ritenzione e' in RIGHE (non in orari) e il default e' 2000.

SICUREZZA PRIMA
---------------
1. Ogni scrittura e' in try/except e NON propaga: un log rotto non puo'
   perdere una parola ne' fermare la dettatura. `append_record` rende False.
2. Append in sola scrittura (O_APPEND, un solo os.write della riga intera):
   mai read-modify-write, perche' piu' processi potrebbero scrivere. La
   rientrata in coda del file e' atomica a livello di write().
3. fsync NON e' richiesto a ogni riga: rallenterebbe la dettatura per un file
   che e' gia' best-effort (le sessioni successive ripartono pulite).
4. MAI api_key, api_key_env o il valore risolto della chiave. L'host e' solo
   il netloc: niente query string, niente userinfo, niente percorso.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import math
import os
import re
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Percorso INIETTABILE: i test lo riassegnano (o lo passano a append_record)
# perche' scrivere sul percorso reale inquinerebbe la sessione dell'utente.
CHUNK_LOG_PATH = Path.home() / ".cache" / "bravoric-stt-clipboard" / "chunk_log.jsonl"
# NB: il lock non ha una costante dedicata. _rotate() lo deriva dal path
# ricevuto con path.suffix + ".lock", che per chunk_log.jsonl da
# chunk_log.jsonl.lock: era qui una costante che diceva un'altra cosa
# (chunk_log.lock) ed era l'unica definizione di un oggetto che nessuno
# usava, cioe' due nomi diversi per lo stesso file con uno solo vivo.

DEFAULT_MAX_LINES = 2000
# Tetto della ritenzione richiesta: il valore 1_000_000 che c'era gia' qui
# sotto, solo messo a nome (come DEFAULT_MAX_LINES e MAX_ERR_CHARS) perche' lo
# stesso limite compare anche in config.py e config_editor.py e i tre numeri
# non possono divergere. NON e' un limite nuovo: e' quello preesistente.
MAX_MAX_LINES = 1_000_000
TS_FORMAT = "%Y-%m-%dT%H:%M:%S"
# Tetto del campo `err`: il messaggio di un'eccezione HTTP puo' essere lunghissimo
# e il log serve a leggere, non ad archiviare. Non e' una troncatura del log:
# e' una troncatura del MESSAGGIO, dichiarata qui perche' nessuno se ne
# accorga altrimenti.
MAX_ERR_CHARS = 500

# api_key / key / token / authorization =<valore>: il valore sparisce, il nome
# del parametro resta (serve per capire cosa era rotto). Copre sia
# "api_key=abc" sia "api_key: abc" sia "Authorization: Bearer abc".
_SECRET_RE = re.compile(
    r"((?:api[_-]?key|access[_-]?key|client[_-]?secret|secret|passw(?:or)?d|pwd"
    r"|key|token|auth(?:orization)?|bearer)"
    r"[\"']?\s*[=:]\s*\"?'?)([^\s\"',&]+)",
    re.IGNORECASE,
)
# "Bearer <token>" con SPAZIO (non =/:): senza questa passata il regex sopra,
# su "Authorization: Bearer sk-abc", prende "Bearer" come valore e lascia il
# token in chiaro (misurato).
_BEARER_RE = re.compile(r"(\bbearer\s+)([^\s\"',&]+)", re.IGNORECASE)
# Credenziali nell'URL: https://utente:password@host
_URL_USERINFO_RE = re.compile(r"(://[^/\s:@]+:)([^@\s/]+)(@)")

# Cache del numero di righe per percorso: un conteggio a ogni append costerebbe
# una lettura del file per ogni chunk. La cache vale per il processo: dopo un
# riavvio si riconta una volta sola, quindi un file cresciuto da un altro
# processo puo' superare la ritenzione fino alla prossima rotazione. Il file e'
# di debug e la rotazione e' comunque pigra, non e' un archivio.
_line_counts: dict[str, int] = {}
_count_lock = threading.Lock()


# --------------------------------------------------------------------- scrittura
def endpoint_host(endpoint: str) -> str:
    """Solo il netloc dell'endpoint: MAI query string, MAI userinfo, MAI path.

    `http://10.9.0.2:4001/v1?api_key=SEGRETO` -> "10.9.0.2:4001". Serve
    anche quando l'endpoint non ha schema ("10.9.0.2:4001/v1"): urlsplit
    restituisce netloc vuoto e si cade sul ramo manuale.
    """
    raw = (endpoint or "").strip()
    if not raw:
        return ""
    raw = raw.split("#", 1)[0].split("?", 1)[0]
    try:
        parsed = urlsplit(raw)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.netloc:
        return parsed.netloc.rsplit("@", 1)[-1]
    tail = raw.split("//", 1)[-1]
    return tail.split("/", 1)[0].rsplit("@", 1)[-1]


def redact(text: str) -> str:
    """Toglie il VALORE delle chiavi che non devono finire nel log.

    Non e' una paranoia: un messaggio di errore di requests/include l'URL
    completo, e l'endpoint puo' portare la chiave in query string. Il nome del
    parametro resta leggibile, che e' cio' che serve per capire il difetto.
    """
    text = _BEARER_RE.sub(r"\1<redacted>", text or "")
    text = _URL_USERINFO_RE.sub(r"\1<redacted>\3", text)
    return _SECRET_RE.sub(r"\1<redacted>", text)


def _short_error(exc: object, secrets: tuple[str, ...] = ()) -> str:
    message = str(exc)
    # Il VALORE esatto delle chiavi configurate, prima dei pattern: un 401
    # stile OpenAI ripete la chiave nel corpo ("Incorrect API key provided:
    # sk-...") senza alcun "nome=valore" che un regex possa agganciare, e
    # api_client mette resp.text nell'ApiError. Sotto 6 caratteri non si
    # redige: una "chiave" cosi' corta colpirebbe testo qualunque.
    for secret in secrets:
        if len(secret) >= 6:
            message = message.replace(secret, "<redacted>")
    message = redact(message).strip()
    if len(message) > MAX_ERR_CHARS:
        message = message[:MAX_ERR_CHARS] + "…"
    return message


def _level_secrets(level) -> tuple[str, ...]:
    """Chiavi API note di questo livello (inline o da variabile d'ambiente)."""
    resolver = getattr(level, "resolved_api_key", None)
    try:
        key = resolver() if callable(resolver) else ""
    except Exception:  # noqa: BLE001 - un livello anomalo non deve rompere il log
        return ()
    return (key,) if isinstance(key, str) and key else ()


def make_attempt(level, ms: float, ok: bool, err: object = None) -> dict:
    """Una voce di `attempts`: il livello TENTATO, con i suoi tempi.

    Si costruisce dal livello, non da un dict passato a mano: il campo
    `host` non puo' quindi contenere nient'altro che il netloc, e nessun
    campo puo' contenere la chiave perche' qui non c'e'. `err` accetta
    l'eccezione stessa (non la sua stringa): la redazione e' il mestiere di
    questo modulo, non del chiamante.
    """
    return {
        "level": str(getattr(level, "name", "") or ""),
        "model": str(getattr(level, "model", "") or ""),
        "host": endpoint_host(str(getattr(level, "endpoint", "") or "")),
        "ms": round(max(0.0, float(ms))),
        "ok": bool(ok),
        "err": _short_error(err, _level_secrets(level)) if err else None,
    }


def make_record(*, session: Any, seq: Any, audio_s: float,
                attempts: list[dict] | None, total_ms: float, text: str,
                ts: str | None = None) -> dict:
    """La riga di log. `served_by`/`fallback` sono DERIVATI dagli attempts.

    Non sono parametri: se lo fossero, il chiamante potrebbe scrivere un
    `served_by` che contraddice gli attempts, e il log direbbe una bugia
    proprio nel campo che serve per decidere quale endpoint conviene.
    """
    entries = [dict(a) for a in (attempts or []) if isinstance(a, dict)]
    served_by = None
    for attempt in entries:
        if attempt.get("ok"):
            served_by = attempt.get("level") or None
    return {
        "ts": ts or datetime.now().strftime(TS_FORMAT),
        "session": str(session) if session is not None else "",
        "seq": seq,
        "audio_s": round(float(audio_s), 3),
        "attempts": entries,
        "served_by": served_by,
        "fallback": len(entries) > 1,
        "total_ms": round(max(0.0, float(total_ms))),
        "text_len": len(text or ""),
        "text": text or "",
    }


def _coerce_max_lines(max_lines: Any) -> int:
    """Ritenzione in righe richiesta, ripulita. 0 (o assente) = DEFAULT.

    IL DIFETTO CHE QUI E' CORRETTO: la versione precedente faceva
    `max(1, min(1_000_000, int(max_lines)))`, quindi `0` diventava `1`. Ma
    `StreamConfig.chunk_log_max_lines` e i due TOML di esempio dicono tutti e
    tre che `0` significa "usa il default" (2000). La config personale
    dell'utente non ha la chiave, quindi arrivava 0 e il log teneva UNA riga:
    con un solo chunk nel file `summarize` non ha nulla su cui aggregare e il
    `--summary` per endpoint restava vuoto, cioe' esattamente la ragione per cui
    la feature e' stata chiesta. Non e' un arrotondamento: e' la feature spenta
    di fatto, silenziosamente.

    Regole, allineate a `_coerce_max_concurrency` in config.py (stessa
   house style, nessuna grammatica nuova inventata qui):

        assente / 0 / "" / "0" / negativo / float non esatto /
        non numerico / bool                         -> DEFAULT_MAX_LINES
        intero positivo                             -> se stesso, clampato
                                                       a MAX_MAX_LINES

    - `bool` non e' un conteggio di righe: in TOML `chunk_log_max_lines = true`
      e' una battitura, non "tieni una riga" (che e' esattamente il difetto
      che questa funzione corregge). `int(True) == 1` e' la stessa trappola.
    - Le stringhe numeriche SONO accettate perche' il percorso GUI scrive
      `chunk_log_max_lines = "3"` (config_editor._toml_line_value mette fra
      virgoletti i campi non elencati esplicitamente): rifiutarle romperebbe il
      round-trip prefs.js -> config.toml senza avviso. Il confronto resta
      esplicito, mai `bool(str)`: in Python `bool("false")` e' True.
    - `float` solo se finito e matematicamente esatto (2000.0 -> 2000, 12.7 ->
      default): troncare 12.7 a 12 sarebbe inventare una ritenzione che nessuno
      ha scritto.
    """
    if isinstance(max_lines, bool):
        return DEFAULT_MAX_LINES
    if isinstance(max_lines, int):
        value = max_lines
    elif isinstance(max_lines, float):
        if not math.isfinite(max_lines) or not max_lines.is_integer():
            return DEFAULT_MAX_LINES
        value = int(max_lines)
    elif isinstance(max_lines, str):
        text = max_lines.strip()
        if not text:
            return DEFAULT_MAX_LINES
        try:
            value = int(text)
        except ValueError:
            return DEFAULT_MAX_LINES
    else:
        return DEFAULT_MAX_LINES
    # 0 (e i suoi equivalenti) = "usa il default", mai 1: il tetto inferiore
    # non e' 1, e' DEFAULT_MAX_LINES. Un valore negativo non e' "tieni poco",
    # e' una battitura: torna al default come un valore non numerico.
    if value <= 0:
        return DEFAULT_MAX_LINES
    return min(MAX_MAX_LINES, value)


def _count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def _rotate(path: Path, max_lines: int) -> None:
    """Tiene le ULTIME `max_lines` righe. Ritenzione in righe, non in orari.

    Lock dedicato: l'append e' puro O_APPEND e non lo prende (metterlo
    costerebbe una syscall per ogni chunk), la rotazione sì, cosi' due
    processi non riscrivono il file insieme. Una riga scritta da un terzo
    processo fra la lettura e la sostituzione andrebbe persa: si rimedia
    rileggendo se la dimensione e' cambiata durante la lettura, e il
    progetto ha un solo writer (il supervisore), quindi la finestra e' vuota
    in pratica.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        for _ in range(3):
            before = path.stat().st_size if path.exists() else 0
            try:
                data = path.read_bytes()
            except OSError:
                return
            after = path.stat().st_size if path.exists() else 0
            if before != after:
                # cresciuto durante la lettura: un altro processo sta
                # scrivendo, rileggo invece di tagliare via le sue righe.
                continue
            lines = [line for line in data.splitlines() if line.strip()]
            kept = lines[-max_lines:]
            tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
            with open(tmp, "wb") as handle:
                handle.write(b"".join(line + b"\n" for line in kept))
            os.replace(tmp, path)
            with _count_lock:
                _line_counts[str(path)] = len(kept)
            return
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _bump_count(path: Path, max_lines: int) -> None:
    key = str(path)
    with _count_lock:
        count = _line_counts.get(key)
        if count is None:
            count = _count_lines(path)
        count += 1
        _line_counts[key] = count
    if count > max_lines:
        _rotate(path, max_lines)


def append_record(record: dict, path: Path | str | None = None,
                  max_lines: Any = None) -> bool:
    """Append atomico di UNA riga. Ritorna False se non e' stato scritto.

    Non solleva MAI: il chiamante e' il sequencer, e un log rotto non puo'
    fermare la dettatura. `path` e `max_lines` sono iniettabili perche' i
    test non devono scrivere sul percorso reale dell'utente.
    """
    target = Path(path) if path is not None else CHUNK_LOG_PATH
    limit = _coerce_max_lines(max_lines)
    try:
        payload = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(target), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            # UN solo write() della riga intera: nessun lettore vede meta'
            # riga, e nessun altro scrittore ci mette dentro nel mezzo.
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        _bump_count(target, limit)
        return True
    except Exception as exc:
        # Un log rotto non puo' fermare la dettatura: qui si SEGNA e si torna
        # False, mai si rilancia. Il chiamante e' il sequencer, e la sua firma
        # promette gia' False in caso di errore: rilanciare renderebbe falso
        # quel contratto e perderebbe un chunk gia' trascritto. debug e non
        # warning perche' il supervisore gira staccato: nessuno legge stderr.
        logger.debug("chunk_log: scrittura fallita su %s: %s", target, exc, exc_info=True)
        return False

# ---------------------------------------------------------------------- lettura
def read_records(path: Path | str | None = None) -> list[dict]:
    """Tutte le righe valide, piu' recente in fondo. Mai solleva."""
    target = Path(path) if path is not None else CHUNK_LOG_PATH
    try:
        with open(target, "rb") as handle:
            raw = handle.read()
    except OSError:
        return []
    records = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Riga troncata da un kill -9 o da una scrittura incompleta: si
            # scarta quella riga, non il file.
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value[:19], TS_FORMAT)
    except ValueError:
        return None


def filter_records(records: list[dict], *, last: int | None = None,
                   session: str | None = None,
                   since_minutes: float | None = None,
                   now: datetime | None = None) -> list[dict]:
    """Ultime N righe / di una sessione / degli ultimi N minuti, in quest'ordine.

    Con `--since` una riga SENZA ts leggibile viene scartata: un log senza
    orario non puo' dimostrare di essere dentro la finestra, e tenerla
    produrrebbe un --summary che mente proprio sulla finestra temporale.
    """
    result = list(records)
    if session:
        result = [r for r in result if str(r.get("session", "")) == session]
    if since_minutes is not None:
        reference = now or datetime.now()
        cutoff = reference - timedelta(minutes=max(0.0, float(since_minutes)))
        result = [r for r in result
                  if (parsed := parse_ts(r.get("ts"))) is not None and parsed >= cutoff]
    if last is not None:
        result = result[-max(0, int(last)):] if int(last) else []
    return result


def _percentile(values: list[float], pct: float) -> float | None:
    """Percentile nearest-rank. Nessun campione -> None (mai 0 inventato)."""
    if not values:
        return None
    index = max(0, math.ceil(pct / 100.0 * len(values)) - 1)
    return values[min(index, len(values) - 1)]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def summarize(records: list[dict]) -> list[dict]:
    """Vista per endpoint: tentativi, ok, falliti, ms medio, p50, p95, quota.

    La latenza e' calcolata sui tentativi RIUSCITI: la durata di un timeout
    (8 s su whisper-gpu) non e' una latenza, e' il tetto del timeout, e
    miscelarla con le risposte vere renderebbe il confronto fra endpoint
    illegibile. I tentativi falliti contano comunque in `attempts` e
    `failed`, quindi la loro presenza resta visibile.
    """
    buckets: dict[tuple[str, str, str], dict] = {}
    served_total = 0
    for record in records:
        attempts = record.get("attempts")
        if not isinstance(attempts, list):
            continue
        # Il chunk e' stato SERVITO dall'ULTIMO tentativo riuscito (la
        # catena si ferma al primo ok, quindi in una riga ben formata e'
        # l'ultimo; se ce n fossero due, conta l'ultimo, che e' quello che
        # ha prodotto il testo finale). La quota si accredita su QUEL
        # bucket, non su tutti gli ok: un endpoint che ha risposto ma non
        # e' stato quello scelto non ha servito il chunk.
        served_bucket = None
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            key = (str(attempt.get("level") or ""), str(attempt.get("model") or ""),
                   str(attempt.get("host") or ""))
            bucket = buckets.setdefault(key, {
                "level": key[0], "model": key[1], "host": key[2],
                "attempts": 0, "ok": 0, "failed": 0, "served": 0,
                "_ms": [],
            })
            bucket["attempts"] += 1
            ok = bool(attempt.get("ok"))
            if ok:
                bucket["ok"] += 1
                with contextlib.suppress(TypeError, ValueError):
                    bucket["_ms"].append(float(attempt.get("ms") or 0))
                served_bucket = bucket
            else:
                bucket["failed"] += 1
        if served_bucket is not None:
            # UNA volta sola per chunk, mai per tentativo: altrimenti un
            # chunk con piu' tentativi riusciti conterebbe due volte e la
            # quota non sarebbe piu' una quota.
            served_bucket["served"] += 1
            served_total += 1

    rows = []
    for bucket in buckets.values():
        samples = sorted(bucket.pop("_ms"))
        bucket["ms_avg"] = _round(sum(samples) / len(samples)) if samples else None
        bucket["p50"] = _round(_percentile(samples, 50))
        bucket["p95"] = _round(_percentile(samples, 95))
        bucket["served_pct"] = _round(
            100.0 * bucket["served"] / served_total) if served_total else None
        rows.append(bucket)
    rows.sort(key=lambda r: (-r["attempts"], r["level"], r["model"], r["host"]))
    return rows


# ------------------------------------------------------------------------- CLI
def _format_record(record: dict) -> str:
    attempts = record.get("attempts") if isinstance(record.get("attempts"), list) else []
    if attempts:
        rendered = ", ".join(
            "{level}/{model}@{host} {ms}ms{ok}{err}".format(
                level=a.get("level", "?"), model=a.get("model", "?"),
                host=a.get("host", "?"), ms=a.get("ms", "?"),
                ok=" OK" if a.get("ok") else " KO",
                err=" — " + str(a.get("err")) if a.get("err") else "",
            )
            for a in attempts if isinstance(a, dict))
    else:
        rendered = "(nessun tentativo registrato)"
    return (
        "{ts}  seq={seq}  audio={audio_s:.2f}s  total={total_ms}ms  "
        "served_by={served_by}  fallback={fallback}  session={session}  "
        "| {attempts}".format(
            ts=record.get("ts", "?"), seq=record.get("seq", "?"),
            audio_s=float(record.get("audio_s") or 0.0),
            total_ms=record.get("total_ms", "?"),
            served_by=record.get("served_by") or "-",
            fallback=str(bool(record.get("fallback"))).lower(),
            session=(record.get("session") or "-")[:8],
            attempts=rendered)
        + ("\n      testo: " + str(record.get("text", "")) if record.get("text_len") else "")
    )


def _format_summary(rows: list[dict], total_records: int) -> str:
    if not rows:
        return f"Nessun chunk nel log ({total_records} righe lette, nessun tentativo registrato)."
    lines = [
        (f"{'endpoint':<44} {'tent':>5} {'ok':>4} {'ko':>4} {'ms_avg':>8} "
         f"{'p50':>8} {'p95':>8} {'serviti':>8} {'quota':>7}"),
        "-" * 104,
    ]
    for row in rows:
        label = f"{row['level']}/{row['model']}@{row['host']}"
        served = f"{row['served']}"
        quota = "-" if row["served_pct"] is None else f"{row['served_pct']:.1f}%"
        lines.append(
            f"{label:<44} {row['attempts']:>5} {row['ok']:>4} {row['failed']:>4} "
            f"{_cell(row['ms_avg']):>8} {_cell(row['p50']):>8} {_cell(row['p95']):>8} "
            f"{served:>8} {quota:>7}")
    lines.append("")
    lines.append("ms su tentativi RIUSCITI (un timeout non e' una latenza). "
                 "quota = chunk serviti da quell'endpoint sul totale dei chunk "
                 "con almeno una risposta.")
    return "\n".join(lines)


def _cell(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bravoric-chunk-log",
        description="Legge il log JSONL dei chunk di dettatura "
                    "(~/.cache/bravoric-stt-clipboard/chunk_log.jsonl).")
    parser.add_argument("--last", type=int, default=None, metavar="N",
                        help="ultime N righe, piu' recente in fondo")
    parser.add_argument("--session", default=None, metavar="ID",
                        help="solo le righe di una sessione")
    parser.add_argument("--summary", action="store_true",
                        help="vista aggregata per endpoint: tentativi, ok, falliti, "
                             "ms medio, p50, p95 e quota di chunk serviti")
    parser.add_argument("--since", type=float, default=None, metavar="MINUTI",
                        help="solo le righe degli ultimi N minuti")
    parser.add_argument("--path", default=None, metavar="FILE",
                        help="percorso alternativo del log (default: quello reale)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    target = Path(args.path) if args.path else CHUNK_LOG_PATH
    records = read_records(target)
    selected = filter_records(records, last=args.last, session=args.session,
                              since_minutes=args.since)
    if args.summary:
        print(_format_summary(summarize(selected), len(records)))
    elif not selected:
        where = target
        print(f"Nessun chunk nel log ({len(records)} righe in {where}).")
    else:
        for record in selected:
            print(_format_record(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
