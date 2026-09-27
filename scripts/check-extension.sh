#!/usr/bin/env bash
# check-extension.sh — guardia di regressione rapida (estensione + backend).
# Non richiede GNOME: sintassi, metadata.json, i18n (entrambe le po), logica di
# timeout e test del backend Python.
# Uso: scripts/check-extension.sh   (exit != 0 se qualcosa fallisce)
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$REPO/gnome-extension/bravoric-indicator@local"
FAIL=0
ok()  { printf '  PASS  %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL + 1)); }

echo "== sintassi =="
if node --check "$EXT/extension.js" >/dev/null 2>&1; then ok "extension.js"; else bad "extension.js"; fi
if node --check "$EXT/prefs.js" >/dev/null 2>&1; then ok "prefs.js"; else bad "prefs.js"; fi
if bash -n "$REPO/scripts/install.sh" 2>/dev/null; then ok "install.sh"; else bad "install.sh"; fi

echo "== metadata.json =="
if python3 - "$EXT" <<'PY'
import json, os, sys
ext = sys.argv[1]
meta = json.load(open(os.path.join(ext, "metadata.json")))
errs = []
if meta.get("uuid") != os.path.basename(ext):
    errs.append(f"uuid {meta.get('uuid')!r} != cartella {os.path.basename(ext)!r}")
if not meta.get("settings-schema"):
    errs.append("manca 'settings-schema' (crash in enable(): schema_id undefined)")
for e in errs:
    print("    " + e)
sys.exit(1 if errs else 0)
PY
then ok "uuid coerente e settings-schema presente"; else bad "metadata.json"; fi

echo "== schema GSettings compilato =="
if python3 - "$EXT" <<'PY'
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

ext = sys.argv[1]
schemas_dir = os.path.join(ext, "schemas")
metadata = json.load(open(os.path.join(ext, "metadata.json"), encoding="utf-8"))
schema_id = metadata["settings-schema"]
xml_path = os.path.join(schemas_dir, f"{schema_id}.gschema.xml")
source_keys = {
    key.attrib["name"]
    for key in ET.parse(xml_path).getroot().findall(f".//schema[@id='{schema_id}']/key")
}
try:
    result = subprocess.run(
        ["gsettings", "--schemadir", schemas_dir, "list-keys", schema_id],
        check=True, capture_output=True, text=True,
    )
except (OSError, subprocess.CalledProcessError) as exc:
    print(f"    schema compilato non leggibile: {exc}")
    sys.exit(1)
compiled_keys = set(result.stdout.splitlines())
missing = sorted(source_keys - compiled_keys)
if missing:
    print(f"    gschemas.compiled stantio, chiavi mancanti: {missing}")
    sys.exit(1)
PY
then ok "gschemas.compiled sincronizzato con XML"; else bad "schema GSettings compilato"; fi

echo "== i18n =="
if python3 - "$EXT/po/it.po" "$EXT/po/bravoric-indicator.pot" <<'PY'
import re, sys
po, pot = sys.argv[1], sys.argv[2]

def msgids(path):
    # Giro 16: un regex su riga singola perdeva i msgid avvolti su più righe
    # (msgid "" seguito da "..." di continuazione, xgettext li produce per
    # stringhe lunghe — caso reale già presente in questo .po) — entrambi i
    # lati sarebbero stati scartati identicamente ("" - {""}), niente falso
    # positivo oggi, ma un futuro msgid multilinea davvero mancante non
    # sarebbe stato rilevato qui. Parser che segue le righe di continuazione.
    txt = open(path, encoding="utf-8").read()
    result, cur, mode = set(), None, None
    for raw in txt.splitlines():
        line = raw.strip()
        if line.startswith('msgid "') and line.endswith('"'):
            if cur is not None:
                result.add(cur)
            cur, mode = line[len('msgid "'):-1], "id"
        elif line.startswith('"') and line.endswith('"') and mode == "id":
            cur += line[1:-1]
        elif line.startswith("msgid_plural") or line.startswith("msgstr"):
            mode = None
    if cur is not None:
        result.add(cur)
    return result - {""}

errs = []
fuzzy = len(re.findall(r'^#, fuzzy', open(po, encoding="utf-8").read(), re.M))
if fuzzy:
    errs.append(f"{fuzzy} voci fuzzy in it.po")
missing = msgids(pot) - msgids(po)
if missing:
    errs.append(f"{len(missing)} msgid del .pot assenti in it.po: {sorted(missing)[:3]}")
for e in errs:
    print("    " + e)
sys.exit(1 if errs else 0)
PY
then ok "it.po completo, nessun fuzzy"; else bad "i18n"; fi

# Copertura reale: ogni stringa _()/N_() del sorgente deve risolversi nel .mo.
# (Il controllo sopra confronta solo .po vs .pot: se il .pot e' stantio, il gap
# non emerge. Qui si estrae dal sorgente JS e si interroga gettext davvero.)
if EXT_I18N_OUT=$(python3 - "$EXT" <<'PY'
import gettext, os, re, sys
ext = sys.argv[1]
src = ""
for name in ("extension.js", "prefs.js"):
    src += open(os.path.join(ext, name), encoding="utf-8").read()

def js_unescape(s):
    out, i = [], 0
    m = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            out.append(m.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)

strings = set()
for pat in (r"N_\(\s*'((?:[^'\\]|\\.)*)'", r"_\(\s*'((?:[^'\\]|\\.)*)'"):
    for m in re.finditer(pat, src):
        strings.add(js_unescape(m.group(1)))

t = gettext.translation(
    "bravoric-indicator",
    localedir=os.path.join(ext, "locale"),
    languages=["it"],
    fallback=True,
)
# Identiche per costruzione (acronimi, segnaposto puri, termini tecnici invariati): non serve tradurle.
IDENTICAL = {"OCR", "OK", "Endpoint", "Streaming", "Mode", "Venv: %s", "Schema: %s", "[%s] %s"}
untranslated = [s for s in sorted(strings) if t.gettext(s) == s and s not in IDENTICAL]
if untranslated:
    print(f"    {len(untranslated)} stringhe non tradotte: {untranslated[:5]}")
    sys.exit(1)
print(len(strings))
PY
); then
    ok "copertura gettext del sorgente ($EXT_I18N_OUT stringhe)"
else
    echo "$EXT_I18N_OUT"
    bad "copertura i18n sorgente"
fi

echo "== i18n backend (dominio bravoric-stt-clipboard) =="
if python3 - "$REPO/po/it.po" "$REPO/po/bravoric-stt-clipboard.pot" <<'PY'
import re, sys
po, pot = sys.argv[1], sys.argv[2]

def msgids(path):
    # Giro 16: stesso fix del blocco estensione sopra, msgid multilinea.
    txt = open(path, encoding="utf-8").read()
    result, cur, mode = set(), None, None
    for raw in txt.splitlines():
        line = raw.strip()
        if line.startswith('msgid "') and line.endswith('"'):
            if cur is not None:
                result.add(cur)
            cur, mode = line[len('msgid "'):-1], "id"
        elif line.startswith('"') and line.endswith('"') and mode == "id":
            cur += line[1:-1]
        elif line.startswith("msgid_plural") or line.startswith("msgstr"):
            mode = None
    if cur is not None:
        result.add(cur)
    return result - {""}

errs = []
fuzzy = len(re.findall(r'^#, fuzzy', open(po, encoding="utf-8").read(), re.M))
if fuzzy:
    errs.append(f"{fuzzy} voci fuzzy in it.po")
missing = msgids(pot) - msgids(po)
if missing:
    errs.append(f"{len(missing)} msgid del .pot assenti in it.po: {sorted(missing)[:3]}")
for e in errs:
    print("    " + e)
sys.exit(1 if errs else 0)
PY
then ok "po/it.po backend completo, nessun fuzzy"; else bad "i18n backend"; fi

# Giro 2 (F6): il blocco sopra confronta solo .po vs .pot (presenza del msgid).
# Una voce presente con msgstr "" passava, e gettext in Python restituiva
# l'inglese. Il blocco dell'estensione (riga 109) faceva gia' il controllo
# giusto: qui si replica per il dominio backend, interrogando il .mo compilato.
if BACKEND_I18N_OUT=$(python3 - "$REPO/src/bravoric_stt_clipboard" <<'PY'
import gettext, os, re, sys
src_root = sys.argv[1]
src = ""
for name in sorted(os.listdir(src_root)):
    if not name.endswith(".py"):
        continue
    with open(os.path.join(src_root, name), encoding="utf-8") as handle:
        src += handle.read()

def py_unescape(s):
    out, i = [], 0
    mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            out.append(mapping.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)

# Chiamate _() con argomento letterale (concatenazione compresa: _("a" "b")).
strings = set()
for match in re.finditer(r"(?<![\w.])_\(\s*((?:\"(?:[^\"\\]|\\.)*\"\s*)+)", src):
    parts = re.findall(r"\"((?:[^\"\\]|\\.)*)\"", match.group(1))
    strings.add(py_unescape("".join(parts)))
strings = {s for s in strings if s}

# Guardia di non-vacuità: se l'estrazione non trova nulla il controllo passerebbe
# SEMPRE, cioè esattamente il difetto che questa voce deve chiudere. Il .pot del
# backend è la lista attesa: se le stringhe estratte sono meno, il gate è rotto.
pot = os.path.join(os.path.dirname(src_root), "po", "bravoric-stt-clipboard.pot")
pot_ids = set()
if os.path.exists(pot):
    with open(pot, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if line.startswith('msgid "') and line.endswith('"'):
                pot_ids.add(line[6:-1])
pot_ids.discard("")
if not strings:
    print("    estrazione vuota: nessuna chiamata _() trovata (controllo vacuo)")
    sys.exit(1)
if pot_ids and len(strings) < len(pot_ids):
    print(f"    estratte {len(strings)} stringhe ma il .pot ne elenca {len(pot_ids)}: "
          "estrazione incompleta, controllo vacuo")
    sys.exit(1)

t = gettext.translation(
    "bravoric-stt-clipboard",
    localedir=os.path.join(src_root, "locale"),
    languages=["it"],
    fallback=True,
)
# Identiche per costruzione: acronimi e termini tecnici invariati.
IDENTICAL = {"OCR", "OK", "STT", "API", "HTTP", "HTTPS", "JSON", "URL", "GET", "POST",
             "Venv: %s", "Schema: %s", "Endpoint", "Streaming", "Mode", "[%s] %s"}
untranslated = [s for s in sorted(strings) if t.gettext(s) == s and s not in IDENTICAL]
if untranslated:
    print(f"    {len(untranslated)} stringhe backend non tradotte: {untranslated[:5]}")
    sys.exit(1)
print(len(strings))
PY
); then
    ok "copertura gettext backend ($BACKEND_I18N_OUT stringhe)"
else
    echo "$BACKEND_I18N_OUT"
    bad "copertura i18n backend"
fi

# Guardia giro 14: un mismatch di segnaposto %s/%d tra msgid e msgstr non fa
# fallire msgfmt/gettext (JSON valido, testo sbagliato) — verificato una
# tantum al giro 8 ma mai controllato automaticamente da questa suite.
# Parser minimale che gestisce anche msgstr "" seguito da righe "..." avvolte.
echo "== i18n segnaposto %s/%d (entrambi i domini) =="
if python3 - "$EXT/po/it.po" "$REPO/po/it.po" <<'PY'
import sys

def entries(text):
    result, msgid, msgstr, mode = [], None, None, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('msgid "') and line.endswith('"'):
            if msgid is not None:
                result.append((msgid, msgstr or ""))
            msgid, msgstr, mode = line[len('msgid "'):-1], None, 'id'
        elif line.startswith('msgstr "') and line.endswith('"'):
            msgstr, mode = line[len('msgstr "'):-1], 'str'
        elif line.startswith('"') and line.endswith('"') and mode == 'id':
            msgid += line[1:-1]
        elif line.startswith('"') and line.endswith('"') and mode == 'str':
            msgstr += line[1:-1]
        elif line.startswith('msgid_plural') or line.startswith('msgstr['):
            mode = None  # plural forms non usate in questo progetto, salta
    if msgid is not None:
        result.append((msgid, msgstr or ""))
    return result

errs = []
for pofile in sys.argv[1:]:
    txt = open(pofile, encoding="utf-8").read()
    for msgid, msgstr in entries(txt):
        if not msgid or not msgstr:
            continue
        for spec in ("%s", "%d"):
            if msgid.count(spec) != msgstr.count(spec):
                errs.append(f"{pofile}: {spec} {msgid.count(spec)}->{msgstr.count(spec)} in {msgid!r}")
for e in errs[:5]:
    print("    " + e)
if len(errs) > 5:
    print(f"    ... e altri {len(errs) - 5}")
sys.exit(1 if errs else 0)
PY
then ok "segnaposto %s/%d coerenti tra msgid e msgstr"; else bad "segnaposto %s/%d"; fi

echo "== prefs voice commands (static/node) =="
if node "$REPO/scripts/test-prefs-voice-commands.js"; then
    ok "test-prefs-voice-commands.js"
else
    bad "test-prefs-voice-commands.js"
fi

if command -v gjs >/dev/null 2>&1; then
    echo "== prefs voice commands (gjs smoke) =="
    if gjs -m "$REPO/scripts/test-smoke-gjs-prefs.js"; then
        ok "test-smoke-gjs-prefs.js"
    else
        bad "test-smoke-gjs-prefs.js"
    fi

    # Giro 4 (F5): la pagina Shortcuts validava i CONFLITTI (stringa identica
    # in cinque schemi di sistema) ma non la VALIDITA' dell'acceleratore:
    # una scorciotia che Mutter rifiuta finiva in dconf per sempre e non
    # accadeva mai, senza messaggio. Qui si esegue la funzione REALE estratta
    # da prefs.js sotto gjs vero.
    echo "== validazione acceleratori (gjs, funzione reale) =="
    if (cd "$REPO" && gjs -m "$REPO/scripts/test-shortcut-accelerator.js"); then
        ok "test-shortcut-accelerator.js (21 asserzioni)"
    else
        bad "test-shortcut-accelerator.js"
    fi

    # Giro 2 (F2): TomlBoolEditor rendeva config.toml illeggibile. Il test
    # esegue la CLASSE REALE estratta da prefs.js e valida il risultato con
    # tomllib: non un confronto di stringhe, un parser vero.
    echo "== TomlBoolEditor (gjs, classe reale) =="
    if (cd "$REPO" && gjs -m "$REPO/scripts/test-toml-bool-editor.js"); then
        ok "test-toml-bool-editor.js"
    else
        bad "test-toml-bool-editor.js"
    fi
fi

echo "== consumer streaming (unit) =="
if node "$REPO/scripts/test-stream-consumer.js"; then
    ok "test-stream-consumer.js"
else
    bad "test-stream-consumer.js"
fi

echo "== logica timeout (unit) =="
if node "$REPO/scripts/test-timeout-logic.js"; then
    ok "test-timeout-logic.js (27 asserzioni)"
else
    bad "test-timeout-logic.js"
fi

# Giro 4 (F1 + F2): `ln -sfn` e' silenziosamente inerte se la destinazione e'
# una directory REALE (la copia di `gnome-extensions install`), quindi le
# righe dopo girano sulla copia e il gate delle chiavi passa: upgrade
# apparentemente riuscito col modulo vecchio in memoria. E l'unico messaggio
# sul reload parlava solo della prima installazione, il contrario di quello
# che serve dopo un `git pull`. Il blocco "estensione GNOME Shell" di
# install.sh viene ESEGUITO davvero in un XDG_DATA_HOME finto: non viene
# eseguito install.sh per intero, che scriverebbe in ~/.config e
# ~/.local/share reali.
echo "== install.sh (F1/F2, blocco estensione in XDG finto) =="
if bash "$REPO/scripts/test-install-extension-dir.sh" >/dev/null 2>&1; then
    ok "test-install-extension-dir.sh (20 controlli)"
else
    bad "test-install-extension-dir.sh"
fi

echo "== icon slot completeness =="
if PYTHONPATH="$REPO/src" python3 "$REPO/scripts/test-icon-completeness.py"; then ok "test-icon-completeness.py"; else bad "test-icon-completeness.py"; fi

echo "== backend python (unit) =="
PY=""
# P2 (giro 15): install.sh rispetta XDG_DATA_HOME (portabilità multi-macchina,
# es. NixOS/home separata); qui era hardcoded su ~/.local/share, quindi con
# XDG_DATA_HOME impostato il venv reale non veniva trovato, si cadeva sul
# python3 di sistema (senza 'requests') e la suite falliva con un FAIL
# fuorviante su un'installazione corretta.
XDG_DATA_HOME_="${XDG_DATA_HOME:-$HOME/.local/share}"
for cand in "$XDG_DATA_HOME_/bravoric-stt-clipboard/venv/bin/python" "$REPO/.venv/bin/python"; do
    if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then PY="$(command -v python3 || true)"; fi
BACKEND_SUMMARY=""
BACKEND_STDERR=""
if [ -n "$PY" ]; then
    BACKEND_STDERR_FILE="$(mktemp)"
    BACKEND_SUMMARY="$(PYTHONPATH="$REPO/src" "$PY" "$REPO/scripts/test-backend.py" 2>"$BACKEND_STDERR_FILE" | tail -1)"
    BACKEND_STDERR="$(cat "$BACKEND_STDERR_FILE")"
    rm -f "$BACKEND_STDERR_FILE"
fi
if printf '%s' "$BACKEND_SUMMARY" | grep -qE '^[0-9]+ PASS / 0 FAIL$'; then
    ok "test-backend.py ($(printf '%s' "$BACKEND_SUMMARY" | awk '{print $1}') asserzioni)"
else
    # Giro 16: prima lo stderr era scartato (2>/dev/null) — un'eccezione
    # Python non gestita prima della riga finale dava solo "FAIL
    # test-backend.py" senza traceback, costringendo a rilanciare a mano.
    if [ -n "$BACKEND_STDERR" ]; then
        # ${var//pattern/repl} non antepone il prefisso a OGNI riga di una
        # variabile multi-riga, solo sostituisce match letterali; qui serve
        # indentare ogni riga, quindi resta sed.
        # shellcheck disable=SC2001
        echo "$BACKEND_STDERR" | sed 's/^/    /'
    fi
    bad "test-backend.py"
fi

echo
if [ "$FAIL" -eq 0 ]; then
    echo "TUTTO OK"
else
    echo "$FAIL controlli falliti"
fi
exit "$FAIL"
