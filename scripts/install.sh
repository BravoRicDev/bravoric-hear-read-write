#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Venv in posizione XDG fissa, non dentro il checkout git: l'estensione
# GNOME (extension.js/prefs.js) la trova via GLib.get_user_data_dir(),
# indipendentemente da dove hai clonato il repo su questa macchina.
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/bravoric-stt-clipboard"
VENV_DIR="$DATA_DIR/venv"

# P3 (giro 17): due install.sh lanciati in parallelo (doppio click, doppio
# terminale) correvano entrambi "python3 -m venv" sulla stessa VENV_DIR —
# riprodotto dal vivo: uno dei due falliva con un errore ensurepip criptico
# (sembra un problema Python, non di concorrenza). Lo stato finale non si
# corrompeva mai (il vincitore completa sempre), ma il messaggio fuorviava.
# flock non bloccante: la seconda istanza esce subito con un errore chiaro.
(umask 077 && mkdir -p "$DATA_DIR")  # P3 (giro 18): coerente con CONFIG_DIR (giro 13)
exec 9>"$DATA_DIR/.install.lock"
if ! flock -n 9; then
    echo "ATTENZIONE: un'altra installazione è già in corso (lock: $DATA_DIR/.install.lock)." >&2
    echo "Attendi che finisca, poi rilancia." >&2
    exit 1
fi

echo "== verifica dipendenze di sistema =="
missing=()
for cmd in python3 ffmpeg wl-copy wl-paste notify-send glib-compile-schemas gsettings; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "ATTENZIONE: comandi mancanti: ${missing[*]}" >&2
    echo "Installali con il package manager della tua distro prima di continuare." >&2
    exit 1
fi
PYVER=$(python3 -c 'import sys; print(sys.version_info >= (3, 11))')
if [ "$PYVER" != "True" ]; then
    echo "ATTENZIONE: serve Python >= 3.11 (per tomllib), trovato $(python3 --version)" >&2
    exit 1
fi
# BUG-1 (P2): con pipefail, `ffmpeg ... | grep -q` esce 1 se grep non trova
# nulla, anche se ffmpeg ha exit 0. Salva l'output in una variabile.
ENCODERS=$(ffmpeg -encoders 2>/dev/null || true)
if ! echo "$ENCODERS" | grep -q libopus; then
    echo "ATTENZIONE: ffmpeg senza encoder libopus. Installa ffmpeg con supporto opus" >&2
    echo "(pacchetto spesso chiamato ffmpeg-full o simile a seconda della distro)." >&2
    exit 1
fi
echo "tutte le dipendenze presenti"

echo "== venv Python ($VENV_DIR) =="
mkdir -p "$DATA_DIR"
# Giro 2 (C1): guardia esplicita + PULIZIA. Con set -e, un 'python3 -m venv'
# che produce un ambiente incompleto (spazio esaurito, permessi) non fallisce
# li': falliva piu' tardi, al 'source activate', DOPO che l'estensione non e'
# stata ancora collegata. Il venv a meta' restava in VENV_DIR e il prossimo
# install.sh lo riusava come punto di partenza, fallendo di nuovo: seconda
# modalita' di installazione incompleta, accanto a quella dei .po (F5). Rimosso
# solo se e' INCOMPLETO: un venv valido non viene mai toccato.
venv_incomplete() {
    [ ! -x "$VENV_DIR/bin/python" ] || [ ! -f "$VENV_DIR/bin/activate" ]
}
if ! python3 -m venv "$VENV_DIR"; then
    echo "ERRORE: creazione del venv fallita in $VENV_DIR" >&2
    if venv_incomplete; then
        rm -rf "$VENV_DIR"
        echo "Rimosso l'ambiente incompleto: verrai a creare un venv pulito al prossimo tentativo." >&2
    fi
    exit 1
fi
if venv_incomplete || ! source "$VENV_DIR/bin/activate"; then
    echo "ERRORE: venv incompleto in $VENV_DIR (manca l'interprete o l'activate)." >&2
    rm -rf "$VENV_DIR"
    echo "Rimosso: il prossimo tentativo riparte da zero." >&2
    exit 1
fi
pip install --upgrade pip -q
pip install -e "$PROJECT_DIR" -q

echo "== config utente =="
CONFIG_DIR="$HOME/.config/bravoric-stt-clipboard"
# P3 (giro 13): mkdir con umask locale, directory non elencabile da altri
# utenti locali (i nomi file altrimenti enumerabili anche col contenuto
# protetto da chmod 600).
(umask 077 && mkdir -p "$CONFIG_DIR")
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    # Prompt di default (system_prompt) in italiano o inglese secondo la
    # lingua di sistema rilevata da LANGUAGE/LC_ALL/LC_MESSAGES/LANG.
    LOCALE_STR="${LANGUAGE:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}"
    if [[ "$LOCALE_STR" == it* ]]; then
        EXAMPLE_CONFIG="$PROJECT_DIR/config/config.example.it.toml"
    else
        EXAMPLE_CONFIG="$PROJECT_DIR/config/config.example.toml"
    fi
    # cp su file preesistente non applica l'umask (governa solo creazione di
    # nuovo inode): niente cp preliminare, solo questa (con umask locale).
    (umask 077 && cp "$EXAMPLE_CONFIG" "$CONFIG_DIR/config.toml")
    echo "Config creata in $CONFIG_DIR/config.toml (da $(basename "$EXAMPLE_CONFIG")) — personalizzala (endpoint, key, cert)."
fi
# P2 (giro 13): SEMPRE, non solo alla creazione — un'installazione fatta con
# una versione precedente di install.sh (prima del fix permessi) restava
# world-readable per sempre: il chmod dentro l'if "config non esiste" non
# veniva mai raggiunto su reinstall. Riprodotto dal vivo: config.toml a 644
# con api_key in chiaro, invariato dopo rilancio di install.sh. Idempotente
# e autoriparante a ogni esecuzione.
chmod 600 "$CONFIG_DIR/config.toml"

echo "== estensione GNOME Shell =="
# P2 (giro 18): GNOME Shell cerca le estensioni utente via
# GLib.get_user_data_dir(), che rispetta XDG_DATA_HOME — hardcoded su
# ~/.local/share, con XDG_DATA_HOME diverso il symlink finiva in un path che
# GNOME Shell non guarda mai. Stessa risoluzione già usata per DATA_DIR.
EXT_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/bravoric-indicator@local"
mkdir -p "$(dirname "$EXT_DIR")"

# F1 (giro 4): `ln -sfn` è silenziosamente INERTE quando la destinazione è una
# directory REALE invece di un symlink. `-n` vale solo se la destinazione è
# un symlink a directory: con una directory vera, `ln` ci mette DENTRO il
# link (esce 0, nessun errore) e lascia intatta la copia.
# La copia reale è esattamente ciò che produce `gnome-extensions install`, che
# COPIA e non linka: chi ha installato così ha poi eseguito `git pull` qui
# scambiando l'upgrade per un successo.
# Misurato dal vivo prima della correzione (repo di prova, niente toccato):
#   prima:  drwxr-xr-x  ext_real        <- directory REALE
#   ln -sfn src_ext parent/ext_real  ->  exit 0
#   dopo:   test -L  -> NO: resta una directory REALE
#   contenuto: ext_real/ { marker.txt, src_ext }   <- il link è finito DENTRO
# Le righe sotto (glib-compile-schemas, gate delle chiavi, .mo) girerebbero
# tutte sulla copia stantia, che è anche quella che GNOME Shell carica, e il
# gate delle chiavi PASSEREBBE: installazione apparentemente riuscita e
# modulo vecchio ancora in esecuzione. Il caso peggiore.
# Qui si dice la verità prima di fare qualcosa che sembra funzionare. Non si
# cancella niente: la copia può contenere modifiche locali dell'utente e una
# `rm -rf` non richiesta è più pericolosa del difetto che si corregge.
if [ -e "$EXT_DIR" ] && [ ! -L "$EXT_DIR" ]; then
    echo "ATTENZIONE: $EXT_DIR non è un symlink ma una directory reale (copia)." >&2
    echo "  Succede dopo 'gnome-extensions install', che COPIA i file invece di linkarli." >&2
    echo "  Schema compilato e traduzioni andranno sulla COPIA, non sul repo in" >&2
    echo "  $PROJECT_DIR: un 'git pull' non aggiornerà l'estensione che gira." >&2
    echo "  Per tornare al link: rimuovi la copia e rilancia questo script." >&2
    echo "    rm -rf '$EXT_DIR'   (fai un backup se contiene modifiche locali)" >&2
    EXT_DIR_IS_COPY=1
else
    EXT_DIR_IS_COPY=0
fi

ln -sfn "$PROJECT_DIR/gnome-extension/bravoric-indicator@local" "$EXT_DIR"

# La `ln` può anche riuscire su una directory reale senza toccarla (il caso
# appena descritto): lo si constata DOPO, sul filesystem, e non sull'esito
# della `ln`, che è 0 in entrambi i casi.
if [ "$EXT_DIR_IS_COPY" -eq 1 ] && [ -d "$EXT_DIR" ] && [ ! -L "$EXT_DIR" ]; then
    echo "  Verifica: $EXT_DIR è ancora una copia reale, non un link al repo." >&2
fi

# Lo schema GSettings va compilato in loco: senza gschemas.compiled
# this.getSettings() fallisce all'avvio dell'estensione (scorciatoie + prefs).
glib-compile-schemas --strict "$EXT_DIR/schemas"

# Non basta che il compilatore esca 0: verifica anche che il database appena
# generato esponga tutte le chiavi usate da extension.js. Una copia stantia di
# gschemas.compiled priva di stream-shortcut ha causato un crash di GNOME Shell
# al login (Main.wm.addKeybinding arriva a un'assertion fatale di Mutter).
SCHEMA_ID="org.gnome.shell.extensions.bravoric-indicator"
COMPILED_KEYS="$(gsettings --schemadir "$EXT_DIR/schemas" list-keys "$SCHEMA_ID")"
for key in dictation-shortcut ocr-shortcut stream-shortcut; do
    if ! grep -Fxq "$key" <<<"$COMPILED_KEYS"; then
        echo "ERRORE: schema compilato privo della chiave obbligatoria: $key" >&2
        exit 1
    fi
done

# Traduzioni dell'estensione GNOME (dominio bravoric-indicator): i .po vivono
# nel repo, non nella cartella linkata, quindi si compilano dalla sorgente.
# gettext non è obbligatorio (senza, l'estensione resta in inglese, il sorgente
# msgid), quindi la mancanza di msgfmt avvisa ma non blocca.
if command -v msgfmt >/dev/null 2>&1; then
    for po in "$PROJECT_DIR"/gnome-extension/bravoric-indicator@local/po/*.po; do
        [ -e "$po" ] || continue
        lang="$(basename "$po" .po)"
        mkdir -p "$EXT_DIR/locale/$lang/LC_MESSAGES"
        # Giro 2 (F5): `set -e` + un .po malformato (msgfmt esce 1) uccideva
        # l'installazione QUI, dopo venv, config, symlink e schema gia'
        # installati, e senza il riepilogo finale: installazione a meta' con
        # l'utente che vede solo un exit code. Il commento qui sopra dichiara
        # gia' l'atteggiamento tollerante ("senza gettext l'estensione resta
        # in inglese"): un .po rotto e' lo stesso caso, non un errore fatale.
        # -o su file temporaneo: se msgfmt fallisce a meta', non resta un .mo
        # corrotto che gettext leggerebbe al posto delle stringhe originali.
        mo_tmp="$EXT_DIR/locale/$lang/LC_MESSAGES/.bravoric-indicator.mo.tmp"
        if ! msgfmt -o "$mo_tmp" "$po"; then
            rm -f "$mo_tmp"
            echo "ATTENZIONE: $(basename "$po") non valido, non compilato — l'estensione resterà in inglese." >&2
            continue
        fi
        mv -f "$mo_tmp" "$EXT_DIR/locale/$lang/LC_MESSAGES/bravoric-indicator.mo"
    done
else
    echo "ATTENZIONE: msgfmt (gettext) assente — traduzioni non compilate, l'estensione resterà in inglese." >&2
fi

# Traduzioni del backend Python (dominio bravoric-stt-clipboard, stesse po
# della root): senza il .mo compilato gettext in Python resta in inglese anche
# se i sorgenti sono tradotti. Il .mo va accanto al pacchetto.
if command -v msgfmt >/dev/null 2>&1; then
    for po in "$PROJECT_DIR"/po/*.po; do
        [ -e "$po" ] || continue
        lang="$(basename "$po" .po)"
        mo_dir="$PROJECT_DIR/src/bravoric_stt_clipboard/locale/$lang/LC_MESSAGES"
        mkdir -p "$mo_dir"
        # Giro 2 (F5): stesso trattamento del blocco estensione sopra.
        mo_tmp="$mo_dir/.bravoric-stt-clipboard.mo.tmp"
        if ! msgfmt -o "$mo_tmp" "$po"; then
            rm -f "$mo_tmp"
            echo "ATTENZIONE: $(basename "$po") non valido, non compilato — il backend resterà in inglese." >&2
            continue
        fi
        mv -f "$mo_tmp" "$mo_dir/bravoric-stt-clipboard.mo"
    done
else
    # P3 (giro 14): il blocco gemello sopra (estensione) avvisa se msgfmt
    # manca; questo taceva — nessun crash (gettext Python fallback silenzioso
    # a inglese), ma l'utente non sapeva che il backend restava non tradotto.
    echo "ATTENZIONE: msgfmt (gettext) assente — traduzioni backend non compilate, resterà in inglese." >&2
fi

if [ "$EXT_DIR_IS_COPY" -eq 1 ]; then
    # F1: la riga qui sotto sarebbe falsa nella copia reale — non è stata
    # linkata niente, e quello che GNOME Shell carica non è il repo.
    echo "ATTENZIONE: estensione NON linkata — $EXT_DIR resta una copia reale." >&2
    echo "  Schema e traduzioni sono stati compilati sulla copia, non sul repo." >&2
else
    echo "Estensione linkata, schema e traduzioni compilati."
fi
echo "Abilita con: gnome-extensions enable bravoric-indicator@local"
# F2 (giro 4): qui sotto c'era la riga «(su Wayland serve logout/login per
# caricarla la prima volta)», che parla solo della PRIMA installazione e dice
# quindi esattamente il contrario di quello che serve a chi ha appena fatto
# `git pull`: è l'UPGRADE a lasciare il modulo vecchio in memoria.
# Misurato dal vivo prima della correzione: sulle echo di install.sh
# `grep -nE "restart|ricarica|reload|disable.*enable"` non trovava NULLA, e
# "prima volta" compariva 1 volta. Nessun'altra riguarda il reload.
# Il punto è che `gnome-extensions enable` su un'estensione GIÀ abilitata non
# ricarica: va detto, perché è l'unico punto in cui l'utente viene avvisato.
echo "(su Wayland: logout e login — o Alt+F2 r — per ricaricare l'estensione;"
echo " serve anche dopo un aggiornamento, non solo alla prima installazione)"

echo "== fatto =="
echo "venv installato in: $VENV_DIR"
echo "Prossimo passo: imposta le scorciatoie dalle preferenze dell'estensione"
echo "  gnome-extensions prefs bravoric-indicator@local   (default: Alt+Super+R, Alt+Super+O)"
