import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';
import Gdk from 'gi://Gdk';
import GLib from 'gi://GLib';
import { matchBrace } from './lib/brace-match.mjs';

Adw.init();

console.log('Smoke test GTK4/Adw per Voice Commands UI (+ cattura tasto)...');

// Whitelist identica a COMMAND_KEYS del backend (drift guardata dal test
// node test-prefs-voice-commands.js) e alias espliciti, come in prefs.js.
const COMMAND_KEYS = [
    'Return', 'Enter', 'Tab', 'space', 'Escape', 'BackSpace', 'Delete',
    'Home', 'End', 'Page_Up', 'Page_Down', 'Left', 'Right', 'Up', 'Down',
    ...Array.from({ length: 12 }, (_, i) => `F${i + 1}`),
];
const COMMAND_KEY_SET = new Set(COMMAND_KEYS);
const CAPTURE_KEY_ALIASES = {
    KP_Space: 'space', KP_Enter: 'Return', ISO_Left_Tab: 'Tab',
    KP_Home: 'Home', KP_End: 'End', KP_Page_Up: 'Page_Up', KP_Next: 'Page_Down', KP_Page_Down: 'Page_Down',
    KP_Up: 'Up', KP_Down: 'Down', KP_Left: 'Left', KP_Right: 'Right',
};

// Mock finestra: raccoglie i toast di feedback (window.add_toast in prefs.js).
const toasts = [];
const window = { add_toast: toast => toasts.push(toast.title) };

const group = new Adw.PreferencesGroup({ title: 'Voice commands' });

let commandRows = [];
// Il serializer non e' piu' ricopiato qui: saveCommands chiama la funzione
// REALE estratta da prefs.js piu' in basso (serializeCommandRows), cosi'
// questo smoke test presidia il prodotto e non una sua ricostruzione.
// debounce resta una copia di test: e' infrastruttura del test, non logica di
// prodotto, e mantenerla qui evita di iniettare GLib e _debounceFns.
let savedCommands = null;
let saveCount = 0;

const saveCommands = () => {
    saveCount++;
    savedCommands = serializeCommandRows(commandRows);
};

function debounce(fn) {
    let timeoutId = null;
    const debounced = (...args) => {
        if (timeoutId)
            GLib.source_remove(timeoutId);
        timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 400, () => {
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
    return debounced;
}
const debouncedSaveCommands = debounce(saveCommands);


// --- F4 (giro 2): qui NON c'e' piu' una copia scritta a mano di addCommandRow.
// Prima questo file ne manteneva una copia (176 righe) che poteva divergere dal
// prodotto senza che nessun controllo lo notasse: il reviewer lo ha dimostrato
// con una mutazione di prefs.js, la suite restava VERDE. Adesso la funzione
// REALE viene estratta da prefs.js a runtime ed eseguita: la copia non esiste,
// quindi non puo' divergere.
//
// prefs.js non e' importabile qui (importa la risorsa di GNOME Shell), ma il
// codice di addCommandRow e' JavaScript puro: si estrae per brace-matching e si
// valuta in questo modulo, dove tutte le dipendenze sono gia' definite.
const PREFS_PATH = GLib.build_filenamev([
    GLib.get_current_dir(), 'gnome-extension', 'bravoric-indicator@local', 'prefs.js',
]);
const [prefsOk, prefsBytes] = GLib.file_get_contents(PREFS_PATH);
if (!prefsOk)
    throw new Error(`prefs.js non leggibile: ${PREFS_PATH}`);
const prefsSrc = new TextDecoder().decode(prefsBytes);

function bodyOf(src, signature) {
    const at = src.indexOf(signature);
    if (at === -1)
        throw new Error(`${signature} non trovato in prefs.js`);
    const open = src.indexOf('{', at);
    const end = matchBrace(src, open);
    if (end === -1)
        throw new Error(`graffe non bilanciate in ${signature}`);
    return src.slice(open, end + 1);
}

// Serializer REALE estratto da prefs.js: funzione pura a livello di modulo,
// senza Adw/GLib/gettext, quindi si esegue da sola e non ha bisogno di stub.
// Non e' piu' una copia: saveCommands chiama questa, quindi il smoke test
// presidia il prodotto. Prima la copia poteva divergere in silenzio.
// Si usa lo stesso escavalcappe di addCommandRow (IIFE che ritorna il
// binding): in un modulo, che e' strict mode, una function declaration
// dentro eval() NON esce dal proprio scope e resterebbe non raggiungibile.
const serializeCommandRows = (() => {
    const declared = 'function serializeCommandRows(rows) ';
    if (!prefsSrc.includes(declared))
        throw new Error('serializeCommandRows non trovato in prefs.js');
    // eslint-disable-next-line no-eval
    return eval(`(() => { ${declared}${bodyOf(prefsSrc, declared)}; return serializeCommandRows; })()`);
})();
if (typeof serializeCommandRows !== 'function')
    throw new Error('l\'estrazione di serializeCommandRows non ha prodotto una funzione');

// addCommandRow + gli helper che contiene (commit/start/stopCapture) si
// sostituiscono in blocco unico: presi insieme formano il codice che il test
// esercita davvero.
// Il gettext e' neutro qui (nessuna sessione Shell): _() resta identita', e i
// default dei parametri sono gia' materializzati in prefs.js.
// bodyOf ritorna il blocco a partire da '{': va riaggiunta la testata della
// dichiarazione, altrimenti l'eval riceve un blocco orfano.
// gettext neutro: questo smoke test gira fuori dal contesto di una sessione
// Shell, quindi _() e' l'identita' (le stringhe restano in inglese, come
// prima della sostituzione della copia).
const _ = text => text;
// In prefs.js la riga vive dentro un gruppo "Voice commands": questo smoke
// test usa il proprio 'group', che fa la stessa cosa (appendere la riga).
const commandGroup = group;
const realBlock = `const addCommandRow = command => ${bodyOf(prefsSrc, 'const addCommandRow = ')}`;
// In prefs.js la funzione costruisce `item` e lo mette in commandRows, ma
// `item` non espone il controller di cattura. I test lo pilotano, quindi qui si
// ricava un handle EQUIVALENTE dalla riga restituita: il controller e' quello
// agganciato davvero (key.get_controllers()), e lo stato di cattura si deduce
// dai widget che la funzione stessa modifica (icona del bottone, visibilita'
// del cancel, valore della chiave). Niente dentro prefs.js: il prodotto non
// viene toccato perche' il test possa osservarlo.
// I widget della cattura (controller, bottone di registrazione, bottone annulla)
// sono creati dentro addCommandRow e restano locali: la funzione restituita non
// li espone, e GTK4 non ha un API per rileggerli da un widget. Si intercettano
// al momento in cui prefs.js li aggancia, quindi sono esattamente gli oggetti
// veri, usati dal prodotto. Nessuna modifica a prefs.js.
function captureHandleOf(item, seen) {
    return {
        controller: seen.controllers.find(c => c instanceof Gtk.EventControllerKey),
        isCapturing: () => seen.buttons.some(b => b?.icon_name === 'media-record-symbolic'),
        start: () => seen.buttons.find(b => b?.icon_name === 'input-keyboard-symbolic')?.emit('clicked'),
        stop: () => seen.buttons.find(b => b?.icon_name === 'input-keyboard-symbolic')?.emit('clicked'),
        captureBtn: seen.buttons.find(b => b?.icon_name === 'input-keyboard-symbolic'),
        cancelBtn: seen.buttons.find(b => b?.icon_name === 'process-stop-symbolic'),
    };
}

const evaluated = eval(`(() => { ${realBlock}; return { addCommandRow }; })()`);
const realAddCommandRow = evaluated.addCommandRow;
const addCommandRow = (command) => {
    const before = commandRows.length;
    const seen = { controllers: [], buttons: [] };
    // Intercetta solo per la durata della chiamata: gli oggetti sono creati li'.
    const origAddController = Gtk.Widget.prototype.add_controller;
    const origAddSuffix = Adw.EntryRow.prototype.add_suffix;
    Gtk.Widget.prototype.add_controller = function (controller) {
        seen.controllers.push(controller);
        return origAddController.call(this, controller);
    };
    Adw.EntryRow.prototype.add_suffix = function (suffix) {
        seen.buttons.push(suffix);
        return origAddSuffix.call(this, suffix);
    };
    try {
        realAddCommandRow(command);
    } finally {
        Gtk.Widget.prototype.add_controller = origAddController;
        Adw.EntryRow.prototype.add_suffix = origAddSuffix;
    }
    const item = commandRows[before];
    item.capture = captureHandleOf(item, seen);
    return item;
};
console.log('addCommandRow REALE estratto da prefs.js e in esecuzione');

// 1. Aggiunta comando vuoto (come click su Add command)
addCommandRow(null);
if (commandRows.length !== 1) {
    throw new Error('Riga non aggiunta!');
}
const first = commandRows[0];
if (!first.expander.expanded) {
    throw new Error('ExpanderRow deve essere espanso per un nuovo comando!');
}
if (!first.key.visible || first.scope.visible) {
    throw new Error('Visibilità iniziale errata (key deve essere visibile, scope nascosto)');
}

// 2. Modifica keyword e action
first.keyword.text = 'cancella tutto';
first.aliases.text = 'elimina tutto, wipe';
first.action.selected = 1; // Delete text
if (first.key.visible || !first.scope.visible) {
    throw new Error('Visibilità errata dopo selezione azione Delete');
}
first.scope.selected = 1;
first.ends.active = true;

if (!savedCommands || savedCommands.length !== 1) {
    throw new Error('Salvataggio fallito o incompleto');
}
const cmd = savedCommands[0];
if (cmd.keyword !== 'cancella tutto' || !Array.isArray(cmd.aliases) || cmd.aliases.length !== 2 || cmd.action !== 'delete' || cmd.key !== '' || cmd.scope !== 'chunk' || cmd.ends_session !== true) {
    throw new Error('Dati salvati non corrispondenti: ' + JSON.stringify(cmd));
}

// 3. Cattura tasto: whitelist, alias, feedback, un solo salvataggio.
addCommandRow({ keyword: 'a capo', action: 'key', key: 'Return' });
const cap = commandRows[1];
if (!cap.key.visible || cap.capture.isCapturing()) {
    throw new Error('Stato iniziale cattura errato');
}
if (cap.capture.controller === undefined) {
    throw new Error('Controller di cattura non agganciato');
}
if (typeof cap.capture.controller.set_propagation_phase !== 'function') {
    throw new Error('EventControllerKey non disponibile');
}
const savedBefore = saveCount;

// 3a. Fuori cattura: il controller propaga (false) e non salva.
if (cap.capture.controller.emit('key-pressed', Gdk.KEY_Tab, 0, 0) !== false) {
    throw new Error('Fuori cattura il controller deve propagare (false)');
}
if (cap.capture.isCapturing() || saveCount !== savedBefore) {
    throw new Error('Fuori cattura non deve cambiare stato né salvare');
}

// 3b. Avvio cattura.
cap.capture.start();
if (!cap.capture.isCapturing() || !cap.capture.cancelBtn.visible) {
    throw new Error('startCapture non attivo o bottone Cancel non visibile');
}

// 3c. Modificatori puri: ignorati, si resta in cattura, nessun toast/salvataggio.
const t0 = toasts.length;
cap.capture.controller.emit('key-pressed', Gdk.KEY_Shift_L, 0, 0);
cap.capture.controller.emit('key-pressed', Gdk.KEY_Control_L, 0, 0);
cap.capture.controller.emit('key-pressed', Gdk.KEY_Caps_Lock, 0, Gdk.ModifierType.LOCK_MASK);
if (!cap.capture.isCapturing() || toasts.length !== t0 || saveCount !== savedBefore) {
    throw new Error('I modificatori puri devono essere ignorati restando in cattura');
}

// 3d. Tasto non mappato: feedback, nessun commit, si resta in cattura.
cap.capture.controller.emit('key-pressed', Gdk.KEY_a, 0, 0);
if (!cap.capture.isCapturing() || cap.key.text !== 'Return') {
    throw new Error('Un tasto non mappato non deve fare commit');
}
if (toasts[toasts.length - 1] !== 'Key not supported for voice commands') {
    throw new Error('Feedback mancante per tasto non mappato');
}

// 3e. Accordo con modificatore reale: rifiutato con feedback, nessun commit.
cap.capture.controller.emit('key-pressed', Gdk.KEY_Return, 0, Gtk.accelerator_get_default_mod_mask());
if (!cap.capture.isCapturing() || cap.key.text !== 'Return' || saveCount !== savedBefore) {
    throw new Error('Un accordo modificato non deve fare commit');
}
if (toasts[toasts.length - 1] !== 'Modifier combinations are not supported; press a single key') {
    throw new Error('Feedback mancante per accordo modificato');
}

// 3f. Return valido: commit + un solo salvataggio.
cap.capture.controller.emit('key-pressed', Gdk.KEY_Return, 0, 0);
if (cap.capture.isCapturing() || cap.key.text !== 'Return') {
    throw new Error('Return non catturato');
}
if (saveCount !== savedBefore + 1) {
    throw new Error('La cattura deve salvare esattamente una volta (delta ' + (saveCount - savedBefore) + ')');
}
if (!savedCommands.find(c => c.keyword === 'a capo' && c.key === 'Return')) {
    throw new Error('Valore catturato non serializzato');
}

// 3g. Escape resta catturabile (l'annullamento ha un percorso separato: Cancel).
cap.capture.start();
cap.capture.controller.emit('key-pressed', Gdk.KEY_Escape, 0, 0);
if (cap.capture.isCapturing() || cap.key.text !== 'Escape') {
    throw new Error('Escape deve essere catturabile come "Escape"');
}
if (!savedCommands.find(c => c.keyword === 'a capo' && c.key === 'Escape')) {
    throw new Error('Escape non serializzato');
}

// 3h. Alias keypad / ISO_Left_Tab -> stringa backend esatta.
for (const [keyval, expected] of [
    [Gdk.KEY_KP_Enter, 'Return'],
    [Gdk.KEY_KP_Space, 'space'],
    [Gdk.KEY_ISO_Left_Tab, 'Tab'],
    [Gdk.KEY_KP_Home, 'Home'],
    [Gdk.KEY_KP_End, 'End'],
    [Gdk.KEY_KP_Page_Up, 'Page_Up'],
    [Gdk.KEY_KP_Page_Down, 'Page_Down'],
    [Gdk.KEY_KP_Up, 'Up'],
    [Gdk.KEY_KP_Down, 'Down'],
    [Gdk.KEY_KP_Left, 'Left'],
    [Gdk.KEY_KP_Right, 'Right'],
]) {
    cap.capture.start();
    cap.capture.controller.emit('key-pressed', keyval, 0, 0);
    if (cap.key.text !== expected) {
        throw new Error(`Alias keyval ${keyval} -> ${cap.key.text}, atteso ${expected}`);
    }
}

// 3i. Cancel esplicito: nessun commit, valore invariato.
cap.key.text = 'Tab';
cap.capture.start();
const beforeCancel = saveCount;
cap.capture.cancelBtn.emit('clicked');
if (cap.capture.isCapturing() || cap.key.text !== 'Tab' || saveCount !== beforeCancel) {
    throw new Error('Cancel non deve modificare il valore né salvare');
}

// 3j. CapsLock/NumLock non sono "modificatori reali": il tasto resta catturabile.
cap.capture.start();
cap.capture.controller.emit('key-pressed', Gdk.KEY_Return, 0, Gdk.ModifierType.LOCK_MASK);
if (cap.capture.isCapturing() || cap.key.text !== 'Return') {
    throw new Error('CapsLock non deve bloccare la cattura');
}

// 3k. Passando ad azione Delete la cattura si chiude.
cap.capture.start();
cap.action.selected = 1; // notify::selected -> updateVisibility -> stopCapture
if (cap.capture.isCapturing()) {
    throw new Error('La cattura deve chiudersi passando a Delete');
}

console.log('Smoke test GTK4/Adw PASS: riga, segnali, visibilità, cattura tasto (controller, alias, feedback, un solo salvataggio) senza errori!');

// --- CRASH prefs.js: GLib.markup_escape_text con un argomento solo ------
// Giro 2 (F3) aveva protetto gli angoli della description del gruppo Shortcuts
// con GLib.markup_escape_text(...) passandogli UN SOLO argomento. La firma GJS
// e' (text, length_bytes): la chiamata solleva TypeError e il dialogo Preferenze
// non si apre PIU'. Difetto estetico (etichetta vuota) -> schello totale, e il
// gate restava verde: il test qui sotto esercita la costruzione REALE del gruppo
// estratta da prefs.js, quindi il crash non puo' tornare senza che il gate cada.
(function regressionMarkupEscapeArgs() {
    // La firma va verificata per esecuzione, non per lettura: senza questa
    // asserzione un futuro GJS potrebbe accettare l'argomento facoltativo e il
    // test continuerebbe a pretendere il -1 senza che serva.
    let oneArgThrew = null;
    try {
        GLib.markup_escape_text('Format: <Alt><Super>r (x)');
    } catch (e) {
        oneArgThrew = e;
    }
    if (!(oneArgThrew instanceof TypeError)) {
        throw new Error('GLib.markup_escape_text con 1 argomento dovrebbe lanciare TypeError, '
            + `non ha lanciato (o ha lanciato ${oneArgThrew})`);
    }
    const twoArgs = GLib.markup_escape_text('Format: <Alt><Super>r (x)', -1);
    if (typeof twoArgs !== 'string' || !twoArgs.includes('&lt;Alt&gt;')) {
        throw new Error(`markup_escape_text(text, -1) non e' il testo atteso: ${twoArgs}`);
    }

    // Estrae ed esegue l'istruzione REALE che costruisce il gruppo Shortcuts.
    // Serve un brace-matching che salti commenti e stringhe: il blocco contiene
    // un commento in cui '<Alt><Super>r' e' citato, e un matching ingenuo
    // terminerebbe li' e finirebbe per valutare un blocco spezzato.
    const fnAt = prefsSrc.indexOf('_buildShortcutsPage(window) {');
    if (fnAt === -1)
        throw new Error('_buildShortcutsPage non trovato in prefs.js');
    const declAt = prefsSrc.indexOf('const group = new Adw.PreferencesGroup({', fnAt);
    if (declAt === -1)
        throw new Error('gruppo Shortcuts non trovato in _buildShortcutsPage');
    const objStart = prefsSrc.indexOf('{', declAt);
    let depth = 0, objEnd = -1;
    for (let i = objStart; i < prefsSrc.length; i++) {
        const c = prefsSrc[i];
        if (c === '/' && prefsSrc[i + 1] === '/') {
            const nl = prefsSrc.indexOf('\n', i);
            i = nl === -1 ? prefsSrc.length : nl;
            continue;
        }
        if (c === "'" || c === '"' || c === '`') {
            const quote = c;
            i++;
            while (i < prefsSrc.length && prefsSrc[i] !== quote) {
                if (prefsSrc[i] === '\\') i++;
                i++;
            }
            continue;
        }
        if (c === '{') depth++;
        else if (c === '}' && --depth === 0) { objEnd = i; break; }
    }
    // Dopo la `}` dell'oggetto la sorgente ha `});`: va presa tutta la coda,
    // altrimenti l'eval riceve `...}) group;` e muore di SyntaxError invece
    // che di esercitare il prodotto.
    const semi = prefsSrc.indexOf(';', objEnd);
    const stmt = prefsSrc.slice(declAt, semi + 1);
    if (!stmt.endsWith('});'))
        throw new Error(`estrazione inattesa del gruppo: ...${stmt.slice(-20)}`);

    // Contesto minimo: _() e' gia' l'identita' piu' sopra, page.add finto
    // (serve la pagina Shortcuts, non il dialogo intero).
    const realGroup = eval(`${stmt} group;`);
    if (!realGroup.description)
        throw new Error('description del gruppo Shortcuts VUOTA: markup non parsato');
    if (!realGroup.description.includes('&lt;Alt&gt;')) {
        throw new Error('angoli non protetti nella description: ' + realGroup.description);
    }
    // La stringa originale deve restare leggibile: si confronta col testo
    // sorgente, non con una copia qui dentro.
    const raw = _('Format: <Alt><Super>r (modifier names between angle brackets, then the key)');
    if (realGroup.description !== GLib.markup_escape_text(raw, -1)) {
        throw new Error('description non coincide con markup_escape_text(sorgente, -1)');
    }
    console.log('Regressione crash markup_escape_text OK: 1 arg. = TypeError, '
        + 'gruppo Shortcuts reale costruito con &lt;Alt&gt; e label non vuota');
})();

// --- N2: il revert di "Clear history" non deve toccare una riga distrutta ----
// Giro S3A. Prima il one-shot di clear-history era sganciato: nessun id
// salvato, nessuna guardia. Chiudendo la finestra entro 1,5 s dal click il
// callback scriveva il titolo su una Adw.ActionRow gia' distrutta, e un
// secondo click lasciava pendente il timeout precedente.
//
// Il BLOCCO REALE viene estratto da prefs.js ed eseguito sui widget REALI
// (il blocco costruisce da solo riga e bottone con Adw/Gtk). Solo il GLib e'
// finto: serve osservare le fonti vive e distinguere una rimozione lecita da
// una su id gia' scaduto, che nel GLib vero stampa un CRITICAL. La guardia
// e' verificata per ESECUZIONE, non per lettura del sorgente.
(function regressionClearHistoryRevert() {
    const startAt = prefsSrc.indexOf("const clearHistoryLabel = _('Clear history');");
    if (startAt === -1)
        throw new Error('blocco clear-history non trovato in prefs.js');
    const endMarker = 'historyGroup.add(clearHistoryRow);';
    const endAt = prefsSrc.indexOf(endMarker, startAt);
    if (endAt === -1)
        throw new Error('blocco clear-history non terminato in prefs.js');
    const block = prefsSrc.slice(startAt, endAt + endMarker.length);

    // GLib finto: stesso contratto del vero (id numerici, SOURCE_REMOVE=false).
    let nextId = 1;
    const live = new Map();
    const staleRemovals = [];
    const fakeGLib = {
        PRIORITY_DEFAULT: 0,
        SOURCE_REMOVE: false,
        timeout_add(priority, delayMs, cb) {
            const id = nextId++;
            live.set(id, cb);
            return id;
        },
        source_remove(id) {
            // Su un id scaduto il GLib vero avvisa "Source ID N was not
            // found": e' il difetto N1 che questa guardia deve evitare.
            if (!live.has(id))
                staleRemovals.push(id);
            live.delete(id);
        },
    };
    // Il main loop toglie la fonte PRIMA di chiamarla: una callback che
    // ritorna SOURCE_REMOVE non resta registrata. Simulato cosi', altrimenti
    // il test misurerebbe un registro che il vero GLib non tiene.
    const runPending = () => {
        const callbacks = [...live.values()];
        live.clear();
        callbacks.forEach(cb => cb());
    };

    let configEditorCalls = 0;
    const runConfigEditor = () => {
        configEditorCalls++;
        return { success: true };
    };
    const historyGroup = { add() {} };

    // Il blocco dichiara i propri const/let: new Function li chiude in un
    // corpo di funzione e restituisce i due widget perche' il test li piloti.
    // Adw e Gtk entrano come argomenti perche' new Function ha scope proprio
    // e non vede gli import del modulo.
    const build = new Function('Adw', 'Gtk', 'GLib', '_', 'runConfigEditor', 'historyGroup',
        `${block}\nreturn { clearHistoryRow, clearHistoryButton };`);
    const { clearHistoryRow: row, clearHistoryButton: button } = build(
        Adw, Gtk, fakeGLib, t => t, runConfigEditor, historyGroup);

    // Ogni scrittura del titolo viene contata, e quelle successive alla
    // distruzione sono il difetto: il prodotto non deve poterle fare.
    let alive = true;
    let writesAfterDestroy = 0;
    let backing = row.title;
    Object.defineProperty(row, 'title', {
        get: () => backing,
        set: value => {
            if (!alive)
                writesAfterDestroy++;
            backing = value;
        },
        configurable: true,
    });

    // 1. Il click chiede la cancellazione e arma UN solo timeout di revert.
    button.emit('clicked');
    if (configEditorCalls !== 1)
        throw new Error(`il click non ha chiamato il config editor (${configEditorCalls})`);
    if (row.title !== 'History cleared')
        throw new Error(`il click non ha mostrato la conferma: ${row.title}`);
    if (live.size !== 1)
        throw new Error(`atteso 1 timeout armato, trovati ${live.size}`);

    // 2. Secondo click prima dello scadere: la guardia toglie il timeout
    //    precedente invece di lasciarlo pendente (niente revert fantasma).
    button.emit('clicked');
    if (live.size !== 1)
        throw new Error(`il secondo click ha lasciato ${live.size} timeout: atteso 1`);
    if (staleRemovals.length !== 0)
        throw new Error(`rimosso un id scaduto: ${staleRemovals.join(',')}`);

    // 3. Chiusura della finestra col timeout VIVO: la guardia deve cancellarlo,
    //    altrimenti il callback toccherebbe la riga distrutta.
    alive = false;
    row.emit('destroy');
    if (live.size !== 0)
        throw new Error('alla chiusura il timeout resta vivo: la guardia non cancella');
    runPending();
    if (writesAfterDestroy !== 0)
        throw new Error(`scritte su una riga distrutta: ${writesAfterDestroy} (difetto N2)`);
    if (staleRemovals.length !== 0)
        throw new Error(`la guardia ha rimosso un id scaduto: ${staleRemovals.join(',')}`);

    // 4. Percorso normale: il timeout SCADE e ripristina l'etichetta. Una
    //    chiusura successiva non deve rimuovere quell'id ormai scaduto: e'
    //    il difetto N1, chiuso sulla stessa identica forma.
    alive = true;
    button.emit('clicked');
    if (live.size !== 1)
        throw new Error('il terzo click non ha armato il timeout');
    runPending();
    if (row.title !== 'Clear history')
        throw new Error(`il timeout non ha ripristinato l'etichetta: ${row.title}`);
    row.emit('destroy');
    if (staleRemovals.length !== 0)
        throw new Error(`dopo la scadenza la guardia rimuove un id morto: ${staleRemovals.join(',')}`);
    if (writesAfterDestroy !== 0)
        throw new Error(`scritte su una riga distrutta: ${writesAfterDestroy}`);

    console.log('Regressione N2 OK: il revert di clear-history e\' azzerato alla '
        + 'scadenza, tolto alla chiusura e non scrive mai sulla riga distrutta');
})();

// --- N1: flashButtonLabel non deve lasciare un id di sorgente scaduta ------
// Giro S3A. Prima l'id del one-shot era const e non veniva mai azzerato:
// a fine sessione la guardia 'destroy' callava GLib.source_remove su un id
// MORTO, e il vero GLib risponde con un CRITICAL nel journal.
//
// La funzione REALE viene estratta da prefs.js ed eseguita su un Gtk.Button
// vero, con un GLib finto che distingue una rimozione lecita da una su id
// scaduto: la guardia e' verificata per ESECUZIONE, non per lettura.
(function regressionFlashButtonLabel() {
    const signature = 'function flashButtonLabel(button, text, revertText, delayMs = 1500) ';
    if (!prefsSrc.includes(signature))
        throw new Error('flashButtonLabel non trovato in prefs.js');

    let nextId = 1;
    const live = new Map();
    const staleRemovals = [];
    const fakeGLib = {
        PRIORITY_DEFAULT: 0,
        SOURCE_REMOVE: false,
        timeout_add(priority, delayMs, cb) {
            const id = nextId++;
            live.set(id, cb);
            return id;
        },
        source_remove(id) {
            // Su un id scaduto il GLib vero avvisa "Source ID N was not
            // found": e' il difetto che questa guardia deve evitare.
            if (!live.has(id))
                staleRemovals.push(id);
            live.delete(id);
        },
    };
    // Il main loop toglie la fonte PRIMA di chiamarla: una callback che
    // ritorna SOURCE_REMOVE non resta registrata.
    const runPending = () => {
        const callbacks = [...live.values()];
        live.clear();
        callbacks.forEach(cb => cb());
    };

    // new Function ha scope proprio: GLib entra come argomento.
    const flashButtonLabel = new Function('GLib',
        `${signature}${bodyOf(prefsSrc, signature)} return flashButtonLabel;`)(fakeGLib);
    const button = new Gtk.Button({ label: 'Save' });

    // 1. Il flash mostra subito il nuovo testo e arma un solo timeout.
    flashButtonLabel(button, 'Saved', 'Save');
    if (button.label !== 'Saved')
        throw new Error(`il flash non ha impostato l'etichetta: ${button.label}`);
    if (live.size !== 1)
        throw new Error(`atteso 1 timeout armato, trovati ${live.size}`);

    // 2. Scadenza normale: l'etichetta torna indietro e la fonte sparisce.
    runPending();
    if (button.label !== 'Save')
        throw new Error(`il timeout non ha ripristinato l'etichetta: ${button.label}`);
    if (live.size !== 0)
        throw new Error('la fonte scaduta resta registrata come viva');

    // 3. QUI stava il difetto N1: la chiusura DOPO la scadenza rimuoveva un
    //    id gia' morto. Con l'id azzerato alla scadenza la guardia tace.
    button.emit('destroy');
    if (staleRemovals.length !== 0)
        throw new Error(`la guardia ha rimosso un id scaduto: ${staleRemovals.join(',')}`);

    // 4. Chiusura col timeout ANCORA VIVO: va rimosso, ed una volta sola.
    const pending = new Gtk.Button({ label: 'Save' });
    flashButtonLabel(pending, 'Saved', 'Save');
    if (live.size !== 1)
        throw new Error('il secondo flash non ha armato il timeout');
    pending.emit('destroy');
    if (live.size !== 0)
        throw new Error('alla chiusura il timeout resta vivo: la guardia non cancella');
    if (staleRemovals.length !== 0)
        throw new Error(`la guardia ha rimosso un id scaduto: ${staleRemovals.join(',')}`);

    // 5. La riga 2 usa la stessa forma: dopo la scadenza la seconda guardia
    //    non deve nemmeno guardare l'id (nessuna rimozione registrata).
    if (staleRemovals.length !== 0)
        throw new Error(`rimozioni su id scaduti: ${staleRemovals.join(',')}`);

    console.log('Regressione N1 OK: flashButtonLabel azzera l\'id alla scadenza, '
        + 'la guardia destroy non rimuove mai una sorgente scaduta');
})();

// Pagina General: il metodo REALE _buildGeneralPage estratto da prefs.js ed
// eseguito su widget REALI. Il backend (setGeneralField/runConfigEditor) e'
// finto per osservare cosa la GUI scrive e per simulare un rifiuto.
(function generalPageReal() {
    const sig = '    _buildGeneralPage(window) {';
    const startAt = prefsSrc.indexOf(sig);
    if (startAt === -1)
        throw new Error('_buildGeneralPage non trovato in prefs.js');
    const open = prefsSrc.indexOf('{', startAt + sig.length - 1);
    const close = matchBrace(prefsSrc, open);
    if (close === -1)
        throw new Error('_buildGeneralPage non terminato in prefs.js');
    const body = prefsSrc.slice(open + 1, close);
    const rateMatch = prefsSrc.match(/const SAMPLE_RATES = \[[^\]]*\];/);
    if (!rateMatch)
        throw new Error('SAMPLE_RATES non trovato in prefs.js');
    const fmtMatch = prefsSrc.match(/const AUDIO_FORMAT_PRESETS = \[[\s\S]*?\n\];/);
    if (!fmtMatch)
        throw new Error('AUDIO_FORMAT_PRESETS non trovato in prefs.js');
    const indMatch = prefsSrc.match(/const INDICATOR_SETTINGS = \[[\s\S]*?\n\];/);
    if (!indMatch)
        throw new Error('INDICATOR_SETTINGS non trovato in prefs.js');

    const built = [];
    const rec = cls => new Proxy(cls, {
        construct(target, args) {
            const obj = new target(...args);
            built.push(obj);
            return obj;
        },
    });
    const AdwRec = {
        PreferencesPage: rec(Adw.PreferencesPage),
        PreferencesGroup: rec(Adw.PreferencesGroup),
        SwitchRow: rec(Adw.SwitchRow),
        SpinRow: rec(Adw.SpinRow),
        ComboRow: rec(Adw.ComboRow),
        EntryRow: rec(Adw.EntryRow),
    };
    const GLibFake = {
        PRIORITY_DEFAULT: 0, SOURCE_REMOVE: false,
        timeout_add: (p, d, cb) => { cb(); return 1; },
        source_remove() {},
    };
    const writes = [];
    let accept = true;
    const setGeneralField = (section, field, value) => {
        writes.push([section, field, String(value)]);
        return accept;
    };
    const streamWrites = [];
    let editorAccepts = true;
    const runConfigEditor = args => { streamWrites.push(args); return { success: editorAccepts }; };
    const general = {
        audio_format: 'ogg-opus',
        toggle_debounce_seconds: 1.5, retry_on_error: true, retry_count: 3,
        bitrate_kbps: 24, sample_rate: 24000, double_injection: false,
        clipboard_tool: 'wl-copy', clipboard_paste_tool: 'wl-paste',
    };
    let state = { general, stream: { chunk_log_max_lines: 500 } };
    const debounce = fn => fn;   // il ritardo e' infrastruttura, non logica

    const make = new Function('Adw', 'Gtk', 'Gio', 'GLib', '_', 'N_', 'debounce',
        'getServicesState', 'runConfigEditor', 'setGeneralField',
        `${rateMatch[0]}\n${fmtMatch[0]}\n${indMatch[0]}\nreturn { build: function (window) {${body}}, INDICATOR_SETTINGS };`);
    const built2 = make(AdwRec, Gtk, { SettingsBindFlags: { DEFAULT: 0 } }, GLibFake,
        t => t, t => t, debounce, () => state, runConfigEditor, setGeneralField);
    const method = built2.build;
    const INDICATOR = built2.INDICATOR_SETTINGS;

    // GSettings finto: registra i bind e simula uno schema stantio (has_key falso).
    // Fake GSettings: records binds and simulates a stale schema (has_key false).
    const binds = [];
    let schemaHasKeys = true;
    let errorShown = 0;
    const self = {
        _showConfigError() { errorShown++; },
        getSettings: () => ({
            settings_schema: { has_key: () => schemaHasKeys },
            bind: (key, obj, prop) => binds.push([key, prop, obj.title]),
        }),
    };
    const win = { added: [], add(p) { this.added.push(p); } };

    method.call(self, win);
    if (win.added.length !== 1 || errorShown !== 0)
        throw new Error('la pagina General non e stata aggiunta (o ha mostrato errore)');
    const byTitle = (cls, title) => {
        const row = built.find(o => o instanceof cls && o.title === title);
        if (!row)
            throw new Error(`riga "${title}" non costruita`);
        return row;
    };

    // Valori iniziali letti dallo stato reale, non da default fissi.
    if (byTitle(Adw.SpinRow, 'Attempts').value !== 3)
        throw new Error('Attempts non legge retry_count dallo stato');
    if (byTitle(Adw.SwitchRow, 'Write raw text first').active !== false)
        throw new Error('double_injection=false non rispecchiato');
    if (byTitle(Adw.ComboRow, 'Sample rate').selected !== 3)
        throw new Error('sample_rate 24000 non selezionato (indice 3)');
    if (byTitle(Adw.SpinRow, 'Chunk log size (lines)').value !== 500)
        throw new Error('chunk_log_max_lines non letto');
    if (writes.length !== 0)
        throw new Error(`la costruzione ha scritto in config: ${JSON.stringify(writes)}`);

    // Ogni impostazione dell'indicatore e' legata alla propria chiave GSettings.
    // Every indicator setting is bound to its own GSettings key.
    if (binds.length !== INDICATOR.length || INDICATOR.length !== 10)
        throw new Error(`bind GSettings: attesi 10, trovati ${binds.length}`);
    if (JSON.stringify(binds.map(b => b[0])) !== JSON.stringify(INDICATOR.map(i => i.key)))
        throw new Error('i bind non seguono INDICATOR_SETTINGS');
    if (!binds.every(b => b[1] === 'value'))
        throw new Error('un bind non punta alla proprieta value');

    // Toggle e SpinRow scrivono il campo giusto col formato giusto.
    byTitle(Adw.SwitchRow, 'Retry on error').active = false;
    byTitle(Adw.SpinRow, 'Attempts').value = 5;
    byTitle(Adw.SpinRow, 'Toggle debounce (seconds)').value = 2.5;
    const expect = [
        ['audio', 'retry_on_error', 'false'],
        ['audio', 'retry_count', '5'],
        ['audio', 'toggle_debounce_seconds', '2.5'],
    ];
    if (JSON.stringify(writes) !== JSON.stringify(expect))
        throw new Error(`scritture inattese: ${JSON.stringify(writes)}`);

    // Rifiuto del backend: la ComboRow torna al valore dello stato.
    writes.length = 0;
    accept = false;
    const combo = byTitle(Adw.ComboRow, 'Sample rate');
    combo.selected = 4;
    if (writes[0]?.join('/') !== 'audio/sample_rate/48000')
        throw new Error(`sample_rate non scritto: ${JSON.stringify(writes)}`);
    if (combo.selected !== 3)
        throw new Error('rifiuto del backend: la ComboRow non e tornata a 24000');

    // EntryRow: valore rifiutato -> testo ripristinato all'ultimo buono.
    const entryRow = byTitle(Adw.EntryRow, 'Copy command');
    entryRow.text = 'bad cmd';
    entryRow.emit('apply');
    if (entryRow.text !== 'wl-copy')
        throw new Error(`comando rifiutato non ripristinato: "${entryRow.text}"`);
    accept = true;
    entryRow.text = 'xclip';
    entryRow.emit('apply');
    if (entryRow.text !== 'xclip')
        throw new Error('comando accettato ripristinato per errore');

    // Diagnostica: passa da set-stream, non da set-general.
    byTitle(Adw.SpinRow, 'Chunk log size (lines)').value = 1000;
    if (JSON.stringify(streamWrites.at(-1)) !== JSON.stringify(['set-stream', 'chunk_log_max_lines', '1000']))
        throw new Error(`chunk_log_max_lines scritto male: ${JSON.stringify(streamWrites)}`);

    // Nuove voci: chiave, sezione e formato del valore.
    // New rows: key, section and value format.
    writes.length = 0;
    byTitle(Adw.SpinRow, 'Clipboard command timeout (seconds)').value = 9;
    byTitle(Adw.SpinRow, 'Notification timeout (seconds)').value = 4;
    byTitle(Adw.SpinRow, 'Text shown in notifications (characters)').value = 120;
    byTitle(Adw.SpinRow, 'Minimum cleaned length (fraction)').value = 0.5;
    byTitle(Adw.SpinRow, 'Area selection timeout (seconds)').value = 60;
    const expectNew = [
        ['general', 'clipboard_timeout_seconds', '9'],
        ['general', 'notify_timeout_seconds', '4'],
        ['general', 'notification_content_max_chars', '120'],
        ['general', 'cleanup_min_length_ratio', '0.50'],
        ['ocr', 'screenshot_timeout_seconds', '60'],
    ];
    if (JSON.stringify(writes) !== JSON.stringify(expectNew))
        throw new Error(`scritture nuove inattese: ${JSON.stringify(writes)}`);
    streamWrites.length = 0;
    byTitle(Adw.SpinRow, 'Whisper prompt limit (characters)').value = 900;
    byTitle(Adw.SpinRow, 'Noise floor window (frames)').value = 80;
    byTitle(Adw.SpinRow, 'Noise floor minimum (frames)').value = 15;
    if (JSON.stringify(streamWrites.map(a => a.slice(1).join('='))) !== JSON.stringify(
        ['prompt_max_chars=900', 'vad_floor_window_frames=80', 'vad_min_floor_frames=15']))
        throw new Error(`scritture stream inattese: ${JSON.stringify(streamWrites)}`);

    // Formato di registrazione: preset dallo stato, scrittura atomica, revert.
    // Recording format: preset from the state, atomic write, revert.
    const fmt = byTitle(Adw.ComboRow, 'Recording format');
    if (fmt.selected !== 0)
        throw new Error('audio_format ogg-opus non selezionato (indice 0)');
    streamWrites.length = 0;
    fmt.selected = 2;
    if (JSON.stringify(streamWrites.at(-1)) !== JSON.stringify(['set-audio-format', 'mp3']))
        throw new Error(`preset scritto male: ${JSON.stringify(streamWrites)}`);
    editorAccepts = false;
    fmt.selected = 3;
    if (fmt.selected !== 0)
        throw new Error('rifiuto del backend: il formato non e tornato al valore dello stato');
    editorAccepts = true;
    // Coppia non standard: voce "personalizzato" selezionata, nessuna scrittura.
    // Non-standard pair: "custom" entry selected, no write.
    state = { general: { ...general, audio_format: '' }, stream: {} };
    streamWrites.length = 0;
    method.call(self, { add() {} });
    const fmtCustom = built.filter(o => o instanceof Adw.ComboRow && o.title === 'Recording format').at(-1);
    if (fmtCustom.selected !== 4 || fmtCustom.model.get_n_items() !== 5)
        throw new Error('voce personalizzato non selezionata');
    if (streamWrites.some(a => a[0] === 'set-audio-format'))
        throw new Error('la costruzione ha scritto il formato');
    state = { general, stream: { chunk_log_max_lines: 500 } };

    // Schema stantio: nessun bind (una chiave assente sarebbe fatale).
    // Stale schema: no bind (a missing key would be fatal).
    state = { general, stream: {} };
    schemaHasKeys = false;
    binds.length = 0;
    method.call(self, { add() {} });
    if (binds.length !== 0)
        throw new Error('schema stantio: bind creati comunque');
    schemaHasKeys = true;

    // Config illeggibile: errore mostrato, nessuna riga costruita a meta'.
    state = null;
    const before = built.length;
    method.call(self, { add() {} });
    if (errorShown !== 1)
        throw new Error('config illeggibile: _showConfigError non chiamato');
    if (built.slice(before).some(o => o instanceof Adw.SpinRow))
        throw new Error('config illeggibile: righe costruite comunque');

    console.log('Pagina General REALE OK: valori dallo stato, scritture per campo/formato, revert su rifiuto, set-stream, config illeggibile');
})();
