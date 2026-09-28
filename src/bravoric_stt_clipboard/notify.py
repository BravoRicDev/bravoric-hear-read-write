"""Notifiche native GNOME via notify-send."""
from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

from .config import ICON_SLOT_REGISTRY, NotificationEvent

# Stessi nomi icona usati dall'indicatore top-bar (extension.js ICONS), per
# coerenza visiva tra notifica e stato mostrato in alto a destra.
ICON_RECORDING = "media-record-symbolic"
ICON_PROCESSING = "content-loading-symbolic"
ICON_ERROR = "dialog-error-symbolic"
ICON_READY = "edit-copy-symbolic"

# Icone custom per servizio/fase (fotocamera=OCR, microfono=STT; avvio=stile
# neutro, grezzo=legno, pulito=cyberpunk). Bundlate nel pacchetto Python
# (non nel checkout git) per restare portabili dopo `pip install`.
ICONS_DIR = Path(__file__).resolve().parent / "icons"
_PACKAGED_DEFAULTS = {
    "stt_start": "mic-neutral.png", "stt_raw": "mic-wood.png",
    "stt_clean": "mic-cyberpunk.png", "ocr_start": "camera-neutral.png",
    "ocr_raw": "camera-wood.png", "ocr_clean": "camera-cyberpunk.png",
    "stream_session_start": "stream-session-start.png",
    "error_general": "error-general.png",
}
_FALLBACKS = {
    "processing": ICON_PROCESSING, "ready": ICON_READY,
    "recording": ICON_RECORDING, "error": ICON_ERROR,
}


def resolve_icon(slot: str, override: str = "") -> str:
    """Resolve a registered slot: valid user override, packaged asset, theme icon.

    Bad/missing user paths intentionally fall through and are never fatal.
    """
    metadata = next((item for item in ICON_SLOT_REGISTRY if item.key == slot), None)
    if metadata is None:
        raise ValueError(f"Unknown icon slot: {slot}")
    if override:
        try:
            path = Path(override).expanduser()
            if path.is_file():
                return str(path)
        except (OSError, ValueError):
            pass
    filename = _PACKAGED_DEFAULTS.get(slot)
    if filename:
        packaged = ICONS_DIR / filename
        if packaged.is_file():
            return str(packaged)
    return _FALLBACKS[metadata.fallback_category]


# Impostazioni di runtime, fissate una volta per processo da configure()
# dopo il caricamento della config ([general] notify_timeout_seconds e
# notification_content_max_chars). I default sono i valori storici.
# Runtime settings, set once per process by configure() after the config is
# loaded ([general] notify_timeout_seconds and notification_content_max_chars).
# The defaults are the historical values.
_send_timeout_seconds: float = 10.0
_content_max_chars: int = 80


def configure(cfg) -> None:
    """Applica le impostazioni di notifica della config a questo processo.

    Apply the notification settings of the config to this process.
    """
    global _send_timeout_seconds, _content_max_chars
    _send_timeout_seconds = float(getattr(cfg, "notify_timeout_seconds", 10.0))
    _content_max_chars = int(getattr(cfg, "notification_content_max_chars", 80))


def send(title: str, body: str = "", icon: str = ICON_READY) -> None:
    # B27a: usa -- per evitare che un titolo che inizia con `-` sia
    # interpretato come opzione di notify-send. Deve stare DOPO le opzioni
    # reali (-i): prima di -i, GOption smette di riconoscere opzioni e -i
    # stesso diventa il primo argomento posizionale (SUMMARY) — bug reale,
    # riprodotto dal vivo: ogni notifica falliva silenziosamente (exit 1).
    # B27b: cattura OSError (non solo FileNotFoundError) per evitare
    # che un errore di notify interrompa il flusso principale.
    # Timeout come per wl-copy/wl-paste (B26): senza, un server di notifiche
    # che non risponde blocca notify-send sul timeout D-Bus e con lui il
    # flusso chiamante. TimeoutExpired NON e' un OSError.
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(["notify-send", "-i", icon, "--", title, body], check=False, timeout=_send_timeout_seconds)


def maybe_send(
    master_enabled: bool, event: NotificationEvent, title: str, content: str = "", icon: str = ICON_READY
) -> None:
    if not master_enabled or not event.enabled:
        return
    body = content[:_content_max_chars] if event.content and content else ""
    send(title, body, icon)


def maybe_send_simple(
    master_enabled: bool,
    enabled: bool,
    title: str,
    body: str = "",
    icon: str = ICON_PROCESSING,
) -> None:
    if master_enabled and enabled:
        send(title, body, icon)
