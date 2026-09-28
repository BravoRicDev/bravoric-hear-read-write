#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# i18n-begin (estratto dai test: tienilo autonomo)
# Lingua dei messaggi: italiano se la lingua di sistema (LANGUAGE/LC_ALL/
# LC_MESSAGES/LANG) inizia per "it", inglese altrimenti. Stessa regola usata
# piu' sotto per scegliere il config di esempio.
LOCALE_STR="${LANGUAGE:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}"
if [[ "$LOCALE_STR" == it* ]]; then INSTALL_LANG=it; else INSTALL_LANG=en; fi
# say <italiano> <inglese>: stampa il testo nella lingua scelta (a capo incluso).
say() {
    if [ "$INSTALL_LANG" = it ]; then printf '%s\n' "$1"; else printf '%s\n' "$2"; fi
}
# i18n-end

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
    say "ATTENZIONE: un'altra installazione è già in corso (lock: $DATA_DIR/.install.lock)." "WARNING: another installation is already running (lock: $DATA_DIR/.install.lock)." >&2
    say "Attendi che finisca, poi rilancia." "Wait for it to finish, then run it again." >&2
    exit 1
fi

say "== verifica dipendenze di sistema ==" "== checking system dependencies =="
missing=()
for cmd in python3 ffmpeg wl-copy wl-paste notify-send glib-compile-schemas gsettings; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
if [ "${#missing[@]}" -gt 0 ]; then
    say "ATTENZIONE: comandi mancanti: ${missing[*]}" "WARNING: missing commands: ${missing[*]}" >&2
    say "Installali con il package manager della tua distro prima di continuare." "Install them with your distro's package manager before continuing." >&2
    exit 1
fi
PYVER=$(python3 -c 'import sys; print(sys.version_info >= (3, 11))')
if [ "$PYVER" != "True" ]; then
    say "ATTENZIONE: serve Python >= 3.11 (per tomllib), trovato $(python3 --version)" "WARNING: Python >= 3.11 is required (for tomllib), found $(python3 --version)" >&2
    exit 1
fi
# BUG-1 (P2): con pipefail, `ffmpeg ... | grep -q` esce 1 se grep non trova
# nulla, anche se ffmpeg ha exit 0. Salva l'output in una variabile.
ENCODERS=$(ffmpeg -encoders 2>/dev/null || true)
if ! echo "$ENCODERS" | grep -q libopus; then
    say "ATTENZIONE: ffmpeg senza encoder libopus. Installa ffmpeg con supporto opus" "WARNING: ffmpeg has no libopus encoder. Install ffmpeg with opus support" >&2
    say "(pacchetto spesso chiamato ffmpeg-full o simile a seconda della distro)." "(the package is often called ffmpeg-full or similar, depending on the distro)." >&2
    exit 1
fi
say "tutte le dipendenze presenti" "all dependencies present"

say "== venv Python ($VENV_DIR) ==" "== Python venv ($VENV_DIR) =="
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
    say "ERRORE: creazione del venv fallita in $VENV_DIR" "ERROR: venv creation failed in $VENV_DIR" >&2
    if venv_incomplete; then
        rm -rf "$VENV_DIR"
        say "Rimosso l'ambiente incompleto: verrà creato un venv pulito al prossimo tentativo." "Removed the incomplete environment: a clean venv will be created on the next attempt." >&2
    fi
    exit 1
fi
if venv_incomplete || ! source "$VENV_DIR/bin/activate"; then
    say "ERRORE: venv incompleto in $VENV_DIR (manca l'interprete o l'activate)." "ERROR: incomplete venv in $VENV_DIR (interpreter or activate script missing)." >&2
    rm -rf "$VENV_DIR"
    say "Rimosso: il prossimo tentativo riparte da zero." "Removed: the next attempt starts from scratch." >&2
    exit 1
fi
pip install --upgrade pip -q
pip install -e "$PROJECT_DIR" -q

say "== config utente ==" "== user config =="
CONFIG_DIR="$HOME/.config/bravoric-stt-clipboard"
# P3 (giro 13): mkdir con umask locale, directory non elencabile da altri
# utenti locali (i nomi file altrimenti enumerabili anche col contenuto
# protetto da chmod 600).
(umask 077 && mkdir -p "$CONFIG_DIR")
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    # Prompt di default (system_prompt) in italiano o inglese secondo la
    # lingua di sistema rilevata da LANGUAGE/LC_ALL/LC_MESSAGES/LANG.
    if [ "$INSTALL_LANG" = it ]; then
        EXAMPLE_CONFIG="$PROJECT_DIR/config/config.example.it.toml"
    else
        EXAMPLE_CONFIG="$PROJECT_DIR/config/config.example.toml"
    fi
    # cp su file preesistente non applica l'umask (governa solo creazione di
    # nuovo inode): niente cp preliminare, solo questa (con umask locale).
    (umask 077 && cp "$EXAMPLE_CONFIG" "$CONFIG_DIR/config.toml")
    say "Config creata in $CONFIG_DIR/config.toml (da $(basename "$EXAMPLE_CONFIG")) — personalizzala (endpoint, key, cert)." "Config created in $CONFIG_DIR/config.toml (from $(basename "$EXAMPLE_CONFIG")) — customize it (endpoint, key, cert)."
fi
# P2 (giro 13): SEMPRE, non solo alla creazione — un'installazione fatta con
# una versione precedente di install.sh (prima del fix permessi) restava
# world-readable per sempre: il chmod dentro l'if "config non esiste" non
# veniva mai raggiunto su reinstall. Riprodotto dal vivo: config.toml a 644
# con api_key in chiaro, invariato dopo rilancio di install.sh. Idempotente
# e autoriparante a ogni esecuzione.
chmod 600 "$CONFIG_DIR/config.toml"

say "== estensione GNOME Shell ==" "== GNOME Shell extension =="
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
    say "ATTENZIONE: $EXT_DIR non è un symlink ma una directory reale (copia)." "WARNING: $EXT_DIR is not a symlink but a real directory (a copy)." >&2
    say "  Succede dopo 'gnome-extensions install', che COPIA i file invece di linkarli." "  This happens after 'gnome-extensions install', which COPIES the files instead of linking them." >&2
    say "  Schema compilato e traduzioni andranno sulla COPIA, non sul repo in" "  The compiled schema and translations will go to the COPY, not to the repo in" >&2
    say "  $PROJECT_DIR: un 'git pull' non aggiornerà l'estensione che gira." "  $PROJECT_DIR: a 'git pull' will not update the extension that is running." >&2
    say "  Per tornare al link: rimuovi la copia e rilancia questo script." "  To go back to the link: remove the copy and run this script again." >&2
    say "    rm -rf '$EXT_DIR'   (fai un backup se contiene modifiche locali)" "    rm -rf '$EXT_DIR'   (back it up first if it holds local changes)" >&2
    EXT_DIR_IS_COPY=1
else
    EXT_DIR_IS_COPY=0
fi

ln -sfn "$PROJECT_DIR/gnome-extension/bravoric-indicator@local" "$EXT_DIR"

# La `ln` può anche riuscire su una directory reale senza toccarla (il caso
# appena descritto): lo si constata DOPO, sul filesystem, e non sull'esito
# della `ln`, che è 0 in entrambi i casi.
if [ "$EXT_DIR_IS_COPY" -eq 1 ] && [ -d "$EXT_DIR" ] && [ ! -L "$EXT_DIR" ]; then
    say "  Verifica: $EXT_DIR è ancora una copia reale, non un link al repo." "  Check: $EXT_DIR is still a real copy, not a link to the repo." >&2
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
        say "ERRORE: schema compilato privo della chiave obbligatoria: $key" "ERROR: compiled schema lacks the required key: $key" >&2
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
            say "ATTENZIONE: $(basename "$po") non valido, non compilato — l'estensione resterà in inglese." "WARNING: $(basename "$po") is invalid, not compiled — the extension will stay in English." >&2
            continue
        fi
        mv -f "$mo_tmp" "$EXT_DIR/locale/$lang/LC_MESSAGES/bravoric-indicator.mo"
    done
else
    say "ATTENZIONE: msgfmt (gettext) assente — traduzioni non compilate, l'estensione resterà in inglese." "WARNING: msgfmt (gettext) is missing — translations not compiled, the extension will stay in English." >&2
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
            say "ATTENZIONE: $(basename "$po") non valido, non compilato — il backend resterà in inglese." "WARNING: $(basename "$po") is invalid, not compiled — the backend will stay in English." >&2
            continue
        fi
        mv -f "$mo_tmp" "$mo_dir/bravoric-stt-clipboard.mo"
    done
else
    # P3 (giro 14): il blocco gemello sopra (estensione) avvisa se msgfmt
    # manca; questo taceva — nessun crash (gettext Python fallback silenzioso
    # a inglese), ma l'utente non sapeva che il backend restava non tradotto.
    say "ATTENZIONE: msgfmt (gettext) assente — traduzioni backend non compilate, resterà in inglese." "WARNING: msgfmt (gettext) is missing — backend translations not compiled, it will stay in English." >&2
fi

if [ "$EXT_DIR_IS_COPY" -eq 1 ]; then
    # F1: la riga qui sotto sarebbe falsa nella copia reale — non è stata
    # linkata niente, e quello che GNOME Shell carica non è il repo.
    say "ATTENZIONE: estensione NON linkata — $EXT_DIR resta una copia reale." "WARNING: extension NOT linked — $EXT_DIR remains a real copy." >&2
    say "  Schema e traduzioni sono stati compilati sulla copia, non sul repo." "  Schema and translations were compiled on the copy, not on the repo." >&2
else
    say "Estensione linkata, schema e traduzioni compilati." "Extension linked, schema and translations compiled."
fi
say "Abilita con: gnome-extensions enable bravoric-indicator@local" "Enable with: gnome-extensions enable bravoric-indicator@local"
# F2 (giro 4): qui sotto c'era la riga «(su Wayland serve logout/login per
# caricarla la prima volta)», che parla solo della PRIMA installazione e dice
# quindi esattamente il contrario di quello che serve a chi ha appena fatto
# `git pull`: è l'UPGRADE a lasciare il modulo vecchio in memoria.
# Misurato dal vivo prima della correzione: sulle echo di install.sh
# `grep -nE "restart|ricarica|reload|disable.*enable"` non trovava NULLA, e
# "prima volta" compariva 1 volta. Nessun'altra riguarda il reload.
# Il punto è che `gnome-extensions enable` su un'estensione GIÀ abilitata non
# ricarica: va detto, perché è l'unico punto in cui l'utente viene avvisato.
say "(su Wayland: logout e login — o Alt+F2 r — per ricaricare l'estensione;" "(on Wayland: log out and back in — or Alt+F2 r — to reload the extension;"
say " serve anche dopo un aggiornamento, non solo alla prima installazione)" " this is needed after an update too, not only on first install)"

say "== fatto ==" "== done =="
say "venv installato in: $VENV_DIR" "venv installed in: $VENV_DIR"
say "Prossimo passo: imposta le scorciatoie dalle preferenze dell'estensione" "Next step: set the shortcuts from the extension preferences"
say "  gnome-extensions prefs bravoric-indicator@local   (default: Alt+Super+R dettatura, Alt+Super+O OCR, Alt+Super+S streaming)" "  gnome-extensions prefs bravoric-indicator@local   (default: Alt+Super+R dictation, Alt+Super+O OCR, Alt+Super+S streaming)"
