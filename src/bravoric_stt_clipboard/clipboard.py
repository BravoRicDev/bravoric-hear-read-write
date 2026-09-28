"""Lettura/scrittura clipboard Wayland via wl-copy / wl-paste."""
from __future__ import annotations

import subprocess


def write_text(text: str, tool: str = "wl-copy", timeout: float = 5.0) -> None:
    # B26: timeout per evitare che wl-copy/wl-paste resti appeso se il
    # compositor è assente o occupato. Regolabile: [general]
    # clipboard_timeout_seconds.
    # B26: timeout so that wl-copy/wl-paste never hangs when the compositor
    # is absent or busy. Tunable: [general] clipboard_timeout_seconds.
    subprocess.run([tool], input=text.encode("utf-8"), check=True, timeout=timeout)


def read_image_png(tool: str = "wl-paste", timeout: float = 5.0) -> bytes:
    try:
        result = subprocess.run(
            [tool, "-t", "image/png"], capture_output=True, check=True, timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"no PNG image in the clipboard (exit {exc.returncode})") from exc
    return result.stdout
