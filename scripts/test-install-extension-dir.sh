#!/usr/bin/env bash
# test-install-extension-dir.sh — F1 + F2 (giro 4).
#
# Non esegue install.sh (scriverebbe in ~/.config e ~/.local/share reali).
# Estrae il blocco "estensione GNOME Shell" da install.sh e lo esegue davvero
# in un XDG_DATA_HOME finto: si misura il prodotto, non una riscrittura.
#
# I due difetti, misurati dal reviewer:
#  F1 — `ln -sfn` e' silenziosamente INERTE se la destinazione e' una directory
#       REALE: -n vale solo su symlink, quindi `ln` esce 0 e finisce il link
#       DENTRO. Le righe dopo (glib-compile-schemas, gate delle chiavi, .mo)
#       girano sulla copia stantia, che e' anche quella che GNOME Shell carica,
#       e il gate delle chiavi PASSEREBBE: upgrade apparentemente riuscito col
#       modulo vecchio ancora in memoria.
#  F2 — l'unico messaggio sull'reload parlava solo della PRIMA installazione,
#       dicendo il contrario di quello che serve a chi ha fatto `git pull`.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_SH="$REPO/scripts/install.sh"
SRC_EXT="$REPO/gnome-extension/bravoric-indicator@local"
FAIL=0

# check <nome> <0|1>
check() {
    if [ "$2" = "1" ]; then
        printf '  PASS  %s\n' "$1"
    else
        printf '  FAIL  %s\n' "$1"
        FAIL=$((FAIL + 1))
    fi
}

# grepany <pattern> <file...> -> 1 se trova, 0 se non trova (-nessun output-)
grepany() {
    local pat="$1"
    shift
    if grep -qiE "$pat" "$@" 2>/dev/null; then
        echo 1
    else
        echo 0
    fi
}

block="$(awk '/^echo "== estensione GNOME Shell =="/{f=1} f&&/^echo "== fatto =="/{exit} f' "$INSTALL_SH")"
if [ -z "$block" ]; then
    echo "  FAIL  blocco estensione non trovato in install.sh"
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

RUNNER="$WORK/runner.sh"
{
    printf '#!/usr/bin/env bash\n'
    printf 'set -euo pipefail\n'
    printf '%s\n' "$block"
} >"$RUNNER"

# Entrata: XDG_DATA_HOME finto, PROJECT_DIR = RADICE del checkout finto (la
# sorgente ci vive dentro, sotto gnome-extension/). Scrive out.txt/err.txt e
# restituisce l'exit code del blocco.
run_block() {
    local xdg="$1"
    local proj="$2"
    local logdir
    logdir="$WORK/log-$(basename "$(dirname "$xdg")")"
    mkdir -p "$(dirname "$proj")/home" "$xdg" "$logdir"
    env HOME="$(dirname "$proj")/home" XDG_DATA_HOME="$xdg" PROJECT_DIR="$proj" bash "$RUNNER" >"$logdir/out.txt" 2>"$logdir/err.txt"
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "    (blocco uscito con $rc — stderr del blocco:)"
        sed 's/^/      /' "$logdir/err.txt"
    fi
    return $rc
}

# Dove finiscono stdout/stderr di ogni caso: FUORI da $XDG_DATA_HOME, altrimenti
# i file di log verrebbero creati dentro gnome-shell/extensions PRIMA del symlink
# e falserebbero la misura (e il gate delle chiavi leggerebbe una dir senza
# schemas solo per via del log).
outof() { printf '%s/log-%s/%s' "$WORK" "$(basename "$(dirname "$1")")" "$2"; }

echo "== F1/F2: blocco estensione di install.sh eseguito in XDG finto =="

# --- Caso 1: destinazione REALE (la copia di `gnome-extensions install`) ---
W1="$WORK/caso1"
EXT1="$W1/xdg/gnome-shell/extensions/bravoric-indicator@local"
# PROJECT_DIR = radice del checkout, quindi la sorgente vive sotto proj/.
mkdir -p "$W1/proj/gnome-extension"
cp -r "$SRC_EXT" "$W1/proj/gnome-extension/bravoric-indicator@local"
# Copia REALE, come la produce `gnome-extensions install`: l'intera estensione
# (schemas compresi, altrimenti il gate delle chiavi fallirebbe per motivi
# sbagliati e non misurerebbe nulla) piu' un file che prova l'antichita'.
mkdir -p "$EXT1"
cp -r "$SRC_EXT/." "$EXT1/"
echo "marcatore" >"$EXT1/marker.txt"
run_block "$W1/xdg" "$W1/proj"
rc=$?

OUT1="$(outof "$W1/xdg" out.txt)"
ERR1="$(outof "$W1/xdg" err.txt)"
both1=("$OUT1" "$ERR1")
ok_rc=0
[ "$rc" -eq 0 ] && ok_rc=1
check "caso 1: il blocco termina senza errori (gate delle chiavi passa)" "$ok_rc"
check "F1: avvisa che la destinazione e' una copia reale" "$(grepany 'copia' "${both1[@]}")"
check "F1: dice che schema/traduzioni vanno sulla COPIA e non sul repo" "$(grepany 'non (sul )?repo|sulla COPIA' "${both1[@]}")"
check "F1: dice che git pull non aggiorna l'estensione che gira" "$(grepany 'git pull' "${both1[@]}")"
if grep -q 'Estensione linkata' "$OUT1"; then ok_nolink=0; else ok_nolink=1; fi
check "F1: non dichiara piu' 'Estensione linkata' quando e' una copia" "$ok_nolink"
if [ -f "$EXT1/marker.txt" ]; then ok_marker=1; else ok_marker=0; fi
check "F1: la copia preesistente NON viene cancellata (niente rm -rf non richiesta)" "$ok_marker"
check "F1: avvisa anche che la copia e' ancora reale dopo la ln" "$(grepany 'ancora una copia reale' "${both1[@]}")"

# --- Caso 2: destinazione assente (percorso normale, symlink) -------------
W2="$WORK/caso2"
mkdir -p "$W2/proj/gnome-extension"
cp -r "$SRC_EXT" "$W2/proj/gnome-extension/bravoric-indicator@local"
run_block "$W2/xdg" "$W2/proj"
rc=$?
EXT2="$W2/xdg/gnome-shell/extensions/bravoric-indicator@local"

ok_rc2=0
[ "$rc" -eq 0 ] && ok_rc2=1
check "caso 2 (symlink): il blocco termina senza errori" "$ok_rc2"
if [ -L "$EXT2" ]; then ok_link=1; else ok_link=0; fi
check "caso 2 (symlink): la destinazione e' un symlink al repo" "$ok_link"
OUT2="$(outof "$W2/xdg" out.txt)"
ERR2="$(outof "$W2/xdg" err.txt)"
if grep -qi 'copia' "$ERR2"; then ok_quiet=0; else ok_quiet=1; fi
check "F1: nessun avviso di copia nel caso normale (nessun rumore introdotto)" "$ok_quiet"
check "F1: 'Estensione linkata' dichiara il vero quando il link c'e'" "$(grepany 'Estensione linkata' "$OUT2")"

# --- F2: il messaggio sul reload, in entrambi i casi -----------------------
for n in 1 2; do
    if [ "$n" = "1" ]; then out="$OUT1"; else out="$OUT2"; fi
    check "F2 (caso $n): dice che serve un reload su Wayland" "$(grepany 'logout|Alt.F2' "$out")"
    check "F2 (caso $n): dice che serve ANCHE dopo un aggiornamento" "$(grepany 'aggiornament' "$out")"
    if grep -qi 'prima volta' "$out"; then bad_first=0; else bad_first=1; fi
    check "F2 (caso $n): non dice piu' solo 'per la prima volta'" "$bad_first"
done

# --- Non-vacuità statica di F2: il vecchio testo e' cio' che rendeva l'avviso
# falso per l'upgrade (diceva solo 'prima volta' e non nominava il reload).
if grep -qi 'per la prima volta' "$INSTALL_SH"; then v1=0; else v1=1; fi
check "F2: il vecchio messaggio 'per la prima volta' non e' piu' in install.sh" "$v1"
install_echoes="$(grep -nE '^[[:space:]]*echo' "$INSTALL_SH")"
check "F2: l'output di install.sh nomina il reload (logout/Alt+F2/ricarica)" "$(printf '%s' "$install_echoes" | grepany 'logout|Alt.F2|ricaric|riavvi')"

# --- Non-vacuità meccanica di F1: il puro `ln -sfn` e' davvero inerte sulla
# directory reale (e' il fatto che rendeva il fallimento silenzioso).
LNT="$WORK/lntest"
mkdir -p "$LNT/parent/ext_real" "$LNT/src_ext"
ln -sfn "$LNT/src_ext" "$LNT/parent/ext_real" 2>/dev/null
if [ -d "$LNT/parent/ext_real" ] && [ ! -L "$LNT/parent/ext_real" ]; then ln_inert=1; else ln_inert=0; fi
check "F1: il meccanismo e' reale — ln -sfn su directory non la sostituisce" "$ln_inert"

echo
if [ "$FAIL" -eq 0 ]; then
    echo "TUTTO OK"
else
    echo "$FAIL controlli falliti"
fi
exit "$FAIL"
