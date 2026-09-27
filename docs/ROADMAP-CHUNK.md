# ROADMAP-CHUNK — Dettatura "streaming" con incolla diretto

> **Stato**: piano deciso (tutte le domande chiuse), non ancora implementato.
> **Obiettivo**: nuova modalità di dettatura che incolla il testo **direttamente** nel campo
> attivo (senza che l'utente prema `Ctrl+V`), con possibilità di segmentare l'audio **sui
> silenzi** così che il testo compaia "quasi istantaneamente".
> **Vincolo**: **non toccare il progetto attuale**. Tutto è additivo: nuovi moduli, nuova
> scorciatoia, nuova sezione di config. STT (`Alt+Super+R`) e OCR (`Alt+Super+O`) restano
> identici nel comportamento.
> **Rete di sicurezza**: backup completo già creato in
> `~/Progetti/bravoric-stt-clipboard-20260924-093215.tar.gz` (7,2 MB, 87 file, integrità
> verificata con `gzip -t`; esclusa solo la cache `.opencode`).

---

## 1. Requisiti decisi (con Riccardo)

| # | Requisito | Decisione |
| --- | --- | --- |
| 1 | Scorciatoia dedicata | **`Alt + Super + D`** (toggle: 1ª pressione avvia, 2ª ferma) |
| 2 | Incolla diretto | Sì: il testo va **direttamente** nel campo attivo, **senza** che l'utente prema `Ctrl+V` |
| 3 | Endpoint | **Dedicato**, configurabile, con **3 livelli di fallback** (stessa struttura di `[stt]`/`[ocr]`). Può essere lo stesso della trascrizione in blocco o diverso |
| 4 | Cleanup LLM | **DISATTIVATO** per questa funzionalità (niente ripasso LLM: sarebbe il collo di bottiglia da 5–75 s) |
| 5 | Modalità `at_end` | **Registrazione unica continua** (esattamente come STT oggi), **nessun chunk**: alla 2ª pressione trascrive tutto e incolla una volta sola. Niente segmentazione |
| 6 | Modalità `per_chunk` | **Segmentazione sui silenzi** (VAD via `silencedetect` di ffmpeg) + incolla **chunk per chunk** appena pronti |
| 7 | Soglia silenzio | **Configurabile dall'utente** (durata del silenzio che chiude un chunk) — si applica **solo** a `per_chunk` |
| 8 | Configurabilità | Endpoint/modello/chiave/soglia/modalità/pacing dalla GUI (come per STT/OCR) |

---

## 2. Perché è possibile (verifiche fatte sul sistema reale)

- **L'estensione gira dentro `gnome-shell`** (il compositor stesso), quindi può simulare
  input a livello privilegiato — cosa impossibile a un'app normale.
- **Nessun tool esterno di incolla è installato** (`wtype`, `ydotool`, `dotool`, `xdotool`
  tutti assenti): l'incolla **deve** essere fatto dall'estensione.
- **API di input virtuale confermata funzionante su GNOME Shell 50.4**, già usata da due
  estensioni installate (`clipboard-indicator@tudmotu.com/keyboard.js`,
  `emoji-copy@felipeftn/emojiButton.js`):
  ```js
  const seat = Clutter.get_default_backend().get_default_seat();
  this._vk = seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
  this._vk.notify_keyval(
      Clutter.get_current_event_time() * 1000,
      keyval,
      Clutter.KeyState.PRESSED /* o RELEASED */
  );
  ```
- **La clipboard si imposta dall'estensione** con `St.Clipboard.get_default().set_text(...)`
  (già usato in `extension.js` per le azioni di menu) → in modalità streaming il backend
  **non** ha bisogno di `wl-copy`.

### Rischio noto: il focus (risolto)
Oggi *l'utente* preme `Ctrl+V`, quindi decide **dove** finisce il testo. Con l'incolla
automatico decide la macchina al momento in cui il testo è pronto: se l'utente ha spostato il
focus altrove, il testo finisce nel posto sbagliato. **Decisione presa**: si incolla **sempre
nel focus corrente** (comportamento più naturale durante la dettatura); il rischio è accettato.

---

## 3. Architettura

```text
┌─────────────┐   Alt+Super+D    ┌──────────────────────────────────────────┐
│  GNOME Shell │ ───────────────▶ │  estensione bravoric-indicator           │
│  (estensione)│                  │  • registra stream-shortcut              │
│              │ ◀─────────────── │  • lancia bin/stream-toggle              │
│              │   stream_state   │  • monitora stream_state.json            │
│              │      .json       │  • quando c'è testo da consegnare:       │
│              │                  │      St.Clipboard.set_text() + Ctrl+V    │
└─────────────┘                  └──────────────────────────────────────────┘
                                                  ▲
                                                  │ scrive testo + stato
┌─────────────────────────────────────────────────┴──────────────────────────┐
│  backend Python  (bravoric-stream-toggle → cli.stream_toggle_main)          │
│                                                                             │
│  ── modalità at_end ──                                                      │
│   stream.py  ──▶  ffmpeg registrazione continua (1 file)                    │
│                   alla 2ª pressione: transcribe_audio() sul file intero     │
│                                                                             │
│  ── modalità per_chunk ──                                                   │
│   stream.py  ──▶  ffmpeg (pulse → silencedetect → segmenti OGG)             │
│                      │  parse stderr (silence_start/end)                    │
│                      └─▶ assembla utterance ──▶ transcribe_audio()          │
│                                                                             │
│   in entrambe: fallback [stream] a 3 livelli, NIENTE cleanup                │
│   └─▶ aggiorna stream_state.json  {session_id, active, mode, chunks[], idx} │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Flusso sessione — modalità `at_end`
1. **`Alt+Super+D`** (1ª volta) → avvia una registrazione continua (un file), stato *recording*,
   notifica "registrazione avviata".
2. L'utente parla quanto vuole (nessun chunk, nessuna trascrizione intermedia).
3. **`Alt+Super+D`** (2ª volta) → ferma ffmpeg, trascrive il file intero con l'endpoint
   `[stream]` (3 livelli, **senza cleanup**), scrive il testo in `stream_state.json` e
   l'estensione lo **incolla una volta** nel focus corrente. Stato → *idle*.

### Flusso sessione — modalità `per_chunk`
1. **`Alt+Super+D`** (1ª volta) → avvia ffmpeg con `silencedetect` + segmentazione, azzera
   `stream_state.json`, stato *recording*, notifica "registrazione avviata".
2. L'utente parla. ffmpeg emette su stderr gli eventi `silence_start` / `silence_end`.
3. A ogni silenzio abbastanza lungo, il backend chiude l'utterance, la manda allo STT dedicato
   (**senza cleanup**) e appende il testo ai `chunks` in `stream_state.json`.
4. L'estensione vede il file cambiare e **incolla subito** ogni nuovo chunk nel focus corrente
   (pacing configurabile tra un incolla e l'altro).
5. **`Alt+Super+D`** (2ª volta) → ferma ffmpeg, chiude l'ultimo chunk, stato → *idle*, notifica.

---

## 4. File nuovi / modificati

### Nuovi
- `src/bravoric_stt_clipboard/stream.py` — orchestratore sessione streaming.
- `bin/stream-toggle` — wrapper bash (identico a `bin/stt-toggle`, invoca `bravoric-stream-toggle`).
- `ROADMAP-CHUNK.md` — questo documento.

### Modificati (additivi, nessuna regressione attesa)
- `src/bravoric_stt_clipboard/cli.py` — aggiunta `stream_toggle_main()`.
- `src/bravoric_stt_clipboard/config.py` — aggiunta `StreamConfig` + parsing sezione `[stream]`.
- `src/bravoric_stt_clipboard/audio.py` — **nota**: probabile parametro opzionale
  `lock_path` (default = comportamento attuale) per riusare la registrazione con un lock
  dedicato. Additivo, nessun cambio di comportamento.
- `src/bravoric_stt_clipboard/stt.py` — **nota**: guardia additiva "blocca se stream attivo"
  (vedi §6, decisione concorrenza).
- `pyproject.toml` — `bravoric-stream-toggle = "bravoric_stt_clipboard.cli:stream_toggle_main"`.
- `config/config.example.toml` + `config.example.it.toml` — sezione `[stream]`.
- `gnome-extension/bravoric-indicator@local/extension.js` — scorciatoia, virtual keyboard,
  monitor `stream_state.json`, helper `pasteText()`.
- `gnome-extension/bravoric-indicator@local/schemas/*.gschema.xml` — chiave `stream-shortcut`.
- `gnome-extension/bravoric-indicator@local/prefs.js` — UI per endpoint/soglia/modalità/pacing.
- `scripts/install.sh` — installa `bin/stream-toggle`.
- `scripts/check-extension.sh` — guardia per il nuovo shortcut/stato.
- `po/` (backend) e `gnome-extension/.../po/` (estensione) — nuove stringhe.

---

## 5. Dettaglio tecnico

### 5.1 Modalità `at_end`
- Riusa la meccanica di registrazione di `audio.py` (ffmpeg pulse → OGG/Opus, un solo file),
  ma con un **lock dedicato** `stream.lock` (vedi §5.3) per non collidere con `recording.lock`
  di STT.
- Alla 2ª pressione: stop ffmpeg → `transcribe_audio` sul file intero via
  `try_with_fallback(cfg.stream_fallback, …)` → **nessun cleanup** → testo finale.
- Nessun `silencedetect`, nessuna segmentazione, nessuna soglia silenzio.

### 5.2 Modalità `per_chunk`
- **Registrazione + segmentazione** con **un solo** processo ffmpeg:
  ```bash
  ffmpeg -f pulse -i default -ac 1 -ar <sample_rate> \
         -af silencedetect=noise=<noise_db>dB:d=<silence_seconds> \
         -c:a libopus -b:a <bitrate>k \
         -f segment -segment_time 1 -reset_timestamps 1 \
         <session_dir>/seg-%05d.ogg
  ```
  Lo stesso processo fornisce **sia** i segmenti OGG **sia** gli eventi di silenzio su stderr
  (`silence_start: T` / `silence_end: T | silence_duration: D`), mappati sugli indici dei
  segmenti (segmento *i* copre `[i, i+1)` secondi).
- **Assemblaggio utterance**: i segmenti tra la fine del silenzio precedente e l'inizio del
  successivo vengono concatenati con `ffmpeg -f concat -c copy` (nessun re-encoding) → un
  file `.ogg` per utterance.
- **Trascrizione**: `try_with_fallback(cfg.stream_fallback, lambda l: transcribe_audio(l, ogg))`
  — riusa `api_client.transcribe_audio` e `fallback.try_with_fallback` esistenti.
  **Nessuna** chiamata a `cleanup_with_validation`.
- **Soglie ausiliarie**:
  - `min_utterance_seconds` — scarta blip troppo corti (evita STT su rumore).
  - `max_utterance_seconds` — flush forzato se l'utente parla senza pause.
- **Chunk vuoti**: se lo STT restituisce stringa vuota o solo spazi → **scartato in silenzio**
  (niente incolla, niente modifica all'indice, nessuna notifica).

### 5.3 Stato condiviso
- **Lock dedicato** `stream.lock` in `XDG_RUNTIME_DIR/bravoric-stt-clipboard/` (separato da
  `recording.lock` di STT).
- **Stato** in `XDG_RUNTIME_DIR/bravoric-stt-clipboard/stream_state.json`:
  ```json5
  {
    "session_id": "uuid-o-timestamp",
    "active": true,
    "mode": "at_end" | "per_chunk",
    "chunks": ["testo1", "testo2", "..."],  // testo dei chunk pronti (per_chunk) o testo unico (at_end)
    "next_chunk_index": 0,                   // indice del prossimo chunk da incollare
    "last_activity": 1727000000.0
  }
  ```
  Scrittura **atomica** (`os.replace`, come `status.py`/`output_history.py`).

### 5.4 Estensione — `extension.js`
- Nuovo keybinding `stream-shortcut` in `enable()` / `disable()` (come `dictation-shortcut`).
- Virtual keyboard creato in `enable()`, distrutto (`run_dispose()`) in `disable()`.
- File monitor su `stream_state.json` con lo stesso debounce di `status.json` (250 ms).
- Logica di consegna:
  - se `session_id` cambia → reset `_streamIndex = 0`;
  - se `chunks.length > _streamIndex` → incolla `chunks[_streamIndex++]` (vale per entrambe le
    modalità: in `at_end` c'è un solo elemento, in `per_chunk` uno per volta).
- Helper `pasteText(text)`:
  1. `St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text)`;
  2. `press(Clutter.KEY_Control_L)`, `press(Clutter.KEY_v)`, `release(Clutter.KEY_v)`,
     `release(Clutter.KEY_Control_L)`.
- **Pacing**: fra due incolla consecutivi va inserito un ritardo **configurabile** (default
  **250 ms**) per evitare che il chunk successivo sovrascriva la clipboard prima che l'app in
  focus la legga.

### 5.5 Config — `[stream]`
```toml
[stream]

# Modalità: "at_end" (registrazione unica + incolla alla fine) | "per_chunk" (chunk sui silenzi)
mode = "per_chunk"

# --- solo per "per_chunk" ---
silence_seconds = 0.7        # durata del silenzio che chiude un chunk
noise_db = -30               # livello sotto cui conta come silenzio (solo valore iniziale:
                             # il VAD adatta la soglia al rumore di fondo misurato)
min_utterance_seconds = 0.4  # scarta blip più corti
max_utterance_seconds = 30   # flush forzato se parli senza pause
paste_delay_ms = 250         # pausa tra un incolla e il successivo

# --- contesto STT (SUGGERIMENTO-STT) ---
language = "it"              # lingua ISO 639-1 forzata per ogni chunk ("" = auto)
prompt = ""                  # initial_prompt personale (stile/vocabolario), combinato
                             # con la coda degli ultimi 2-3 chunk (budget ~800 char)
hotwords = ""                # boosting vocabolario (efficace solo sul whisper locale)

# Endpoint dedicato con 3 livelli di fallback (stessa struttura di [stt]/[ocr]).
[[stream.fallback]]
name = "scrocco-llm"
endpoint = "http://10.9.0.2:4001/v1"
model = "scrocco-llm-fissone"
api_key_env = "SCROCCO_FISSONE_API_KEY"
ca_cert = "~/PiAgent/certs/scrocco-fissone.crt"
timeout_seconds = 120
```
> **Nota**: nessuna chiave `enabled` per il cleanup — per questa modalità è **sempre off**.
> **Nota contesto**: `language`/`prompt`/`hotwords` sono passati a `transcribe_audio` per
> ogni chunk. Il `prompt` effettivo è `build_prompt(personal, last_chunks)`: il prompt
> personale resta integro, i chunk recenti riempiono il budget residuo (i più vecchi
> vengono scartati se non ci stanno). Allucinazioni note e duplicati consecutivi sono
> filtrati sia in `update_last_chunks` sia in `build_prompt`.

---

## 6. Decisioni prese (tutte le domande chiuse)

| # | Domanda | Decisione |
| --- | --- | --- |
| 1 | Join dei chunk in `at_end` | **Nessun chunk in `at_end`**: è una registrazione unica continua (come STT oggi) + incolla automatico alla fine. Nessun join da fare |
| 2 | Sicurezza focus | **Incolla sempre nel focus corrente** (rischio accettato) |
| 3 | Chunk vuoti / silenzio | **Scarta in silenzio** (nessun incolla, nessuna notifica, indice invariato) |
| 4 | Concorrenza con STT/OCR | **Blocca se un'altra operazione è attiva**, con notifica breve («Altra operazione in corso») |
| 5 | Pacing `per_chunk` | **Parametro configurabile, default 250 ms** |

### 5.6 Codice già pronto nel progetto (trovato dai subagenti — file:line)

> Tutto il seguente è **VERBATIM** nel codice attuale: da riusare, non reinventare. Le
> "MANCANZE" sono le aree dove serve codice nuovo.

**Backend — `src/bravoric_stt_clipboard/`**

| Modulo | Codice pronto (file:line) | Riuso streaming |
| --- | --- | --- |
| `audio.py` | `LOCK_PATH` (28), `ToggleDebouncedError` (31), `is_recording()` (57), `start_recording(audio_cfg)` (76), `stop_recording(audio_cfg)` (138) | `start_recording`→registrazione continua (at_end); `is_recording`→guardia concorrenza. |
| `clipboard.py` | `write_text(text, tool="wl-copy")` (7) | scrive chunk nella clipboard. |
| `notify.py` | icone `ICON_RECORDING` (12),`ICON_PROCESSING` (13),`ICON_ERROR` (14),`ICON_READY` (15); `send()` (48), `maybe_send()` (60), `maybe_send_simple()` (69) | notifiche recording/chunk/error. |
| `status.py` | `STATUS_PATH` (11), `STATE_IDLE/RECORDING/PROCESSING/ERROR` (13-16), `write_status()` (39), `read_status()` (66) | modello per `stream_state.json` (stesso pattern). |
| `storage.py` | `save_if_enabled()` (17), `save_text_if_enabled()` (43) | opzionale: salva audio/testo. |
| `output_history.py` | `HISTORY_PATH` (15), `append_entry()` (71), `read_history()` (85), `clear_history()` (89) | registra chunk. |
| `i18n.py` | `DOMAIN` (9), `LOCALE_DIR` (10), `_` (13) | stringhe notifica. |
| `fallback.py` | `try_with_fallback()` (21), `cleanup_with_validation()` (36) | `try_with_fallback` per i 3 livelli stream. |
| `api_client.py` | `transcribe_audio()` (52-78), `ApiError` (13) | **VERBATIM** — trascrive un file audio, zero modifiche. |

**MANCANZE backend** (codice nuovo obbligatorio):
- Segmentazione audio su silenzio / streaming di chunk (nessuna in `audio.py`).
- Callback "per-chunk": parse `silencedetect` da stderr ffmpeg.
- Stato intermedio "chunk-ready" in `status.py`.
- Append alla clipboard (`wl-copy -a`) — `clipboard.py` ha solo overwrite.

### Config / CLI / packaging

| Area | Codice pronto (file:line) |
|---|---|
| `pyproject.toml` | entry points `bravoric-stt-toggle`/`bravoric-ocr-capture` (10-12) → aggiungere `bravoric-stream-toggle`. |
| `cli.py` | `stt_toggle_main()` (8-38), `ocr_capture_main()` (40-68) → modello per `stream_toggle_main()`. |
| `config.py` | `Config` dataclass (93-119), `_parse_fallback_list()` (122-140), `load_config()` (143-175), `_build_config()` (178-254) con `raw.get("section", {})` → aggiungere `StreamConfig` + `raw.get("stream", {})`. |
| `config_editor.py` | `SERVICES` (27-35), `LEVEL_FIELDS` (38-40), `set_level_field()` (144-165), `set_section_field()` (168-180), `set_storage_field()` (194-210), `get_state()` (245-322) → pattern per `set_stream_field()` / stream in `get_state()`. |
| `bin/stt-toggle`, `bin/ocr-capture` | bash wrapper (1-3) → modello per `bin/stream-toggle`. |
| `scripts/test-backend.py` | `check()` (29-31), `main()` (41-414) → aggiungere blocco `== stream ==`. |
| `scripts/check-extension.sh` | `ok()`/`bad()` (6-8) → aggiungere controllo stream. |

### Estensione GNOME

| Area | Codice pronto (file:line) |
|---|---|
| Scorciatoie | import `Meta`/`Shell` (5-6); `getSettings()` (492); `addKeybinding` dictation/ocr (493-502); `removeKeybinding` (506-507); menu Dictation/OCR (137-139, 141-143). |
| Lancio binari | `spawnBackground(binName)` (53-68), `VENV_BIN` (22-24), binari usati `bravoric-stt-toggle` (138,496), `bravoric-ocr-capture` (142,501). |
| Stato | `STATUS_PATH` (13-15), `_watchStatusFile` (355-368), debounce 250 ms (341-353), `_refreshStatus` (371-459), polling 30 s (163-167), timeout `STATE_TIMEOUT_SECONDS`/`PROCESSING_TIMEOUT_SECONDS` (44-51), `_timeoutLimitFor` (308-314). |
| Cronologia | `HISTORY_PATH` (16-18), `_refreshHistory` (232-287). |
| Icone/OSD | `THEME_ICONS` (26-31), icona applicata (420), `showCopiedOsd` (70-76). |
| Clipboard | `St.Clipboard.set_text` ×3 (127, 210-211, 277). |
| prefs.js | `fillPreferencesWindow` (288-340), `SHORTCUT_KEYS` (30-33), `SwitchRow` (302/324/702), `SpinRow`+`Adjustment` (466-472), `debounce` (185-193, usato 473/494/796), `runConfigEditor` (127-143), `CONFIG_EDITOR_BIN` (23), `TomlBoolEditor` (227-281). |
| Schema | `gschema.xml` (3-11): 2 chiavi `dictation-shortcut`/`ocr-shortcut` tipo `as`. |
| i18n | `po/bravoric-indicator.pot`, `po/it.po`, `po/LINGUAS` (`it`); `locale/.../bravoric-indicator.mo`. |

**MANCANZE estensione** (codice nuovo obbligatorio):
- **Nessun meccanismo di incolla / input virtuale esiste** in tutta l'estensione (nessun
  `VirtualInputDevice`, nessun `xdotool`/`ydotool`). Deve essere creato in `extension.js`.
- Nessun `stream-shortcut` nello schema né in `SHORTCUT_KEYS`.
- Nessuna pagina "Streaming" in `prefs.js`.

### Nota implementativa sulla decisione #4 (concorrenza)
Per bloccare in modo simmetrico servono due guardie:
- **stream → STT**: all'avvio della sessione stream, se `audio.is_recording()` è vero → blocco.
- **STT → stream**: all'avvio di STT, se una sessione stream è attiva → blocco. Questa è
  l'**unica modifica al flusso esistente** (`stt.py`), puramente additiva (una guardia che non
  cambia il comportamento normale).
- **OCR**: non ha lock ed è transiente; per ora **non** è guardato. Da valutare se aggiungere
  un lock anche a OCR.

---

## 7. Ordine di lavoro proposto

1. Backup (fatto).
2. `config.py` + `config.example*.toml` (sezione `[stream]`).
3. `stream.py` — modalità `at_end` (più semplice, riusa `audio.py`) → poi `per_chunk`.
   Testabile da riga di comando senza GUI.
4. `cli.py` + `pyproject.toml` + `bin/stream-toggle`.
5. Schema GSettings + `extension.js` (shortcut + virtual keyboard + incolla + pacing).
6. `prefs.js` (UI configurazione).
7. i18n (pot/po/mo backend + estensione).
8. Test: estendere `test-backend.py` e `check-extension.sh`; prova live in sessione reale.
9. Aggiornare `ROADMAP.md` con l'esito.

---

## 8. Stato attuale

- [x] Requisiti raccolti e decisi
- [x] Verifiche di fattibilità (input virtuale, assenza tool, API GNOME 50.4)
- [x] Backup di sicurezza
- [x] Questo documento
- [x] Tutte le 5 domande aperte chiuse
- [x] Implementazione (step 2–9)
  - VAD adattivo lato Python (ffmpeg emette PCM s16le su stdout; RMS frame-by-frame,
    soglia = pavimento di rumore stimato + margine, clampata in [-55, -15] dB).
  - Modalità `at_end` e `per_chunk` operative.
  - Stop "gentile" (drain): il supervisore svuota la coda PCM, trascrive l'ultima
    utterance e pubblica il chunk **mentre `active` è ancora true**, poi chiude.
  - Contesto STT: `language`/`prompt`/`hotwords` per chunk, `build_prompt` con budget
    ~800 char e coda degli ultimi 2-3 chunk (SUGGERIMENTO-STT).
  - Server Whisper locale OpenAI-compatibile (`bin/whisper-server.py`, faster-whisper)
    con `initial_prompt`/`hotwords`, unit systemd `bravoric-whisper.service`.
  - GUI: campi lingua/prompt/hotwords per STT e streaming; `config_editor` crea le
    sezioni `[stt]`/`[stream]` mancanti nei config legacy.
- [ ] Prova live dell'utente con la scorciatoia di dettatura streaming.
