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
    delay: ClassVar[float] = 0.0  # ritardo della risposta di visione/pulizia / vision/cleanup reply delay

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
            if FakeApi.delay:
                time.sleep(FakeApi.delay)
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

    def cli(self, entry: str, args: tuple = (), language: str = "en") -> subprocess.CompletedProcess:
        """Entry point CLI vera con argv ESPLICITO (bottoni e menu li passano cosi').
        REAL CLI entry point with EXPLICIT argv (buttons and menu pass them this way)."""
        code = f"from bravoric_stt_clipboard.cli import {entry}; raise SystemExit({entry}({list(args)!r}))"
        return subprocess.run([sys.executable, "-c", code], env=self.env(language), capture_output=True,
                              text=True, timeout=90, cwd=str(self.root), check=False)

    def spawn(self, entry: str, args: tuple = (), language: str = "en") -> subprocess.Popen:
        code = f"from bravoric_stt_clipboard.cli import {entry}; raise SystemExit({entry}({list(args)!r}))"
        return subprocess.Popen([sys.executable, "-c", code], env=self.env(language), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=str(self.root))

    def stt_lock(self) -> Path:
        return self.runtime / "bravoric-stt-clipboard" / "recording.lock"

    def ocr_lock(self) -> Path:
        return self.runtime / "bravoric-stt-clipboard" / "ocr.lock"

    def write_status(self, payload: dict) -> None:
        path = self.home / ".cache" / "bravoric-stt-clipboard" / "status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"timestamp": time.time(), **payload}))

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


def wait_for(cond, timeout: float = 10.0, step: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # uno zombie non e' vivo / a zombie is not alive
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


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


    print("== STT start/stop ESPLICITI: il pulsante ferma davvero, senza tastiera / explicit start/stop ==")
    env = Env()
    try:
        env.write_config([good])
        first = env.cli("stt_toggle_main", ("start",))
        check("start esplicito: registrazione avviata (lock creato)", first.returncode == 0 and env.stt_lock().exists(), first.stderr[-200:])
        lock_before = env.stt_lock().read_text()
        again = env.cli("stt_toggle_main", ("start",))
        check("secondo start con registrazione in corso: no-op (stesso lock, nessun secondo ffmpeg)",
              again.returncode == 0 and env.stt_lock().read_text() == lock_before)
        # Lo stop arriva SUBITO (debounce da 0.1 s nel test: ffmpeg finto appena partito): attende, non sparisce.
        # The stop arrives RIGHT AWAY (0.1 s debounce in the test): it waits, it does not vanish.
        stopped = env.cli("stt_toggle_main", ("stop",))
        check("stop esplicito: ferma la registrazione e trascrive (exit 0)", stopped.returncode == 0, stopped.stderr[-300:])
        check("stop: lock rimosso, audio temporaneo ripulito, appunti scritti, stato idle",
              not env.stt_lock().exists() and not list(env.tmp.glob("*")) and "Ciao mondo, prova." in env.clipboard_writes()
              and env.status().get("state") == "idle", f"{env.clipboard_writes()} {env.status()} {list(env.tmp.glob('*'))}")
        writes_before = list(env.clipboard_writes())
        again = env.cli("stt_toggle_main", ("stop",))
        check("stop IDEMPOTENTE: senza registrazione non ne avvia una nuova (nessun lock, rc 0)",
              again.returncode == 0 and not env.stt_lock().exists() and not list(env.tmp.glob("*")))
        check("stop idempotente: nessuna scrittura negli appunti", env.clipboard_writes() == writes_before)
    finally:
        env.cleanup()

    print("== STT stop dentro il debounce: attende, non ignora / stop inside the debounce waits ==")
    env = Env()
    try:
        env.write_config([good], extra_audio="")
        cfg = env.home / ".config" / "bravoric-stt-clipboard" / "config.toml"
        cfg.write_text(cfg.read_text().replace("toggle_debounce_seconds = 0.1", "toggle_debounce_seconds = 1.5"))
        env.cli("stt_toggle_main", ("start",))
        started = time.time()
        stopped = env.cli("stt_toggle_main", ("stop",))
        elapsed = time.time() - started
        check("stop a meta' debounce (1.5 s): il click non e' perso, la registrazione si ferma e trascrive",
              stopped.returncode == 0 and not env.stt_lock().exists() and "Ciao mondo, prova." in env.clipboard_writes(),
              f"{stopped.stderr[-200:]} {env.clipboard_writes()}")
        check("stop nel debounce: ha atteso il residuo del debounce (>= 1 s), non e' tornato subito",
              elapsed >= 1.0, f"{elapsed:.2f}s")
        # Il toggle da scorciatoia resta invariato: dentro il debounce viene ignorato (nessuna regressione).
        # The shortcut toggle is unchanged: inside the debounce it is ignored (no regression).
        env.cli("stt_toggle_main", ("start",))
        toggled = env.toggle()
        check("toggle da scorciatoia dentro il debounce: ignorato come prima (lock ancora presente)",
              toggled.returncode == 0 and env.stt_lock().exists())
        env.cli("stt_toggle_main", ("stop",))
    finally:
        env.cleanup()

    print("== STT stato residuo: stop ripulisce un 'recording' senza lock / stale state ==")
    env = Env()
    try:
        env.write_config([good])
        env.write_status({"state": "recording", "service": "stt"})
        result = env.cli("stt_toggle_main", ("stop",))
        check("status 'recording/stt' senza lock (backend morto): lo stop lo riporta a idle e NON avvia una registrazione",
              result.returncode == 0 and env.status().get("state") == "idle" and not env.stt_lock().exists(), str(env.status()))
        env.write_status({"state": "recording"})
        env.cli("stt_toggle_main", ("stop",))
        check("status 'recording' senza service e senza lock: ripulito anch'esso",
              env.status().get("state") == "idle", str(env.status()))
        env.write_status({"state": "recording", "service": "stream"})
        env.cli("stt_toggle_main", ("stop",))
        check("status 'recording/stream' (altro servizio): lo stop STT non lo tocca",
              env.status().get("state") == "recording" and env.status().get("service") == "stream", str(env.status()))
    finally:
        env.cleanup()

    print("== OCR annullabile: selezione screenshot / cancellable OCR: screenshot selection ==")
    env = Env()
    try:
        env.set_screenshot("hang")
        env.write_ocr_config(good, capture_screenshot=True, extra_ocr="screenshot_timeout_seconds = 60\n")
        proc = env.spawn("ocr_capture_main", ("start",))
        check("selezione avviata: lock OCR con il pid del processo e del gnome-screenshot",
              wait_for(lambda: env.ocr_lock().exists() and "child_pid" in env.ocr_lock().read_text()), str(env.status()))
        lock = json.loads(env.ocr_lock().read_text())
        check("durante la selezione lo stato e' processing/ocr (annullabile: nessun cancellable=false)",
              env.status().get("state") == "processing" and env.status().get("service") == "ocr"
              and env.status().get("cancellable") is not False, str(env.status()))
        second = env.cli("ocr_capture_main", ("start",))
        check("secondo start con selezione attiva: ignorato (nessuna seconda cattura)",
              second.returncode == 0 and json.loads(env.ocr_lock().read_text())["pid"] == lock["pid"])
        started = time.time()
        cancelled = env.cli("ocr_capture_main", ("cancel",))
        elapsed = time.time() - started
        check("cancel: exit 0 e il processo OCR termina in pochi secondi (senza tastiera, senza Esc)",
              cancelled.returncode == 0 and wait_for(lambda: proc.poll() is not None, 5) and elapsed < 6,
              f"{cancelled.stderr[-200:]} {elapsed:.1f}s")
        check("cancel: il gnome-screenshot di selezione e' chiuso (nessun overlay orfano)",
              wait_for(lambda: not pid_alive(lock["child_pid"]), 3), str(lock))
        check("cancel: lock rimosso, stato idle, appunti NON toccati, nessuna notifica d'errore",
              not env.ocr_lock().exists() and env.status().get("state") == "idle" and env.clipboard_writes() == []
              and not any("error" in n.lower() for n in env.notifications()), f"{env.status()} {env.notifications()}")
        check("processo OCR annullato: uscita pulita (rc 0)", proc.returncode == 0, str(proc.returncode))
        again = env.cli("ocr_capture_main", ("cancel",))
        check("cancel IDEMPOTENTE: senza OCR attivo non fa nulla e non avvia una cattura (rc 0, nessun lock)",
              again.returncode == 0 and not env.ocr_lock().exists() and env.clipboard_writes() == [])
    finally:
        env.cleanup()

    print("== OCR: la scorciatoia (toggle senza argomenti) annulla se attivo / shortcut toggle cancels ==")
    env = Env()
    try:
        env.set_screenshot("hang")
        env.write_ocr_config(good, capture_screenshot=True, extra_ocr="screenshot_timeout_seconds = 60\n")
        proc = env.spawn("ocr_capture_main", ())
        wait_for(lambda: env.ocr_lock().exists() and "child_pid" in env.ocr_lock().read_text())
        toggled = env.ocr()
        check("seconda pressione della scorciatoia con selezione attiva: annulla (mai una seconda selezione)",
              toggled.returncode == 0 and wait_for(lambda: proc.poll() is not None, 5) and env.status().get("state") == "idle"
              and not env.ocr_lock().exists(), f"{toggled.stderr[-200:]} {env.status()}")
    finally:
        env.cleanup()

    print("== OCR annullato durante la richiesta lenta: nessuna scrittura negli appunti / cancel during slow request ==")
    env = Env()
    FakeApi.delay = 20
    try:
        env.set_wl_paste_image(True)
        env.write_ocr_config(good)
        hits_before = len(FakeApi.hits)
        proc = env.spawn("ocr_capture_main", ("start",))
        check("richiesta di visione partita (server raggiunto) e stato processing/ocr",
              wait_for(lambda: len(FakeApi.hits) > hits_before and env.status().get("state") == "processing"), str(env.status()))
        cancelled = env.cli("ocr_capture_main", ("cancel",))
        check("cancel durante la richiesta: il processo termina (rc 0) entro pochi secondi",
              cancelled.returncode == 0 and wait_for(lambda: proc.poll() is not None, 6) and proc.returncode == 0,
              f"{cancelled.stderr[-200:]} {proc.poll()}")
        check("cancel durante la richiesta: appunti mai scritti, lock rimosso, stato idle",
              env.clipboard_writes() == [] and not env.ocr_lock().exists() and env.status().get("state") == "idle",
              f"{env.clipboard_writes()} {env.status()}")
    finally:
        FakeApi.delay = 0
        env.cleanup()

    print("== OCR: gia' alla scrittura negli appunti non promette un cancel impossibile / past the point of no return ==")
    env = Env()
    try:
        env.set_wl_paste_image(True)
        env.write_ocr_config(good)
        env.set_wl_copy_sleep(4)  # scrittura lenta: la finestra "committed" dura qualche secondo
        proc = env.spawn("ocr_capture_main", ("start",))
        check("alla scrittura negli appunti lo stato dichiara cancellable=false",
              wait_for(lambda: env.status().get("cancellable") is False, 8), str(env.status()))
        env.cli("ocr_capture_main", ("cancel",))
        proc.wait(timeout=30)
        env.set_wl_copy_sleep(0)
        check("cancel tardivo: rifiutato, l'OCR termina il proprio lavoro e lo stato non resta bloccato",
              env.status().get("state") in ("idle", "error") and not env.ocr_lock().exists(), str(env.status()))
    finally:
        env.cleanup()

    print("== stato condiviso: OCR e stop durante altri servizi / shared state: OCR and stop during other services ==")
    env = Env()
    try:
        env.write_config([good])
        env.cli("stt_toggle_main", ("start",))
        env.write_status({"state": "recording", "service": "stt"})
        env.set_wl_paste_image(True)
        env.write_ocr_config(good)  # sovrascrive la config: la dettatura in corso ha gia' letto la sua | overwrites the config: the running dictation already read its own
        env.cli("ocr_capture_main", ("start",))
        check("STT in registrazione + OCR completo: lo stato resta recording/stt (l'OCR non lo spegne)",
              env.status().get("state") == "recording" and env.status().get("service") == "stt", str(env.status()))
        # Ripristina la config di dettatura e chiude la registrazione.
        # Restore the dictation config and close the recording.
        env.write_config([good])
        env.cli("stt_toggle_main", ("stop",))
        env.write_status({"state": "processing", "service": "stt"})
        env.cli("stt_toggle_main", ("stop",))
        check("stop STT durante 'processing' (nessun lock): non tocca lo stato e non avvia nulla",
              env.status().get("state") == "processing" and not env.stt_lock().exists(), str(env.status()))
        env.write_status({"state": "idle"})
    finally:
        env.cleanup()

    print("== OCR: lock morto e residui / dead lock and leftovers ==")
    env = Env()
    try:
        env.set_wl_paste_image(True)
        env.write_ocr_config(good)
        env.ocr_lock().parent.mkdir(parents=True, exist_ok=True)
        env.ocr_lock().write_text(json.dumps({"pid": 2 ** 22 - 3, "started_at": time.time()}))
        result = env.cli("ocr_capture_main", ("start",))
        check("lock di un processo morto: e' un residuo, l'OCR parte e completa",
              result.returncode == 0 and "testo letto dall'immagine" in env.clipboard_writes() and not env.ocr_lock().exists(),
              f"{result.stderr[-200:]} {env.clipboard_writes()}")
        env.write_status({"state": "processing", "service": "ocr"})
        result = env.cli("ocr_capture_main", ("cancel",))
        check("status 'processing/ocr' senza OCR vivo: cancel lo riporta a idle (il controllo non resta su Annulla)",
              result.returncode == 0 and env.status().get("state") == "idle", str(env.status()))
        env.write_status({"state": "recording", "service": "stt"})
        env.cli("ocr_capture_main", ("cancel",))
        check("cancel non tocca una registrazione STT in corso", env.status().get("state") == "recording", str(env.status()))
    finally:
        env.cleanup()

    server.shutdown()
    print()
    print(f"{PASS} PASS / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
