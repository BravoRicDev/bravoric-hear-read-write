# Bravoric STT/OCR Clipboard

A GNOME Shell extension + Python backend for voice dictation and screenshot OCR on
Wayland, with a resilient multi-endpoint fallback chain for speech-to-text and
LLM cleanup — including a fully local, offline path via a self-hosted Whisper server.

Three ways to get text into the clipboard without typing it:

- **STT (push-to-talk dictation)** — press a shortcut, speak, press again: the
  recording is transcribed and written to the clipboard.
- **OCR (screenshot capture)** — select a region of the screen, get the text
  it contains. By default it reads whatever image is already in the
  clipboard (e.g. from your own screenshot tool); optionally, it can trigger
  an interactive area selection itself (`gnome-screenshot`) on the same
  shortcut.
- **Streaming dictation** — continuous, low-latency dictation that types
  directly into the focused field as you speak, with voice commands
  ("new line", "delete last word", …) and live context so the model sees
  what was already typed.

Mouse and touch friendly: optional one-click buttons in the top bar (dictation,
OCR, streaming; Preferences → General → Quick buttons, all off by default) start
and stop the actions without opening the menu.

A top-bar indicator shows the current state (idle / recording / processing)
and a preferences window (GTK4/Adw) configures every part of it — no config
file editing required for day-to-day use.

## Why the fallback chain

Every transcription/cleanup call goes through an ordered list of
OpenAI-compatible endpoints (`[[stt.fallback]]` in the config): if one is
unreachable, in cooldown after repeated failures, or returns an empty result,
the next one is tried automatically. This is what makes a completely
**offline** setup practical: point the first level at a local
[`bin/whisper-server.py`](bin/whisper-server.py) (a `faster-whisper` model
served behind an OpenAI-compatible HTTP API) with cloud endpoints as
lower-priority fallback,
or run any mix of local/remote/free-tier endpoints in whatever order fits.
Streaming dictation additionally supports a parallel dispatch mode across
endpoints marked `parallel = true`, with a per-endpoint concurrency gate and
circuit-breaker cooldowns (see `endpoint_breaker.py`) so an endpoint that
starts failing doesn't stall the whole session.

## Architecture

Two processes, one shared state directory:

- **`gnome-extension/bravoric-indicator@local/`** — the GNOME Shell extension
  (GJS/ESM): top-bar indicator, preferences UI, and the streaming-dictation
  keystroke/paste worker. Talks to the backend only through files (status,
  config, stream context) and by spawning the CLI entry points below —
  never a direct in-process dependency.
- **`src/bravoric_stt_clipboard/`** — the Python backend: audio capture,
  the fallback/circuit-breaker chain, OCR, clipboard/paste, notifications,
  atomic config editing, and the streaming-dictation supervisor
  (VAD-based chunk segmentation, per-endpoint dispatch, in-order delivery).

Every write to shared state (config, status, history, chunk log) is atomic
(`tempfile.mkstemp` + `fsync` + `os.replace`) and lock-protected
(`fcntl.flock`), so a crash or a killed process never leaves a half-written
file behind.

## Requirements

- GNOME Shell 45–50 on Wayland
- Python ≥ 3.11
- `ffmpeg` (with `libopus`), `wl-clipboard`, `notify-send`,
  `glib-compile-schemas`, `gsettings`
- Optional: `gnome-screenshot`, only if you enable *Take screenshot on
  capture* (`[ocr] capture_screenshot = true`). If it is missing the OCR
  shortcut shows a clear notification instead of failing silently.

## Install

```sh
scripts/install.sh
```

Checks dependencies, creates a dedicated venv under
`$XDG_DATA_HOME/bravoric-stt-clipboard`, installs the backend into it,
copies a starter config to `~/.config/bravoric-stt-clipboard/config.toml`
(locale-aware: Italian or English template), and links the extension into
GNOME Shell's extensions directory. Idempotent — safe to re-run after a
`git pull`.

Default shortcuts (changeable in the preferences): `Alt+Super+R` dictation,
`Alt+Super+O` OCR, `Alt+Super+S` streaming dictation.

Then enable the extension (`gnome-extensions enable bravoric-indicator@local`,
or via the Extensions app) and open its preferences to point the fallback
chain at your endpoints.

## Language and customization

- **English and Italian** everywhere a user can read text: the preferences
  window, the indicator menu, notifications, `install.sh` and the
  `bravoric-chunk-log` help. The language follows `LANGUAGE`/`LC_ALL`/
  `LC_MESSAGES`/`LANG`; anything not Italian falls back to English. The
  config template installed on first run follows the same rule.
- **Everything is configurable from the preferences window**: fallback
  levels, notification switches (one master switch, plus one per event and
  service), notification icons (one slot per event), audio, clipboard
  commands, streaming behaviour and diagnostics. The TOML file stays the
  source of truth; the window edits it in place.
- **Bilingual comments**: every code comment and docstring is written twice,
  Italian first and English second, in the same block. `scripts/check-extension.sh`
  fails on a substantial comment block that is clearly single-language, so new
  code follows the same convention.
- **Debugging the streaming chain**: `bravoric-chunk-log --summary` shows
  which endpoint served each chunk, failure counts and latency percentiles
  (`--last N`, `--session ID`, `--since MINUTES` narrow it down).

## Testing

```sh
scripts/check-extension.sh
```

One command, no running GNOME session required: JS/Python syntax, i18n
coverage (every UI string resolves in both locales, placeholders match),
GSettings schema sync, and the full test suite — including GJS smoke tests
that execute real extracted functions/classes from the extension source
(not hand-written copies), and 800+ backend assertions covering the fallback
chain, circuit breaker, atomic writes, and the streaming VAD/dispatch logic.

## Security and privacy

Dictated text and audio are sensitive, so the backend treats them that way
(each point below has a regression test):

- Everything under `~/.cache/bravoric-stt-clipboard/` and the config file
  are `0600`, including after log rotation; runtime directories holding
  streaming audio are created `0700`, owned by you, never a symlink.
- API keys never reach logs or notifications: error bodies, URLs and
  `Authorization: Bearer …` values are redacted (pattern-based and by the
  exact configured key) and truncated before they become an error message.
- Signals (`SIGINT`/`SIGTERM`/`SIGKILL`) are only sent to processes whose
  command line still matches ffmpeg or the streaming supervisor, so a stale
  lock with a reused PID can never kill an unrelated process.
- Config writes are atomic, locked and validated as TOML before replacing
  the file; user text is escaped, including control characters.
- One phrase blacklist (Preferences → Streaming) discards known Whisper
  hallucinations on silence, for both streaming and push-to-talk dictation.

## Development history

[`docs/`](docs/) has the working notes from building this: bug write-ups,
measured trade-offs, and the reasoning behind trickier decisions (mostly in
Italian). Not required reading to use the extension.

## License

TBD.
