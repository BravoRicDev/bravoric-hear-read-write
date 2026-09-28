#!/usr/bin/env python3
"""Test end-to-end della dettatura: il comando VERO `stt_toggle_main` da un capo all'altro.

End-to-end test of dictation: the REAL `stt_toggle_main` command from end to end.

Nessun mock del codice del progetto. Si sostituiscono soltanto i programmi esterni,
con finti eseguibili in un PATH dedicato (ffmpeg, wl-copy, wl-paste, notify-send), e i
server di trascrizione, con un piccolo server HTTP locale che parla il formato OpenAI.
Cosi' girano davvero: configurazione TOML, lock del registratore, sequenza
start/stop, chiamate HTTP con fallback, pulizia LLM, scrittura negli appunti, notifiche
tradotte, stato per l'estensione, cronologia. Tutto in una HOME, un runtime e una
cache temporanei: non tocca nessun file dell'utente.

No mocking of the project's code. Only the external programs are replaced, with fake
executables in a dedicated PATH (ffmpeg, wl-copy, wl-paste, notify-send), and the
transcription servers, with a small local HTTP server that speaks the OpenAI format.
So these really run: TOML configuration, recorder lock, start/stop sequence, HTTP calls
with fallback, LLM cleanup, clipboard writing, translated notifications, state for the
extension, history. Everything in a temporary HOME, runtime and cache: it touches no
user file.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))


# ---------------------------------------------------------------- server finto
class FakeApi(BaseHTTPRequestHandler):
    """Server OpenAI-compatibile minimo. / Minimal OpenAI-compatible server."""

    transcript = "ciao mondo prova"
    cleaned = "Ciao mondo, prova."
    ocr_text = "testo letto dall'immagine"
    hits: ClassVar[list[str]] = []

    def log_message(self, *args) -> None:  # silenzio / silence
        pass

    def _reply(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Nome imposto da http.server. / Name imposed by http.server.
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)
        FakeApi.hits.append(self.path)
        try:
            wants_json = "response_format" in json.loads(raw_body)
        except ValueError:
            wants_json = False
        if self.path.endswith("/audio/transcriptions"):
            self._reply({"text": FakeApi.transcript})
        elif self.path.endswith("/chat/completions"):
            # Pulizia LLM (schema JSON) oppure estrazione OCR (testo semplice).
            # LLM cleanup (JSON schema) or OCR extraction (plain text).
            content = json.dumps({"corrected_text": FakeApi.cleaned}) if wants_json else FakeApi.ocr_text
            self._reply({"choices": [{"message": {"content": content}}]})
        else:
            self._reply({"error": "not found"}, 404)


# --------------------------------------------------------------- ambiente finto
class Env:
    """HOME, runtime, cache e PATH finti per una esecuzione. / Fake HOME, runtime, cache and PATH."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="brv-e2e-"))
        self.home = self.root / "home"
        self.runtime = self.root / "runtime"
        self.bin = self.root / "bin"
        for d in (self.home / ".config" / "bravoric-stt-clipboard", self.home / ".cache", self.runtime, self.bin):
            d.mkdir(parents=True)
        self.runtime.chmod(0o700)
        # TMPDIR dedicato: il file audio temporaneo nasce li' e si puo' verificare che sparisca.
        # Dedicated TMPDIR: the temporary audio file is born there and can be checked to vanish.
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        self.clip_log = self.root / "clipboard.log"
        self.notify_log = self.root / "notify.log"
        self.wl_copy_sleep = 0
        self._write_tools()

    def _tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def _write_tools(self) -> None:
        # ffmpeg finto: crea il file di uscita (ultimo argomento) e resta vivo fino a SIGINT.
        # Fake ffmpeg: creates the output file (last argument) and stays alive until SIGINT.
        self._tool("ffmpeg", 'out="${@: -1}"\nprintf "OggS-fake" > "$out"\ntrap "exit 0" INT TERM\nwhile true; do sleep 0.1; done\n')
        self._tool("notify-send", f'printf "%s\\n" "$*" >> "{self.notify_log}"\n')
        self._tool("wl-paste", "exit 1\n")
        self.set_wl_copy_sleep(0)

    def set_wl_copy_sleep(self, seconds: int) -> None:
        if seconds:
            # `exec sleep`: il timeout uccide direttamente lo sleep, senza processi orfani.
            # `exec sleep`: the timeout kills the sleep directly, leaving no orphan processes.
            self._tool("wl-copy", f"exec sleep {seconds}\n")
        else:
            self._tool("wl-copy", f'cat >> "{self.clip_log}"\nprintf "\\n<<END>>\\n" >> "{self.clip_log}"\n')

    def write_config(self, levels: list[str], cleanup: bool = True, extra_general: str = "",
                     extra_audio: str = "", extra_tail: str = "") -> None:
        def level(url: str, name: str) -> str:
            return (f'[[%s]]\nname = "{name}"\nendpoint = "{url}"\nmodel = "m"\napi_key = "k"\n'
                    'timeout_seconds = 5\n')
        text = (
            "[general]\nnotifications = true\nclipboard_tool = \"wl-copy\"\nclipboard_paste_tool = \"wl-paste\"\n"
            "notify_timeout_seconds = 5\n" + extra_general +
            "[audio]\ntoggle_debounce_seconds = 0.1\nretry_on_error = false\n" + extra_audio +
            "[stt_cleanup]\nenabled = " + ("true" if cleanup else "false") + '\nsystem_prompt = "fix"\n'
        )
        for i, url in enumerate(levels):
            text += level(url, f"l{i}") % "stt.fallback"
        if cleanup:
            text += level(levels[-1], "c0") % "stt_cleanup.fallback"
        text += extra_tail
        cfg = self.home / ".config" / "bravoric-stt-clipboard" / "config.toml"
        cfg.write_text(text)
        cfg.chmod(0o600)

    def set_wl_paste_image(self, available: bool) -> None:
        """wl-paste finto: restituisce un PNG vero oppure fallisce. / Fake wl-paste: real PNG or failure."""
        png = ROOT / "src" / "bravoric_stt_clipboard" / "icons" / "error-general.png"
        self._tool("wl-paste", f'cat "{png}"\n' if available else "exit 1\n")

    def set_screenshot(self, mode: str) -> None:
        """gnome-screenshot finto: ok (scrive il PNG), cancel (Esc) o hang (non risponde).
        Fake gnome-screenshot: ok (writes the PNG), cancel (Esc) or hang (does not answer)."""
        png = ROOT / "src" / "bravoric_stt_clipboard" / "icons" / "error-general.png"
        if mode == "ok":
            body = f'while [ "$1" != "--file" ]; do shift; done\ncp "{png}" "$2"\n'
        elif mode == "cancel":
            body = "exit 1\n"
        else:
            body = "exec sleep 30\n"
        self._tool("gnome-screenshot", body)

    def write_ocr_config(self, url: str, capture_screenshot: bool = False, extra_ocr: str = "") -> None:
        text = (
            "[general]\nnotifications = true\nclipboard_tool = \"wl-copy\"\nclipboard_paste_tool = \"wl-paste\"\n"
            "notify_timeout_seconds = 5\n"
            "[ocr]\nsystem_prompt = \"extract\"\ncapture_screenshot = "
            + ("true" if capture_screenshot else "false") + "\n" + extra_ocr
            + f'[[ocr.fallback]]\nname = "o0"\nendpoint = "{url}"\nmodel = "m"\napi_key = "k"\ntimeout_seconds = 5\n'
            "[ocr_cleanup]\nenabled = false\nsystem_prompt = \"fix\"\n"
        )
        cfg = self.home / ".config" / "bravoric-stt-clipboard" / "config.toml"
        cfg.write_text(text)
        cfg.chmod(0o600)

    def ocr(self, language: str = "en") -> subprocess.CompletedProcess:
        code = "from bravoric_stt_clipboard.cli import ocr_capture_main; raise SystemExit(ocr_capture_main())"
        return subprocess.run([sys.executable, "-c", code], env=self.env(language), capture_output=True,
                              text=True, timeout=90, cwd=str(self.root), check=False)

    def env(self, language: str = "en") -> dict:
        e = dict(os.environ)
        e.update({
            "HOME": str(self.home), "XDG_RUNTIME_DIR": str(self.runtime),
            "XDG_CACHE_HOME": str(self.home / ".cache"), "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "PATH": f"{self.bin}:{e['PATH']}", "TMPDIR": str(self.tmp), "PYTHONPATH": str(ROOT / "src"),
            "LANGUAGE": language, "LC_ALL": "C" if language == "en" else "it_IT.UTF-8",
            "LANG": "C" if language == "en" else "it_IT.UTF-8",
        })
        return e

    def toggle(self, language: str = "en") -> subprocess.CompletedProcess:
        code = "from bravoric_stt_clipboard.cli import stt_toggle_main; raise SystemExit(stt_toggle_main())"
        return subprocess.run([sys.executable, "-c", code], env=self.env(language), capture_output=True,
                              text=True, timeout=60, cwd=str(self.root), check=False)

    def clipboard_writes(self) -> list[str]:
        if not self.clip_log.exists():
            return []
        return [c.strip("\n") for c in self.clip_log.read_text().split("\n<<END>>\n") if c.strip("\n")]

    def notifications(self) -> list[str]:
        return self.notify_log.read_text().splitlines() if self.notify_log.exists() else []

    def status(self) -> dict:
        path = self.home / ".cache" / "bravoric-stt-clipboard" / "status.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def dictate(env: Env, language: str = "en") -> tuple[subprocess.CompletedProcess, subprocess.CompletedProcess]:
    """Prima pressione (avvia la registrazione), pausa, seconda pressione (ferma e trascrive).
    First press (starts recording), pause, second press (stops and transcribes)."""
    first = env.toggle(language)
    time.sleep(0.6)
    second = env.toggle(language)
    return first, second


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    good = f"http://127.0.0.1:{port}/v1"
    dead = "http://127.0.0.1:1/v1"

    print("== dettatura completa: trascrizione + pulizia LLM + appunti / full dictation ==")
    env = Env()
    try:
        env.write_config([good])
        first = env.toggle()
        # Non-vacuita': dopo la prima pressione il lock e il file audio ESISTONO davvero.
        # Non-vacuity: after the first press the lock and the audio file REALLY exist.
        check("dopo la prima pressione il lock e il file audio esistono",
              (env.runtime / "bravoric-stt-clipboard" / "recording.lock").exists() and len(list(env.tmp.glob("bravoric-stt-*"))) == 1,
              str(list(env.tmp.glob("*"))))
        time.sleep(0.6)
        second = env.toggle()
        check("prima pressione: registrazione avviata (exit 0)", first.returncode == 0, first.stderr[-200:])
        check("seconda pressione: trascrizione conclusa (exit 0)", second.returncode == 0, second.stderr[-300:])
        writes = env.clipboard_writes()
        check("appunti: prima il testo grezzo, poi quello pulito (doppia iniezione)",
              writes == ["ciao mondo prova", "Ciao mondo, prova."], str(writes))
        check("il server ha visto una trascrizione e una pulizia",
              any(h.endswith("/audio/transcriptions") for h in FakeApi.hits)
              and any(h.endswith("/chat/completions") for h in FakeApi.hits))
        check("stato per l'estensione: di nuovo idle", env.status().get("state") == "idle", str(env.status()))
        history = env.home / ".cache" / "bravoric-stt-clipboard" / "output_history.json"
        check("cronologia scritta con testo grezzo e pulito",
              history.exists() and "Ciao mondo, prova." in history.read_text() and "ciao mondo prova" in history.read_text())
        titles = env.notifications()
        check("notifiche inglesi: 'raw text ready' e 'cleaned text ready'",
              any("STT: raw text ready" in n for n in titles) and any("STT: cleaned text ready" in n for n in titles), str(titles))
        check("nessun file audio temporaneo lasciato in giro", not list(env.tmp.glob("*")), str(list(env.tmp.glob("*"))))
        check("il lock del registratore e' stato rimosso", not (env.runtime / "bravoric-stt-clipboard" / "recording.lock").exists())
    finally:
        env.cleanup()

    print("== lingua italiana: notifiche tradotte / Italian: translated notifications ==")
    env = Env()
    try:
        env.write_config([good])
        _, second = dictate(env, "it")
        titles = env.notifications()
        check("notifiche italiane 'testo grezzo pronto' e 'testo pulito pronto'",
              any("STT: testo grezzo pronto" in n for n in titles) and any("STT: testo pulito pronto" in n for n in titles), str(titles))
    finally:
        env.cleanup()

    print("== fallback: il primo endpoint e' morto, il secondo risponde / fallback ==")
    env = Env()
    try:
        env.write_config([dead, good], cleanup=False)
        _, second = dictate(env)
        # Senza pulizia la doppia iniezione scrive due volte lo stesso testo (grezzo e finale).
        # Without cleanup the double injection writes the same text twice (raw and final).
        writes = env.clipboard_writes()
        check("con il primo livello irraggiungibile il testo arriva comunque dal secondo",
              bool(writes) and set(writes) == {"ciao mondo prova"}, str(writes) + second.stderr[-200:])
    finally:
        env.cleanup()

    print("== tutti gli endpoint morti: errore visibile, appunti intatti / all endpoints down ==")
    env = Env()
    try:
        env.write_config([dead], cleanup=False)
        _, second = dictate(env)
        check("appunti NON toccati quando la trascrizione fallisce", env.clipboard_writes() == [])
        check("errore notificato all'utente", any("transcription error" in n for n in env.notifications()), str(env.notifications()))
        check("lo stato non resta bloccato su processing/recording", env.status().get("state") in ("idle", "error"), str(env.status()))
    finally:
        env.cleanup()

    print("== impostazioni regolabili: lunghezza del testo nelle notifiche / tunable settings ==")
    env = Env()
    try:
        env.write_config([good], extra_general="notification_content_max_chars = 12\n")
        dictate(env)
        bodies = [n for n in env.notifications() if "raw text ready" in n]
        # Il minimo ammesso e' 10: 12 caratteri di "ciao mondo prova" sono "ciao mondo p".
        # The minimum allowed is 10: 12 characters of "ciao mondo prova" are "ciao mondo p".
        check("notification_content_max_chars=12: il corpo e' troncato a 12 caratteri",
              bool(bodies) and bodies[0].endswith("raw text ready ciao mondo p"), str(bodies))
    finally:
        env.cleanup()

    print("== impostazioni regolabili: timeout degli appunti / clipboard timeout ==")
    env = Env()
    try:
        env.set_wl_copy_sleep(30)
        env.write_config([good], cleanup=False, extra_general="clipboard_timeout_seconds = 1\n")
        started = time.time()
        _, second = dictate(env)
        elapsed = time.time() - started
        # Due scritture (grezzo e finale) da 1 s ciascuna piu' l'avvio dei processi: con il
        # default di 5 s a scrittura servirebbero oltre 10 s, con 30 s di sleep molto di piu'.
        # Two writes (raw and final) of 1 s each plus process start-up: with the 5 s default
        # per write it would take over 10 s, with 30 s of sleep much more.
        check("wl-copy che si blocca viene interrotto dal timeout configurato (1 s), non atteso",
              elapsed < 9, f"{elapsed:.1f}s")
        check("l'utente e' avvisato dell'errore sugli appunti", any("clipboard error" in n for n in env.notifications()), str(env.notifications()))
    finally:
        env.cleanup()

    print("== interruttori e regole della config applicati davvero / config switches and rules really applied ==")
    env = Env()
    try:
        env.write_config([good], extra_general="")
        cfg = env.home / ".config" / "bravoric-stt-clipboard" / "config.toml"
        cfg.write_text(cfg.read_text().replace("notifications = true", "notifications = false", 1))
        dictate(env)
        check("[general] notifications = false: nessuna notifica del backend, testo negli appunti comunque",
              env.notifications() == [] and env.clipboard_writes() != [], str(env.notifications()))
    finally:
        env.cleanup()
    env = Env()
    try:
        env.write_config([good], extra_tail="[clipboard]\ndouble_injection = false\n")
        dictate(env)
        check("[clipboard] double_injection = false: una sola scrittura, col testo finale pulito",
              env.clipboard_writes() == ["Ciao mondo, prova."], str(env.clipboard_writes()))
    finally:
        env.cleanup()
    env = Env()
    try:
        env.write_config([good], extra_tail="[notifications]\nstt_on_raw_ready = false\n")
        dictate(env)
        titles = env.notifications()
        check("stt_on_raw_ready = false: niente notifica del grezzo, resta quella del testo pulito",
              not any("raw text ready" in n for n in titles) and any("cleaned text ready" in n for n in titles), str(titles))
    finally:
        env.cleanup()
    env = Env()
    try:
        FakeApi.cleaned = "x"
        env.write_config([good], extra_general="cleanup_min_length_ratio = 0.7\n")
        dictate(env)
        check("cleanup_min_length_ratio = 0.7: una pulizia troppo corta viene scartata, restano gli appunti col testo grezzo",
              set(env.clipboard_writes()) == {"ciao mondo prova"}, str(env.clipboard_writes()))
    finally:
        FakeApi.cleaned = "Ciao mondo, prova."
        env.cleanup()
    env = Env()
    try:
        FakeApi.cleaned = "x"
        env.write_config([good], extra_general="cleanup_min_length_ratio = 0\n")
        dictate(env)
        check("cleanup_min_length_ratio = 0: la stessa pulizia corta viene accettata",
              env.clipboard_writes()[-1:] == ["x"], str(env.clipboard_writes()))
    finally:
        FakeApi.cleaned = "Ciao mondo, prova."
        env.cleanup()
    env = Env()
    try:
        FakeApi.transcript = "Grazie."
        env.write_config([good], cleanup=False, extra_tail='[stream]\nblacklist = "grazie, thank you"\n')
        dictate(env)
        check("blacklist: un'allucinazione come INTERA trascrizione non tocca gli appunti e avvisa",
              env.clipboard_writes() == [] and any("transcription error" in n for n in env.notifications()),
              f"{env.clipboard_writes()} {env.notifications()}")
    finally:
        FakeApi.transcript = "ciao mondo prova"
        env.cleanup()

    print("== OCR dagli appunti: immagine -> visione -> appunti / OCR from the clipboard ==")
    env = Env()
    try:
        env.set_wl_paste_image(True)
        env.write_ocr_config(good)
        result = env.ocr()
        check("OCR: exit 0", result.returncode == 0, result.stderr[-300:])
        check("OCR: il testo letto finisce negli appunti", "testo letto dall'immagine" in env.clipboard_writes(), str(env.clipboard_writes()))
        check("OCR: stato di nuovo idle", env.status().get("state") == "idle", str(env.status()))
        check("OCR: notifica 'OCR: raw text ready'", any("OCR: raw text ready" in n for n in env.notifications()), str(env.notifications()))
    finally:
        env.cleanup()

    print("== OCR senza immagine negli appunti: errore chiaro / OCR without an image ==")
    env = Env()
    try:
        env.set_wl_paste_image(False)
        env.write_ocr_config(good)
        env.ocr()
        check("OCR senza immagine: appunti intatti e notifica dedicata",
              env.clipboard_writes() == [] and any("no image in clipboard" in n for n in env.notifications()), str(env.notifications()))
    finally:
        env.cleanup()

    print("== OCR con selezione area (gnome-screenshot) / OCR with area selection ==")
    env = Env()
    try:
        env.set_screenshot("ok")
        env.write_ocr_config(good, capture_screenshot=True)
        env.ocr()
        check("screenshot: il PNG catturato viene letto e il testo arriva negli appunti",
              "testo letto dall'immagine" in env.clipboard_writes(), str(env.clipboard_writes()))
    finally:
        env.cleanup()
    env = Env()
    try:
        env.set_screenshot("cancel")
        env.write_ocr_config(good, capture_screenshot=True)
        result = env.ocr()
        check("Esc durante la selezione: nessun errore, appunti intatti, stato idle",
              result.returncode == 0 and env.clipboard_writes() == []
              and not any("error" in n.lower() for n in env.notifications()) and env.status().get("state") in ("idle", None),
              f"{result.returncode} {env.notifications()} {env.status()}")
    finally:
        env.cleanup()
    env = Env()
    try:
        env.set_screenshot("hang")
        env.write_ocr_config(good, capture_screenshot=True, extra_ocr="screenshot_timeout_seconds = 5\n")
        started = time.time()
        result = env.ocr()
        elapsed = time.time() - started
        check("screenshot che non risponde: la selezione scade dopo screenshot_timeout_seconds (5 s), non prima ne' dopo 30 s",
              4.5 <= elapsed < 12, f"{elapsed:.1f}s")
        check("scadenza della selezione: silenziosa (nessun errore, appunti intatti)",
              env.clipboard_writes() == [] and not any("error" in n.lower() for n in env.notifications()), str(env.notifications()))
    finally:
        env.cleanup()

    server.shutdown()
    print()
    print(f"{PASS} PASS / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
