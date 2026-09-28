#!/usr/bin/env python3
"""Test end-to-end della dettatura in STREAMING: sessione, VAD, trascrizione, chiusura.

End-to-end test of STREAMING dictation: session, VAD, transcription, close.

Il comando vero `stream_toggle_main` avvia il supervisore staccato, che legge PCM dal
suo ffmpeg. Qui ffmpeg e' un finto eseguibile che emette voce (un tono) alternata a
silenzio in tempo reale, cosi' il VAD vero segmenta le frasi e ogni frase viene
trascritta da un server HTTP locale. Si verifica lo stato che l'estensione legge
(stream_state.json), l'esclusione reciproca con la dettatura STT e la chiusura pulita
con `stop`. HOME, runtime e TMPDIR sono temporanei: nessun file dell'utente viene toccato.

The real `stream_toggle_main` starts the detached supervisor, which reads PCM from its
ffmpeg. Here ffmpeg is a fake executable that emits speech (a tone) alternated with
silence in real time, so the real VAD segments the sentences and each sentence is
transcribed by a local HTTP server. What is checked is the state the extension reads
(stream_state.json), the mutual exclusion with STT dictation and the clean close with
`stop`. HOME, runtime and TMPDIR are temporary: no user file is touched.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Riusa l'ambiente finto e il server HTTP del test STT. / Reuses the STT test's fake environment and HTTP server.
_spec = importlib.util.spec_from_file_location("e2e_stt", HERE / "test-e2e-stt.py")
assert _spec is not None and _spec.loader is not None
e2e = importlib.util.module_from_spec(_spec)
sys.modules["e2e_stt"] = e2e
_spec.loader.exec_module(e2e)

# ffmpeg finto: in modalita' file (STT) crea il file e attende SIGINT; in modalita' PCM
# (ultimo argomento "-") emette frame s16le a 16 kHz in tempo reale: silenzio, un tono
# di "voce", silenzio, e cosi' via.
# Fake ffmpeg: in file mode (STT) it creates the file and waits for SIGINT; in PCM mode
# (last argument "-") it emits 16 kHz s16le frames in real time: silence, a "voice"
# tone, silence, and so on.
FAKE_FFMPEG = """#!/usr/bin/env python3
import math, signal, struct, sys, time
signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
out = sys.argv[-1]
if out != "-":
    open(out, "wb").write(b"OggS-fake")
    while True:
        time.sleep(0.1)
RATE, FRAME = 16000, 480
def frame(voice, n):
    if voice:
        return struct.pack("<%dh" % FRAME, *[int(12000 * math.sin(2 * math.pi * 300 * (n * FRAME + i) / RATE)) for i in range(FRAME)])
    return struct.pack("<%dh" % FRAME, *[((n * 7 + i * 13) % 41) - 20 for i in range(FRAME)])
plan = [(False, 0.6), (True, 1.5), (False, 1.6)] * 3 + [(False, 3600.0)]
n = 0
for voice, seconds in plan:
    for _ in range(int(seconds * RATE / FRAME)):
        sys.stdout.buffer.write(frame(voice, n))
        sys.stdout.buffer.flush()
        n += 1
        time.sleep(FRAME / RATE)
"""


def write_stream_config(env: e2e.Env, url: str) -> None:
    text = (
        "[general]\nnotifications = true\nclipboard_tool = \"wl-copy\"\nclipboard_paste_tool = \"wl-paste\"\n"
        "notify_timeout_seconds = 5\n"
        "[audio]\ntoggle_debounce_seconds = 0.1\n"
        "[stream]\nmode = \"per_chunk\"\nlanguage = \"it\"\nsilence_seconds = 0.7\n"
        "min_utterance_seconds = 0.4\npaste_channel = \"clipboard\"\n"
        f'[[stream.fallback]]\nname = "s0"\nendpoint = "{url}"\nmodel = "m"\napi_key = "k"\ntimeout_seconds = 5\n'
    )
    cfg = env.home / ".config" / "bravoric-stt-clipboard" / "config.toml"
    cfg.write_text(text)
    cfg.chmod(0o600)


def run_stream(env: e2e.Env, *args: str) -> subprocess.CompletedProcess:
    code = ("import sys; from bravoric_stt_clipboard.cli import stream_toggle_main; "
            "raise SystemExit(stream_toggle_main(sys.argv[1:]))")
    return subprocess.run([sys.executable, "-c", code, *args], env=env.env(), capture_output=True,
                          text=True, timeout=90, cwd=str(env.root), check=False)


def state_of(env: e2e.Env) -> dict:
    path = env.home / ".cache" / "bravoric-stt-clipboard" / "stream_state.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def wait_for(cond, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.3)
    return False


def kill_leftovers(env: e2e.Env) -> None:
    """Rete di sicurezza: nessun supervisore o ffmpeg finto deve sopravvivere al test.
    Safety net: no supervisor or fake ffmpeg may survive the test."""
    lock = env.runtime / "bravoric-stt-clipboard" / "stream.lock"
    try:
        pid = json.loads(lock.read_text()).get("pid")
    except (OSError, ValueError):
        pid = None
    if isinstance(pid, int):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    subprocess.run(["pkill", "-f", str(env.bin / "ffmpeg")], check=False)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), e2e.FakeApi)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    good = f"http://127.0.0.1:{port}/v1"

    print("== sessione streaming: VAD vero, trascrizione, stato per l'estensione / streaming session ==")
    env = e2e.Env()
    try:
        (env.bin / "ffmpeg").write_text(FAKE_FFMPEG)
        write_stream_config(env, good)
        started = run_stream(env)
        e2e.check("avvio: exit 0", started.returncode == 0, started.stderr[-300:])
        lock = env.runtime / "bravoric-stt-clipboard" / "stream.lock"
        e2e.check("avvio: il lock della sessione esiste", lock.exists())
        e2e.check("avvio: stato attivo per l'estensione (stream_state.json)",
                  wait_for(lambda: state_of(env).get("active") is True, 8), str(state_of(env))[:200])
        e2e.check("avvio: status.json dichiara recording con service stream",
                  wait_for(lambda: env.status().get("state") == "recording" and env.status().get("service") == "stream", 8),
                  str(env.status()))

        chunks_ok = wait_for(lambda: any("ciao mondo prova" in c for c in state_of(env).get("chunks", [])), 25)
        e2e.check("il VAD vero taglia la frase e il testo trascritto compare in stream_state.json",
                  chunks_ok, str(state_of(env).get("chunks")))
        e2e.check("il server HTTP ha ricevuto una trascrizione per frase",
                  sum(1 for h in e2e.FakeApi.hits if h.endswith("/audio/transcriptions")) >= 1)

        print("== esclusione reciproca con la dettatura STT / mutual exclusion with STT ==")
        stt = env.toggle()
        e2e.check("con lo streaming vivo la scorciatoia STT non avvia un secondo ffmpeg",
                  not (env.runtime / "bravoric-stt-clipboard" / "recording.lock").exists(), stt.stderr[-200:])

        print("== chiusura con `stop` / close with `stop` ==")
        stopped = run_stream(env, "stop")
        e2e.check("stop: exit 0", stopped.returncode == 0, stopped.stderr[-300:])
        e2e.check("stop: il lock della sessione viene rimosso",
                  wait_for(lambda: not lock.exists(), 15))
        e2e.check("stop: stato non piu' attivo", wait_for(lambda: state_of(env).get("active") is False, 15), str(state_of(env))[:200])
        e2e.check("stop: status.json torna idle", wait_for(lambda: env.status().get("state") == "idle", 15), str(env.status()))
        again = run_stream(env, "stop")
        e2e.check("stop e' idempotente: senza sessione esce 0 e non avvia nulla",
                  again.returncode == 0 and not lock.exists(), again.stderr[-200:])
        log = env.home / ".cache" / "bravoric-stt-clipboard" / "chunk_log.jsonl"
        e2e.check("il log dei chunk registra chi ha risposto",
                  log.exists() and '"served_by": "s0"' in log.read_text().replace('":"', '": "'), log.read_text()[:200] if log.exists() else "")
    finally:
        kill_leftovers(env)
        env.cleanup()

    print("== streaming con fallback: primo endpoint morto, secondo vivo / streaming fallback ==")
    env = e2e.Env()
    try:
        (env.bin / "ffmpeg").write_text(FAKE_FFMPEG)
        write_stream_config(env, good)
        cfg = env.home / ".config" / "bravoric-stt-clipboard" / "config.toml"
        text = cfg.read_text()
        dead_level = ('[[stream.fallback]]\nname = "dead"\nendpoint = "http://127.0.0.1:1/v1"\nmodel = "m"\n'
                      'api_key = "k"\ntimeout_seconds = 5\n')
        # Il livello morto va PRIMA di quello vivo, nella stessa sezione [stream].
        # The dead level goes BEFORE the live one, in the same [stream] section.
        cfg.write_text(text.replace('[[stream.fallback]]\nname = "s0"', dead_level + '[[stream.fallback]]\nname = "s0"'))
        e2e.check("avvio della sessione con fallback: exit 0", run_stream(env).returncode == 0)
        log = env.home / ".cache" / "bravoric-stt-clipboard" / "chunk_log.jsonl"

        def served_by_second() -> bool:
            if not log.exists():
                return False
            for line in log.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("served_by") == "s0" and rec.get("fallback") is True:
                    return True
            return False

        e2e.check("il chunk e' servito dal secondo livello e il log dichiara fallback=true",
                  wait_for(served_by_second, 30), log.read_text()[:300] if log.exists() else "")
        e2e.check("il testo arriva comunque nello stato per l'estensione",
                  any("ciao mondo prova" in c for c in state_of(env).get("chunks", [])), str(state_of(env).get("chunks")))
        run_stream(env, "stop")
        e2e.check("chiusura pulita anche con il fallback",
                  wait_for(lambda: not (env.runtime / "bravoric-stt-clipboard" / "stream.lock").exists(), 15))
    finally:
        kill_leftovers(env)
        env.cleanup()

    server.shutdown()
    print()
    print(f"{e2e.PASS} PASS / {e2e.FAIL} FAIL")
    return 1 if e2e.FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
