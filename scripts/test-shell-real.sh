#!/usr/bin/env bash
# test-shell-real.sh — l'estensione dentro un GNOME Shell VERO (headless).
# The extension inside a REAL (headless) GNOME Shell.
#
# Avvia `gnome-shell --headless` su una sessione D-Bus dedicata, con config, dati,
# cache e runtime in una directory temporanea: non tocca la sessione dell'utente,
# il suo dconf, il suo venv o la sua config. Carica l'estensione vera piu' una
# sonda di test (scripts/lib/shell-probe@local) che descrive i widget della top bar
# e sa cliccare un bottone rapido. Verifica: caricamento (stato ACTIVE), bottoni
# rapidi che compaiono/spariscono dalle impostazioni, ordine, dimensioni, nomi
# accessibili, e che un click lanci davvero il binario del backend (un finto venv
# li registra in un file).
# It starts `gnome-shell --headless` on a dedicated D-Bus session, with config,
# data, cache and runtime in a temporary directory: it never touches the user's
# session, dconf, venv or config. It loads the real extension plus a test probe
# (scripts/lib/shell-probe@local) that describes the top-bar widgets and can click a
# quick button. It verifies: loading (ACTIVE state), quick buttons appearing and
# disappearing from the settings, order, sizes, accessible names, and that a click
# really launches the backend binary (a fake venv records them in a file).
#
# Se gnome-shell headless non e' disponibile (nessun GPU/EGL, strumenti mancanti)
# il test dichiara SKIP ed esce 0: non e' un fallimento del progetto.
# If headless gnome-shell is not available (no GPU/EGL, missing tools) the test
# says SKIP and exits 0: it is not a failure of the project.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_SRC="$REPO/gnome-extension/bravoric-indicator@local"
PROBE_SRC="$REPO/scripts/lib/shell-probe@local"

for tool in gnome-shell dbus-run-session gdbus gsettings gnome-extensions python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "  SKIP  strumento mancante / missing tool: $tool"
        exit 0
    fi
done

T="$(mktemp -d /tmp/brv-shell-XXXXXX)"
# BRV_KEEP=1 conserva la directory temporanea per il debug (log di gnome-shell inclusi).
# BRV_KEEP=1 keeps the temporary directory for debugging (gnome-shell logs included).
# La pulizia riprova: il mount FUSE del portale documenti (runtime/doc) si smonta con
# un attimo di ritardo dopo la fine della sessione D-Bus.
# The cleanup retries: the document portal's FUSE mount (runtime/doc) unmounts a moment
# after the D-Bus session ends.
cleanup() {
    for _ in 1 2 3 4 5; do
        chmod -R u+rwX "$T" 2>/dev/null
        rm -rf "$T" 2>/dev/null
        [ -e "$T" ] || return 0
        sleep 1
    done
}
if [ -z "${BRV_KEEP:-}" ]; then trap cleanup EXIT; fi
mkdir -p "$T/config" "$T/data/gnome-shell/extensions" "$T/cache" "$T/runtime" "$T/home" "$T/probe"
chmod 700 "$T/runtime"
ln -s "$EXT_SRC" "$T/data/gnome-shell/extensions/bravoric-indicator@local"
ln -s "$PROBE_SRC" "$T/data/gnome-shell/extensions/shell-probe@local"

# Finto venv: ogni binario registra il proprio nome in $T/calls.
# Fake venv: every binary records its own name in $T/calls.
BIN="$T/data/bravoric-stt-clipboard/venv/bin"
mkdir -p "$BIN"
for name in bravoric-stt-toggle bravoric-ocr-capture bravoric-stream-toggle; do
    printf '#!/usr/bin/env bash\necho "%s" >> "%s/calls"\n' "$name" "$T" > "$BIN/$name"
    chmod +x "$BIN/$name"
done

export XDG_CONFIG_HOME="$T/config" XDG_DATA_HOME="$T/data" XDG_CACHE_HOME="$T/cache" XDG_RUNTIME_DIR="$T/runtime"
export HOME="$T/home" BRV_PROBE_DIR="$T/probe" T EXT_SRC
export LANG=C LC_ALL=C

# Parte dentro la sessione D-Bus isolata: scrive nei file di $T e stampa PASS/FAIL.
# Runs inside the isolated D-Bus session: writes to the files in $T and prints PASS/FAIL.
cat > "$T/inner.sh" <<'INNER'
SCHEMADIR="$EXT_SRC/schemas"
SCHEMA=org.gnome.shell.extensions.bravoric-indicator
setkey() { gsettings --schemadir "$SCHEMADIR" set "$SCHEMA" "$1" "$2"; }
gsettings set org.gnome.shell disable-user-extensions false
gsettings set org.gnome.shell enabled-extensions "['bravoric-indicator@local', 'shell-probe@local']"
gnome-shell --headless --wayland --no-x11 --virtual-monitor 1280x720 > "$T/shell.log" 2>&1 &
SHELL_PID=$!
up=0
for i in $(seq 1 60); do
    if gdbus call --session -d org.gnome.Shell -o /org/gnome/Shell -m org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1; then up=1; break; fi
    sleep 0.5
done
if [ "$up" != 1 ]; then
    echo "  SKIP  gnome-shell headless non si avvia qui / does not start here"
    kill "$SHELL_PID" 2>/dev/null
    echo SKIPPED > "$T/result"
    exit 0
fi
# Attende che la sonda scriva il primo dump. / Waits for the probe's first dump.
for i in $(seq 1 40); do [ -s "$T/probe/dump.json" ] && break; sleep 0.5; done
echo READY > "$T/ready"
# Il resto lo orchestra il chiamante tramite file: resta in vita finche' non c'e' `stop`.
# The rest is orchestrated by the caller through files: stays alive until `stop` exists.
while [ ! -e "$T/stop" ]; do
    # Esegue qui, nello stesso ambiente del gnome-shell, i comandi chiesti dal chiamante.
    # Runs here, in the same environment as gnome-shell, the commands the caller asks for.
    if [ -e "$T/inner-go" ]; then
        rm -f "$T/inner-go"
        bash "$T/inner-cmd.sh" > "$T/inner-out" 2>&1
        touch "$T/inner-done"
    fi
    sleep 0.2
done
kill "$SHELL_PID" 2>/dev/null
sleep 1
kill -9 "$SHELL_PID" 2>/dev/null
INNER

timeout 150 dbus-run-session -- bash "$T/inner.sh" > "$T/session.log" 2>&1 &
SESSION_PID=$!

FAIL=0
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL + 1)); }

# Attende READY o SKIPPED. / Waits for READY or SKIPPED.
for i in $(seq 1 130); do
    [ -e "$T/ready" ] && break
    [ -e "$T/result" ] && break
    sleep 0.5
done
if [ -e "$T/result" ] && [ "$(cat "$T/result")" = SKIPPED ]; then
    grep -E "SKIP" "$T/session.log" | head -1
    touch "$T/stop"; wait "$SESSION_PID" 2>/dev/null
    exit 0
fi
if [ ! -e "$T/ready" ]; then
    fail "la sessione headless non e' pronta in tempo / the headless session is not ready in time"
    touch "$T/stop"; wait "$SESSION_PID" 2>/dev/null
    exit 1
fi

# Esegue un comando nella sessione D-Bus isolata, nello stesso ambiente (bus, dconf,
# XDG_*) del gnome-shell, e ne restituisce l'output.
# Runs a command in the isolated D-Bus session, in the same environment (bus, dconf,
# XDG_*) as gnome-shell, and returns its output.
in_session() {
    printf '%s\n' "$1" > "$T/inner-cmd.sh"
    rm -f "$T/inner-done" "$T/inner-out"
    touch "$T/inner-go"
    for i in $(seq 1 60); do [ -e "$T/inner-done" ] && break; sleep 0.2; done
    cat "$T/inner-out" 2>/dev/null
}

ext_state() { in_session "gnome-extensions info $1" | awk '/State:/ {print $2}'; }
setkey() { in_session "gsettings --schemadir '$EXT_SRC/schemas' set org.gnome.shell.extensions.bravoric-indicator $1 $2" >/dev/null; }
dump() { cat "$T/probe/dump.json" 2>/dev/null; }
wait_dump() {  # wait_dump <python-condition on `items`>
    for i in $(seq 1 30); do
        if python3 -c "
import json,sys
items=json.load(open('$T/probe/dump.json'))
sys.exit(0 if ($1) else 1)" 2>/dev/null; then return 0; fi
        sleep 0.4
    done
    return 1
}
probe_cmd() { rm -f "$T/probe/cmd-result"; echo "$1" > "$T/probe/cmd"; for i in $(seq 1 20); do [ -e "$T/probe/cmd-result" ] && break; sleep 0.3; done; cat "$T/probe/cmd-result" 2>/dev/null; }

echo "== estensione in GNOME Shell vero / extension in a real GNOME Shell =="
[ "$(ext_state bravoric-indicator@local)" = ACTIVE ] && pass "l'estensione e' ACTIVE" || fail "l'estensione non e' ACTIVE ($(ext_state bravoric-indicator@local))"
[ "$(ext_state shell-probe@local)" = ACTIVE ] && pass "la sonda di test e' ACTIVE" || fail "la sonda non e' ACTIVE"

wait_dump "any(i['role']=='bravoric-indicator@local' for i in items)" \
    && pass "l'indicatore principale e' nella top bar" || fail "l'indicatore principale non e' nella top bar"
wait_dump "not any(i['quick'] for i in items)" \
    && pass "di default nessun bottone rapido" || fail "bottoni rapidi presenti di default"

echo "== accendere i tre bottoni dalle impostazioni / turning the three buttons on =="
setkey show-dictation-button true; setkey show-ocr-button true; setkey show-stream-button true
wait_dump "[i['role'].rsplit('-',1)[-1] for i in items if i['quick']]==['dictation','ocr','stream']" \
    && pass "compaiono i tre bottoni nell'ordine dettatura, OCR, streaming (da sinistra)" \
    || { fail "ordine o presenza dei bottoni rapidi errati: $(dump)"; }
wait_dump "max([n for n,i in enumerate(items) if i['quick']])<[n for n,i in enumerate(items) if i['role']=='bravoric-indicator@local'][0]" \
    && pass "i bottoni rapidi stanno a sinistra dell'indicatore principale" || fail "i bottoni rapidi non sono a sinistra dell'indicatore"
wait_dump "all(i['width']>=32 and i['height']>=24 for i in items if i['quick'])" \
    && pass "area cliccabile adeguata (>= 32 x 24 px)" || fail "area cliccabile troppo piccola: $(dump)"
wait_dump "all(i['reactive'] and i['accessible_name'] for i in items if i['quick'])" \
    && pass "tutti cliccabili e con nome accessibile" || fail "bottoni non cliccabili o senza nome accessibile: $(dump)"

echo "== il click lancia il binario del backend / a click launches the backend binary =="
for key in dictation ocr stream; do
    [ "$(probe_cmd "click $key")" = clicked ] && pass "click su $key inviato" || fail "click su $key non eseguito"
done
sleep 1.5
python3 - "$T/calls" <<'PY' && pass "i tre binari sono stati lanciati (stt, ocr, stream)" || fail "binari lanciati diversi dal previsto: $(cat "$T/calls" 2>/dev/null | tr '\n' ' ')"
import sys
calls = open(sys.argv[1]).read().split()
sys.exit(0 if sorted(calls) == ['bravoric-ocr-capture', 'bravoric-stream-toggle', 'bravoric-stt-toggle'] else 1)
PY

echo "== spegnere un bottone / turning one off =="
setkey show-ocr-button false
wait_dump "[i['role'].rsplit('-',1)[-1] for i in items if i['quick']]==['dictation','stream']" \
    && pass "spento l'OCR restano dettatura e streaming, nello stesso ordine" || fail "spegnimento errato: $(dump)"

echo "== errori JavaScript / JavaScript errors =="
if grep -E "JS ERROR|bravoric" "$T/shell.log" | grep -viE "backend not installed" | grep -qi "error"; then
    fail "errori JS nel log di gnome-shell:"; grep -E "JS ERROR|bravoric" "$T/shell.log" | head -5
else
    pass "nessun errore JS relativo all'estensione nel log di gnome-shell"
fi
[ -e "$T/probe/probe-error" ] && fail "la sonda ha segnalato un errore: $(cat "$T/probe/probe-error")" || pass "la sonda non ha errori"

touch "$T/stop"
wait "$SESSION_PID" 2>/dev/null
echo
if [ "$FAIL" -eq 0 ]; then echo "TUTTO OK"; else echo "$FAIL controlli falliti"; fi
exit "$FAIL"
