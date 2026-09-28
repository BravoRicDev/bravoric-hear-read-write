import GObject from 'gi://GObject';
import St from 'gi://St';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import Clutter from 'gi://Clutter';

import { Extension, gettext as _ } from 'resource:///org/gnome/shell/extensions/extension.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import { consumeStreamSnapshot, classifyStreamItem, computeStreamDelete, parseBlacklist } from './stream-consumer.mjs';
// Solo i due watcher che _init avvia davvero: il debounce del refresh non ha
// un punto di aggancio qui dentro, lo arma watchStatusFile dal modulo (che
// chiama lui l'export scheduleRefresh). Un import per un metodo che nessuno
// chiama e' solo un secondo cadavere, dello stesso genere di quello rimosso.
import { watchStatusFile, watchStreamStateFile } from './watch-cache.mjs';
import { setRecordingBlink } from './recording-blink.mjs';

const STATUS_PATH = GLib.build_filenamev([
    GLib.get_home_dir(), '.cache', 'bravoric-stt-clipboard', 'status.json',
]);
const HISTORY_PATH = GLib.build_filenamev([
    GLib.get_home_dir(), '.cache', 'bravoric-stt-clipboard', 'output_history.json',
]);
const STREAM_STATE_PATH = GLib.build_filenamev([
    GLib.get_home_dir(), '.cache', 'bravoric-stt-clipboard', 'stream_state.json',
]);
// Testo VIVO del campo, letto dal backend per costruire il prompt di contesto.
// Contiene solo cio' che l'utente ha davvero davanti: i chunk cancellati non ci
// sono piu' e le parole comando non ci sono mai state (sono state eseguite).
const STREAM_LIVE_TEXT_PATH = GLib.build_filenamev([
    GLib.get_home_dir(), '.cache', 'bravoric-stt-clipboard', 'stream_live_text.json',
]);

// Venv in posizione XDG fissa (vedi scripts/install.sh), portabile tra
// macchine/utenti diversi: stesso schema usato da prefs.js.
const VENV_BIN = GLib.build_filenamev([
    GLib.get_user_data_dir(), 'bravoric-stt-clipboard', 'venv', 'bin',
]);

const THEME_ICONS = {
    idle: 'audio-input-microphone-symbolic',
    recording: 'media-record-symbolic',
    processing: 'content-loading-symbolic',
    error: 'dialog-error-symbolic',
};

const BLINK_CLASS = 'bravoric-recording-icon';
const BLINK_INTERVAL_MS = 500;
const REFRESH_DEBOUNCE_MS = 250;
// Anteprima delle voci di Cronologia nel menu (caratteri, non byte): oltre
// questa lunghezza il testo e' troncato, non accorciato.
const HISTORY_PREVIEW_CHARS = 50;
// Anteprima dell'ultimo output in _refreshStatus: stessa funzione del troncamento,
// lunghezza maggiore perche' non c'e' il tag di servizio accanto.
const LAST_OUTPUT_PREVIEW_CHARS = 60;
// notify_keyval() e' Clutter e aspetta il tempo evento in MICROSECONDI, mentre
// Clutter.get_current_event_time() restituisce i MILLISECONDI: la conversione
// e' unita' di sistema, non un fattore di misura (Giro 2, B2-frontend-B).
const EVENT_TIME_MS_TO_US = 1000;
// Il file monitor scatta solo quando il backend *scrive*: se muore a metà
// (crash, VPN giù) non arriva più alcun evento. Questo timer rivaluta
// periodicamente lo stato così la logica di timeout viene comunque eseguita.
const TIMEOUT_CHECK_INTERVAL_SECONDS = 30;

// Pacing fra due incolla consecutivi in modalità per_chunk (D5).
// Il valore effettivo arriva da stream_state.json (paste_delay_ms); questo è
// solo il fallback se il backend non l'ha ancora scritto.
const STREAM_PACING_DEFAULT_MS = 250;
const STREAM_SETTLE_MS = 30;
const STREAM_DEBOUNCE_MS = 250;
// Intervallo fra due caratteri consecutivi in modalita' per_chunk con
// paste_channel='type': e' un ritardo di HIGS, non un timeout, e serve a
//che l'applicazione riceva i key event uno alla volta.
const TYPE_KEY_INTERVAL_MS = 3;
// Tetto di attesa del drenaggio della coda PRIMA di chiudere la sessione a
// comando vocale. Prima la fine sessione non aveva un tetto: se la coda non
// si svuotava il pallino rosso restava acceso indefinitamente (difetto
// segnalato dall'utente). Superato il tetto si chiude comunque, scartando
// quello che resta in coda.
const STREAM_END_TIMEOUT_MS = 5 * 1000;
// Quanto spesso _requestStreamEnd ricontrolla se la coda si e' svuotata. Non e'
// un timeout: e' l'intervallo fra due interrogazioni di stream_state.json, e
// non deve essere confuso con STREAM_END_TIMEOUT_MS qui sopra.
const STREAM_END_POLL_MS = 100;

// Un'operazione può morire a metà (backend ucciso, VPN giù, crash durante
// l'elaborazione): senza questi limiti l'icona resterebbe bloccata per sempre
// in uno stato non-idle. Chiave = stato; per il processing il limite dipende
// dal servizio (STT più veloce, OCR più lento).
const STATE_TIMEOUT_SECONDS = {
    recording: 15 * 60,
    error: 5 * 60,
};
const PROCESSING_TIMEOUT_SECONDS = {
    stt: 30 * 60,
    ocr: 120 * 60,
};

// Ogni notifica generata dall'estensione ha il suo interruttore GSettings
// (GUI: pagina Notifiche > Extension notifications): `notify-errors` per gli
// errori (streaming, backend, file di stato), `notify-status` per i messaggi
// di stato (file di stato ripristinato, timeout). Impostato in enable(),
// azzerato in disable(). Un gschemas.compiled stantio senza la chiave (o una
// lettura che fallisce) NON deve mai spegnere una notifica ne' sollevare:
// resta acceso, come prima dell'introduzione dell'interruttore.
let notificationSettings = null;

function notificationEnabled(key) {
    try {
        if (notificationSettings?.settings_schema?.has_key(key))
            return notificationSettings.get_boolean(key);
    } catch (e) {
        logError(e, `bravoric-indicator: lettura di ${key} fallita`);
    }
    return true;
}

function notifyErrorIfEnabled(title, body) {
    if (notificationEnabled('notify-errors'))
        Main.notifyError(title, body);
}

function notifyStatusIfEnabled(title, body) {
    if (notificationEnabled('notify-status'))
        Main.notify(title, body);
}

// Unico punto di avvio dei binari del venv. Gli due nomi pubblici sotto sono
// wrapper di una riga: chiamarli non cambia nulla di come vengono costruiti
// e notificati, ma il codice (build del path, test di esistenza, avviso
// "backend non installato", cattura dell'eccezione) smette di esistere due
// volte.
function spawnVenvBinary(binName, args, logPrefix) {
    const path = GLib.build_filenamev([VENV_BIN, binName]);
    if (!GLib.file_test(path, GLib.FileTest.EXISTS)) {
        notifyErrorIfEnabled(
            _('Bravoric backend not installed'),
            _('Run scripts/install.sh from the project repo first.'),
        );
        return;
    }
    try {
        Gio.Subprocess.new([path, ...args], Gio.SubprocessFlags.NONE);
    } catch (e) {
        logError(e, logPrefix);
        notifyErrorIfEnabled(_('Bravoric error'), e.message);
    }
}
// Dipendenze iniettate ai moduli puri: i test eseguono watch-cache.mjs
// davvero, con stub al posto di Gio/GLib, quindi il modulo non li importa.
const ioDeps = { Gio, GLib, logError };


function spawnBackground(binName, ...args) {
    spawnVenvBinary(binName, args, `bravoric-indicator: impossibile lanciare ${binName}`);
}

function showCopiedOsd() {
    const icon = Gio.ThemedIcon.new('edit-copy-symbolic');
    const message = _('Copied to clipboard');
    if (Main.osdWindowManager.showAll)
        Main.osdWindowManager.showAll(icon, message, null, null);
    else
        Main.osdWindowManager.show(-1, icon, message, null, null);
}

// Giro 16: era un no-op silenzioso, a differenza di spawnBackground (stesso
// scenario venv assente) che avvisa con Main.notifyError. Click su "Svuota
// cronologia" non faceva nulla senza spiegazione. Ora l'avviso arriva da
// spawnVenvBinary, comune a tutti i binari del venv.
function spawnConfigEditor(...args) {
    spawnVenvBinary('bravoric-config-editor', args,
        'bravoric-indicator: impossibile eseguire config-editor');
}

const BravoricIndicator = GObject.registerClass(
class BravoricIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.0, 'Bravoric STT/OCR');
        this._extension = extension;
        this._cancellable = new Gio.Cancellable();

        // Risorse di sessione: inizializzate a null/0 e NON affidate al fatto
        // che `undefined` sia falsy. destroy() le legge tutte per nome e i
        // metodi le azzerano dopo l'uso: se un campo non esistesse, il teardown
        // lavorerebbe per caso e un campo nuovo si aggiungerebbe in silenzio.
        // _monitor/_monitorId e _streamMonitor/_streamMonitorId sono i campi che
        // destroy() disconnette: _watchStatusFile e _watchStreamStateFile li
        // scrivono (vedi _watchCacheFile).
        this._monitor = null;
        this._monitorId = 0;
        this._streamMonitor = null;
        this._streamMonitorId = 0;
        this._blinkTimeoutId = null;
        this._refreshDebounceId = null;
        this._timeoutCheckId = null;
        this._statusRefreshGen = 0;
        this._historyRefreshGen = 0;
        this._virtualDevice = null;

        // contatori di stato: inizializzati esplicitamente qui, non affidati
        // al fatto che `undefined` sia falsy (fragile se il codice cambia).
        this._statusParseErrors = 0;
        this._timeoutWarned = false;

        // Stato dettatura streaming (modalità per_chunk).
        this._streamSessionId = null;
        this._streamIndex = 0;
        this._streamDebounceId = null;
        this._streamWasActive = false;
        this._streamQueue = [];
        this._streamWorkerActive = false;
        this._streamPasteBlocked = false;
        // _streamSegments null = buffer sconosciuto (reload, cambio sessione):
        // NON [], altrimenti un delete partirebbe da "campo vuoto" invece che
        // dal fail-safe no-op dichiarato in _writeStreamLiveText.
        this._streamSegments = null;
        this._streamRules = [];
        this._streamBlacklist = new Set();
        this._streamPasteDeviceWarned = false;
        this._streamPasteTimerId = null;
        this._streamTypeTimerId = null;
        this._streamEndRequested = null;
        this._streamEndTimerId = null;
        this._pasteDelayMs = STREAM_PACING_DEFAULT_MS;
        this._pasteShortcut = 'ctrl+v';
        this._pasteChannel = 'clipboard';

        this.accessible_name = _('Bravoric STT/OCR indicator');
        this._setAccessibleState('idle');

        this._icon = new St.Icon({
            gicon: this._idleGicon(),
            style_class: 'system-status-icon',
            icon_size: 24,
        });
        this.add_child(this._icon);

        this._lastOutputText = '';
        this._lastOutputItem = new PopupMenu.PopupMenuItem(_('Last output: (none)'));
        this._lastOutputItem.setSensitive(false);
        this._lastOutputItem.connect('activate', () => {
            if (!this._lastOutputText)
                return;
            St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, this._lastOutputText);
            // Giro 14: stessa azione del click su una voce di Cronologia
            // (copia negli appunti), che mostra l'OSD — qui non lo faceva,
            // feedback incoerente tra le due voci di menu equivalenti.
            showCopiedOsd();
        });
        this.menu.addMenuItem(this._lastOutputItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._dictationItem = new PopupMenu.PopupImageMenuItem(_('Dictation'), 'audio-input-microphone-symbolic');
        this._dictationItem.connect('activate', () => spawnBackground('bravoric-stt-toggle'));
        this.menu.addMenuItem(this._dictationItem);

        this._ocrItem = new PopupMenu.PopupImageMenuItem(_('OCR'), 'camera-photo-symbolic');
        this._ocrItem.connect('activate', () => spawnBackground('bravoric-ocr-capture'));
        this.menu.addMenuItem(this._ocrItem);

        const configItem = new PopupMenu.PopupImageMenuItem(_('Configuration'), 'preferences-system-symbolic');
        configItem.connect('activate', () => this._extension.openPreferences());
        this.menu.addMenuItem(configItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._historySubmenu = new PopupMenu.PopupSubMenuMenuItem(_('History'));
        this.menu.addMenuItem(this._historySubmenu);

        this._streamItem = new PopupMenu.PopupImageMenuItem(_('Streaming'), 'audio-input-microphone-symbolic');
        this._streamItem.connect('activate', () => spawnBackground('bravoric-stream-toggle'));
        this.menu.addMenuItem(this._streamItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(this._buildDiagnosticsSubmenu());

        this._watchStatusFile();
        this._watchStreamStateFile();
        this._startVirtualDevice();
        this._refreshStatus();
        this._refreshHistory();

        // Rivalutazione periodica indipendente dal file monitor: copre il caso
        // in cui il backend muore senza più scrivere status.json (nessun evento
        // 'changed'), che altrimenti lascerebbe l'icona bloccata per sempre.
        this._timeoutCheckId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, TIMEOUT_CHECK_INTERVAL_SECONDS, () => {
                this._refreshStatus();
                return GLib.SOURCE_CONTINUE;
            });
    }

    // Fatti grezzi (path calcolati + stato su disco) condivisi da
    // _diagnosticsReport (testo per la clipboard) e _buildDiagnosticsSubmenu
    // (righe tradotte del menu): prima duplicati identici salvo la sola
    // differenza _()/nessun _() sulle etichette 'OK'/'MISSING'. Un solo
    // punto dove VENV_BIN e lo schema compilato vengono localizzati.
    _diagnosticsFacts() {
        const venvToggle = GLib.build_filenamev([VENV_BIN, 'bravoric-stt-toggle']);
        const schemaPath = GLib.build_filenamev([
            this._extension.path, 'schemas', 'gschemas.compiled',
        ]);
        return {
            venvToggle,
            schemaPath,
            venvOk: GLib.file_test(venvToggle, GLib.FileTest.EXISTS),
            schemaOk: GLib.file_test(schemaPath, GLib.FileTest.EXISTS),
            statusOk: GLib.file_test(STATUS_PATH, GLib.FileTest.EXISTS),
            historyOk: GLib.file_test(HISTORY_PATH, GLib.FileTest.EXISTS),
        };
    }

    _diagnosticsReport() {
        // Report testuale completo (path + stato): copiato negli appunti così
        // il menu resta compatto ma il dettaglio è sempre recuperabile.
        const f = this._diagnosticsFacts();
        const label = (ok) => ok ? 'OK' : 'MISSING';
        return [
            'bravoric-indicator diagnostics',
            'venv: ' + label(f.venvOk) + ' (' + VENV_BIN + ')',
            'schema: ' + label(f.schemaOk) + ' (' + f.schemaPath + ')',
            'status.json: ' + label(f.statusOk) + ' (' + STATUS_PATH + ')',
            'output_history.json: ' + label(f.historyOk) + ' (' + HISTORY_PATH + ')',
            'parse errors: ' + (this._statusParseErrors || 0),
            'timeout warned: ' + !!this._timeoutWarned,
        ].join('\n');
    }

    _buildDiagnosticsSubmenu() {
        const submenu = new PopupMenu.PopupSubMenuMenuItem(_('Diagnostics'));
        const f = this._diagnosticsFacts();
        const label = (ok) => ok ? _('OK') : _('MISSING');
        const rows = [
            _('Venv: %s').replace('%s', label(f.venvOk)),
            _('Schema: %s').replace('%s', label(f.schemaOk)),
            _('Status file: %s').replace('%s', label(f.statusOk)),
            _('History file: %s').replace('%s', label(f.historyOk)),
        ];
        for (const row of rows) {
            const item = new PopupMenu.PopupMenuItem(row);
            item.setSensitive(false);
            submenu.menu.addMenuItem(item);
        }
        submenu.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const copyItem = new PopupMenu.PopupImageMenuItem(_('Copy diagnostics'), 'edit-copy-symbolic');
        copyItem.connect('activate', () => {
            St.Clipboard.get_default().set_text(
                St.ClipboardType.CLIPBOARD, this._diagnosticsReport());
            showCopiedOsd();
        });
        submenu.menu.addMenuItem(copyItem);
        return submenu;
    }

    _historyTagLabel(service, kind) {
        if (service === 'stt' && kind === 'raw')
            return _('STT raw');
        if (service === 'stt' && kind === 'clean')
            return _('STT clean');
        if (service === 'ocr' && kind === 'raw')
            return _('OCR raw');
        if (service === 'ocr' && kind === 'clean')
            return _('OCR clean');
        // kind/service inatteso (voce corrotta o formato futuro): non
        // indovinare un servizio non richiesto, mostra il dato grezzo.
        return `${service ?? '?'} ${kind ?? '?'}`;
    }

    _refreshHistory() {
        // Generation token (stesso motivo di _refreshStatus, giro 13): il file
        // monitor osserva l'intera directory, quindi un burst di eventi può
        // far partire due load_contents_async sovrapposti su HISTORY_PATH; se
        // completano fuori ordine il menu mostrerebbe per un istante dati stale.
        this._historyRefreshGen += 1;
        const gen = this._historyRefreshGen;
        const file = Gio.File.new_for_path(HISTORY_PATH);
        file.load_contents_async(this._cancellable, (source, result) => {
            if (this._cancellable.is_cancelled())
                return; // estensione disabilitata mentre la lettura era in corso
            if (gen !== this._historyRefreshGen)
                return; // superata da una lettura più recente

            let entries = [];
            try {
                const [, contents] = source.load_contents_finish(result);
                entries = JSON.parse(new TextDecoder().decode(contents));
            } catch {
                entries = []; // file non ancora creato o vuoto
            }
            // JSON valido ma di forma inattesa (oggetto, scalare, null): senza
            // questa guardia entries.length sarebbe undefined e il for...of
            // sottostante lancerebbe un TypeError fuori dal try/catch.
            if (!Array.isArray(entries))
                entries = [];

            this._historySubmenu.label.text = _('History (%d)').replace('%d', String(entries.length));
            this._historySubmenu.menu.removeAll();

            if (entries.length === 0) {
                const emptyItem = new PopupMenu.PopupMenuItem(_('No history yet'));
                emptyItem.setSensitive(false);
                this._historySubmenu.menu.addMenuItem(emptyItem);
                return;
            }

            for (const entry of entries) {
                if (!entry || typeof entry !== 'object')
                    continue;
                const text = typeof entry.text === 'string' ? entry.text : '';
                const tag = this._historyTagLabel(entry.service, entry.kind);
                const preview = text.slice(0, HISTORY_PREVIEW_CHARS);
                const item = new PopupMenu.PopupMenuItem(_('[%s] %s').replace('%s', tag).replace('%s', preview));
                item.connect('activate', () => {
                    St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text);
                    showCopiedOsd();
                });
                this._historySubmenu.menu.addMenuItem(item);
            }

            const clearItem = new PopupMenu.PopupMenuItem(_('Clear history'));
            clearItem.connect('activate', () => spawnConfigEditor('clear-history'));
            this._historySubmenu.menu.addMenuItem(clearItem);
        });
    }

    // Il lampeggio vive in recording-blink.mjs, modulo puro eseguito dai test;
    // qui resta l'aggancio con i valori dichiarati in questo file.
    _setRecordingBlink(active) {
        setRecordingBlink(this, ioDeps, active, BLINK_CLASS, BLINK_INTERVAL_MS);
    }

    _timeoutLimitFor(state, service) {
        // Nessun limite per 'idle': è lo stato di riposo. Per il processing
        // il limite dipende dal servizio, che può mancare (status vecchio).
        if (state === 'processing')
            return service ? (PROCESSING_TIMEOUT_SECONDS[service] || null) : null;
        // P4: il limite su 'recording' NON vale per una sessione streaming
        // per_chunk. I 15 minuti sono un watchdog pensato per una
        // registrazione STT, che occupa il microfono e va chiusa: qui il
        // supervisore batte il cuore di status.json (stream.heartbeat), ma
        // l'età che l'estensione legge è comunque quella del cuore e non
        // prova nulla sul fatto che la sessione sia viva. Applicare il limite
        // spegneva una dettatura in corso (icona a idle, notifica "Recording
        // timed out") mentre ffmpeg teneva ancora il microfono, e riattivava
        // la voce Streaming che avviava un secondo ffmpeg. Una sessione
        // streaming che muole davvero si libera da sola: il supervisore
        // finito scrive IDLE, e il lock stale viene ripulito da is_stream_active().
        if (state === 'recording' && service === 'stream')
            return null;
        return STATE_TIMEOUT_SECONDS[state] || null;
    }

    _timeoutMessage(state) {
        if (state === 'recording')
            return _('Recording timed out');
        if (state === 'error')
            return _('Error state reset');
        return _('Processing timed out');
    }

    _setAccessibleState(state) {
        const labels = {
            idle: _('Current state: idle'),
            recording: _('Current state: recording'),
            processing: _('Current state: processing'),
            error: _('Current state: error'),
        };
        this.get_accessible().accessible_description = labels[state] || labels.idle;
    }

    _idleGicon() {
        const path = GLib.build_filenamev([this._extension.path, 'icons', 'bravoric-symbolic.svg']);
        if (GLib.file_test(path, GLib.FileTest.EXISTS))
            return Gio.icon_new_for_string(path);
        return Gio.ThemedIcon.new(THEME_ICONS.idle);
    }

    _watchStatusFile() {
        watchStatusFile(this, ioDeps, STATUS_PATH, REFRESH_DEBOUNCE_MS);
    }

    _watchStreamStateFile() {
        watchStreamStateFile(this, ioDeps, STREAM_STATE_PATH, STREAM_DEBOUNCE_MS);
    }

    _onStreamStateChanged() {
        let state;
        try {
            const [, contents] = GLib.file_get_contents(STREAM_STATE_PATH);
            state = JSON.parse(new TextDecoder().decode(contents));
        } catch {
            return; // file assente o corrotto: niente da incollare
        }
        if (!state || typeof state !== 'object' || Array.isArray(state))
            return;

        const previousSession = this._streamSessionId;
        const result = consumeStreamSnapshot(this, state);
        const sessionChanged = previousSession !== this._streamSessionId;
        if (sessionChanged) {
            this._streamSegments = null; // history unknown after restart/session switch: deletes are fail-safe no-op.
            // Giro 2 (F1): la coda apparteneva alla sessione PRECEDENTE. Il
            // blocco su un invio fallito poteva lasciare in testa chunk mai
            // consegnati: al cambio di sessione venivano RICONSEGNATI insieme ai
            // nuovi (testo duplicato) e, se il capo era un comando 'delete',
            // computeStreamDelete li eseguiva sui segmenti gia' sostituiti dai
            // chunk nuovi (BackSpace distruttivi sul testo appena dettato).
            // Svuotare qui chiude entrambi i casi distruttivi: con la coda
            // svuotata l'item in testa non viene piu' rieseguito, e i segmenti
            // sono null, quindi un delete residuo resta il fail-safe no-op
            // dichiarato sopra. Non e' uno scarto di testo consegnato: quei
            // chunk non sono mai arrivati nel campo della sessione vecchia.
            this._streamQueue = [];
        }
        this._streamBlacklist = parseBlacklist(state.blacklist);
        if (!result.accepted)
            return;
        if (result.invalid > 0)
            console.warn(`bravoric-indicator: scartati ${result.invalid} chunk stream non validi`);

        if (Number.isFinite(state.paste_delay_ms) && state.paste_delay_ms >= 0)
            this._pasteDelayMs = state.paste_delay_ms;
        this._pasteShortcut = state.paste_shortcut === 'ctrl+shift+v' ? 'ctrl+shift+v' : 'ctrl+v';
        this._pasteChannel = state.paste_channel === 'type' ? 'type' : 'clipboard';

        // Consuma tutto lo snapshot: il monitor/debounce può aver accorpato più scritture.
        this._streamRules = Array.isArray(state.commands) ? state.commands : [];
        this._streamQueue.push(...result.items.map(item => classifyStreamItem(item, this._streamRules, this._streamBlacklist)));
        // L'azzzeramento del latch viene DOPO il push, non prima: al cambio di
        // sessione la coda appena svuotata va riempita con i chunk nuovi prima di
        // decidere se il blocco ha ancora senso. I due casi in cui il latch si
        // libera sono: coda vuota (tutto scartato dalla blacklist) o cambio di
        // sessione (coda ripulita e ricaricata con i chunk nuovi).
        if (this._streamQueue.length === 0 || sessionChanged)
            this._streamPasteBlocked = false;
        this._startStreamPasteWorker();
    }

    // Ritardo di attesa fra due CONSEGNE consecutive (D5): arriva da
    // stream_state.json con paste_delay_ms, il fallback qui dentro e' il
    // default dichiarato in STREAM_PACING_DEFAULT_MS. Non e' il tempo di
    // digitazione di un singolo carattere (quello e' TYPE_KEY_INTERVAL_MS) ne
    // l'attesa fra l'invio della scorciatoia e il chunk successivo (quella e'
    // STREAM_SETTLE_MS): qui si aspetta DOPO che il chunk e' stato consegnato,
    // per dare all'applicazione il tempo di consumarlo.
    _pacingDelayMs() {
        return Number.isFinite(this._pasteDelayMs)
            ? this._pasteDelayMs : STREAM_PACING_DEFAULT_MS;
    }

    // Coda e modello del campo sono due viste dello STESSO fatto: un chunk e'
    // consegnato quando sparisce dalla coda ed entra nei segmenti. La coda
    // funziona solo se le due cose succadono insieme, quindi stanno in un
    // unico metodo.
    //
    // Chiamato DOPO l'invio riuscito su entrambi i canali, e i due canali
    // rimangono percorsi DIVERSI: il clipboard chiama questo qui appena
    // l'invio e' riuscito, il canale 'type' quando ha finito di digitare i
    // caratteri. La coda non puo' essere svuotata in anticipo nel secondo
    // caso, perche' i caratteri non sono ancora nel campo.
    _commitStreamItem(item) {
        this._streamQueue.shift();
        if (!this._streamSegments)
            this._streamSegments = [];
        this._streamSegments.push(item.text);
        this._writeStreamLiveText();
    }

    _startStreamPasteWorker() {
        // `this._cancellable` e' l'unica risorsa che `destroy()` annulla per
        // prima (Gio.Cancellable.cancel, riga 1129) e che non viene riaperta
        // da nessun altro percorso: dopo il teardown la sua condizione e' vera
        // e resta vera. Sostituisce il flag di ciclo di vita `_streamDestroyed`
        // (G1): non dice "l'istanza distrutta", dice "questo lavoro non e'
        // piu' in corso", che e' la domanda che le guardie facevano davvero.
        if (this._cancellable.is_cancelled() || this._streamWorkerActive || this._streamPasteBlocked
            || this._streamQueue.length === 0)
            return;
        if (!this._virtualDevice) {
            if (!this._streamPasteDeviceWarned) {
                this._streamPasteDeviceWarned = true;
                notifyErrorIfEnabled(_('Streaming paste unavailable'),
                    _('The virtual keyboard is unavailable; pending chunks were kept in memory.'));
                logError(new Error('tastiera virtuale non disponibile; chunk stream mantenuti in coda'),
                    'bravoric-indicator: paste stream sospeso');
            }
            return; // Nessun retry busy-loop; un prossimo evento può riprovare.
        }

        this._streamWorkerActive = true;
        while (this._streamQueue.length && this._streamQueue[0].action === 'drop')
            this._streamQueue.shift();
        if (this._streamQueue.length === 0) {
            this._streamWorkerActive = false;
            return;
        }
        const item = this._streamQueue[0];
        if (item.action !== 'paste') {
            this._runStreamCommand(item);
            return;
        }
        const delay = this._pacingDelayMs();
        if (this._pasteChannel === 'type') {
            this._typeStreamItem(item);
            return;
        }
        St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, item.text);
        this._streamPasteTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, STREAM_SETTLE_MS, () => {
            this._streamPasteTimerId = null;
            if (this._cancellable.is_cancelled()) {
                this._streamWorkerActive = false;
                return GLib.SOURCE_REMOVE;
            }

            const modifiers = [Clutter.KEY_Control_L];
            if (this._pasteShortcut === 'ctrl+shift+v')
                modifiers.push(Clutter.KEY_Shift_L);
            const pressed = [];
            let sent = true;
            try {
                for (const key of modifiers) {
                    if (!this._sendKey(key, Clutter.KeyState.PRESSED)) {
                        sent = false;
                        break;
                    }
                    pressed.push(key);
                }
                if (sent) {
                    if (!this._sendKey(Clutter.KEY_v, Clutter.KeyState.PRESSED)) {
                        sent = false;
                    } else {
                        pressed.push(Clutter.KEY_v);
                    }
                }
            } finally {
                for (const key of pressed.reverse()) {
                    if (!this._sendKey(key, Clutter.KeyState.RELEASED))
                        sent = false;
                }
            }
            if (sent) {
                this._commitStreamItem(item);
            } else {
                // I tasti effettivamente premuti sono già stati rilasciati nel finally;
                // il risultato dell'incolla resta ambiguo.
                notifyErrorIfEnabled(_('Streaming paste incomplete'),
                    _('A chunk could not be sent. Check the focused field before restarting to avoid duplicates.'));
                logError(new Error(
                    `invio Ctrl+V fallito per ${item.sessionId} chunk ${item.index}; `
                    + 'chunk conservato in testa alla coda, riattivare l\'estensione '
                    + 'solo dopo aver verificato il campo per evitare duplicati'),
                    'bravoric-indicator: paste stream incompleto');
                // Esito ambiguo: trattieni l'elemento e blocca il drain per non
                // dichiararlo consegnato né ritentare automaticamente/duplicare.
                this._streamPasteBlocked = true;
            }
            this._streamPasteTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
                this._streamPasteTimerId = null;
                this._streamWorkerActive = false;
                this._startStreamPasteWorker();
                return GLib.SOURCE_REMOVE;
            });
            return GLib.SOURCE_REMOVE;
        });
    }

    _typeStreamItem(item) {
        if (typeof Clutter.unicode_to_keysym !== 'function' || !this._virtualDevice) {
            this._streamWorkerActive = false;
            if (!this._streamPasteDeviceWarned) {
                this._streamPasteDeviceWarned = true;
                notifyErrorIfEnabled(_('Streaming typing unavailable'), _('Unicode key conversion or the virtual keyboard is unavailable; pending chunks were kept in memory.'));
            }
            return;
        }

        const characters = [];
        for (let offset = 0; offset < item.text.length;) {
            const codepoint = item.text.codePointAt(offset);
            characters.push(String.fromCodePoint(codepoint));
            offset += codepoint > 0xFFFF ? 2 : 1;
        }
        let index = 0;
        let failed = false;
        const sendChar = character => {
            let keyval;
            try {
                const codepoint = character.codePointAt(0);
                keyval = character === '\n' ? Clutter.KEY_Return
                    : character === '\t' ? Clutter.KEY_Tab
                        : Clutter.unicode_to_keysym(codepoint);
            } catch (error) {
                logError(error, 'bravoric-indicator: conversione tasto stream fallita');
                return false;
            }
            if (!Number.isInteger(keyval) || keyval === 0)
                return false;
            const down = this._sendKey(keyval, Clutter.KeyState.PRESSED);
            const up = this._sendKey(keyval, Clutter.KeyState.RELEASED);
            return down && up;
        };
        const tick = () => {
            this._streamTypeTimerId = null;
            if (this._cancellable.is_cancelled()) {
                this._streamWorkerActive = false;
                return GLib.SOURCE_REMOVE;
            }
            if (index < characters.length && !sendChar(characters[index++]))
                failed = true;
            if (failed) {
                this._streamPasteBlocked = true;
                this._streamWorkerActive = false;
                notifyErrorIfEnabled(_('Streaming typing incomplete'), _('A keystroke could not be sent. Check the focused field before restarting to avoid duplicates.'));
                return GLib.SOURCE_REMOVE;
            }
            if (index < characters.length) {
                this._streamTypeTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, TYPE_KEY_INTERVAL_MS, tick);
                return GLib.SOURCE_REMOVE;
            }
            this._commitStreamItem(item);
            const delay = this._pacingDelayMs();
            this._streamTypeTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
                this._streamTypeTimerId = null;
                this._streamWorkerActive = false;
                this._startStreamPasteWorker();
                return GLib.SOURCE_REMOVE;
            });
            return GLib.SOURCE_REMOVE;
        };
        this._streamTypeTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, TYPE_KEY_INTERVAL_MS, tick);
    }

    // I 4 rami di uscita per errore qui sotto restano ESPLICITI e non vengono
    // fusi in un helper: ognuno blocca la coda e deve azzerare da solo
    // _streamWorkerActive, il latch (tasto fallito ha un messaggio proprio;
    // "delete su scope invalido" e "azione sconosciuta" condividono oggi lo
    // stesso testo _('Invalid streaming command') — non sono 4 messaggi
    // distinti, sono 3: la differenza fra questi due casi non e' visibile
    // all'utente. Non e' un difetto da correggere qui: cambiare il testo e'
    // una scelta di prodotto (nuovo msgid, nuova traduzione IT), non un
    // cleanup. Questo commento descrive cosa il codice fa OGGI, non cosa
    // dovrebbe fare.
    // che _startStreamPasteWorker() controlla per non rientrare. Unificarli
    // sposterebbe dentro l'helper la lista dei blocchi, e il contatore di
    // test-timeout-logic.js (4 blocchi, ciascuno seguito dal reset prima del
    // return) smetterebbe di presidiare il ramo vero. Anche keyMap resta
    // qui dentro, ricostruita a ogni chiamata: le chiavi sono poche e il
    // costo e' nullo rispetto a un invio di tasti, mentre spostarla fuori
    // romperebbe i due ambienti di eval che ricevono solo Clutter, Main,
    // logError, computeStreamDelete, GLib e _ come variabili libere.
    _runStreamCommand(item) {
        const command = item.command || {};
        const keyMap = {
            Return: Clutter.KEY_Return, Enter: Clutter.KEY_Return, Tab: Clutter.KEY_Tab,
            space: Clutter.KEY_space, Escape: Clutter.KEY_Escape, BackSpace: Clutter.KEY_BackSpace,
            Delete: Clutter.KEY_Delete, Home: Clutter.KEY_Home, End: Clutter.KEY_End,
            Page_Up: Clutter.KEY_Page_Up, Page_Down: Clutter.KEY_Page_Down,
            Left: Clutter.KEY_Left, Right: Clutter.KEY_Right, Up: Clutter.KEY_Up, Down: Clutter.KEY_Down,
            ...Object.fromEntries(Array.from({length: 12}, (_, i) => [`F${i + 1}`, Clutter[`KEY_F${i + 1}`]])),
        };
        if (command.action === 'key' && keyMap[command.key] !== undefined) {
            const key = keyMap[command.key];
            const down = this._sendKey(key, Clutter.KeyState.PRESSED);
            const up = this._sendKey(key, Clutter.KeyState.RELEASED);
            // Ogni ramo di uscita per errore DEVE rilasciare _streamWorkerActive:
            // il flag e' il latch che _startStreamPasteWorker() controlla per non
            // rientrare, e senza questo reset un solo tasto fallito lo lasciava
            // True per tutta la sessione GNOME: la coda si svuotava in _streamQueue
            // ma il worker non ripartiva piu', nemmeno alla sessione successiva
            // (che azzera solo _streamPasteBlocked, vedi _onStreamStateChanged).
            if (!down || !up) {
                this._streamPasteBlocked = true;
                this._streamWorkerActive = false;
                notifyErrorIfEnabled(_('Streaming command failed'),
                    _('The key command could not be completed; the queue is blocked.'));
                return;
            }
            // Tasto DISTRUTTIVO: il testo e' sparito davvero dal campo, ma il
            // modello _streamSegments — che rappresenta cosa c'e' nel campo —
            // restava invariato e il file di contesto non veniva riscritto. Il
            // backend leggeva quindi come "presenti nel campo" parole appena
            // cancellate. Difetto misurato per esecuzione (DIF-1).
            //
            // Il conteggio dei caratteri NON e' reinventato qui: si riusa
            // computeStreamDelete, che e' gia' risolto e testato. Sul modello il
            // tasto distruttivo vale una cancellazione di CHUNK, che e' il
            // default di scope: il numero di caratteri che un singolo
            // BackSpace cancella dipende dallo stato del campo focus, che qui
            // non e' conosciuto, quindi trattarlo come chunk e' l'unica
            // approssimazione onesta. Nota che NON si inviano altri tasti: il
            // tasto e' gia' stato premuto e rilasciato sopra.
            if (command.key === 'BackSpace' || command.key === 'Delete') {
                const erased = computeStreamDelete(this._streamSegments || [], 'chunk');
                this._streamSegments = erased.segments;
                this._writeStreamLiveText();
            }
        } else if (command.action === 'delete') {
            const result = computeStreamDelete(this._streamSegments || [], command.scope);
            if (result.count === -1) {
                this._streamPasteBlocked = true;
                this._streamWorkerActive = false;
                notifyErrorIfEnabled(_('Invalid streaming command'), _('The configured key or delete scope is invalid; the queue is blocked.'));
                return;
            }
            if (result.count > 0) {
                for (let count = 0; count < result.count; count++) {
                    const pressed = this._sendKey(Clutter.KEY_BackSpace, Clutter.KeyState.PRESSED);
                    const released = this._sendKey(Clutter.KEY_BackSpace, Clutter.KeyState.RELEASED);
                    if (!pressed || !released) {
                        this._streamSegments = null;
                        this._streamPasteBlocked = true;
                        this._streamWorkerActive = false;
                        notifyErrorIfEnabled(_('Streaming delete failed'), _('Deletion was partial; restart the extension to avoid deleting the wrong text.'));
                        return;
                    }
                }
            }
            this._streamSegments = result.segments;
            this._writeStreamLiveText();
        } else {
            this._streamPasteBlocked = true;
            this._streamWorkerActive = false;
            notifyErrorIfEnabled(_('Invalid streaming command'), _('The configured key or delete scope is invalid; the queue is blocked.'));
            return;
        }
        this._streamQueue.shift();
        if (command.ends_session) this._requestStreamEnd(item.sessionId);
        this._streamWorkerActive = false;
        this._startStreamPasteWorker();
    }

    _requestStreamEnd(sessionId) {
        // Latch per sessione: serve a non armare DUE timer di chiusura per la
        // stessa sessione (il comando puo' arrivare da piu' code/rerun). Non e'
        // pero' un blocco definitivo: STREAM_END_RETRY_MS sotto fa ritentare,
        // quindi un primo tentativo a vuoto non blocca la sessione per sempre.
        if (this._streamEndRequested === sessionId) return;
        this._streamEndRequested = sessionId;
        const deadline = Date.now() + STREAM_END_TIMEOUT_MS;
        // Latch rilasciato a ogni uscita TERMINALE di check(): senza questo il
        // latch restava armato per tutta la sessione GNOME e un secondo
        // comando di fine sessione (o un ritento) non faceva NULLA. Con
        // `stop` idempotente un ritento non e' pericoloso — e' esattamente
        // cio' che serve quando il primo tentativo e' partito a vuoto.
        const finish = () => {
            if (this._streamEndRequested === sessionId)
                this._streamEndRequested = null;
            return GLib.SOURCE_REMOVE;
        };
        const check = () => {
            // Il timer che ha invocato check è già scaduto: azzera il riferimento
            // prima di un eventuale ri-scheduling, così destroy() non tenta di
            // rimuovere un id non più valido.
            this._streamEndTimerId = null;
            if (this._cancellable.is_cancelled())
                return GLib.SOURCE_REMOVE;
            if (this._streamQueue.length || this._streamWorkerActive) {
                // DICHIARATO (giro 1, nota del reviewer su questo re-arm): la
                // coda BLOCCATA non si svuota da sola — _startStreamPasteWorker()
                // esce subito su _streamPasteBlocked — quindi la condizione qui
                // sopra restava vera per sempre e il timer si ri-riprogrammava
                // ogni 100 ms (10 sveglia al secondo) fino a destroy(): lavoro
                // inutile perpetuo, e la fine sessione non poteva comunque
                // partire perche' dipende dal drenaaggio della coda. Con la
                // coda bloccata si rinuncia e si lascia la traccia nel log:
                // l'errore e' gia' stato notificato all'utente.
                if (this._streamPasteBlocked) {
                    logError(new Error(`fine sessione stream ${sessionId} abbandonata: coda bloccata da un errore di invio, attesa infinita`),
                        'bravoric-indicator: stream end abbandonato');
                    return finish();
                }
                if (Date.now() > deadline) {
                    // Difetto segnalato dall'utente: la coda non si svuota (per
                    // es. un chunk che attende un endpoint lento) e senza questo
                    // tetto il pallino ROSSO restava acceso perche' la sessione
                    // non veniva mai chiusa. Meglio chiudere con quello che c'e'
                    // che restare appesi: la coda residua viene scartata dal
                    // cambio di sessione (_onStreamStateChanged).
                    logError(new Error(`fine sessione stream ${sessionId}: coda non svuotata entro ${STREAM_END_TIMEOUT_MS} ms, chiusura forzata`),
                        'bravoric-indicator: stream end forzato');
                    this._streamQueue = [];
                    this._streamEndTimerId = null;
                    spawnBackground('bravoric-stream-toggle', 'stop');
                    return finish();
                }
                this._streamEndTimerId = GLib.timeout_add(
                    GLib.PRIORITY_DEFAULT, STREAM_END_POLL_MS,
                    () => { check(); return GLib.SOURCE_REMOVE; });
                return GLib.SOURCE_REMOVE;
            }
            // DIFETTO SEGNALATO (pallino rosso che resta). Prima qui la
            // condizione era `state.session_id === sessionId && state.active
            // === true`: se la lettura di stream_state.json era obsoleta
            // (scrittura non ancora visibile, o il supervisore che aveva
            // gia' scritto active=false) il toggle NON partiva e si tornava
            // in silenzio: nessun log, nessuna notifica, e il pallino restava
            // acceso perche' il backend non scriveva mai IDLE.
            //
            // Il toggle semplice non e' una strada: `bravoric-stream-toggle`
            // SENZA argomenti e' un toggle vero e proprio, e con nessuna
            // sessione viva AVVIA una nuova registrazione invece di chiudere
            // (misurato in stream_toggle_main: rc 0, lock creato,
            // state.active True). Spararlo qui avrebbe riaperto il
            // microfono invece di chiuderlo.
            //
            // Si usa quindi il sottocomando `stop`, che e' idempotente: chiude
            // la sessione se c'e', e se non c'e' esce 0 senza fare nulla. La
            // verifica sul session_id resta, ma serve solo a NON chiudere una
            // sessione DIVERSA (un'altra sessione puo' essere partita nel
            // frattempo): non e' piu' una condizione di partenza.
            try {
                const [, data] = GLib.file_get_contents(STREAM_STATE_PATH);
                const state = JSON.parse(new TextDecoder().decode(data));
                if (state.session_id !== sessionId) {
                    // Sessione gia' cambiata: chiudere quella sarebbe dannoso.
                    // Non e' un fallimento: non e' piu' la nostra da chiudere.
                    logError(new Error(`sessione ${sessionId} non piu' attiva (ora ${state.session_id}): nessuna chiusura necessaria`),
                        'bravoric-indicator: stream end già avvenuto');
                    return finish();
                }
                spawnBackground('bravoric-stream-toggle', 'stop');
            } catch (e) {
                // Lettura dello stato impossibile: NON si torna in silenzio.
                // Si tenta comunque la chiusura — `stop` e' idempotente e
                // sicuro anche se non sappiamo lo stato — e si lascia la traccia.
                logError(e, 'stream end state verification');
                spawnBackground('bravoric-stream-toggle', 'stop');
            }
            return finish();
        };
        this._streamEndTimerId = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT, STREAM_END_POLL_MS,
            () => { check(); return GLib.SOURCE_REMOVE; });
    }

    _startVirtualDevice() {
        // Tastiera virtuale Clutter per inviare Ctrl+V (nessun tool esterno
        // come xdotool/ydotool: non presenti sul sistema, verificato).
        try {
            const seat = Clutter.get_default_backend().get_default_seat();
            this._virtualDevice = seat.create_virtual_device(
                Clutter.InputDeviceType.KEYBOARD_DEVICE);
        } catch (e) {
            logError(e, 'bravoric-indicator: impossibile creare tastiera virtuale');
            this._virtualDevice = null;
        }
    }

    // Giro 2 (B2-frontend-B): get_current_event_time() restituisce i
    // MILLISECONDI, notify_keyval si aspetta i MICROSECONDI. Le tre reference
    // del progetto stesso (docs/ROADMAP-CHUNK.md:41-45,
    // clipboard-indicator/keyboard.js:24, emoji-copy:304) scrivono tutti
    // "* 1000": la prescrizione era gia' scritta e non era stata applicata.
    // Impatto non misurabile senza Mutter dal vivo: qui si corregge solo
    // l'unita' dichiarata dall'API. Il commento sta SOPRA la funzione, non
    // dentro: il corpo deve restare compatto, il gate ci misura la distanza
    // fra i due return per verificare che il fallimento venga segnalato.
    _sendKey(keyval, state) {
        if (!this._virtualDevice)
            return false;
        try {
            this._virtualDevice.notify_keyval(
                Clutter.get_current_event_time() * EVENT_TIME_MS_TO_US, keyval, state);
            return true;
        } catch (e) {
            logError(e, 'bravoric-indicator: errore invio tasto');
            return false;
        }
    }

    // Scrive il testo che l'utente ha davvero nel campo, cosi' il backend puo'
    // usarlo come contesto. Scrittura atomica: il backend puo' leggere in
    // qualsiasi momento e trovare sempre un JSON valido, mai mezzo scritto.
    // _streamSegments null = buffer sconosciuto (dopo un reload): in quel caso
    // non scriviamo nulla, cosi' il backend ripiega sulla sua approssimazione.
    //
    // Giro 2 (C2): il file dichiara "solo cio' che l'utente ha davanti", e il
    // backend lo consuma in get_context_snapshot per costruire il prompt della
    // trascrizione successiva. _streamSegments accoglieva anche segmenti NON
    // consegnati, perche' un chunk riconsegnato dalla coda bloccata finiva
    // dentro l'elenco al cambio di sessione (misurato dal reviewer) e il
    // prompt successivo riceveva frasi mai uscite dalla bocca dell'utente. Il
    // push dei segmenti avviene solo dopo l'invio riuscito, in entrambi i rami
    // (clipboard e type): qui il file registra quello che c'e', senza filtro.
    _writeStreamLiveText() {
        if (!this._streamSessionId)
            return;
        const segments = this._streamSegments;
        if (!Array.isArray(segments))
            return;
        const file = Gio.File.new_for_path(STREAM_LIVE_TEXT_PATH);
        const payload = JSON.stringify({
            session_id: this._streamSessionId,
            segments,
            updated_at: Date.now() / 1000,
        });
        try {
            file.replace_contents_async(new TextEncoder().encode(payload), null, false,
                // PRIVATE: il file contiene il testo dettato in tempo reale; senza,
                // GIO lo crea con l'umask (0644) e ogni utente locale lo legge.
                // Gli altri file di ~/.cache/bravoric-stt-clipboard sono 0600.
                Gio.FileCreateFlags.REPLACE_DESTINATION | Gio.FileCreateFlags.PRIVATE, null, (source, result) => {
                    try {
                        source.replace_contents_finish(result);
                    } catch (e) {
                        // Il file di contesto e' un miglioramento: se la scrittura
                        // fallisce il backend usa last_chunks, nessun errore utente.
                        logError(e, 'bravoric-indicator: scrittura contesto vivo fallita');
                    }
                });
        } catch (e) {
            logError(e, 'bravoric-indicator: scrittura contesto vivo fallita');
        }
    }

    _refreshStatus() {
        // Generation token: il timer periodico (30s) chiama _refreshStatus
        // direttamente, senza passare dal debounce del file monitor, quindi
        // due load_contents_async possono essere in volo insieme. Senza
        // questo controllo, una risposta più vecchia che completa dopo una
        // più recente sovrascrive l'icona/label con dati stale.
        this._statusRefreshGen += 1;
        const gen = this._statusRefreshGen;
        const file = Gio.File.new_for_path(STATUS_PATH);
        file.load_contents_async(this._cancellable, (source, result) => {
            if (this._cancellable.is_cancelled())
                return; // estensione disabilitata mentre la lettura era in corso
            if (gen !== this._statusRefreshGen)
                return; // superata da una lettura più recente

            let contents;
            try {
                [, contents] = source.load_contents_finish(result);
            } catch {
                return; // status file non ancora creato
            }
            try {
                const data = JSON.parse(new TextDecoder().decode(contents));
                // B4: valida la forma di data prima di accedere a data.state.
                if (!data || typeof data !== 'object' || Array.isArray(data))
                    throw new Error('status.json: forma inattesa');
                if (this._statusParseErrors >= 3)
                    notifyStatusIfEnabled(_('Status file restored'), _('OK'));
                this._statusParseErrors = 0;

                const reportedState = data.state && THEME_ICONS[data.state] ? data.state : 'idle';
                let state = reportedState;
                const limit = this._timeoutLimitFor(reportedState, data.service);
                if (limit && Number.isFinite(data.timestamp)) {
                    const ageSeconds = Date.now() / 1000 - data.timestamp;
                    if (ageSeconds > limit) {
                        state = 'idle';
                        if (!this._timeoutWarned) {
                            this._timeoutWarned = true;
                            notifyStatusIfEnabled(this._timeoutMessage(reportedState), _('Check the backend'));
                        }
                    } else {
                        // stato attivo e recente: pronto a riavvisare se si
                        // dovesse bloccare di nuovo più avanti
                        this._timeoutWarned = false;
                    }
                } else {
                    this._timeoutWarned = false;
                }
                this._icon.gicon = state === 'idle' ? this._idleGicon() : Gio.ThemedIcon.new(THEME_ICONS[state]);
                this._setRecordingBlink(state === 'recording');
                this._setAccessibleState(state);

                // Una sola cattura alla volta: durante recording/processing le
                // voci di avvio sono disabilitate (evita race su audio/clipboard).
                // B3: in stato error l'utente deve poter riprovare (la scorciatoia
                // funziona già, ma le voci menu restano disabilitate per 5 minuti).
                const canStart = state === 'idle' || state === 'error';
                this._dictationItem.setSensitive(canStart);
                this._ocrItem.setSensitive(canStart);
                // Giro 3 (F7): anche la voce Streaming è un avvio di cattura,
                // quindi la stessa guardia delle altre due. Prima non riceveva
                // mai setSensitive: restava cliccabile durante recording/
                // processing e il click partiva a vuoto (bravoric-stream-toggle
                // rispondeva False senza mostrare nulla). Le tre voci si
                // abilitano e disabilitano insieme.
                this._streamItem.setSensitive(canStart);
                if (state === 'processing' && data.service) {
                    this._lastOutputItem.setSensitive(false);
                    this._lastOutputItem.label.text = data.service === 'stt'
                        ? _('STT: transcribing…') : _('OCR: extracting text…');
                } else if (data.last_output) {
                    this._lastOutputText = data.last_output;
                    this._lastOutputItem.setSensitive(true);
                    const preview = data.last_output.slice(0, LAST_OUTPUT_PREVIEW_CHARS);
                    this._lastOutputItem.label.text = _('Last output: %s').replace('%s', preview);
                } else {
                    // Errore/stop senza output (es. API giù): senza questo ramo la
                    // voce resterebbe bloccata su "transcribing…" e disabilitata,
                    // nascondendo l'ultimo output valido già copiabile.
                    const preview = this._lastOutputText
                        ? this._lastOutputText.slice(0, LAST_OUTPUT_PREVIEW_CHARS) : null;
                    this._lastOutputItem.setSensitive(preview !== null);
                    this._lastOutputItem.label.text = preview !== null
                        ? _('Last output: %s').replace('%s', preview)
                        : _('Last output: (none)');
                }
            } catch (e) {
                logError(e, 'bravoric-indicator: status.json malformato');
                this._statusParseErrors = (this._statusParseErrors || 0) + 1;
                if (this._statusParseErrors === 3) {
                    notifyErrorIfEnabled(_('Status file error'), _('Check config.toml'));
                }
            }
        });
    }

    destroy() {
        this._cancellable.cancel();
        if (this._virtualDevice) {
            // G32: il device e' un GObject creato dal seat e agganciato a lui
            // per tutta la sessione: senza run_dispose resta registrato nel
            // backend di Clutter anche dopo che l'estensione e' stata spenta e
            // riaccesa (disable/enable, sblocco schermo, reload della shell),
            // accumulando device morti che mantengono la tastiera attiva. Non
            // c'e' destroy() alterno: questo e' l'unico punto in cui la
            // proprietaria del device viene a sapere che puo' liberarlo.
            this._virtualDevice.run_dispose();
            this._virtualDevice = null;
        }
        if (this._streamMonitor) {
            if (this._streamMonitorId)
                this._streamMonitor.disconnect(this._streamMonitorId);
            this._streamMonitor.cancel();
            this._streamMonitor = null;
        }
        if (this._streamDebounceId) {
            GLib.source_remove(this._streamDebounceId);
            this._streamDebounceId = null;
        }
        if (this._streamPasteTimerId) {
            GLib.source_remove(this._streamPasteTimerId);
            this._streamPasteTimerId = null;
        }
        if (this._streamTypeTimerId) {
            GLib.source_remove(this._streamTypeTimerId);
            this._streamTypeTimerId = null;
        }
        if (this._streamEndTimerId) {
            GLib.source_remove(this._streamEndTimerId);
            this._streamEndTimerId = null;
        }
        this._streamQueue = [];
        if (this._monitor) {
            if (this._monitorId)
                this._monitor.disconnect(this._monitorId);
            // B7: cancel() rilascia inotify/fanotify; senza questo c'è un leak
            // ad ogni ciclo disable/enable.
            this._monitor.cancel();
            this._monitor = null;
        }
        if (this._blinkTimeoutId) {
            GLib.source_remove(this._blinkTimeoutId);
            this._blinkTimeoutId = null;
        }
        if (this._refreshDebounceId) {
            GLib.source_remove(this._refreshDebounceId);
            this._refreshDebounceId = null;
        }
        if (this._timeoutCheckId) {
            GLib.source_remove(this._timeoutCheckId);
            this._timeoutCheckId = null;
        }
        super.destroy();
    }
});

export default class BravoricIndicatorExtension extends Extension {
    enable() {
        // Prima dell'indicatore: gia' la sua costruzione puo' notificare.
        this._settings = this.getSettings();
        notificationSettings = this._settings;

        this._indicator = new BravoricIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);

        this._registeredKeybindings = new Set();

        // Un gschemas.compiled stantio non deve mai arrivare a
        // Main.wm.addKeybinding(): Mutter tratta una chiave assente come
        // assertion fatale e può terminare l'intera sessione GNOME. È il caso
        // reale osservato al login dopo l'aggiunta di stream-shortcut.
        const bindings = [
            ['dictation-shortcut', 'bravoric-stt-toggle'],
            ['ocr-shortcut', 'bravoric-ocr-capture'],
            ['stream-shortcut', 'bravoric-stream-toggle'],
        ];
        const schema = this._settings.settings_schema;
        for (const [name, command] of bindings) {
            if (!schema?.has_key(name)) {
                console.error(`bravoric-indicator: schema GSettings privo di ${name}; scorciatoia ignorata`);
                continue;
            }
            Main.wm.addKeybinding(
                name, this._settings,
                Meta.KeyBindingFlags.IGNORE_AUTOREPEAT, Shell.ActionMode.ALL,
                () => spawnBackground(command),
            );
            this._registeredKeybindings.add(name);
        }
    }

    disable() {
        for (const name of this._registeredKeybindings ?? [])
            Main.wm.removeKeybinding(name);
        this._registeredKeybindings = null;
        this._settings = null;
        notificationSettings = null;

        // Cancellable/monitor/timer vivono su BravoricIndicator (this._indicator),
        // non su questa classe Extension: BravoricIndicator.destroy() li ripulisce
        // gia' (cancella _cancellable, disconnette e cancella _monitor e
        // _streamMonitor, rimuove i source dei cinque timer/debounce e azzera
        // ogni campo dopo averlo usato). Il riferimento e' al METODO e ai
        // campi che legge, non alle righe: un numero di riga muore a ogni
        // estrazione in modulo, ed e' gia' morto una volta qui.
        // Giro 13: rimosso qui un cleanup duplicato che
        // operava sui campi omonimi di `this` (Extension), sempre undefined
        // — dead code silenzioso, nessun leak reale ma fuorviante da leggere.
        this._indicator?.destroy();
        this._indicator = null;
    }
}
