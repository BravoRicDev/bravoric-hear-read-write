import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';
import Gdk from 'gi://Gdk';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

import { ExtensionPreferences, gettext as _ } from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

// Gli stessi helper REALI che extension.js usa per classificare i chunk:
// la validazione blacklist/comandi deve normalizzare come il backend, altrimenti
// la GUI accetta (o rifiuta) cose diverse da quelle che config.py blocca al
// reload. Stessa sorgente, nessuna copia da tenere allineata a mano.
// The same REAL helpers that extension.js uses to classify chunks:
// blacklist/command validation must normalize like the backend, otherwise
// the GUI accepts (or rejects) things different from what config.py blocks
// at reload. Same source, no copy to keep aligned by hand.
import { normalizeCommandKeyword, parseBlacklist } from './stream-consumer.mjs';

// Marcatore no-op per xgettext (--keyword=N_): segna per l'estrazione le
// stringhe usate nei const a livello di modulo, dove _() reale non è
// chiamabile (vedi nota più sotto). La traduzione vera avviene con _()
// al punto d'uso, dentro i metodi.
// No-op marker for xgettext (--keyword=N_): it marks for extraction the
// strings used in module-level consts, where a real _() cannot be called
// (see the note further below). The real translation happens with _() at the
// point of use, inside the methods.
const N_ = s => s;

// Whitelist dei tasti accettati dal backend per i comandi vocali (azione
// "key"). Deve restare IDENTICA a COMMAND_KEYS in
// src/bravoric_stt_clipboard/config.py: scripts/test-prefs-voice-commands.js
// confronta le due liste e fallisce in caso di drift. La validazione del
// backend resta comunque autoritativa.
// Whitelist of the keys accepted by the backend for voice commands ("key"
// action). It must stay IDENTICAL to COMMAND_KEYS in
// src/bravoric_stt_clipboard/config.py: scripts/test-prefs-voice-commands.js
// compares the two lists and fails on drift. The backend validation stays
// authoritative anyway.
const COMMAND_KEYS = [
    'Return', 'Enter', 'Tab', 'space', 'Escape', 'BackSpace', 'Delete',
    'Home', 'End', 'Page_Up', 'Page_Down', 'Left', 'Right', 'Up', 'Down',
    ...Array.from({ length: 12 }, (_, i) => `F${i + 1}`),
];
const COMMAND_KEY_SET = new Set(COMMAND_KEYS);

// Alias ESPLICITI e rivisti da Gdk.keyval_name() al nome esatto atteso dal
// backend. Non si fa uno strip generico di "KP_": solo questi alias valgono.
// Scelta documentata: KP_Enter -> "Return" (il runtime mappa sia Return sia
// Enter su Clutter.KEY_Return; "Return" è il valore canonico e già il default
// della riga). ISO_Left_Tab -> "Tab"; le varianti keypad di frecce/Home/End/Page
// sono mappate al nome backend corrispondente. Nota: Gdk.keyval_name()
// canonicalizza il keypad Page_Down in "KP_Next" (0xff9b), quindi l'alias
// KP_Next è necessario; teniamo anche KP_Page_Down per difesa.
// Tasti non mappati NON vengono salvati (feedback localizzato, si resta in cattura).
// EXPLICIT aliases, revised from Gdk.keyval_name() to the exact name the
// backend expects. No generic strip of "KP_": only these aliases apply.
// Documented choice: KP_Enter -> "Return" (the runtime maps both Return and
// Enter to Clutter.KEY_Return; "Return" is the canonical value and already
// the row default). ISO_Left_Tab -> "Tab"; the keypad variants of
// arrows/Home/End/Page are mapped to the corresponding backend name. Note:
// Gdk.keyval_name() canonicalizes the keypad Page_Down as "KP_Next"
// (0xff9b), so the KP_Next alias is necessary; we also keep KP_Page_Down as
// a defense. Unmapped keys are NOT saved (localized feedback, we stay in
// capture).
const CAPTURE_KEY_ALIASES = {
    KP_Space: 'space',
    KP_Enter: 'Return',
    ISO_Left_Tab: 'Tab',
    KP_Home: 'Home',
    KP_End: 'End',
    KP_Page_Up: 'Page_Up',
    KP_Next: 'Page_Down',
    KP_Page_Down: 'Page_Down',
    KP_Up: 'Up',
    KP_Down: 'Down',
    KP_Left: 'Left',
    KP_Right: 'Right',
};

const CONFIG_PATH = GLib.build_filenamev([
    GLib.get_home_dir(), '.config', 'bravoric-stt-clipboard', 'config.toml',
]);

// Il venv sta in posizione XDG fissa (vedi scripts/install.sh), indipendente
// da dove è stato clonato il repo: portabile tra macchine/utenti diversi.
// The venv sits in a fixed XDG location (see scripts/install.sh),
// independent of where the repo was cloned: portable across different
// machines/users.
const VENV_BIN = GLib.build_filenamev([
    GLib.get_user_data_dir(), 'bravoric-stt-clipboard', 'venv', 'bin',
]);
const CONFIG_EDITOR_BIN = GLib.build_filenamev([VENV_BIN, 'bravoric-config-editor']);

// Scorciatoie globali: schema GSettings proprio dell'estensione
// (schemas/org.gnome.shell.extensions.bravoric-hear-read-write.gschema.xml),
// gestite in extension.js via Main.wm.addKeybinding — non più scritte nella
// lista globale org.gnome.settings-daemon...media-keys (approccio precedente,
// vedi cleanupLegacyMediaKeysShortcuts sotto per la migrazione automatica).
// Global shortcuts: the extension's own GSettings schema
// (schemas/org.gnome.shell.extensions.bravoric-hear-read-write.gschema.xml),
// managed in extension.js via Main.wm.addKeybinding — no longer written in
// the global list org.gnome.settings-daemon...media-keys (previous approach,
// see cleanupLegacyMediaKeysShortcuts below for the automatic migration).
const SHORTCUT_KEYS = [
    { schemaKey: 'dictation-shortcut', label: N_('Dictation shortcut') },
    { schemaKey: 'ocr-shortcut', label: N_('OCR shortcut') },
    { schemaKey: 'stream-shortcut', label: N_('Streaming dictation shortcut') },
];

const LEGACY_MEDIA_KEYS_PATHS = [
    '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/bravoric-stt/',
    '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/bravoric-ocr/',
];

function cleanupLegacyMediaKeysShortcuts() {
    try {
        const mediaKeys = new Gio.Settings({ schema_id: 'org.gnome.settings-daemon.plugins.media-keys' });
        const current = mediaKeys.get_strv('custom-keybindings');
        const filtered = current.filter(p => !LEGACY_MEDIA_KEYS_PATHS.includes(p));
        if (filtered.length !== current.length) {
            mediaKeys.set_strv('custom-keybindings', filtered);
            Gio.Settings.sync();
        }
    } catch (e) {
        logError(e, 'bravoric-hear-read-write: impossibile pulire scorciatoie legacy');
    }
}

// Schemi di sistema che contengono scorciatoie globali "note": non copre le
// scorciatoie custom di altre estensioni (non enumerabili in modo affidabile),
// ma intercetta i conflitti più comuni (WM, Shell, Mutter, tasti multimediali).
// System schemas that contain "known" global shortcuts: it does not cover
// the custom shortcuts of other extensions (not reliably enumerable), but it
// intercepts the most common conflicts (WM, Shell, Mutter, multimedia keys).
const KEYBINDING_CONFLICT_SCHEMAS = [
    'org.gnome.desktop.wm.keybindings',
    'org.gnome.shell.keybindings',
    'org.gnome.mutter.keybindings',
    'org.gnome.mutter.wayland.keybindings',
    'org.gnome.settings-daemon.plugins.media-keys',
];

// F5 (giro 4): validazione VERA dell'acceleratore, non ricerca di conflitti.
// Prima l'unico controllo prima della scrittura era markConflict, che cerca
// una stringa IDENTICA in cinque schemi di sistema: non è una validazione. Una
// scorciotia non valida restava salvata in dconf per sempre e non accadeva
// MAI, senza un solo messaggio in nessuna lingua (misurato: la GUI scriveva
// 'Alt+Super+R' e '<Alt><Super>r' identici, il primo non funziona e il secondo sì).
// ATTENZIONE alla forma: Gtk.accelerator_parse NON ritorna un booleano in
// GJS, ritorna [ok, keyval, mods], un array boxed che in JavaScript è SEMPRE
// truthy — anche [false, 0, 0]. `if (!Gtk.accelerator_parse(t))` passerebbe
// quindi SEMPRE e sarebbe un controllo vacuo, cioè esattamente il difetto che
// questa funzione deve chiudere. Misurato su gjs 1.88.1:
//   '<Alt><Super>r'  -> [true, 114, 67108872]   ok
//   'Alt+Super+R'    -> [false, 0, 0]           rifiutata da Mutter
// Per questo si destruttura, e si passa anche da accelerator_valid: anche un
// acceleratore SENZA tasto ('<Super>') parsea, ma Mutter non lo accetta come
// keybinding, quindi valid() serve a chiudere quel buco.
// F5 (round 4): REAL validation of the accelerator, not a search for
// conflicts. Before, the only check before writing was markConflict, which
// looks for an IDENTICAL string in five system schemas: it is not a
// validation. An invalid shortcut stayed saved in dconf forever and NEVER
// fired, without a single message in any language (measured: the GUI wrote
// 'Alt+Super+R' and '<Alt><Super>r' identically, the first does not work and
// the second does).
// WATCH OUT for the form: Gtk.accelerator_parse does NOT return a boolean in
// GJS, it returns [ok, keyval, mods], a boxed array that in JavaScript is
// ALWAYS truthy — even [false, 0, 0]. `if (!Gtk.accelerator_parse(t))` would
// therefore ALWAYS pass and would be a vacuous check, i.e. exactly the defect
// this function must close. Measured on gjs 1.88.1:
//   '<Alt><Super>r'  -> [true, 114, 67108872]   ok
//   'Alt+Super+R'    -> [false, 0, 0]           rejected by Mutter
// That is why it is destructured, and accelerator_valid is used too: even an
// accelerator WITHOUT a key ('<Super>') parses, but Mutter does not accept it
// as a keybinding, so valid() serves to close that hole.
function acceleratorIsValid(text) {
    // Stringa vuota = scorciatoia rimossa, non un errore di sintassi.
    // Empty string = shortcut removed, not a syntax error.
    if (!text)
        return true;
    const [ok, keyval, mods] = Gtk.accelerator_parse(text);
    return ok && Gtk.accelerator_valid(keyval, mods);
}

function findShortcutConflict(binding) {
    if (!binding)
        return null;
    for (const schemaId of KEYBINDING_CONFLICT_SCHEMAS) {
        let settings;
        try {
            settings = new Gio.Settings({ schema_id: schemaId });
        } catch {
            continue; // schema non disponibile su questa versione di GNOME | schema not available on this GNOME version
        }
        for (const key of settings.settings_schema.list_keys()) {
            if (settings.settings_schema.get_key(key).get_value_type().dup_string() !== 'as')
                continue;
            if (settings.get_strv(key).includes(binding))
                return `${schemaId} → ${key}`;
        }
    }
    return null;
}

const SERVICES = [
    { key: 'stt', label: N_('STT — Audio transcription'), hasEnabled: false, hasPrompt: true, hasLanguage: true, hasHotwords: true, promptField: 'prompt' },
    { key: 'stt_cleanup', label: N_('STT — LLM cleanup'), hasEnabled: true, hasPrompt: true, promptField: 'system_prompt' },
    { key: 'ocr', label: N_('OCR — Extraction (Vision)'), hasEnabled: false, hasPrompt: true, promptField: 'system_prompt', hasScreenshotToggle: true },
    { key: 'ocr_cleanup', label: N_('OCR — LLM cleanup'), hasEnabled: true, hasPrompt: true, promptField: 'system_prompt' },
];

const LEVEL_ENTRY_FIELDS = [
    ['endpoint', N_('Endpoint')],
    ['model', N_('Model')],
    ['api_key_env', N_('API key environment variable')],
    ['ca_cert', N_('CA certificate (path)')],
];

const STORAGE_TYPES = [
    { section: 'stt_original', label: N_('STT — Original audio file') },
    { section: 'stt_raw', label: N_('STT — Raw transcription') },
    { section: 'stt_clean', label: N_('STT — LLM-cleaned transcription') },
    { section: 'ocr_original', label: N_('OCR — Original screenshot') },
    { section: 'ocr_raw', label: N_('OCR — Raw extraction') },
    { section: 'ocr_clean', label: N_('OCR — LLM-cleaned extraction') },
];

function setStorageField(section, field, value) {
    return runConfigEditor(['set-storage', section, field, String(value)]).success;
}

// P5: le chiavi di notifica passano da config_editor, che ha gia' il lock,
// la scrittura atomica e la validazione tomllib. Prima TomlBoolEditor
// scriveva config.toml con _readText + replace + replace_contents, SENZA
// lock: due scrittori, e la perdita era reale e silenziosa (un salvataggio
// di streaming appena fatto veniva annullato da un click su uno switch).
// Qui il valore torna anche al chiamante: se la scrittura fallisce, lo
// switch viene rimesso com'era invece di restare falsamente attivato.
// P5: the notification keys go through config_editor, which already has the
// lock, the atomic write and the tomllib validation. Before, TomlBoolEditor
// wrote config.toml with _readText + replace + replace_contents, WITHOUT a
// lock: two writers, and the loss was real and silent (a streaming save just
// made was undone by a click on a switch). Here the value also goes back to
// the caller: if the write fails, the switch is put back as it was instead of
// staying falsely on.
function setNotificationField(key, value) {
    return runConfigEditor(['set-notification', key, String(value)]).success;
}

const ICON_SLOTS = [
    { slot: 'stt_start', label: N_('STT — Processing started') },
    { slot: 'stt_raw', label: N_('STT — Raw text ready') },
    { slot: 'stt_clean', label: N_('STT — Cleaned text ready') },
    { slot: 'ocr_start', label: N_('OCR — Processing started') },
    { slot: 'ocr_raw', label: N_('OCR — Raw text ready') },
    { slot: 'ocr_clean', label: N_('OCR — Cleaned text ready') },
    { slot: 'stt_recording_start', label: N_('STT — Recording started') },
    { slot: 'stream_session_start', label: N_('Stream — Session started') },
    { slot: 'stream_processing_start', label: N_('Stream — Transcribing') },
    { slot: 'stream_session_end', label: N_('Stream — Session ended') },
    { slot: 'stream_chunk_delivered', label: N_('Stream — Text delivered') },
    { slot: 'error_general', label: N_('General — Error') },
];

function setIconField(slot, value) {
    return runConfigEditor(['set-icon', slot, value]).success;
}

// Messaggi di log per sviluppatori (journalctl), mai mostrati in UI:
// restano in italiano, non serve tradurli.
// Log messages for developers (journalctl), never shown in the UI: they stay
// in Italian, no need to translate them.
function runConfigEditor(args, stdinData = null) {
    try {
        const proc = Gio.Subprocess.new(
            [CONFIG_EDITOR_BIN, ...args],
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
                | (stdinData !== null ? Gio.SubprocessFlags.STDIN_PIPE : 0),
        );
        const [, stdout, stderr] = proc.communicate_utf8(stdinData, null);
        const success = proc.get_successful();
        if (!success)
            logError(new Error(`config_editor fallito: ${stderr}`), args.join(' '));
        return { success, stdout: stdout ? stdout.trim() : '' };
    } catch (e) {
        logError(e, `impossibile eseguire ${CONFIG_EDITOR_BIN}`);
        return { success: false, stdout: '' };
    }
}

function getServicesState() {
    const { success, stdout } = runConfigEditor(['get']);
    if (!success)
        return null;
    try {
        return JSON.parse(stdout);
    } catch (e) {
        logError(e, 'output config_editor get non è JSON valido');
        return null;
    }
}

// NORMALIZZAZIONE UNICA dei flag per-livello che arrivano come stringhe dai
// LEVEL_FIELDS di config_editor.get_state() (str(True) == "True"): sono
// "True"/"False", non booleani. Un solo criterio per tutto il file, usato sia
// dal banner del dispatch sia per lo stato iniziale degli Adw.SwitchRow, che
// senza questo partono SEMPRE accesi (!!'False' === true) e al primo click si
// spengono. Non reimplementarlo altrove: due copie divergono in silenzio e
// la griglia di valori in scripts/test-prefs-voice-commands.js smette di
// valere per entrambe.
// SINGLE NORMALIZATION of the per-level flags that arrive as strings from
// the LEVEL_FIELDS of config_editor.get_state() (str(True) == "True"): they
// are "True"/"False", not booleans. A single criterion for the whole file,
// used both by the dispatch banner and for the initial state of the
// Adw.SwitchRow, which without this ALWAYS start on (!!'False' === true) and
// turn off at the first click. Do not reimplement it elsewhere: two copies
// diverge silently and the grid of values in
// scripts/test-prefs-voice-commands.js stops holding for both.
function levelFlagTrue(value) {
    return String(value).toLowerCase() === 'true';
}

// Serializzazione delle righe della tabella dei comandi vocali nel payload
// JSON che config_editor set-stream-commands si aspetta. Funzione PURA a
// livello di modulo (niente Adw, niente GLib): e' estraibile ed eseguibile
// da solo, quindi i test la esercitano davvero invece di reimplementarla.
// Le righe senza keyword sono scartate: sono placeholder per aggiungerne
// una nuova, non comandi.
// Serialization of the rows of the voice commands table into the JSON
// payload that config_editor set-stream-commands expects. PURE function at
// module level (no Adw, no GLib): it is extractable and runnable on its own,
// so the tests really exercise it instead of reimplementing it. Rows without
// a keyword are discarded: they are placeholders to add a new one, not
// commands.
function serializeCommandRows(rows) {
    return rows.filter(row => row.keyword.text.trim()).map(row => ({
        keyword: row.keyword.text,
        aliases: row.aliases.text.split(',').map(value => value.trim()).filter(Boolean),
        action: row.action.selected === 0 ? 'key' : 'delete',
        key: row.action.selected === 0 ? row.key.text : '',
        scope: row.action.selected === 1 ? (row.scope.selected === 1 ? 'chunk' : 'word') : '',
        ends_session: row.ends.active,
    }));
}

// Stato del dispatch per-chunk. Sorgente UNICA: config_editor.get_state(),
// MAI ricalcolato in JS dallo stato dei widget. Motivo: i due divergono gia'
// appena si tocca un interruttore per-livello (gli switch scrivono con
// subprocess, ma il testo del banner e' gia' in memoria): ricalcolarlo in JS
// riprodurrebbe esattamente l'ambiguita' che questo toggle vuole togliere.
// Restituisce { subtitle, note } gia' tradotti e formattati.
// State of the per-chunk dispatch. SINGLE source: config_editor.get_state(),
// NEVER recomputed in JS from the widgets' state. Reason: the two already
// diverge as soon as a per-level switch is touched (the switches write with
// a subprocess, but the banner text is already in memory): recomputing it in
// JS would reproduce exactly the ambiguity this toggle wants to remove.
// Returns { subtitle, note } already translated and formatted.
function dispatchStatusFromState(streamState) {
    const levels = Array.isArray(streamState.levels) ? streamState.levels : [];
    // `parallel` arriva come stringa ("True"/"False" dai LEVEL_FIELDS, che
    // stringificano con str()) e NON come booleano: `!!'False'` e' true e lo
    // switch partirebbe SEMPRE acceso, al primo click si spegnerebbe invece di
    // accendersi. Il criterio e' levelFlagTrue, definito piu' in su: questo
    // banner e gli switch per-livello non possono che concordare, perche' non
    // esiste una seconda regola da mettere d'accordo con questa.
    // `parallel` arrives as a string ("True"/"False" from the LEVEL_FIELDS, which
    // stringify with str()) and NOT as a boolean: `!!'False'` is true and the
    // switch would ALWAYS start on, at the first click it would turn off instead
    // of on. The criterion is levelFlagTrue, defined higher up: this banner and
    // the per-level switches can only agree, because there is no second rule to
    // reconcile with this one.
    const checked = levels.filter(level => levelFlagTrue(level.parallel));
    const excluded = levels.filter(level => !levelFlagTrue(level.parallel));
    const namesOf = list => list
        .map(l => (l.name || '').trim())
        .filter(Boolean)
        .join(', ');
    const excludedNames = namesOf(excluded);

    if (streamState.dispatch_mode === 'sequential') {
        // I flag per-livello sono IGNORATI: dirlo, altrimenti uno switch acceso
        // sembra governare qualcosa che in realta' non governa.
        // The per-level flags are IGNORED: say so, otherwise a switch that is on
        // seems to govern something that in reality it does not govern.
        return {
            subtitle: _('Sequential: one level at a time, in order.'),
            note: excludedNames
                ? _('Ignored in sequential mode: %s').replace('%s', excludedNames)
                : '',
        };
    }

    if (checked.length === 0) {
        // Il degrada di "auto" detto PER NOME: senza questo, zero interruttori
        // accesi sembrerebbero un errore invece di una proprieta' del default.
        // The "degrades" of "auto" said BY NAME: without this, zero switches on
        // would look like an error instead of a property of the default.
        return { subtitle: _('Sequential (no level marked parallel).'), note: '' };
    }

    // N endpoint nel pool. "up to K workers" e' un TETTO, non una promessa:
    // in AUTO il cap vero e' ricalcolato ad ogni sessione sugli endpoint non in
    // cooldown, quindi puo' essere PIU basso del numero dichiarato.
    // N endpoints in the pool. "up to K workers" is a CAP, not a promise: in
    // AUTO the real cap is recomputed at every session on the endpoints not in
    // cooldown, so it can be LOWER than the declared number.
    const cap = Number(streamState.max_concurrent_chunks);
    const auto = streamState.max_concurrent_chunks_auto === true;
    const subtitle = _('Parallel: %d endpoints in the pool, up to %d workers.')
        .replace('%d', String(checked.length))
        .replace('%d', String(Number.isFinite(cap) && cap > 0 ? cap : 1));
    const notes = [];
    if (auto)
        notes.push(_('The worker cap is automatic: it is recalculated at each session from the endpoints not in cooldown, so the real number can be lower.'));
    if (excludedNames)
        // L'informazione che oggi manca del tutto: chi NON partecipa al pool e
        // quindi resta IRRAGGUNGIBILE finche' un altro livello e' parallelo.
        // The information that today is missing altogether: who does NOT take part
        // in the pool and therefore stays UNREACHABLE as long as another level is
        // parallel.
        notes.push(_('Not in the pool, unreachable while another level is parallel: %s')
            .replace('%s', excludedNames));
    return { subtitle, note: notes.join(' ') };
}

// Testo della riga per-livello "use in parallel". Unico posto: prima era una
// stringa sola che descriveva SOLO "auto" e mentiva in "sequential", dove il
// flag e' proprio ignorato. Un solo msgid, non due copie.
// Text of the per-level "use in parallel" row. Single place: before it was a
// single string that described ONLY "auto" and lied in "sequential", where
// the flag is in fact ignored. One msgid, not two copies.
function parallelRowSubtitle(globalParallel) {
    return globalParallel === false
        ? _('Ignored: the global Dispatch mode is Sequential. This level takes no part in the parallel pool.')
        : _('Send chunks to this endpoint at the same time as the other levels marked parallel, up to the slot limit below. Levels not marked here are NOT used as fallback.');
}

function setLevelField(service, index, field, value) {
    if (field === 'api_key') {
        // P2 (giro 14): l'api_key non deve passare come argv — /proc/PID/cmdline
        // è leggibile da altri utenti locali per la durata del subprocess
        // (confermato dal vivo). '-' è il segnale a config_editor.py di
        // leggere il valore da stdin invece che dall'argomento.
        // P2 (round 14): the api_key must not go through argv — /proc/PID/cmdline is
        // readable by other local users for the duration of the subprocess
        // (confirmed live). '-' is the signal to config_editor.py to read the value
        // from stdin instead of from the argument.
        return runConfigEditor(['set-level', service, String(index), field, '-'], value).success;
    }
    return runConfigEditor(['set-level', service, String(index), field, value]).success;
}

function setGeneralField(section, field, value) {
    return runConfigEditor(['set-general', section, field, String(value)]).success;
}

// Frequenze di campionamento accettate dall'encoder Opus (ffmpeg rifiuta le
// altre, es. 44100): stesso elenco di config_editor.GENERAL_FIELDS.
// Sample rates accepted by the Opus encoder (ffmpeg rejects the others,
// e.g. 44100): same list as config_editor.GENERAL_FIELDS.
const SAMPLE_RATES = [8000, 12000, 16000, 24000, 48000];

// Preset di formato di registrazione: nome (chiave di AUDIO_FORMATS nel
// backend) e etichetta. Nomi propri: non si traducono.
// Recording format presets: name (key of AUDIO_FORMATS in the backend) and
// label. Proper names: not translated.
const AUDIO_FORMAT_PRESETS = [
    { preset: 'ogg-opus', label: 'Ogg Opus' },
    { preset: 'ogg-vorbis', label: 'Ogg Vorbis' },
    { preset: 'mp3', label: 'MP3' },
    { preset: 'flac', label: 'FLAC' },
];

// Valori dell'indicatore in GSettings (schema): chiave, limiti e testi. Le
// chiavi sono lette da extension.js con settingInt(); un'anti-deriva nei test
// verifica che schema, estensione e questa lista coincidano.
// Indicator values stored in GSettings (schema): key, bounds and texts. The
// keys are read by extension.js through settingInt(); an anti-drift test
// checks that the schema, the extension and this list agree.
// Bottoni rapidi (chiavi booleane dello schema, default spento): stesse chiavi
// di QUICK_BUTTONS in quick-buttons.mjs; un'anti-deriva nei test le confronta.
// Quick buttons (boolean schema keys, default off): same keys as QUICK_BUTTONS
// in quick-buttons.mjs; an anti-drift test compares them.
const QUICK_BUTTON_SETTINGS = [
    { key: 'show-dictation-button', title: N_('Dictation button'), subtitle: N_('One-click start/stop of dictation, next to the indicator') },
    { key: 'show-ocr-button', title: N_('OCR button'), subtitle: N_('One-click text capture from the screen, next to the indicator') },
    { key: 'show-stream-button', title: N_('Streaming button'), subtitle: N_('One-click start/stop of streaming dictation, next to the indicator') },
];

const INDICATOR_SETTINGS = [
    { key: 'recording-timeout-minutes', title: N_('Recording watchdog (minutes)'), subtitle: N_('Reset a recording state that stops updating after this long'), lower: 1, upper: 600, step: 1 },
    { key: 'stt-timeout-minutes', title: N_('Dictation processing watchdog (minutes)'), subtitle: N_('Reset a stuck dictation processing state after this long'), lower: 1, upper: 600, step: 1 },
    { key: 'ocr-timeout-minutes', title: N_('OCR processing watchdog (minutes)'), subtitle: N_('Reset a stuck OCR processing state after this long'), lower: 1, upper: 1440, step: 5 },
    { key: 'error-timeout-minutes', title: N_('Error state duration (minutes)'), subtitle: N_('Return to idle this long after an error'), lower: 1, upper: 120, step: 1 },
    { key: 'timeout-check-interval-seconds', title: N_('Watchdog check interval (seconds)'), subtitle: N_('How often the status is re-checked when the backend stops writing'), lower: 5, upper: 300, step: 5 },
    { key: 'history-preview-chars', title: N_('History preview length (characters)'), subtitle: N_('How much of each history entry the menu shows'), lower: 10, upper: 500, step: 5 },
    { key: 'last-output-preview-chars', title: N_('Last output preview length (characters)'), subtitle: N_('How much of the last output the menu shows'), lower: 10, upper: 500, step: 5 },
    { key: 'blink-interval-ms', title: N_('Recording blink interval (ms)'), subtitle: N_('Half-period of the indicator blink while recording'), lower: 100, upper: 2000, step: 50 },
    { key: 'type-key-interval-ms', title: N_('Typing key interval (ms)'), subtitle: N_('Delay between simulated keystrokes when streaming types instead of pasting'), lower: 1, upper: 100, step: 1 },
    { key: 'stream-end-timeout-seconds', title: N_('Streaming end wait (seconds)'), subtitle: N_('How long to wait for pending text before a voice command ends the session'), lower: 1, upper: 60, step: 1 },
];

function setSectionField(service, field, value) {
    return runConfigEditor(['set-section', service, field, value]).success;
}

function flashButtonLabel(button, text, revertText, delayMs = 1500) {
    button.label = text;
    let timeoutId = null;
    timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delayMs, () => {
        // Giro 18: l'id va azzerato QUI. Tenendolo, la guardia sotto lo
        // rimuoverebbe a fine sessione una sorgente gia' scaduta, e
        // GLib.source_remove su un id morto stampa un CRITICAL.
        // Round 18: the id must be reset HERE. Keeping it, the guard below would
        // remove an already expired source at the end of the session, and
        // GLib.source_remove on a dead id prints a CRITICAL.
        timeoutId = null;
        button.label = revertText;
        return GLib.SOURCE_REMOVE;
    });
    // Giro 18: se il bottone viene distrutto prima dello scadere (chiusura
    // della finestra Preferences), il timeout toccherebbe un widget morto.
    // Round 18: if the button is destroyed before the expiry (closing the
    // Preferences window), the timeout would touch a dead widget.
    button.connect('destroy', () => {
        if (timeoutId) {
            GLib.source_remove(timeoutId);
            timeoutId = null;
        }
    });
}

// Giro 17: le SpinRow (retention/max-entries/timeout) collegavano notify::value
// direttamente a runConfigEditor — ogni tick di scroll/click-and-hold sulle
// frecce spawna e attende SINCRONAMENTE (communicate_utf8, bloccante) un
// intero processo Python, congelando visibilmente la finestra Preferences.
// Giro 18: registry dei debounce attivi, cancellati
// tutti alla chiusura della finestra per evitare che il
// callback tocchi widget già morti.
// Round 17: the SpinRows (retention/max-entries/timeout) connected
// notify::value directly to runConfigEditor — every scroll tick /
// click-and-hold on the arrows spawns and SYNCHRONOUSLY waits for
// (communicate_utf8, blocking) a whole Python process, visibly freezing the
// Preferences window. Round 18: registry of the active debounces, all
// cancelled when the window closes to keep the callback from touching
// already dead widgets.
const _debounceFns = new Set();

// debounce() ritarda l'esecuzione fino a che i tick smettono di arrivare.
// Restituisce la funzione debounced con un metodo .cancel() che
// annulla il timeout pendente.
// debounce() delays the execution until the ticks stop arriving. It returns
// the debounced function with a .cancel() method that cancels the pending
// timeout.
function debounce(fn, delayMs = 400) {
    let timeoutId = null;
    const debounced = (...args) => {
        if (timeoutId)
            GLib.source_remove(timeoutId);
        timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delayMs, () => {
            timeoutId = null;
            fn(...args);
            return GLib.SOURCE_REMOVE;
        });
    };
    debounced.cancel = () => {
        if (timeoutId) {
            GLib.source_remove(timeoutId);
            timeoutId = null;
        }
    };
    _debounceFns.add(debounced);
    return debounced;
}

// Notifiche generate dall'estensione stessa (Shell), non dal backend: vivono in
// GSettings (chiavi dello schema), non in config.toml.
// Notifications generated by the extension itself (Shell), not by the
// backend: they live in GSettings (schema keys), not in config.toml.
const EXTENSION_NOTIFICATIONS = [
    { key: 'notify-errors', title: N_('Errors'), subtitle: N_('Notify about streaming paste/typing failures, invalid voice commands, a missing backend and status file errors') },
    { key: 'notify-status', title: N_('Status messages'), subtitle: N_('Notify when the status file is restored or the backend times out') },
];

const NOTIFICATION_GROUPS = [
    {
        prefix: 'stt',
        title: N_('STT notifications (voice dictation)'),
        simpleRow: {
            title: N_('Processing started'),
            subtitle: N_('Notify when transcription starts (no text available at this point)'),
        },
        contentRows: [
            { title: N_('Raw text ready'), subtitle: N_('Notify when the raw text is in the clipboard'), key: 'raw_ready' },
            { title: N_('Cleaned text ready'), subtitle: N_('Notify when the LLM-corrected text is in the clipboard'), key: 'cleanup_ready' },
        ],
        extraRows: [
            { title: N_('Recording started'), subtitle: N_('Notify when the microphone starts recording'), key: 'recording_start' },
            { title: N_('Errors'), subtitle: N_('Notify when transcription or clipboard writing fails'), key: 'error' },
        ],
    },
    {
        prefix: 'ocr',
        title: N_('OCR notifications (screenshot)'),
        simpleRow: {
            title: N_('Processing started'),
            subtitle: N_('Notify when extraction starts (no text available at this point)'),
        },
        contentRows: [
            { title: N_('Raw text ready'), subtitle: N_('Notify when the raw text is in the clipboard'), key: 'raw_ready' },
            { title: N_('Cleaned text ready'), subtitle: N_('Notify when the LLM-corrected text is in the clipboard'), key: 'cleanup_ready' },
        ],
        extraRows: [
            { title: N_('Errors'), subtitle: N_('Notify when screenshot capture, text extraction or clipboard writing fails'), key: 'error' },
        ],
    },
    {
        prefix: 'stream',
        title: N_('Streaming notifications'),
        simpleRow: {
            title: N_('Streaming session started'),
            subtitle: N_('Notify when streaming dictation starts'),
        },
        contentRows: [
            { title: N_('Transcribed text ready'), subtitle: N_('Notify when streamed text is ready'), key: 'raw_ready' },
        ],
        extraRows: [
            { title: N_('Streaming session ended'), subtitle: N_('Notify when the streaming session ends'), key: 'session_end' },
            { title: N_('Errors'), subtitle: N_('Notify about streaming errors, a busy microphone or a failed transcription'), key: 'error' },
        ],
    },
];

// Il file config.toml è generato e gestito interamente da questo progetto
// (formato fisso, una chiave per riga): sostituzione mirata per riga è
// sufficiente e più robusta di trascinare un parser TOML completo in GJS.
// The config.toml file is generated and managed entirely by this project
// (fixed format, one key per line): targeted per-line replacement is enough
// and more robust than dragging a full TOML parser into GJS.
class TomlBoolEditor {
    constructor(path) {
        this._path = path;
        this._file = Gio.File.new_for_path(path);
    }

    _readText() {
        // B19: proteggi load_contents da race condition (file cancellato tra
        // isinstance check e load_contents).
        // B19: protect load_contents from race conditions (file deleted between the
        // isinstance check and load_contents).
        try {
            const [ok, contents] = this._file.load_contents(null);
            if (!ok)
                throw new Error(`Impossibile leggere ${this._path}`);
            return new TextDecoder().decode(contents);
        } catch (e) {
            if (e.message?.includes('Impossibile leggere'))
                throw e;
            throw new Error(`Impossibile leggere ${this._path}: ${e.message}`);
        }
    }

    // Pattern UNICO per readBool e writeBool: i due devono concordare, altrimenti
    // la GUI mente sullo stato (read non aggancia la riga) e write cade nel ramo
    // di inserimento, duplicando la chiave e rendendo il TOML illeggibile.
    // TOLLERANZE (giro 2, F2):
    //   - spazi/tab attorno a '=' e prima del commento;
    //   - indentazione iniziale (la chiave puo' stare dentro una sezione);
    //   - commento in fondo: "key = true  # nota" -> la riga AGGIORNA davvero
    //     invece di duplicare la chiave. Senza questo, un commento (normalissimo)
    //     rendeva illeggibile l'intera config: config.py tradue
    //     TOMLDecodeError in ConfigError e tutto il backend diventa muto.
    // La chiave viene quotata: un nome con metacaratteri di regex non deve
    // poter agganciare una riga diversa.
    // SINGLE pattern for readBool and writeBool: the two must agree, otherwise
    // the GUI lies about the state (read does not hook the line) and write falls
    // into the insert branch, duplicating the key and making the TOML unreadable.
    // TOLERANCES (round 2, F2):
    //   - spaces/tabs around '=' and before the comment;
    //   - leading indentation (the key may sit inside a section);
    //   - trailing comment: "key = true  # note" -> the line really UPDATES
    //     instead of duplicating the key. Without this, a (perfectly normal)
    //     comment made the whole config unreadable: config.py turns
    //     TOMLDecodeError into ConfigError and the whole backend goes mute.
    // The key is quoted: a name with regex metacharacters must not be able to
    // hook a different line.
    static boolPattern(key) {
        const escaped = String(key).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        return new RegExp(`^[ \\t]*${escaped}[ \\t]*=[ \\t]*(true|false)[ \\t]*(?:#.*)?$`, 'm');
    }

    readBool(key, fallback) {
        let text;
        try {
            text = this._readText();
        } catch (e) {
            // B19: non crashare se il file è stato cancellato tra due letture.
            // B19: do not crash if the file was deleted between two reads.
            logError(e, `readBool fallback=${fallback}`);
            return fallback;
        }
        const match = text.match(TomlBoolEditor.boolPattern(key));
        return match ? match[1] === 'true' : fallback;
    }

    writeBool(key, value) {
        let text;
        try {
            text = this._readText();
        } catch (e) {
            // B5: se il file non esiste/è illeggibile, non crashare nel signal handler.
            // B5: if the file does not exist/is unreadable, do not crash in the signal
            // handler.
            logError(e, `impossibile leggere ${this._path} per writeBool`);
            return;
        }
        const pattern = TomlBoolEditor.boolPattern(key);
        const replacement = `${key} = ${value}`;
        let newText;
        if (pattern.test(text)) {
            newText = text.replace(pattern, replacement);
        } else {
            // Le configurazioni create prima dell'aggiunta di una preferenza
            // non contengono la nuova chiave. Inseriscila in [notifications]
            // invece di mostrare uno switch che sembra funzionare ma non salva.
            // Configurations created before a preference was added do not contain the
            // new key. Insert it in [notifications] instead of showing a switch that
            // seems to work but does not save.
            const section = text.match(/^\[notifications\]\s*$/m);
            if (!section) {
                logError(new Error('[notifications] mancante'), `impossibile scrivere ${key}`);
                return;
            }
            const sectionEnd = section.index + section[0].length;
            const tail = text.slice(sectionEnd);
            const nextSection = tail.match(/^\[/m);
            const insertAt = nextSection ? sectionEnd + nextSection.index : text.length;
            const before = text.slice(0, insertAt).replace(/\s*$/, '\n');
            const after = text.slice(insertAt).replace(/^\s*/, '\n');
            newText = `${before}${replacement}\n${after}`;
        }
        try {
            this._file.replace_contents(
                new TextEncoder().encode(newText), null, false,
                Gio.FileCreateFlags.NONE, null,
            );
        } catch (e) {
            logError(e, `impossibile scrivere ${this._path} per writeBool`);
        }
    }
}

export default class BravoricPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const editor = new TomlBoolEditor(CONFIG_PATH);

        const page = new Adw.PreferencesPage({
            title: _('Notifications'),
            icon_name: 'preferences-system-notifications-symbolic',
        });
        window.add(page);

        // Interruttore generale ([general] notifications): prima non aveva
        // nessuna riga in GUI. Sta in [general], non in [notifications]: passa da
        // set-general e si legge dallo stato del backend, non da TomlBoolEditor.
        // Master switch ([general] notifications): before it had no row in the GUI
        // at all. It sits in [general], not in [notifications]: it goes through
        // set-general and is read from the backend state, not from TomlBoolEditor.
        const generalState = getServicesState()?.general;
        if (generalState) {
            const masterGroup = new Adw.PreferencesGroup({ title: _('All notifications') });
            page.add(masterGroup);
            const masterRow = new Adw.SwitchRow({
                title: _('Show notifications'),
                subtitle: _('Master switch for every notification sent by the backend. Notifications from the extension itself are controlled in the group below.'),
                active: generalState.notifications ?? true,
            });
            masterRow.connect('notify::active', () => {
                if (!setGeneralField('general', 'notifications', masterRow.active ? 'true' : 'false'))
                    masterRow.active = getServicesState()?.general?.notifications ?? true;
            });
            masterGroup.add(masterRow);
        }

        for (const notifGroup of NOTIFICATION_GROUPS) {
            const group = new Adw.PreferencesGroup({ title: _(notifGroup.title) });
            page.add(group);

            const startKey = `${notifGroup.prefix}_on_processing_start`;
            const startRow = new Adw.SwitchRow({
                title: _(notifGroup.simpleRow.title),
                subtitle: _(notifGroup.simpleRow.subtitle),
                active: editor.readBool(startKey, true),
            });
            startRow.connect('notify::active', () => {
                // P5: ritorna com'era se la scrittura è fallita, invece di
                // lasciare lo switch attivato su un config che non l'ha.
                // P5: it goes back as it was if the write failed, instead of leaving the
                // switch on for a config that does not have it.
                if (!setNotificationField(startKey, startRow.active))
                    startRow.active = editor.readBool(startKey, true);
            });
            group.add(startRow);

            // Notifiche solo on/off (senza testo): ogni notifica del backend
            // ha il suo interruttore, nessuna resta non configurabile.
            // On/off-only notifications (no text): every backend notification has its
            // own switch, none stays non-configurable.
            for (const extra of notifGroup.extraRows ?? []) {
                const extraKey = `${notifGroup.prefix}_on_${extra.key}`;
                const extraRow = new Adw.SwitchRow({
                    title: _(extra.title),
                    subtitle: _(extra.subtitle),
                    active: editor.readBool(extraKey, true),
                });
                extraRow.connect('notify::active', () => {
                    if (!setNotificationField(extraKey, extraRow.active))
                        extraRow.active = editor.readBool(extraKey, true);
                });
                group.add(extraRow);
            }

            for (const row of notifGroup.contentRows) {
                const enabledKey = `${notifGroup.prefix}_on_${row.key}`;
                const contentKey = `${enabledKey}_content`;

                const expander = new Adw.ExpanderRow({
                    title: _(row.title),
                    subtitle: _(row.subtitle),
                    show_enable_switch: true,
                    enable_expansion: editor.readBool(enabledKey, true),
                });
                expander.connect('notify::enable-expansion', () => {
                    if (!setNotificationField(enabledKey, expander.enable_expansion))
                        expander.enable_expansion = editor.readBool(enabledKey, true);
                });

                const contentRow = new Adw.SwitchRow({
                    title: _('Show content in notification text'),
                    subtitle: _('If off, shows only the title (no transcribed text)'),
                    active: editor.readBool(contentKey, true),
                });
                contentRow.connect('notify::active', () => {
                    if (!setNotificationField(contentKey, contentRow.active))
                        contentRow.active = editor.readBool(contentKey, true);
                });
                expander.add_row(contentRow);

                group.add(expander);
            }
        }

        const extSettings = this.getSettings();
        const extGroup = new Adw.PreferencesGroup({ title: _('Extension notifications') });
        page.add(extGroup);
        for (const item of EXTENSION_NOTIFICATIONS) {
            // Schema stantio (gschemas.compiled non ricompilato): niente bind,
            // che con una chiave assente e' un'assertion fatale.
            // Stale schema (gschemas.compiled not recompiled): no bind, which with a
            // missing key is a fatal assertion.
            if (!extSettings.settings_schema.has_key(item.key))
                continue;
            const extRow = new Adw.SwitchRow({ title: _(item.title), subtitle: _(item.subtitle) });
            extSettings.bind(item.key, extRow, 'active', Gio.SettingsBindFlags.DEFAULT);
            extGroup.add(extRow);
        }

        this._buildGeneralPage(window);
        this._buildShortcutsPage(window);
        this._buildStreamPage(window);
        this._buildStoragePage(window);
        this._buildIconsPage(window);
        this._buildServicesPage(window);

        // ExtensionPrefsDialog è un Adw.PreferencesDialog su GNOME 50 e non
        // espone il segnale `close` (né `close-request`). `destroy` è ereditato
        // da Gtk.Widget ed è disponibile anche sulle PreferencesWindow delle
        // versioni GNOME precedenti supportate.
        // ExtensionPrefsDialog is an Adw.PreferencesDialog on GNOME 50 and does not
        // expose the `close` signal (nor `close-request`). `destroy` is inherited
        // from Gtk.Widget and is also available on the PreferencesWindow of the
        // earlier supported GNOME versions.
        window.connect('destroy', () => {
            _debounceFns.forEach(fn => fn.cancel?.());
            _debounceFns.clear();
        });
    }

    _buildShortcutsPage(window) {
        cleanupLegacyMediaKeysShortcuts();
        const settings = this.getSettings();

        const page = new Adw.PreferencesPage({
            title: _('Shortcuts'),
            icon_name: 'preferences-desktop-keyboard-symbolic',
        });
        window.add(page);

        const group = new Adw.PreferencesGroup({
            title: _('Global keyboard shortcuts'),
            // Giro 2 (F3): la description di Adw.PreferencesGroup e' MARKUP.
            // '<Alt><Super>r' veniva interpretato come tag: GTK segnalava
            // "error parsing markup" e l'etichetta restava VUOTA, mentre nel
            // sorgente la stringa sembrava intatta. Solo gli angoli della
            // descrizione vanno protetti: sono l'unico carattere di markup
            // che questa stringa contiene, e sono anche esattamente la
            // notazione che l'utente deve leggere per capire il formato.
            //
            // IL SECONDO ARGOMENTO NON E' UN ACCESSORIO. La firma in GJS e'
            // markup_escape_text(text, length_bytes): senza lunghezza la
            // chiamata solleva TypeError ("At least 2 arguments required") e
            // l'intero dialogo Preferenze non si apre piu'. -1 = "fino alla
            // fine della stringa", che e' il comportamento volutamente
            // usato anche in Gtk.accelerator_parse piu' sotto.
            // Round 2 (F3): the description of Adw.PreferencesGroup is MARKUP.
            // '<Alt><Super>r' was interpreted as a tag: GTK reported "error parsing
            // markup" and the label stayed EMPTY, while in the source the string looked
            // intact. Only the angle brackets of the description need protecting: they
            // are the only markup character this string contains, and they are also
            // exactly the notation the user must read to understand the format.
            //
            // THE SECOND ARGUMENT IS NOT AN ACCESSORY. The signature in GJS is
            // markup_escape_text(text, length_bytes): without a length the call raises
            // TypeError ("At least 2 arguments required") and the whole Preferences
            // dialog no longer opens. -1 = "up to the end of the string", which is the
            // behavior deliberately used also in Gtk.accelerator_parse further below.
            description: GLib.markup_escape_text(
                _('Format: <Alt><Super>r (modifier names between angle brackets, then the key)'), -1),
        });
        page.add(group);

        for (const shortcut of SHORTCUT_KEYS) {
            const row = new Adw.EntryRow({
                title: _(shortcut.label),
                text: settings.get_strv(shortcut.schemaKey)[0] || '',
                show_apply_button: true,
            });

            const markConflict = binding => {
                // Confronta anche con l'altra scorciatoia propria: entrambe
                // vivono nello stesso schema, findShortcutConflict scansiona
                // solo schemi esterni e non le vede in conflitto tra loro.
                // It also compares with the other own shortcut: both live in the same
                // schema, findShortcutConflict scans only external schemas and does not see
                // them in conflict with each other.
                let conflict = null;
                if (binding) {
                    for (const other of SHORTCUT_KEYS) {
                        if (other.schemaKey === shortcut.schemaKey)
                            continue;
                        if (settings.get_strv(other.schemaKey)[0] === binding) {
                            conflict = _(other.label);
                            break;
                        }
                    }
                }
                if (!conflict)
                    conflict = findShortcutConflict(binding);
                if (conflict) {
                    row.add_css_class('error');
                    row.set_tooltip_text(_('Already used by: %s').replace('%s', conflict));
                } else {
                    row.remove_css_class('error');
                    row.set_tooltip_text('');
                }
            };

            const markInvalid = binding => {
                row.add_css_class('error');
                row.set_tooltip_text(_('Not a valid shortcut: %s').replace('%s', binding));
            };

            // Aggiorna lo stato visivo della riga e dice se il valore corrente
            // è utilizzabile. Un solo punto di verità per il caricamento iniziale
            // e per l'apply: altrimenti il valore già invalido in dconf resterebbe
            // verde all'apertura delle preferenze.
            // Updates the row's visual state and says whether the current value is
            // usable. A single point of truth for the initial load and for the apply:
            // otherwise a value already invalid in dconf would stay green when the
            // preferences open.
            const refreshEntryState = () => {
                if (!acceleratorIsValid(row.text)) {
                    markInvalid(row.text);
                    return false;
                }
                markConflict(row.text);
                return true;
            };

            row.connect('apply', () => {
                // F5: validazione PRIMA di scrivere. Se non parsea non si
                // scrive: la riga resta marcata errore con il motivo, e il
                // valore in dconf resta quello di prima invece di una
                // scorciatoia che non accadrà mai.
                // F5: validation BEFORE writing. If it does not parse it is not written: the
                // row stays marked as an error with the reason, and the value in dconf stays
                // the previous one instead of a shortcut that will never fire.
                if (!refreshEntryState())
                    return;
                settings.set_strv(shortcut.schemaKey, row.text ? [row.text] : []);
                Gio.Settings.sync();
            });
            refreshEntryState();
            group.add(row);
        }
    }

    _buildStreamPage(window) {
        const page = new Adw.PreferencesPage({
            title: _('Streaming'),
            icon_name: 'audio-input-microphone-symbolic',
        });
        window.add(page);

        const state = getServicesState();
        if (!state || !state.stream) {
            this._showConfigError(page);
            return;
        }

        const streamState = state.stream;

        const modeGroup = new Adw.PreferencesGroup({
            title: _('Streaming dictation (experimental)'),
            description: _('Direct typing into the focused field. "At end": single recording transcribed when stopped. "Per chunk": continuous segmentation on pauses.'),
        });
        page.add(modeGroup);

        const modeRow = new Adw.ComboRow({
            title: _('Mode'),
            model: new Gtk.StringList({
                strings: [_('Per chunk (transcribe on pauses)'), _('At end (transcribe when stopped)')],
            }),
            selected: streamState.mode === 'at_end' ? 1 : 0,
        });
        modeRow.connect('notify::selected', () => {
            const newMode = modeRow.selected === 1 ? 'at_end' : 'per_chunk';
            runConfigEditor(['set-stream', 'mode', newMode]);
        });
        modeGroup.add(modeRow);

        // --- dispatch parallelo/sequenziale (toggle GLOBALE) ---------------
        // Tre fatti che la GUI non diceva e che rendono il pasticcio ambiguo:
        //  1. il toggle NON e' a caldo (nessuno riavvia la sessione: il
        //     supervisor tiene la StreamConfig in memoria per tutta la
        //     sessione) -> avviso esplicito, non silenzio;
        //  2. vale solo per la modalita' per-chunk: in "at_end" i flag per-
        //     livello sono gia' ignorati dal codice, quindi qui non cambia
        //     niente -> detto nella UI, non implementato in at_end;
        //  3. "sequenziale" NON vuol dire "un chunk alla volta": restano i
        //     worker (3 di default), ognuno percorre la catena.
        // --- parallel/sequential dispatch (GLOBAL toggle) ---------------
        // Three facts the GUI did not say and that make the mess ambiguous:
        //  1. the toggle is NOT hot (nobody restarts the session: the supervisor
        //     keeps the StreamConfig in memory for the whole session) -> explicit
        //     warning, not silence;
        //  2. it applies only to the per-chunk mode: in "at_end" the per-level flags
        //     are already ignored by the code, so here nothing changes -> said in the
        //     UI, not implemented in at_end;
        //  3. "sequential" does NOT mean "one chunk at a time": the workers remain
        //     (3 by default), each one walks the chain.
        const dispatchModeRow = new Adw.ComboRow({
            title: _('Dispatch mode (per-chunk mode only)'),
            subtitle: _('Auto uses the pool of endpoints marked parallel; sequential ignores every per-level switch and walks the levels in order. Not applied to a running session: it takes effect at the next one.'),
            model: new Gtk.StringList({
                strings: [_('Auto (parallel if any level is marked)'), _('Sequential (one level at a time)')],
            }),
            selected: streamState.dispatch_mode === 'sequential' ? 1 : 0,
        });
        // Il banner e' una ActionRow NON editabile: mostra solo. La sua
        // sorgente e' get_state(), riletto dopo ogni scrittura, mai lo stato
        // dei widget (vedi dispatchStatusFromState).
        // The banner is a NON-editable ActionRow: it only shows. Its source is
        // get_state(), re-read after every write, never the widgets' state (see
        // dispatchStatusFromState).
        const dispatchStatusRow = new Adw.ActionRow({
            title: _('Current dispatch'),
        });

        // Registra le righe per-livello che dipendono dal toggle globale, senza
        // ricostruirle: la GUI resta quella di prima, cambia solo chi governa.
        // Vive sull'istanza perche' le righe sono create da _buildLevelExpander.
        // Registers the per-level rows that depend on the global toggle, without
        // rebuilding them: the GUI stays the one from before, only what governs
        // changes. It lives on the instance because the rows are created by
        // _buildLevelExpander.
        this._parallelModeSink = [];
        const refreshDispatchStatus = () => {
            const current = getServicesState();
            const fresh = current?.stream ?? streamState;
            const { subtitle, note } = dispatchStatusFromState(fresh);
            dispatchStatusRow.subtitle = note ? `${subtitle} ${note}` : subtitle;
            // in "sequential" nessuno slot e' applicato: legare la sensibilita'
            // anche al toggle GLOBALE, non solo a parallelRow.active.
            // in "sequential" no slot is applied: bind the sensitivity also to the
            // GLOBAL toggle, not only to parallelRow.active.
            this._parallelModeSink.forEach(entry => entry(fresh));
        };
        // Giro 2 (B4-frontend-B): il banner e' dichiarato "solo. mostra" e la
        // sua fonte e' get_state() riletto, ma le due caselle che il banner
        // DESCRIVE (parallel/max_concurrency per livello) scrivevano il file e
        // non ricalcolavano il banner: dopo aver acceso 'parallel' su un
        // livello la riga restava "Sequential (no level marked parallel)"
        // fino a riaprire la finestra. Esposta sull'istanza perche' i due
        // handler vivono in _buildLevelExpander, fuori da questo scope.
        // Round 2 (B4-frontend-B): the banner is declared "display only" and its
        // source is the re-read get_state(), but the two boxes the banner DESCRIBES
        // (parallel/max_concurrency per level) wrote the file and did not recompute
        // the banner: after turning on 'parallel' on a level the row stayed
        // "Sequential (no level marked parallel)" until the window was reopened.
        // Exposed on the instance because the two handlers live in
        // _buildLevelExpander, outside this scope.
        this._refreshDispatchStatus = refreshDispatchStatus;
        dispatchModeRow.connect('notify::selected', () => {
            const newMode = dispatchModeRow.selected === 1 ? 'sequential' : 'auto';
            runConfigEditor(['set-stream', 'dispatch_mode', newMode]);
            refreshDispatchStatus();
        });
        modeGroup.add(dispatchModeRow);
        modeGroup.add(dispatchStatusRow);
        // Subito: almeno un caller deve popolare il banner, altrimenti la riga
        // resta vuota finche' l'utente non tocca il toggle.
        // Right away: at least one caller must populate the banner, otherwise the
        // row stays empty until the user touches the toggle.
        refreshDispatchStatus();

        const pasteChannelRow = new Adw.ComboRow({
            title: _('Paste channel'),
            subtitle: _('Choose clipboard insertion or simulated keystrokes.'),
            model: new Gtk.StringList({ strings: [_('Clipboard'), _('Keystrokes')] }),
            selected: streamState.paste_channel === 'type' ? 1 : 0,
        });
        const pasteShortcutRow = new Adw.ComboRow({
            title: _('Paste shortcut'),
            subtitle: _('Use Ctrl+Shift+V for terminals/TUIs such as opencode.'),
            model: new Gtk.StringList({ strings: ['Ctrl+V', 'Ctrl+Shift+V'] }),
            selected: streamState.paste_shortcut === 'ctrl+shift+v' ? 1 : 0,
        });
        pasteShortcutRow.connect('notify::selected', () => {
            runConfigEditor(['set-stream', 'paste_shortcut', pasteShortcutRow.selected === 1 ? 'ctrl+shift+v' : 'ctrl+v']);
        });
        const updatePasteShortcutSensitivity = () => {
            pasteShortcutRow.set_sensitive(pasteChannelRow.selected === 0);
        };
        pasteChannelRow.connect('notify::selected', () => {
            runConfigEditor(['set-stream', 'paste_channel', pasteChannelRow.selected === 1 ? 'type' : 'clipboard']);
            updatePasteShortcutSensitivity();
        });
        updatePasteShortcutSensitivity();
        modeGroup.add(pasteChannelRow);
        modeGroup.add(pasteShortcutRow);

        const pacingRow = new Adw.SpinRow({
            title: _('Paste delay (ms)'),
            subtitle: _('Pause before pasting to ensure clipboard is ready (default 250 ms)'),
            adjustment: new Gtk.Adjustment({
                lower: 50, upper: 2000, step_increment: 50,
                value: streamState.paste_delay_ms ?? 250,
            }),
        });
        pacingRow.connect('notify::value', debounce(() => {
            runConfigEditor(['set-stream', 'paste_delay_ms', String(Math.round(pacingRow.value))]);
        }));
        modeGroup.add(pacingRow);

        // Giro 2 (B5-frontend-B): max_concurrent_chunks compariva SOLO dentro
        // dispatchStatusFromState (lettura per il banner) e nessun widget la
        // governava, mentre il backend la sa gia' scrivere (STREAM_FIELDS in
        // config_editor.py). Con dispatch_mode=auto e piu' endpoint nel pool,
        // il tetto di worker era quindi invisibile e non modificabile dalla
        // GUI. Stessa forma degli altri SpinRow del gruppo, stesso backend.
        // Round 2 (B5-frontend-B): max_concurrent_chunks appeared ONLY inside
        // dispatchStatusFromState (read for the banner) and no widget governed it,
        // while the backend already knows how to write it (STREAM_FIELDS in
        // config_editor.py). With dispatch_mode=auto and more endpoints in the pool,
        // the worker cap was therefore invisible and not editable from the GUI. Same
        // shape as the other SpinRows of the group, same backend.
        const maxWorkersRow = new Adw.SpinRow({
            title: _('Max parallel workers'),
            subtitle: _('Upper bound of chunks pasted at the same time (auto mode; sequential always uses one)'),
            adjustment: new Gtk.Adjustment({
                lower: 1, upper: 32, step_increment: 1,
                value: Math.max(1, Number(streamState.max_concurrent_chunks) || 1),
            }),
        });
        maxWorkersRow.connect('notify::value', debounce(() => {
            runConfigEditor(['set-stream', 'max_concurrent_chunks', String(Math.round(maxWorkersRow.value))]);
            // Il banner legge questo numero: ricalcolarlo qui evita che dica
            // un tetto diverso da quello appena salvato.
            // The banner reads this number: recomputing it here avoids it saying a cap
            // different from the one just saved.
            this._refreshDispatchStatus?.();
        }));
        modeGroup.add(maxWorkersRow);

        const chunkGroup = new Adw.PreferencesGroup({
            title: _('Chunk segmentation (per-chunk mode only)'),
            description: _('Fine-tune how pauses in speech are detected to split chunks.'),
        });
        page.add(chunkGroup);

        const silenceRow = new Adw.SpinRow({
            title: _('Silence duration (seconds)'),
            subtitle: _('Length of silence that triggers transcription of the current chunk'),
            adjustment: new Gtk.Adjustment({
                lower: 0.1, upper: 5.0, step_increment: 0.1,
                value: streamState.silence_seconds ?? 0.7,
            }),
            digits: 1,
        });
        silenceRow.connect('notify::value', debounce(() => {
            runConfigEditor(['set-stream', 'silence_seconds', silenceRow.value.toFixed(1)]);
        }));
        chunkGroup.add(silenceRow);

        const noiseRow = new Adw.SpinRow({
            title: _('Silence noise threshold (dB)'),
            subtitle: _('Audio level below which sound is considered silence (e.g. -30 dB)'),
            adjustment: new Gtk.Adjustment({
                lower: -60, upper: 0, step_increment: 5,
                value: streamState.noise_db ?? -30,
            }),
        });
        noiseRow.connect('notify::value', debounce(() => {
            runConfigEditor(['set-stream', 'noise_db', String(Math.round(noiseRow.value))]);
        }));
        chunkGroup.add(noiseRow);

        const marginRow = new Adw.SpinRow({
            title: _('Adaptive VAD margin (dB)'),
            subtitle: _(
                'How far above the measured adaptive noise floor speech must be to count as voice. Distinct from the initial silence threshold above; the VAD re-estimates the floor while listening.'),
            adjustment: new Gtk.Adjustment({
                lower: 0, upper: 20, step_increment: 0.5,
                value: streamState.vad_margin_db ?? 6.0,
            }),
            digits: 1,
        });
        marginRow.connect('notify::value', debounce(() => {
            runConfigEditor(['set-stream', 'vad_margin_db', marginRow.value.toFixed(1)]);
        }));
        chunkGroup.add(marginRow);

        const minUttRow = new Adw.SpinRow({
            title: _('Min utterance (seconds)'),
            subtitle: _('Shorter audio fragments are discarded as noise'),
            adjustment: new Gtk.Adjustment({
                lower: 0.1, upper: 2.0, step_increment: 0.1,
                value: streamState.min_utterance_seconds ?? 0.4,
            }),
            digits: 1,
        });
        minUttRow.connect('notify::value', debounce(() => {
            runConfigEditor(['set-stream', 'min_utterance_seconds', minUttRow.value.toFixed(1)]);
        }));
        chunkGroup.add(minUttRow);

        const maxUttRow = new Adw.SpinRow({
            title: _('Max utterance (seconds)'),
            subtitle: _('Forces chunk transcription if you speak continuously without pauses'),
            adjustment: new Gtk.Adjustment({
                lower: 5, upper: 120, step_increment: 5,
                value: streamState.max_utterance_seconds ?? 30,
            }),
        });
        maxUttRow.connect('notify::value', debounce(() => {
            runConfigEditor(['set-stream', 'max_utterance_seconds', String(Math.round(maxUttRow.value))]);
        }));
        chunkGroup.add(maxUttRow);

        // Lingua, Initial prompt e Hotwords per stream
        // Language, Initial prompt and Hotwords for stream
        const langRow = new Adw.EntryRow({
            title: _('Language'),
            tooltip_text: _('ISO 639-1 language code for transcription (e.g. "it" for Italian)'),
            text: streamState.language ?? 'it',
            show_apply_button: true,
        });
        langRow.connect('apply', () => {
            runConfigEditor(['set-stream', 'language', langRow.text]);
        });
        modeGroup.add(langRow);

        const promptRow = new Adw.EntryRow({
            title: _('Initial prompt'),
            tooltip_text: _('Optional prompt to guide the transcription (context for Whisper)'),
            text: streamState.prompt ?? '',
            show_apply_button: true,
        });
        promptRow.connect('apply', () => {
            runConfigEditor(['set-stream', 'prompt', promptRow.text]);
        });
        modeGroup.add(promptRow);

        const hotwordsRow = new Adw.EntryRow({
            title: _('Hotwords'),
            tooltip_text: _('Bias transcription toward these words (space-separated)'),
            text: streamState.hotwords ?? '',
            show_apply_button: true,
        });
        hotwordsRow.connect('apply', () => {
            runConfigEditor(['set-stream', 'hotwords', hotwordsRow.text]);
        });
        modeGroup.add(hotwordsRow);

        const contextRow = new Adw.SwitchRow({
            title: _('Context'),
            subtitle: _('Append the last 3 transcribed chunks to the prompt for better context'),
            active: streamState.context_enabled ?? true,
        });
        contextRow.connect('notify::active', () => {
            runConfigEditor(['set-stream', 'context_enabled', String(contextRow.active)]);
        });
        modeGroup.add(contextRow);

        this._buildVoiceCommandGroup(window, page, modeGroup, streamState);

        // Sezione fallback per stream. get_state() espone gli endpoint con
        // la stessa chiave `levels` usata dagli altri servizi.
        // Fallback section for stream. get_state() exposes the endpoints with the
        // same `levels` key used by the other services.
        if (Array.isArray(streamState.levels)) {
            const fallbackGroup = new Adw.PreferencesGroup({
                title: _('Streaming STT endpoint'),
                description: _('Dedicated endpoint for stream transcription (independent of standard STT).'),
            });
            page.add(fallbackGroup);

            streamState.levels.forEach((level, idx) => {
                fallbackGroup.add(this._buildLevelExpander('stream', idx, level, streamState));
            });
        }
    }

    // F2-resto: estratto da _buildStreamPage (era ~570 righe, questo blocco ne
    // valeva quasi metà). GUI e comportamento identici: stessi widget, stesso
    // ordine di aggiunta ai gruppi, nessuna logica toccata — solo spostati i
    // confini del metodo. blacklistRow resta qui (non in _buildStreamPage)
    // anche se va in `modeGroup`: la sua validazione (blacklistConflicts)
    // dipende da commandRows, che vive in questa chiusura.
    // F2-rest: extracted from _buildStreamPage (it was ~570 lines, this block
    // was almost half of it). GUI and behavior identical: same widgets, same
    // order of addition to the groups, no logic touched — only the boundaries of
    // the method were moved. blacklistRow stays here (not in _buildStreamPage)
    // even though it goes into `modeGroup`: its validation (blacklistConflicts)
    // depends on commandRows, which lives in this closure.
    _buildVoiceCommandGroup(window, page, modeGroup, streamState) {
        // Regole vocali: ogni salvataggio sostituisce atomicamente l'intera lista.
        // Voice rules: every save atomically replaces the whole list.
        const commandGroup = new Adw.PreferencesGroup({
            title: _('Voice commands'),
            description: _('Exact spoken keywords can press a key or delete the last word/chunk.'),
        });
        page.add(commandGroup);
        let commandRows = [];
        // BUG-6 (ciclo 3): notifyCommandError/notifyBlacklistError duplicavano
        // lo stesso guard typeof window.add_toast + fallback logError. Un
        // solo punto qui. NON estendere questa unificazione a notifyCapture
        // (dentro addCommandRow, piu' sotto): quella funzione vive nel blocco
        // che scripts/test-smoke-gjs-prefs.js estrae per brace-matching e
        // valuta in un eval isolato (quel file, righe 130-154) - un
        // riferimento a un helper esterno ad addCommandRow diventerebbe un
        // ReferenceError SOLO dentro quell'eval, non nell'estensione reale.
        // BUG-6 (cycle 3): notifyCommandError/notifyBlacklistError duplicated the
        // same typeof window.add_toast guard + logError fallback. One single point
        // here. Do NOT extend this unification to notifyCapture (inside
        // addCommandRow, further below): that function lives in the block that
        // scripts/test-smoke-gjs-prefs.js extracts by brace-matching and evaluates
        // in an isolated eval (that file, lines 130-154) - a reference to a helper
        // external
        //  to addCommandRow would become a ReferenceError ONLY inside that eval, not
        // in the real extension.
        const notifyToast = (message, context) => {
            if (typeof window.add_toast === 'function') {
                window.add_toast(new Adw.Toast({ title: message }));
            } else {
                logError(new Error(message), context);
            }
        };
        const notifyCommandError = () => {
            const message = `${_('Command validation failed')}: ${_('Check that keywords are unique and command settings are valid.')}`;
            notifyToast(message, 'voice commands');
        };
        // Giro 2 (B3-frontend-B): la blacklist non e' un comando, quindi il
        // messaggio "Command validation failed / check keywords" era
        // semplicemente sbagliato. Toast dedicato.
        // Round 2 (B3-frontend-B): the blacklist is not a command, so the message
        // "Command validation failed / check keywords" was simply wrong. Dedicated
        // toast.
        const notifyBlacklistError = conflict => {
            const detail = _('The phrase "%s" is already a command keyword or alias. Remove it from the blacklist, or rename the command.').replace('%s', conflict);
            const message = `${_('Blacklist conflicts with a voice command')}: ${detail}`;
            notifyToast(message, 'voice blacklist');
        };
        // Stessa regola di config.py load_config (riga 649-657): una frase
        // della blacklist che coincide, normalizzata, con una keyword o un
        // alias di un comando rende la config INVALIDA al reload, e
        // config_editor.set_stream_field la accetta senza dire nulla (nessuna
        // validazione li') — quindi il backend si spegneva al riavvio, senza
        // avviso. Qui il conflitto viene detto PRIMA di scrivere.
        // Same rule as config.py load_config (lines 649-657): a blacklist phrase
        // that coincides, normalized, with a keyword or an alias of a command makes
        // the config INVALID at reload, and config_editor.set_stream_field accepts
        // it without saying anything (no validation there) — so the backend shut
        // down at restart, with no warning. Here the conflict is reported BEFORE
        // writing.
        const blacklistConflicts = blacklistText => {
            const phrases = parseBlacklist(blacklistText);
            if (phrases.size === 0)
                return null;
            const spoken = new Set();
            for (const row of commandRows) {
                const keyword = row.keyword.text.trim();
                if (keyword)
                    spoken.add(normalizeCommandKeyword(keyword));
                for (const alias of row.aliases.text.split(',')) {
                    const normalized = normalizeCommandKeyword(alias);
                    if (normalized)
                        spoken.add(normalized);
                }
            }
            for (const phrase of phrases) {
                if (spoken.has(phrase))
                    return phrase;
            }
            return null;
        };
        const saveCommands = () => {
            const commands = serializeCommandRows(commandRows);
            const { success } = runConfigEditor(['set-stream-commands', JSON.stringify(commands)]);
            if (!success)
                notifyCommandError();
        };
        const debouncedSaveCommands = debounce(saveCommands);
        const addCommandRow = command => {
            const isDelete = command?.action === 'delete';
            const expander = new Adw.ExpanderRow({
                title: command?.keyword ? command.keyword : _('Command keyword'),
                expanded: !command,
            });

            const remove = new Gtk.Button({
                icon_name: 'user-trash-symbolic',
                valign: Gtk.Align.CENTER,
                css_classes: ['flat'],
                tooltip_text: _('Remove'),
            });
            expander.add_suffix(remove);

            const keyword = new Adw.EntryRow({
                title: _('Keyword'),
                text: command?.keyword ?? '',
            });
            const aliases = new Adw.EntryRow({
                title: _('Aliases / Alternative phrases'),
                text: Array.isArray(command?.aliases) ? command.aliases.join(', ') : '',
                tooltip_text: _('Comma-separated equivalent phrases that trigger this command.'),
                show_apply_button: true,
            });
            const action = new Adw.ComboRow({
                title: _('Action'),
                model: new Gtk.StringList({
                    strings: [_('Press key'), _('Delete text')],
                }),
                selected: isDelete ? 1 : 0,
            });
            const key = new Adw.EntryRow({
                title: _('Key (e.g. Return)'),
                text: command?.key ?? 'Return',
                visible: !isDelete,
            });

            // --- Cattura tasto (premi il tasto invece di digitarlo) ---
            // Il valore salvato resta esattamente uno di COMMAND_KEYS; la
            // digitazione manuale dell'EntryRow resta come fallback. La cattura
            // è un'azione esplicita (bottone suffisso) per non intercettare la
            // digitazione normale né la navigazione da tastiera.
            // --- Key capture (press the key instead of typing it) ---
            // The saved value stays exactly one of COMMAND_KEYS; manual typing in the
            // EntryRow stays as a fallback. The capture is an explicit action (suffix
            // button) so as not to intercept normal typing or keyboard navigation.
            const captureBtn = new Gtk.Button({
                icon_name: 'input-keyboard-symbolic',
                valign: Gtk.Align.CENTER,
                css_classes: ['flat'],
                tooltip_text: _('Record key'),
            });
            const cancelBtn = new Gtk.Button({
                icon_name: 'process-stop-symbolic',
                valign: Gtk.Align.CENTER,
                css_classes: ['flat'],
                tooltip_text: _('Cancel'),
                visible: false,
            });
            key.add_suffix(captureBtn);
            key.add_suffix(cancelBtn);

            let capturing = false;
            let suppressKeyChanged = false;

            const notifyCapture = message => {
                if (typeof window.add_toast === 'function')
                    window.add_toast(new Adw.Toast({ title: message }));
                else
                    logError(new Error(message), 'voice commands');
            };

            const stopCapture = () => {
                if (!capturing)
                    return;
                capturing = false;
                captureBtn.icon_name = 'input-keyboard-symbolic';
                captureBtn.tooltip_text = _('Record key');
                cancelBtn.visible = false;
            };

            // Un solo percorso di salvataggio: aggiorna il testo sopprimendo il
            // 'changed' (che altrimenti debouncerebbe un secondo salvataggio) e
            // invoca saveCommands() esattamente una volta, con un valore già
            // validato contro la whitelist.
            // A single save path: it updates the text suppressing 'changed' (which would
            // otherwise debounce a second save) and invokes saveCommands() exactly once,
            // with a value already validated against the whitelist.
            const commitCapturedKey = value => {
                stopCapture();
                suppressKeyChanged = true;
                key.text = value;
                suppressKeyChanged = false;
                debouncedSaveCommands.cancel();
                saveCommands();
            };

            const startCapture = () => {
                if (capturing)
                    return;
                capturing = true;
                captureBtn.icon_name = 'media-record-symbolic';
                captureBtn.tooltip_text = _('Press a key…');
                cancelBtn.visible = true;
                key.grab_focus();
            };

            const controller = new Gtk.EventControllerKey();
            controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE);
            controller.connect('key-pressed', (_controller, keyval, _keycode, state) => {
                if (!capturing)
                    return false;
                // Modificatori "puri" (Shift/Ctrl/Alt/Super/Meta/Hyper e lock):
                // ignora e resta in cattura (come gdk_keyval_is_modifier: range
                // [Shift_L, Hyper_R] più i tasti ISO_Level3/5_Shift).
                // "Pure" modifiers (Shift/Ctrl/Alt/Super/Meta/Hyper and locks): ignore and
                // stay in capture (like gdk_keyval_is_modifier: range [Shift_L, Hyper_R]
                // plus the ISO_Level3/5_Shift keys).
                if ((keyval >= Gdk.KEY_Shift_L && keyval <= Gdk.KEY_Hyper_R)
                    || keyval === Gdk.KEY_ISO_Level3_Shift
                    || keyval === Gdk.KEY_ISO_Level5_Shift)
                    return true;
                // Accordi con modificatori reali: rifiuta con feedback, nessuno
                // strip. CapsLock/NumLock sono mascherati da
                // accelerator_get_default_mod_mask(), quindi non contano.
                // Chords with real modifiers: reject with feedback, no strip.
                // CapsLock/NumLock are masked by accelerator_get_default_mod_mask(), so they
                // do not count.
                if ((state & Gtk.accelerator_get_default_mod_mask()) !== 0) {
                    notifyCapture(_('Modifier combinations are not supported; press a single key'));
                    return true;
                }
                const name = Gdk.keyval_name(keyval);
                const resolved = name && (COMMAND_KEY_SET.has(name) ? name : CAPTURE_KEY_ALIASES[name]);
                if (!resolved) {
                    notifyCapture(_('Key not supported for voice commands'));
                    return true;
                }
                commitCapturedKey(resolved);
                return true;
            });
            key.add_controller(controller);

            captureBtn.connect('clicked', startCapture);
            cancelBtn.connect('clicked', stopCapture);

            const scope = new Adw.ComboRow({
                title: _('word or chunk'),
                model: new Gtk.StringList({ strings: [_('Single word'), _('Whole chunk')] }),
                selected: command?.scope === 'chunk' ? 1 : 0,
                visible: isDelete,
            });
            const ends = new Adw.SwitchRow({
                title: _('End session'),
                active: command?.ends_session ?? false,
            });

            expander.add_row(keyword);
            expander.add_row(aliases);
            expander.add_row(action);
            expander.add_row(key);
            expander.add_row(scope);
            expander.add_row(ends);

            const item = { expander, keyword, aliases, action, key, scope, ends };
            commandRows.push(item);
            commandGroup.add(expander);

            const updateVisibility = () => {
                const deleting = action.selected === 1;
                if (deleting)
                    stopCapture();
                key.visible = !deleting;
                scope.visible = deleting;
            };

            const onKeywordChanged = () => {
                const text = keyword.text.trim();
                expander.title = text || _('Command keyword');
                debouncedSaveCommands();
            };

            keyword.connect('changed', onKeywordChanged);
            keyword.connect('apply', saveCommands);
            aliases.connect('changed', debouncedSaveCommands);
            aliases.connect('apply', saveCommands);
            action.connect('notify::selected', () => {
                updateVisibility();
                saveCommands();
            });
            key.connect('changed', () => {
                if (!suppressKeyChanged)
                    debouncedSaveCommands();
            });
            key.connect('apply', saveCommands);
            scope.connect('notify::selected', saveCommands);
            ends.connect('notify::active', saveCommands);
            remove.connect('clicked', () => {
                commandGroup.remove(expander);
                commandRows = commandRows.filter(x => x !== item);
                saveCommands();
            });
        };
        for (const command of streamState.commands ?? []) addCommandRow(command);
        const blacklistRow = new Adw.EntryRow({
            title: _('Chunk blacklist'),
            text: streamState.blacklist ?? '',
            tooltip_text: _('Comma-separated phrases; a transcription that matches a whole phrase is discarded (streaming chunks and dictation), ignoring case and trailing punctuation.'),
            show_apply_button: true,
        });
        blacklistRow.connect('apply', () => {
            // Validazione PRIMA di scrivere: il valore non viene salvato, cosi'
            // la config resta valida per il backend al reload.
            // Validation BEFORE writing: the value is not saved, so the config stays
            // valid for the backend at reload.
            const conflict = blacklistConflicts(blacklistRow.text);
            if (conflict) {
                notifyBlacklistError(conflict);
                return;
            }
            const { success } = runConfigEditor(['set-stream', 'blacklist', blacklistRow.text]);
            if (!success)
                notifyCommandError();
        });
        modeGroup.add(blacklistRow);

        const addCommand = new Gtk.Button({ label: _('Add command'), halign: Gtk.Align.START });
        addCommand.connect('clicked', () => addCommandRow(null));
        commandGroup.add(addCommand);
    }

    _buildGeneralPage(window) {
        const page = new Adw.PreferencesPage({
            title: _('General'),
            icon_name: 'preferences-other-symbolic',
        });
        window.add(page);

        const state = getServicesState();
        if (!state?.general) {
            this._showConfigError(page);
            return;
        }
        const general = state.general;

        // SpinRow che salva con debounce via set-general (o set-stream).
        // SpinRow that saves with debounce via set-general (or set-stream).
        const spin = (group, { title, subtitle, lower, upper, step, digits = 0, value, save }) => {
            const row = new Adw.SpinRow({
                title: _(title),
                subtitle: _(subtitle),
                adjustment: new Gtk.Adjustment({ lower, upper, step_increment: step, value }),
                digits,
            });
            row.connect('notify::value', debounce(() => save(digits > 0 ? row.value.toFixed(digits) : String(Math.round(row.value)))));
            group.add(row);
            return row;
        };
        const toggle = (group, { title, subtitle, active, save }) => {
            const row = new Adw.SwitchRow({ title: _(title), subtitle: _(subtitle), active });
            row.connect('notify::active', () => save(row.active ? 'true' : 'false'));
            group.add(row);
            return row;
        };
        // EntryRow con "apply": se il backend rifiuta il valore torna all'ultimo buono.
        // EntryRow with "apply": if the backend rejects the value it goes back to
        // the last good one.
        const entry = (group, { title, subtitle, text, save }) => {
            let lastGood = text;
            const row = new Adw.EntryRow({
                title: _(title), tooltip_text: _(subtitle), text, show_apply_button: true,
            });
            row.connect('apply', () => {
                if (save(row.text.trim()))
                    lastGood = row.text.trim();
                else
                    row.text = lastGood;
            });
            group.add(row);
            return row;
        };

        const audioGroup = new Adw.PreferencesGroup({ title: _('Audio recording') });
        page.add(audioGroup);
        spin(audioGroup, {
            title: N_('Toggle debounce (seconds)'),
            subtitle: N_('Ignore a second press within this time after a recording starts'),
            lower: 0.1, upper: 10, step: 0.1, digits: 1, value: general.toggle_debounce_seconds ?? 1,
            save: v => setGeneralField('audio', 'toggle_debounce_seconds', v),
        });
        toggle(audioGroup, {
            title: N_('Retry on error'),
            subtitle: N_('Run the whole transcription chain again when every endpoint fails'),
            active: general.retry_on_error ?? true,
            save: v => setGeneralField('audio', 'retry_on_error', v),
        });
        spin(audioGroup, {
            title: N_('Attempts'),
            subtitle: N_('Total attempts of the transcription chain when retry is on'),
            lower: 1, upper: 10, step: 1, value: general.retry_count ?? 2,
            save: v => setGeneralField('audio', 'retry_count', v),
        });
        spin(audioGroup, {
            title: N_('Bitrate (kbps)'),
            subtitle: N_('Audio bitrate of the recording sent for transcription'),
            lower: 8, upper: 320, step: 8, value: general.bitrate_kbps ?? 16,
            save: v => setGeneralField('audio', 'bitrate_kbps', v),
        });
        const rateRow = new Adw.ComboRow({
            title: _('Sample rate'),
            subtitle: _('Only rates supported by the Opus encoder are offered'),
            model: new Gtk.StringList({ strings: SAMPLE_RATES.map(r => `${r} Hz`) }),
            selected: Math.max(0, SAMPLE_RATES.indexOf(general.sample_rate)),
        });
        rateRow.connect('notify::selected', () => {
            if (!setGeneralField('audio', 'sample_rate', SAMPLE_RATES[rateRow.selected]))
                rateRow.selected = Math.max(0, SAMPLE_RATES.indexOf(getServicesState()?.general?.sample_rate));
        });
        audioGroup.add(rateRow);

        // Formato di registrazione: format e codec cambiano insieme (preset).
        // Se la config ha una coppia non standard si aggiunge una voce
        // "personalizzato" selezionata, che non scrive nulla finche' non si
        // sceglie un preset.
        // Recording format: format and codec change together (preset). If the
        // config holds a non-standard pair a selected "custom" entry is added,
        // which writes nothing until a preset is chosen.
        const formatLabels = AUDIO_FORMAT_PRESETS.map((p, i) => (
            i === 0 ? _('%s (recommended)').replace('%s', p.label) : p.label));
        const currentPreset = AUDIO_FORMAT_PRESETS.findIndex(p => p.preset === general.audio_format);
        if (currentPreset === -1)
            formatLabels.push(_('Custom (set in config.toml)'));
        const formatRow = new Adw.ComboRow({
            title: _('Recording format'),
            subtitle: _('The ffmpeg encoder for the chosen format must be installed; it applies from the next recording'),
            model: new Gtk.StringList({ strings: formatLabels }),
            selected: currentPreset === -1 ? formatLabels.length - 1 : currentPreset,
        });
        formatRow.connect('notify::selected', () => {
            const picked = AUDIO_FORMAT_PRESETS[formatRow.selected];
            if (!picked)
                return;   // voce "personalizzato": nessuna scrittura / custom entry: no write
            if (!runConfigEditor(['set-audio-format', picked.preset]).success) {
                const back = AUDIO_FORMAT_PRESETS.findIndex(
                    p => p.preset === getServicesState()?.general?.audio_format);
                formatRow.selected = back === -1 ? formatLabels.length - 1 : back;
            }
        });
        audioGroup.add(formatRow);

        const clipGroup = new Adw.PreferencesGroup({ title: _('Clipboard') });
        page.add(clipGroup);
        toggle(clipGroup, {
            title: N_('Write raw text first'),
            subtitle: N_('Copy the raw text right away, then replace it with the cleaned text when ready. Off: only the final text is copied'),
            active: general.double_injection ?? true,
            save: v => setGeneralField('clipboard', 'double_injection', v),
        });
        entry(clipGroup, {
            title: N_('Copy command'),
            subtitle: N_('Must behave like wl-copy: reads the text on stdin and puts it in the clipboard (default: wl-copy)'),
            text: general.clipboard_tool ?? 'wl-copy',
            save: v => setGeneralField('general', 'clipboard_tool', v),
        });
        entry(clipGroup, {
            title: N_('Paste command'),
            subtitle: N_('Must behave like wl-paste: called with -t image/png to read a screenshot for OCR (default: wl-paste)'),
            text: general.clipboard_paste_tool ?? 'wl-paste',
            save: v => setGeneralField('general', 'clipboard_paste_tool', v),
        });

        // Aggiunto al gruppo Clipboard dopo le due voci comando.
        // Added to the Clipboard group after the two command rows.
        spin(clipGroup, {
            title: N_('Clipboard command timeout (seconds)'),
            subtitle: N_('Give up on the copy/paste command after this long'),
            lower: 1, upper: 60, step: 1, value: general.clipboard_timeout_seconds ?? 5,
            save: v => setGeneralField('general', 'clipboard_timeout_seconds', v),
        });

        const notifyGroup = new Adw.PreferencesGroup({ title: _('Notification delivery') });
        page.add(notifyGroup);
        spin(notifyGroup, {
            title: N_('Notification timeout (seconds)'),
            subtitle: N_('Give up on notify-send after this long'),
            lower: 1, upper: 60, step: 1, value: general.notify_timeout_seconds ?? 10,
            save: v => setGeneralField('general', 'notify_timeout_seconds', v),
        });
        spin(notifyGroup, {
            title: N_('Text shown in notifications (characters)'),
            subtitle: N_('Longest piece of transcribed text put in a notification body'),
            lower: 10, upper: 500, step: 10, value: general.notification_content_max_chars ?? 80,
            save: v => setGeneralField('general', 'notification_content_max_chars', v),
        });

        const cleanupGroup = new Adw.PreferencesGroup({ title: _('Text cleanup and OCR') });
        page.add(cleanupGroup);
        spin(cleanupGroup, {
            title: N_('Minimum cleaned length (fraction)'),
            subtitle: N_('An LLM cleanup shorter than this fraction of the raw text is discarded and retried; 0 accepts any length'),
            lower: 0, upper: 1, step: 0.05, digits: 2, value: general.cleanup_min_length_ratio ?? 0.7,
            save: v => setGeneralField('general', 'cleanup_min_length_ratio', v),
        });
        spin(cleanupGroup, {
            title: N_('Area selection timeout (seconds)'),
            subtitle: N_('How long OCR waits for you to select a screen area before giving up'),
            lower: 5, upper: 600, step: 5, value: general.screenshot_timeout_seconds ?? 120,
            save: v => setGeneralField('ocr', 'screenshot_timeout_seconds', v),
        });

        // Impostazioni dell'indicatore (GSettings, non config.toml): valgono
        // per l'estensione stessa e si applicano senza riavvio.
        // Indicator settings (GSettings, not config.toml): they belong to the
        // extension itself and apply without a restart.
        const extSettings = this.getSettings();
        // Bottoni rapidi: interruttori sulla stessa GSettings, visibili subito
        // nella top bar (nessun riavvio). Schema stantio: nessun bind.
        // Quick buttons: switches on the same GSettings, visible in the top bar
        // right away (no restart). Stale schema: no bind.
        const quickGroup = new Adw.PreferencesGroup({
            title: _('Quick buttons'),
            description: _('Buttons in the top bar, separate from the menu: one click or tap starts or stops the action. All off by default.'),
        });
        page.add(quickGroup);
        for (const item of QUICK_BUTTON_SETTINGS) {
            if (!extSettings.settings_schema.has_key(item.key))
                continue;
            const quickRow = new Adw.SwitchRow({ title: _(item.title), subtitle: _(item.subtitle) });
            extSettings.bind(item.key, quickRow, 'active', Gio.SettingsBindFlags.DEFAULT);
            quickGroup.add(quickRow);
        }

        const indicatorGroup = new Adw.PreferencesGroup({ title: _('Indicator') });
        page.add(indicatorGroup);
        for (const item of INDICATOR_SETTINGS) {
            // Schema stantio: niente bind, con una chiave assente e' fatale.
            // Stale schema: no bind, a missing key would be fatal.
            if (!extSettings.settings_schema.has_key(item.key))
                continue;
            const row = new Adw.SpinRow({
                title: _(item.title),
                subtitle: _(item.subtitle),
                adjustment: new Gtk.Adjustment({
                    lower: item.lower, upper: item.upper, step_increment: item.step,
                    value: item.lower,
                }),
            });
            extSettings.bind(item.key, row, 'value', Gio.SettingsBindFlags.DEFAULT);
            indicatorGroup.add(row);
        }

        const reliabilityGroup = new Adw.PreferencesGroup({ title: _('Streaming reliability') });
        page.add(reliabilityGroup);
        spin(reliabilityGroup, {
            title: N_('Endpoint cooldown (seconds)'),
            subtitle: N_('How long an endpoint stays out of the pool after repeated failures; 0 never pauses one. Applies from the next session'),
            lower: 0, upper: 86400, step: 60, value: state.stream?.endpoint_cooldown_seconds ?? 3600,
            save: v => runConfigEditor(['set-stream', 'endpoint_cooldown_seconds', v]).success,
        });

        const diagGroup = new Adw.PreferencesGroup({ title: _('Diagnostics') });
        page.add(diagGroup);
        spin(diagGroup, {
            title: N_('Whisper prompt limit (characters)'),
            subtitle: N_('Longest prompt sent to the transcription model (vocabulary and context are trimmed to fit)'),
            lower: 100, upper: 4000, step: 50, value: state.stream?.prompt_max_chars ?? 800,
            save: v => runConfigEditor(['set-stream', 'prompt_max_chars', v]).success,
        });
        spin(diagGroup, {
            title: N_('Noise floor window (frames)'),
            subtitle: N_('Recent ~30 ms frames used to estimate the background noise; applies from the next session'),
            lower: 20, upper: 1000, step: 10, value: state.stream?.vad_floor_window_frames ?? 100,
            save: v => runConfigEditor(['set-stream', 'vad_floor_window_frames', v]).success,
        });
        spin(diagGroup, {
            title: N_('Noise floor minimum (frames)'),
            subtitle: N_('Frames needed before the noise estimate is trusted; applies from the next session'),
            lower: 5, upper: 200, step: 5, value: state.stream?.vad_min_floor_frames ?? 20,
            save: v => runConfigEditor(['set-stream', 'vad_min_floor_frames', v]).success,
        });
        spin(diagGroup, {
            title: N_('Chunk log size (lines)'),
            subtitle: N_('Lines kept in the streaming chunk log; 0 uses the default (2000)'),
            lower: 0, upper: 1000000, step: 100, value: state.stream?.chunk_log_max_lines ?? 0,
            save: v => runConfigEditor(['set-stream', 'chunk_log_max_lines', v]).success,
        });
    }

    _buildStoragePage(window) {
        const page = new Adw.PreferencesPage({
            title: _('Storage'),
            icon_name: 'folder-symbolic',
        });
        window.add(page);

        const state = getServicesState();
        if (!state) {
            this._showConfigError(page);
            return;
        }

        const folderGroup = new Adw.PreferencesGroup({ title: _('History folder') });
        page.add(folderGroup);

        const folderRow = new Adw.ActionRow({
            title: _('Save location'),
            subtitle: state.storage?.base_dir ?? '',
        });
        const chooseButton = new Gtk.Button({ label: _('Choose…'), valign: Gtk.Align.CENTER });
        chooseButton.connect('clicked', () => {
            const dialog = new Gtk.FileDialog({ title: _('Choose history folder') });
            dialog.select_folder(window, null, (dlg, result) => {
                try {
                    const folder = dlg.select_folder_finish(result);
                    const path = folder.get_path();
                    // Giro 13: get_path() torna null per location senza
                    // rappresentazione locale (mount sftp/mtp/cloud via
                    // portale) — senza guardia, String(null) scriveva la
                    // stringa letterale "null" in config.toml come path.
                    // Round 13: get_path() returns null for locations with no local
                    // representation (sftp/mtp/cloud mounts via portal) — without a guard,
                    // String(null) wrote the literal string "null" into config.toml as a path.
                    if (!path) {
                        logError(new Error('cartella senza path locale (mount remoto?)'), 'storage folder');
                        return;
                    }
                    if (setStorageField('base', 'base_dir', path))
                        folderRow.subtitle = path;
                } catch (e) {
                    if (!e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                        logError(e, 'bravoric-hear-read-write: errore selezione cartella');
                }
            });
        });
        folderRow.add_suffix(chooseButton);
        folderGroup.add(folderRow);

        const typesGroup = new Adw.PreferencesGroup({
            title: _('What to save'),
            description: _('Everything is off by default. Retention 0 hours means never auto-delete.'),
        });
        page.add(typesGroup);

        for (const type of STORAGE_TYPES) {
            const typeState = state.storage?.[type.section];
            if (!typeState) continue;
            const expander = new Adw.ExpanderRow({
                title: _(type.label),
                show_enable_switch: true,
                enable_expansion: typeState.enabled,
            });
            expander.connect('notify::enable-expansion', () => {
                setStorageField(type.section, 'enabled', expander.enable_expansion);
            });

            const retentionRow = new Adw.SpinRow({
                title: _('Auto-delete after (hours, 0 = never)'),
                adjustment: new Gtk.Adjustment({
                    lower: 0, upper: 8760, step_increment: 1,
                    value: typeState?.retention_hours ?? 0,
                }),
            });
            retentionRow.connect('notify::value', debounce(() => {
                setStorageField(type.section, 'retention_hours', Math.round(retentionRow.value));
            }));
            expander.add_row(retentionRow);

            typesGroup.add(expander);
        }

        const historyGroup = new Adw.PreferencesGroup({
            title: _('Output history (tray menu)'),
            description: _('Quick-access list of recent STT/OCR outputs shown in the tray menu, separate from the on-disk storage above.'),
        });
        page.add(historyGroup);

        const maxEntriesRow = new Adw.SpinRow({
            title: _('Max history entries'),
            adjustment: new Gtk.Adjustment({
                lower: 0, upper: 200, step_increment: 1,
                value: state.history?.max_entries ?? 20,
            }),
        });
        maxEntriesRow.connect('notify::value', debounce(() => {
            runConfigEditor(['set-history-max', String(Math.round(maxEntriesRow.value))]);
        }));
        historyGroup.add(maxEntriesRow);

        const clearHistoryLabel = _('Clear history');
        const clearHistoryRow = new Adw.ActionRow({ title: clearHistoryLabel, activatable: true });
        const clearHistoryButton = new Gtk.Button({ icon_name: 'user-trash-symbolic', valign: Gtk.Align.CENTER, tooltip_text: clearHistoryLabel });
        let revertId = null;
        clearHistoryButton.connect('clicked', () => {
            const ok = runConfigEditor(['clear-history']).success;
            clearHistoryRow.title = ok ? _('History cleared') : clearHistoryLabel;
            // Il revert è un one-shot: senza azzerare l'id alla scadenza e senza
            // toglierlo alla chiusura, la finestra che si chiude entro 1,5 s
            // lascierebbe il callback su una riga già distrutta.
            // The revert is a one-shot: without resetting the id at expiry and without
            // removing it at close, a window that closes within 1.5 s would leave the
            // callback on an already destroyed row.
            if (revertId)
                GLib.source_remove(revertId);
            revertId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1500, () => {
                revertId = null;
                clearHistoryRow.title = clearHistoryLabel;
                return GLib.SOURCE_REMOVE;
            });
        });
        clearHistoryRow.connect('destroy', () => {
            if (revertId) {
                GLib.source_remove(revertId);
                revertId = null;
            }
        });
        clearHistoryRow.connect('activated', () => clearHistoryButton.emit('clicked'));
        clearHistoryRow.add_suffix(clearHistoryButton);
        historyGroup.add(clearHistoryRow);
    }

    _buildIconsPage(window) {
        const page = new Adw.PreferencesPage({
            title: _('Icons'),
            icon_name: 'image-x-generic-symbolic',
        });
        window.add(page);

        const state = getServicesState();
        if (!state) {
            this._showConfigError(page);
            return;
        }

        const group = new Adw.PreferencesGroup({
            title: _('Custom notification icons'),
            description: _('Choose an image override for backend notifications. Empty uses a packaged icon when available, then a category-appropriate GNOME themed icon. Extension-owned alerts retain GNOME Shell standard presentation and are not icon-configurable.'),
        });
        page.add(group);

        for (const item of ICON_SLOTS) {
            const iconState = state.icons[item.slot];
            const row = new Adw.ActionRow({
                title: _(item.label),
                subtitle: iconState?.override ?? iconState?.default ?? '',
            });

            const chooseButton = new Gtk.Button({ icon_name: 'document-open-symbolic', valign: Gtk.Align.CENTER, tooltip_text: _('Choose icon image') });
            chooseButton.connect('clicked', () => {
                const dialog = new Gtk.FileDialog({ title: _('Choose icon image') });
                dialog.open(window, null, (dlg, result) => {
                    try {
                        const file = dlg.open_finish(result);
                        const path = file.get_path();
                        if (!path) {
                            logError(new Error('icona senza path locale (mount remoto?)'), 'icon slot');
                            return;
                        }
                        if (setIconField(item.slot, path))
                            row.subtitle = path;
                    } catch (e) {
                        if (!e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                            logError(e, 'bravoric-hear-read-write: errore selezione icona');
                    }
                });
            });

            const resetButton = new Gtk.Button({ icon_name: 'edit-undo-symbolic', valign: Gtk.Align.CENTER, tooltip_text: _('Reset to default') });
            resetButton.connect('clicked', () => {
                if (setIconField(item.slot, ''))
                    row.subtitle = iconState?.default ?? '';
            });

            row.add_suffix(chooseButton);
            row.add_suffix(resetButton);
            group.add(row);
        }
    }

    _showConfigError(page) {
        // Giro 15: fattorizzato da _buildServicesPage. Prima, Storage/Icons
        // tornavano silenziosamente (pagina vuota e muta) se config_editor
        // falliva, assumendo che l'utente avesse già visto l'errore su
        // Services — ma quella pagina viene costruita per ultima
        // (fillPreferencesWindow), quindi chi apre Storage/Icons per primo
        // (scenario reale: backend non ancora installato) non vedeva alcun
        // indizio del problema.
        // Round 15: factored out of _buildServicesPage. Before, Storage/Icons
        // silently returned (empty and mute page) if config_editor failed, assuming
        // the user had already seen the error on Services — but that page is built
        // last (fillPreferencesWindow), so whoever opens Storage/Icons first (real
        // scenario: backend not installed yet) saw no hint of the problem.
        const editorMissing = !Gio.File.new_for_path(CONFIG_EDITOR_BIN).query_exists(null);
        const errGroup = new Adw.PreferencesGroup({
            title: _('Unable to read the configuration'),
            description: editorMissing
                ? _('Backend not installed. Run: bash scripts/install.sh from the project repo.\n(expected at: %s)').replace('%s', CONFIG_EDITOR_BIN)
                : _('%s not found or invalid.').replace('%s', CONFIG_PATH),
        });
        page.add(errGroup);
    }

    _buildServicesPage(window) {
        const page = new Adw.PreferencesPage({
            title: _('Services'),
            icon_name: 'network-server-symbolic',
        });
        window.add(page);

        this._buildFileActionsGroup(page, window);

        const state = getServicesState();
        if (!state) {
            this._showConfigError(page);
            return;
        }

        for (const svc of SERVICES)
            this._buildServiceGroup(page, svc, state[svc.key]);
    }

    _buildFileActionsGroup(page, window) {
        const group = new Adw.PreferencesGroup({ title: _('Configuration file') });
        page.add(group);

        const editRow = new Adw.ActionRow({
            title: _('Edit configuration file'),
            subtitle: CONFIG_PATH,
            activatable: true,
        });
        editRow.connect('activated', () => {
            // Giro 13: senza try/catch, nessuna app di default per .toml (o
            // altro fallimento di lancio) produceva un click silenzioso —
            // eccezione loggata da GJS ma non propagata, nessun feedback.
            // Round 13: without try/catch, no default app for .toml (or another launch
            // failure) produced a silent click — exception logged by GJS but not
            // propagated, no feedback.
            try {
                // Giro 14: concatenazione stringa produce un URI malformato
                // se il path contiene spazi o caratteri riservati (#, ?, %);
                // Gio.File.get_uri() fa l'escaping corretto (pattern verificato
                // in gsconnect@andyholmes.github.io).
                // Round 14: string concatenation produces a malformed URI if the path
                // contains spaces or reserved characters (#, ?, %); Gio.File.get_uri() does
                // the correct escaping (pattern verified in gsconnect@andyholmes.github.io).
                const uri = Gio.File.new_for_path(CONFIG_PATH).get_uri();
                Gio.AppInfo.launch_default_for_uri(uri, null);
            } catch (e) {
                logError(e, 'impossibile aprire config.toml con l\'app di default');
            }
        });
        editRow.add_suffix(new Gtk.Image({ icon_name: 'document-edit-symbolic' }));
        group.add(editRow);

        const resetRow = new Adw.ActionRow({
            title: _('Reset to default'),
            subtitle: _('Overwrites all customizations (endpoints, keys, prompts) with defaults'),
            activatable: true,
        });
        resetRow.connect('activated', () => this._confirmReset(window));
        resetRow.add_suffix(new Gtk.Image({ icon_name: 'edit-undo-symbolic' }));
        group.add(resetRow);
    }

    _confirmReset(window) {
        const heading = _('Reset to default?');
        const body = _('This overwrites all your customizations (endpoints, keys, prompts) with the default configuration. This cannot be undone.');

        // Adw.AlertDialog esiste da libadwaita 1.5 (GNOME 46); Adw.MessageDialog
        // da 1.2 (GNOME 42+), deprecato in 1.5 ma ancora presente e funzionante.
        // metadata.json dichiara il supporto a GNOME 45 (libadwaita 1.4), dove
        // AlertDialog non esiste: senza fallback il click su "Reset" fallirebbe
        // in silenzio (TypeError non catturato). Il parent si passa in modo
        // diverso: AlertDialog lo prende da present(), MessageDialog da
        // transient_for.
        // Adw.AlertDialog exists since libadwaita 1.5 (GNOME 46); Adw.MessageDialog
        // since 1.2 (GNOME 42+), deprecated in 1.5 but still present and working.
        // metadata.json declares support for GNOME 45 (libadwaita 1.4), where
        // AlertDialog does not exist: without a fallback the click on "Reset" would
        // fail silently (uncaught TypeError). The parent is passed in a different
        // way: AlertDialog takes it from present(), MessageDialog from
        // transient_for.
        const useAlert = typeof Adw.AlertDialog === 'function';
        const dialog = useAlert
            ? new Adw.AlertDialog({ heading, body })
            : new Adw.MessageDialog({ transient_for: window, heading, body });

        dialog.add_response('cancel', _('Cancel'));
        dialog.add_response('reset', _('Reset'));
        dialog.set_response_appearance('reset', Adw.ResponseAppearance.DESTRUCTIVE);
        dialog.set_default_response('cancel');
        dialog.set_close_response('cancel');
        dialog.connect('response', (_dlg, response) => {
            if (response !== 'reset')
                return;
            const ok = runConfigEditor(['reset']).success;
            if (ok) {
                window.close();
                return;
            }
            // Giro 13: un fallimento (config_editor mancante, permessi, TOML
            // template invalido) non mostrava nulla — l'utente non sapeva se
            // il reset era avvenuto. Riusa lo stesso pattern AlertDialog/
            // MessageDialog per un feedback esplicito.
            // Round 13: a failure (missing config_editor, permissions, invalid TOML
            // template) showed nothing — the user did not know whether the reset had
            // happened. It reuses the same AlertDialog/MessageDialog pattern for
            // explicit feedback.
            const errHeading = _('Reset failed');
            const errBody = _('Check the system log (journalctl) for details.');
            const errDialog = useAlert
                ? new Adw.AlertDialog({ heading: errHeading, body: errBody })
                : new Adw.MessageDialog({ transient_for: window, heading: errHeading, body: errBody });
            errDialog.add_response('ok', _('OK'));
            if (useAlert)
                errDialog.present(window);
            else
                errDialog.present();
        });
        if (useAlert)
            dialog.present(window);
        else
            dialog.present();
    }

    _buildServiceGroup(page, svc, svcState) {
        // Config corrotta/incompleta: sezione servizio assente → salta invece
        // di dereferenziare undefined (TypeError che abortirebbe la pagina).
        // Corrupt/incomplete config: service section missing → skip instead of
        // dereferencing undefined (a TypeError that would abort the page).
        if (!svcState)
            return;
        const group = new Adw.PreferencesGroup({ title: _(svc.label) });
        page.add(group);

        if (svc.hasEnabled) {
            const enabledRow = new Adw.SwitchRow({
                title: _('Enabled'),
                active: svcState.enabled,
            });
            enabledRow.connect('notify::active', () => {
                setSectionField(svc.key, 'enabled', enabledRow.active ? 'true' : 'false');
            });
            group.add(enabledRow);
        }

        if (svc.hasLanguage) {
            const langRow = new Adw.EntryRow({
                title: _('Language'),
                tooltip_text: _('ISO 639-1 language code for transcription (e.g. "it" for Italian)'),
                text: svcState.language ?? 'it',
                show_apply_button: true,
            });
            langRow.connect('apply', () => {
                setSectionField(svc.key, 'language', langRow.text);
            });
            group.add(langRow);
        }

        if (svc.hasScreenshotToggle) {
            const screenshotRow = new Adw.SwitchRow({
                title: _('Take screenshot on capture'),
                subtitle: _('Select a screen area (gnome-screenshot) instead of reading an image already in the clipboard.'),
                active: svcState.capture_screenshot ?? false,
            });
            screenshotRow.connect('notify::active', () => {
                setSectionField(svc.key, 'capture_screenshot', screenshotRow.active ? 'true' : 'false');
            });
            group.add(screenshotRow);
        }

        if (svc.hasPrompt) {
            const promptField = svc.promptField || 'system_prompt';
            group.add(this._buildPromptRow(svc.key, promptField, svcState[promptField] ?? ''));
        }

        if (svc.hasHotwords) {
            const hotwordsRow = new Adw.EntryRow({
                title: _('Hotwords'),
                tooltip_text: _('Bias transcription toward these words (space-separated)'),
                text: svcState.hotwords ?? '',
                show_apply_button: true,
            });
            hotwordsRow.connect('apply', () => {
                setSectionField(svc.key, 'hotwords', hotwordsRow.text);
            });
            group.add(hotwordsRow);
        }

        svcState.levels?.forEach((level, idx) => {
            group.add(this._buildLevelExpander(svc.key, idx, level));
        });
    }

    _buildPromptRow(serviceKey, fieldName, promptText) {
        const label = fieldName === 'prompt' ? _('Initial prompt') : _('System prompt');
        const textView = new Gtk.TextView({
            wrap_mode: Gtk.WrapMode.WORD_CHAR,
            top_margin: 8, bottom_margin: 8, left_margin: 8, right_margin: 8,
        });
        textView.buffer.set_text(promptText ?? '', -1);

        const scrolled = new Gtk.ScrolledWindow({
            child: textView,
            min_content_height: 100,
            has_frame: true,
        });

        const saveButtonLabel = _('Save prompt');
        const saveButton = new Gtk.Button({
            label: saveButtonLabel,
            margin_top: 6,
            halign: Gtk.Align.END,
            css_classes: ['suggested-action'],
        });
        saveButton.connect('clicked', () => {
            const [start, end] = textView.buffer.get_bounds();
            const text = textView.buffer.get_text(start, end, false);
            const ok = setSectionField(serviceKey, fieldName, text);
            flashButtonLabel(saveButton, ok ? _('Saved ✓') : _('Error'), saveButtonLabel);
        });

        const box = new Gtk.Box({ orientation: Gtk.Orientation.VERTICAL, spacing: 4 });
        box.append(scrolled);
        box.append(saveButton);

        const row = new Adw.ActionRow({ title: label });
        row.set_child(box);
        return row;
    }

    _buildApiKeyRow(serviceKey, index, level) {
        const row = new Adw.PasswordEntryRow({
            title: _('Direct API key (optional)'),
            text: level.api_key ?? '',
            show_apply_button: true,
        });
        row.connect('apply', () => setLevelField(serviceKey, index, 'api_key', row.text));
        return row;
    }

    _buildLevelExpander(serviceKey, index, level, streamState = null) {
        // `streamState` e' il blocco stream letto da get_state(): serve SOLO
        // per il testo iniziale delle righe parallele, gia' in mano al
        // chiamante. Non si rilegge il config qui dentro: getServicesState() e'
        // un subprocess e chiamarlo per ogni livello costerebbe una lettura del
        // file per riga, e la stringa tradotta del banner non e' un test
        // affidabile dello stato. Gli aggiornamenti successivi arrivano dal sink
        // con il config riletto, un'unica volta per cambio.
        // L'oggetto `level` resta qui il dict delle stringhe originali, cosi'
        // com'e' arrivato. La normalizzazione avviene dentro il ramo stream
        // (guard del test 1e): hotwords_in_prompt e' un campo solo-stream,
        // e il parallelismo pure.
        // `streamState` is the stream block read from get_state(): it is needed ONLY
        // for the initial text of the parallel rows, already in the caller's hands.
        // The config is not re-read in here: getServicesState() is a subprocess and
        // calling it for every level would cost one file read per row, and the
        // banner's translated string is not a reliable test of the state. The later
        // updates arrive from the sink with the config re-read, once per change.
        // The `level` object stays here the dict of the original strings, as it
        // arrived. The normalization happens inside the stream branch (guard of test
        // 1e): hotwords_in_prompt is a stream-only field, and so is the parallelism.
        const globalParallel = (streamState?.dispatch_mode ?? 'auto') !== 'sequential';
        const expander = new Adw.ExpanderRow({
            title: _('Level %d').replace('%d', String(index + 1)),
            subtitle: level.name || _('(empty)'),
        });

        for (const [fieldKey, title] of LEVEL_ENTRY_FIELDS) {
            const rowTitle = fieldKey === 'api_key_env'
                ? _('API key environment variable (used if the direct key is empty)')
                : _(title);
            const row = new Adw.EntryRow({
                title: rowTitle,
                text: level[fieldKey] ?? '',
                show_apply_button: true,
            });
            row.connect('apply', () => setLevelField(serviceKey, index, fieldKey, row.text));
            expander.add_row(row);

            if (fieldKey === 'api_key_env')
                expander.add_row(this._buildApiKeyRow(serviceKey, index, level));
        }

        const timeoutRow = new Adw.SpinRow({
            title: _('Timeout (seconds)'),
            adjustment: new Gtk.Adjustment({
                lower: 1, upper: 600, step_increment: 1,
                value: Number.isFinite(parseInt(level.timeout_seconds, 10))
                    ? parseInt(level.timeout_seconds, 10) : 60,
            }),
        });
        timeoutRow.connect('notify::value', debounce(() => {
            setLevelField(serviceKey, index, 'timeout_seconds', String(Math.round(timeoutRow.value)));
        }));
        expander.add_row(timeoutRow);

        // hotwords_in_prompt esiste solo in FallbackLevel per il servizio
        // stream (api_client.transcribe_audio lo legge per costruire il prompt
        // con il vocabolario). stt e ocr non hanno il campo: non mostrarlo li'
        // evita una casella che il backend rifiuterebbe.
        // hotwords_in_prompt exists only in FallbackLevel for the stream service
        // (api_client.transcribe_audio reads it to build the prompt with the
        // vocabulary). stt and ocr do not have the field: not showing it there
        // avoids a box the backend would reject.
        if (serviceKey === 'stream') {
            // Normalizza UNA volta sola i due flag booleani, perche' i
            // LEVEL_FIELDS di config_editor.get_state() li stringificano con
            // str() e arrivano come "True"/"False": senza questo,
            // `!!level.hotwords_in_prompt` e' sempre true (`!!'False'`), lo
            // switch parte acceso e il primo click lo SPEGNE invece di
            // accenderlo. Stessa regola di levelFlagTrue() usata dal banner,
            // nessun secondo criterio. Copia locale: `level` non viene
            // toccato, cosi' gli altri campi restano le stringhe originali.
            // Normalizes the two boolean flags ONCE only, because the LEVEL_FIELDS of
            // config_editor.get_state() stringify them with str() and they arrive as
            // "True"/"False": without this, `!!level.hotwords_in_prompt` is always true
            // (`!!'False'`), the switch starts on and the first click turns it OFF
            // instead of on. Same rule as levelFlagTrue() used by the banner, no second
            // criterion. Local copy: `level` is not touched, so the other fields stay
            // the original strings.
            level = {
                ...level,
                hotwords_in_prompt: levelFlagTrue(level.hotwords_in_prompt),
                parallel: levelFlagTrue(level.parallel),
            };
            const hotwordsInPromptRow = new Adw.SwitchRow({
                title: _('Add hotwords to the prompt'),
                subtitle: _('Send this level\'s hotwords inside the prompt as proper nouns, in addition to the hotwords field'),
                active: !!level.hotwords_in_prompt,
            });
            hotwordsInPromptRow.connect('notify::active', debounce(() => {
                setLevelField(serviceKey, index, 'hotwords_in_prompt', String(hotwordsInPromptRow.active));
            }));
            expander.add_row(hotwordsInPromptRow);
        }

        // parallel e max_concurrency sono i campi del pool parallelo e degli
        // slot per endpoint (SPEC-MAX-CONCURRENCY): valgono solo per stream,
        // quindi stesso guard di sopra. Stt e ocr non li hanno nel
        // FallbackLevel e il backend rifiuterebbe la scrittura.
        // parallel and max_concurrency are the fields of the parallel pool and of
        // the per-endpoint slots (SPEC-MAX-CONCURRENCY): they apply only to stream,
        // so the same guard as above. Stt and ocr do not have them in the
        // FallbackLevel and the backend would reject the write.
        if (serviceKey === 'stream') {
            const parsedConcurrency = parseInt(level.max_concurrency, 10);
            // La riga parallela oggi promette il comportamento di "auto" e
            // MENTE in "sequential", dove il flag e' proprio ignorato. Il testo
            // segue il toggle globale invece di descrivere un mondo che non
            // esiste piu'.
            // The parallel row today promises the behavior of "auto" and LIES in
            // "sequential", where the flag is in fact ignored. The text follows the
            // global toggle instead of describing a world that no longer exists.
            const parallelSubtitle = parallelRowSubtitle(globalParallel);
            const parallelRow = new Adw.SwitchRow({
                title: _('Use this endpoint in parallel'),
                subtitle: parallelSubtitle,
                active: !!level.parallel,
            });
            const concurrencyRow = new Adw.SpinRow({
                title: _('Concurrent requests for this endpoint'),
                tooltip_text: _('How many chunks this endpoint may process at the same time. 1 means requests are serialized, as whisper.cpp does. It applies in the parallel pool, and in sequential mode only when the global worker count is greater than 1.'),
                adjustment: new Gtk.Adjustment({
                    lower: 1, upper: 8, step_increment: 1,
                    value: Number.isFinite(parsedConcurrency)
                        ? Math.min(8, Math.max(1, parsedConcurrency)) : 3,
                }),
                digits: 1,
            });
            // Lo slot vale solo se il livello partecipa al pool E il toggle
            // globale non e' in "sequential": quando parallel e' spento la
            // riga resta leggibile ma non editabile. Si disabilita con
            // `sensitive`, non con `editable` (che su una SpinRow non esiste
            // ed e' comunque asserito come assente in questo file). Il toggle
            // GLOBALE conta quanto parallelRow.active: in "sequential" nessuno
            // slot e' applicato e una SpinRow attiva che non governa nulla e'
            // una bug che si vede solo dopo.
            // `globalParallelLive` e' aggiornato dal sink col config RILETTO
            // (non dallo stato dei widget): una `const` di costruzione
            // andrebbe stantia dopo un cambio del toggle globale e la riga
            // resterebbe attiva mentre il toggle dice "sequential".
            // The slot applies only if the level takes part in the pool AND the global
            // toggle is not on "sequential": when parallel is off the row stays readable
            // but not editable. It is disabled with `sensitive`, not with `editable`
            // (which does not exist on a SpinRow and is asserted as absent in this file
            // anyway). The GLOBAL toggle counts as much as parallelRow.active: in
            // "sequential" no slot is applied and an active SpinRow that governs nothing
            // is a bug that only shows later. `globalParallelLive` is updated by the sink
            // with the RE-READ config (not from the widgets' state): a construction-time
            // `const` would go stale after a change of the global toggle and the row
            // would stay active while the toggle says "sequential".
            let globalParallelLive = globalParallel;
            const syncSensitivity = () => {
                concurrencyRow.sensitive = parallelRow.active && globalParallelLive !== false;
            };
            syncSensitivity();
            parallelRow.connect('notify::active', syncSensitivity);
            if (this._parallelModeSink) {
                // Aggiornata dal toggle globale con il config RILETTO, non con
                // lo stato dei widget: stessa fonte del banner.
                // Updated by the global toggle with the RE-READ config, not with the
                // widgets' state: same source as the banner.
                this._parallelModeSink.push(fresh => {
                    const isSequential = (fresh?.dispatch_mode ?? 'auto') === 'sequential';
                    globalParallelLive = !isSequential;
                    parallelRow.subtitle = parallelRowSubtitle(globalParallelLive);
                    syncSensitivity();
                });
            }
            parallelRow.connect('notify::active', debounce(() => {
                setLevelField(serviceKey, index, 'parallel', String(parallelRow.active));
                // Giro 2 (B4-frontend-B): il banner descrive proprio queste
                // due caselle, quindi va ricalcolato anche quando cambiano
                // loro. Lettura dal config riletto, mai dallo stato dei widget.
                // Round 2 (B4-frontend-B): the banner describes exactly these two boxes, so
                // it must be recomputed also when they change. Read from the re-read config,
                // never from the widgets' state.
                this._refreshDispatchStatus?.();
            }));
            concurrencyRow.connect('notify::value', debounce(() => {
                setLevelField(serviceKey, index, 'max_concurrency', String(Math.round(concurrencyRow.value)));
                this._refreshDispatchStatus?.();
            }));
            expander.add_row(parallelRow);
            expander.add_row(concurrencyRow);
        }

        return expander;
    }
}
