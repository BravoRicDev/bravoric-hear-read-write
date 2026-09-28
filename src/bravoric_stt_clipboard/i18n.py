"""Traduzioni backend Python: stringhe sorgente in inglese, cataloghi .mo
per le altre lingue (oggi l'italiano) in locale/<lang>/LC_MESSAGES/. Lingua
rilevata dalle variabili d'ambiente di sistema (LANGUAGE/LC_ALL/LC_MESSAGES/
LANG).

Python backend translations: English source strings, .mo catalogs for the
other languages (today Italian) in locale/<lang>/LC_MESSAGES/. The language
is detected from the system environment variables (LANGUAGE/LC_ALL/
LC_MESSAGES/LANG).
"""
from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "bravoric-stt-clipboard"
LOCALE_DIR = Path(__file__).resolve().parent / "locale"

_translation = gettext.translation(DOMAIN, localedir=LOCALE_DIR, fallback=True)
_ = _translation.gettext
