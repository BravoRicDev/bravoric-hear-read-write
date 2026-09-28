# Bravoric Hear, Read & Write

**Voice and OCR input for GNOME terminals and applications**

Bravoric Hear, Read & Write is a GNOME Shell extension plus a Python backend for
real-world dictation, OCR, and text entry on GNOME/Wayland. Many extensions do
only one part of this job, do not let you choose the provider, or offer weak
local OCR. This project exists because a tool that works in a terminal, a web
form, tmux, SSH, and ordinary applications is more useful than a demo.

## English

### What it does

The goal is practical productivity with tailor-made control, not another
one-size-fits-all voice widget:

1. **Streaming dictation.** Words are segmented and delivered directly to the
   focused field while you speak, like the familiar macOS experience but on
   GNOME and Wayland.
2. **One character at a time.** The `type` channel sends keystrokes instead of
   pasting. It works in terminals, tmux/SSH, web forms, and applications where
   clipboard paste is unavailable or undesirable. It does not depend on a
   shared clipboard and does not look like a simple intercepted paste.
3. **Explicit voice commands.** Configure commands such as `invio` (Enter),
   `cancella` (delete a word or chunk), Backspace, and other actions. The user
   decides which words trigger which actions.
4. **OCR and complete dictation.** Read text from an image already in the
   clipboard or select a screenshot interactively; push-to-talk STT copies a
   complete transcription to the clipboard.
5. **Configurable providers.** Use OpenAI-compatible endpoints that are local,
   cloud, or mixed. Ordered fallback is automatic: choose the backends and the
   system keeps trying the next usable one.
6. **Deep customization.** The GUI and TOML configuration expose endpoints,
   models, fallback order, timeouts, VAD, hotwords, prompts, blacklist rules,
   voice commands, shortcuts, paste channel, notifications, icons, and
   streaming behaviour.
7. **User-controlled computer actions.** This is explicit voice control, not an
   autonomous AI agent. The AI never takes over the computer: the user chooses
   what to say and when to send it.
8. **Privacy by choice.** Select local or remote endpoints yourself; no service
   is imposed by the project.

### Modes and interface

- **Push-to-talk STT:** press the shortcut, speak, press it again; the backend
  records, transcribes, and copies the result.
- **Streaming per chunk:** voice activity detection closes chunks, transcribes
  them, and delivers them in order to the focused field.
- **OCR:** process the current clipboard image or select a screen region.
- **Quick buttons and indicators:** optional top-bar buttons start dictation, OCR,
  or streaming, while the GNOME indicator shows idle, recording, and processing
  state.
- **Fallback log:** persistent chunk and endpoint logs record the selected
  endpoint, result, and timings, so a failed provider is diagnosable rather than
  mysterious.

### Architecture and requirements

The repository contains two cooperating parts:

- `gnome-extension/bravoric-hear-read-write@riccardomurru.it/` is the GNOME Shell
  portion: indicator, preferences, quick buttons, OCR/STT controls, and the
  streaming input worker.
- `src/bravoric_stt_clipboard/` is the Python backend: audio capture, STT/OCR,
  provider fallback, clipboard and typing output, notifications, and logs.

The E.G.O. catalog submission for the GNOME extension portion is currently under
review and is not yet available; the Python backend is required for the complete
feature set.

Requirements: GNOME Shell 45–50 on Wayland, Python 3.11+, `ffmpeg` with
`libopus`, `wl-clipboard`, `notify-send`, `glib-compile-schemas`, and
`gsettings`. `gnome-screenshot` is optional for interactive OCR capture.

### Installation and first configuration

```sh
scripts/install.sh
```

The installer creates a Python virtual environment under
`$XDG_DATA_HOME/bravoric-stt-clipboard`, installs the backend, creates
`~/.config/bravoric-stt-clipboard/config.toml` from the language-appropriate
example when needed, and links the extension into GNOME Shell. It is safe to
run again after a pull. It never overwrites an existing personal config.

Enable the extension and open its preferences:

```sh
gnome-extensions enable bravoric-hear-read-write@riccardomurru.it
gnome-extensions prefs bravoric-hear-read-write@riccardomurru.it
```

The default shortcuts are `Alt+Super+R` for STT, `Alt+Super+O` for OCR, and
`Alt+Super+S` for streaming. Wayland may require logging out and in, or
restarting the shell, after an extension update.

A minimal OpenAI-compatible STT provider in `config.toml` looks like this:

```toml
[[stt.fallback]]
name = "local-whisper"
endpoint = "http://127.0.0.1:8000/v1"
model = "whisper-1"
api_key_env = ""
api_key = ""
timeout_seconds = 120
```

Add further `[[stt.fallback]]` entries for automatic fallback. Equivalent
fallback lists exist for OCR and optional text cleanup. Keep API keys in the
personal config or an environment variable; do not commit them.

### Output channels and voice commands

Streaming has two deliberately different output channels:

- `paste_channel = "clipboard"` puts each chunk in the clipboard and sends the
  configured paste shortcut (`Ctrl+V`, or `Ctrl+Shift+V` for terminals). This is
  fast and compatible with normal GUI fields.
- `paste_channel = "type"` injects each character as a key event. The clipboard
  is not used, which makes it suitable for terminals, tmux/SSH, and fields that
  reject paste.

Example explicit commands in the `[stream]` section:

```toml
[[stream.command]]
keyword = "invio"
action = "key"
key = "Return"

[[stream.command]]
keyword = "cancella"
action = "delete"
scope = "word" # or "chunk"
```

Commands are user-defined exact matches (case-insensitive), and aliases can be
added. They are actions requested by the user, not autonomous decisions.

### Checks and development

Run targeted checks while developing, for example:

```sh
node --input-type=module --check < gnome-extension/bravoric-hear-read-write@riccardomurru.it/extension.js
python3 -m py_compile src/bravoric_stt_clipboard/*.py
```

The repository gate is `scripts/check-extension.sh`; it checks JavaScript and
Python syntax, metadata, schema synchronization, translations, and the test
suite. Run it when you are ready for the complete validation.

## Italiano

### Cosa fa

L'obiettivo è la produttività reale con un controllo sartoriale, non l'ennesimo
widget vocale standard:

1. **Dettatura in streaming.** Le parole vengono segmentate e inviate al campo
   focalizzato mentre si parla, come su macOS ma su GNOME e Wayland.
2. **Un carattere alla volta.** Il canale `type` invia tasti invece di incollare.
   Funziona in terminali, tmux/SSH, form web e applicazioni in cui il paste da
   clipboard non funziona o non è desiderato. Non dipende da una clipboard
   condivisa e non appare come un semplice paste intercettabile.
3. **Comandi vocali espliciti.** Si possono configurare `invio`, `cancella`
   (parola o chunk), Backspace e altre azioni. È l'utente a decidere quali
   parole attivano quali azioni.
4. **OCR e dettatura completa.** Si legge un'immagine già nella clipboard o si
   seleziona uno screenshot; la STT push-to-talk copia una trascrizione completa
   nella clipboard.
5. **Provider configurabili.** Si possono usare endpoint OpenAI-compatible
   locali, cloud o misti. Il fallback ordinato è automatico: si scelgono i
   backend e il sistema continua con il primo disponibile.
6. **Configurabilità estrema.** GUI e TOML espongono endpoint, modelli, fallback,
   timeout, VAD, hotword, prompt, blacklist, comandi vocali, shortcut, canale
   di paste, notifiche, icone e comportamento dello streaming.
7. **Azioni sempre decise dall'utente.** È controllo vocale esplicito, non un
   agente AI autonomo. L'AI non prende il controllo del computer: l'utente
   decide cosa dire e quando inviarlo.
8. **Privacy e scelta.** Gli endpoint locali o remoti li sceglie l'utente; il
   progetto non impone alcun servizio.

### Modalità e interfaccia

- **STT push-to-talk:** si preme la shortcut, si parla, si ripreme; il backend
  registra, trascrive e copia il risultato.
- **Streaming per chunk:** il rilevamento dell'attività vocale chiude i chunk,
  li trascrive e li consegna in ordine al campo focalizzato.
- **OCR:** elabora l'immagine nella clipboard oppure seleziona una regione dello
  schermo.
- **Quick button e indicatori:** pulsanti opzionali nella barra superiore
  avviano STT, OCR o streaming; l'indicatore GNOME mostra inattività,
  registrazione ed elaborazione.
- **Log persistente del fallback:** registra endpoint usato, esito e tempi, per
  capire subito dove si è fermato un provider.

### Architettura e requisiti

Il repository contiene due parti che collaborano:

- `gnome-extension/bravoric-hear-read-write@riccardomurru.it/` è la parte GNOME Shell:
  indicatore, preferenze, quick button, controlli OCR/STT e worker di input.
- `src/bravoric_stt_clipboard/` è il backend Python: acquisizione audio,
  STT/OCR, fallback dei provider, output clipboard e typing, notifiche e log.

La sottomissione al catalogo E.G.O. della parte GNOME dell'estensione è
attualmente in revisione e non è ancora disponibile; il backend Python è
necessario per le funzioni complete.

Requisiti: GNOME Shell 45–50 su Wayland, Python 3.11+, `ffmpeg` con `libopus`,
`wl-clipboard`, `notify-send`, `glib-compile-schemas` e `gsettings`.
`gnome-screenshot` è opzionale per la cattura OCR interattiva.

### Installazione e prima configurazione

```sh
scripts/install.sh
```

Lo script crea un virtualenv Python in
`$XDG_DATA_HOME/bravoric-stt-clipboard`, installa il backend, crea
`~/.config/bravoric-stt-clipboard/config.toml` dall'esempio nella lingua giusta
se manca e collega l'estensione a GNOME Shell. È sicuro da rilanciare dopo un
pull e non sovrascrive una configurazione personale esistente.

```sh
gnome-extensions enable bravoric-hear-read-write@riccardomurru.it
gnome-extensions prefs bravoric-hear-read-write@riccardomurru.it
```

Le shortcut predefinite sono `Alt+Super+R` per STT, `Alt+Super+O` per OCR e
`Alt+Super+S` per streaming. Dopo un aggiornamento Wayland può richiedere
logout/login o il riavvio della shell.

Esempio minimo di endpoint STT OpenAI-compatible in `config.toml`:

```toml
[[stt.fallback]]
name = "whisper-locale"
endpoint = "http://127.0.0.1:8000/v1"
model = "whisper-1"
api_key_env = ""
api_key = ""
timeout_seconds = 120
```

Si possono aggiungere altri blocchi `[[stt.fallback]]` per il fallback
automatico; esistono liste equivalenti per OCR e pulizia testuale opzionale.
Le chiavi API vanno nella configurazione personale o in una variabile d'ambiente,
mai nel repository.

### Canali di output e comandi vocali

Lo streaming distingue due canali:

- `paste_channel = "clipboard"` mette ogni chunk nella clipboard e invia la
  shortcut configurata (`Ctrl+V`, oppure `Ctrl+Shift+V` nei terminali). È rapido
  e compatibile con i normali campi GUI.
- `paste_channel = "type"` invia ogni carattere come evento di tastiera. Non
  usa la clipboard, quindi è adatto a terminali, tmux/SSH e campi che rifiutano
  il paste.

Esempio nella sezione `[stream]`:

```toml
[[stream.command]]
keyword = "invio"
action = "key"
key = "Return"

[[stream.command]]
keyword = "cancella"
action = "delete"
scope = "word" # oppure "chunk"
```

I comandi sono match esatti definiti dall'utente, senza distinzione tra
maiuscole e minuscole; si possono aggiungere alias. Sono azioni richieste
dall'utente, non decisioni autonome.

### Controlli e sviluppo

Controlli mirati possibili durante lo sviluppo:

```sh
node --input-type=module --check < gnome-extension/bravoric-hear-read-write@riccardomurru.it/extension.js
python3 -m py_compile src/bravoric_stt_clipboard/*.py
```

Il gate del repository è `scripts/check-extension.sh`: controlla sintassi
JavaScript e Python, metadata, sincronizzazione dello schema, traduzioni e test.
Va eseguito quando si è pronti alla validazione completa.
