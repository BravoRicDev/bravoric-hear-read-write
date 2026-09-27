"""Lettura/scrittura clipboard Wayland via wl-copy / wl-paste."""
from __future__ import annotations

import subprocess


def write_text(text: str, tool: str = "wl-copy") -> None:
    # B26: timeout per evitare che wl-copy/wl-paste resti appeso se il
    # compositor è assente o occupato.
    subprocess.run([tool], input=text.encode("utf-8"), check=True, timeout=5)


def read_image_png(tool: str = "wl-paste") -> bytes:
    try:
        result = subprocess.run(
            [tool, "-t", "image/png"], capture_output=True, check=True, timeout=5,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"no PNG image in the clipboard (exit {exc.returncode})") from exc
    return result.stdout
