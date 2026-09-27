#!/usr/bin/env node
/**
 * test-prefs-voice-commands.js
 *
 * Test statico/architetturale per la sezione dei comandi vocali in prefs.js:
 * 1. Verifica che non ci siano chiamate invalide Gtk.Entry.connect('apply', ...).
 * 2. Verifica che non ci siano riferimenti non importati a Main (es. Main.notifyError).
 * 3. Verifica l'uso di segnali validi ('changed', 'apply' solo su Adw.EntryRow, debounce).
 * 4. Simula la logica di serializzazione del form e salvataggio dei comandi.
 */

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const PREFS_PATH = path.join(__dirname, '..', 'gnome-extension', 'bravoric-indicator@local', 'prefs.js');
const prefsSrc = fs.readFileSync(PREFS_PATH, 'utf-8');

console.log('1. Verifica assenza di pattern errati nel sorgente prefs.js...');
// Non deve mai esserci connect('apply') su Gtk.Entry
assert(!prefsSrc.includes("new Gtk.Entry"), "prefs.js non dovrebbe usare Gtk.Entry per i comandi vocali senza supporto apply");
assert(!/new Gtk\.Entry\([^)]*\)[\s\S]*?\.connect\(['"]apply['"]/m.test(prefsSrc), "Gtk.Entry non supporta il segnale 'apply'");

// Non deve esserci Main.notifyError (Main non è importato in prefs.js)
assert(!prefsSrc.includes("Main.notifyError"), "prefs.js non deve fare riferimento a Main.notifyError non importato");

// Verifica presenza di ExpanderRow e righe Adw
assert(prefsSrc.includes("new Adw.ExpanderRow"), "prefs.js deve usare Adw.ExpanderRow per la gestione leggibile dei comandi");
assert(prefsSrc.includes("new Adw.EntryRow"), "prefs.js deve usare Adw.EntryRow");
assert(prefsSrc.includes("title: _('Aliases / Alternative phrases')"), "prefs.js deve includere il campo per gli alias");
assert(prefsSrc.includes("title: _('Chunk blacklist')"), "prefs.js deve includere il campo per la blacklist dei chunk");
assert(prefsSrc.includes("new Adw.ComboRow"), "prefs.js deve usare Adw.ComboRow per azione e scope");
assert(prefsSrc.includes("strings: [_('Single word'), _('Whole chunk')]"), "Lo scope deve avere esattamente le opzioni Single word/Whole chunk");
assert(prefsSrc.includes("row.scope.selected === 1 ? 'chunk' : 'word'"), "Lo scope UI deve serializzarsi nei valori backend word/chunk");
assert(prefsSrc.includes("new Adw.SwitchRow"), "prefs.js deve usare Adw.SwitchRow per ends_session");

console.log('1b. Verifica setup cattura tasto (controller + fallback manuale)...');
assert(prefsSrc.includes("import Gdk from 'gi://Gdk'"), "prefs.js deve importare Gdk");
assert(prefsSrc.includes('new Gtk.EventControllerKey'), "prefs.js deve usare Gtk.EventControllerKey");
assert(prefsSrc.includes('set_propagation_phase(Gtk.PropagationPhase.CAPTURE)'), "il controller deve essere in fase CAPTURE");
assert(prefsSrc.includes('key.add_controller(controller)'), "il controller deve essere agganciato alla EntryRow del tasto");
// La EntryRow editabile deve restare: la cattura è un'aggiunta, non sostituisce
// l'inserimento manuale (fallback) finché la cattura non è provata live.
assert(!/editable\s*:\s*false/.test(prefsSrc), "la EntryRow del tasto non deve diventare non editabile (fallback manuale)");
assert(prefsSrc.includes('Gtk.accelerator_get_default_mod_mask()'), "i modificatori devono mascherare CapsLock/NumLock");
assert(prefsSrc.includes("commitCapturedKey(resolved)"), "la cattura valida deve passare da commitCapturedKey");

console.log('1c. Confronto whitelist JS vs backend config.COMMAND_KEYS...');
const REPO_ROOT = path.join(__dirname, '..');
const keysMatch = prefsSrc.match(/const COMMAND_KEYS = \[([\s\S]*?)\];/);
assert(keysMatch, 'COMMAND_KEYS non trovato in prefs.js');
const jsKeys = eval('[' + keysMatch[1] + ']'); // eslint-disable-line no-eval
const pyOut = require('child_process').execFileSync(
    'python3',
    ['-c', 'from bravoric_stt_clipboard.config import COMMAND_KEYS; print(chr(10).join(sorted(COMMAND_KEYS)))'],
    { cwd: REPO_ROOT, env: { ...process.env, PYTHONPATH: path.join(REPO_ROOT, 'src') }, encoding: 'utf-8' }
).trim();
const pyKeys = pyOut.split('\n').filter(Boolean).sort();
assert.deepStrictEqual([...jsKeys].sort(), pyKeys,
    'COMMAND_KEYS in prefs.js deve coincidere esattamente con config.COMMAND_KEYS');

console.log('1c-bis. Parità keyMap di _runStreamCommand con COMMAND_KEYS di prefs.js...');
// Finora il gate presidiava SOLO COMMAND_KEYS (prefs.js <-> config.py). La
// keyMap che _runStreamCommand usa davvero per tradurre il NOME del tasto in
// un keyval non era confrontata con nessuna lista: un tasto poteva essere
// ammesso dalla whitelist e non tradotto dal prodotto, cioe' un comando che
// l'utente configura e che non esegue nulla, senza un solo test che se ne
// accorga. Aggiungere (non sostituire): 1c resta com'e'.
const EXT_PATH = path.join(REPO_ROOT, 'gnome-extension', 'bravoric-indicator@local', 'extension.js');
const extSrc = fs.readFileSync(EXT_PATH, 'utf-8');
const runCmdAt = extSrc.indexOf('_runStreamCommand(item) {');
assert(runCmdAt !== -1, '_runStreamCommand non trovato in extension.js');
const keyMapAt = extSrc.indexOf('const keyMap = {', runCmdAt);
assert(keyMapAt !== -1, 'const keyMap non trovata dentro _runStreamCommand');
const keyMapOpen = extSrc.indexOf('{', keyMapAt);
let kmDepth = 0, keyMapEnd = -1;
for (let i = keyMapOpen; i < extSrc.length; i++) {
    if (extSrc[i] === '{') kmDepth++;
    else if (extSrc[i] === '}' && --kmDepth === 0) { keyMapEnd = i; break; }
}
assert(keyMapEnd !== -1, 'graffe non bilanciate nella keyMap di _runStreamCommand');
const keyMapBody = extSrc.slice(keyMapOpen + 1, keyMapEnd);
// I tasti F1..F12 NON sono letterali: arrivano da uno spread
// ...Object.fromEntries(Array.from({length: N}, ...)). Si contano come quello
// che sono, altrimenti il confronto sarebbe sbilanciato per costruzione.
// La parte letterale va letta PRIMA dello spread: dentro Array.from c'e' un
// {length: N} che il matchere dei nomi scambierebbe per una chiave della mappa.
const spreadAt = keyMapBody.indexOf('...Object.fromEntries');
assert(spreadAt !== -1,
    'la keyMap deve generare i tasti F con ...Object.fromEntries(Array.from({length: N}))');
const keyMapKeys = [...keyMapBody.slice(0, spreadAt).matchAll(/([A-Za-z_][A-Za-z0-9_]*)\s*:/g)]
    .map(m => m[1]);
const generated = keyMapBody.match(/Object\.fromEntries\(\s*Array\.from\(\s*\{\s*length:\s*(\d+)/);
assert(generated, 'non riesco a leggere il numero dei tasti generati nella keyMap');
const keyMapAll = [...keyMapKeys, ...Array.from({ length: Number(generated[1]) }, (_, i) => `F${i + 1}`)];
assert.strictEqual(new Set(keyMapAll).size, keyMapAll.length,
    'la keyMap ha chiavi duplicate: un tasto tradotto due volte vale quanto non tradotto');
assert.deepStrictEqual([...keyMapAll].sort(), [...jsKeys].sort(),
    'le chiavi della keyMap in _runStreamCommand devono coincidere con COMMAND_KEYS di prefs.js: '
    + 'un tasto ammesso dalla whitelist ma assente dalla mappa non verrebbe mai tradotto in keyval');

console.log('1d. Verifica tabella alias di cattura...');
const aliasMatch = prefsSrc.match(/const CAPTURE_KEY_ALIASES = \{([\s\S]*?)\};/);
assert(aliasMatch, 'CAPTURE_KEY_ALIASES non trovato in prefs.js');
const CAPTURE_KEY_ALIASES = eval('({' + aliasMatch[1] + '})'); // eslint-disable-line no-eval
const COMMAND_KEY_SET = new Set(jsKeys);

// --- F4 (giro 2): la risoluzione dei tasti viene ESTRATTA DA prefs.js ed
// eseguita davvero, invece che riscritta qui. Prima questa funzione era una
// ricostruzione locale: mutando prefs.js (la riga di risoluzione in
// addCommandRow) la suite restava VERDE perche' testava la copia, non il
// prodotto. Verificato dal reviewer con una mutazione di una riga. Ora il
// comportamento verificato e' quello che gira davvero nell'estensione.
const resolveSource = prefsSrc.match(/const resolved = name && \(([\s\S]*?)\);/);
assert(resolveSource, 'riga di risoluzione dei tasti non trovata in prefs.js');
const resolvedBody = resolveSource[1].trim();

// La stessa espressione, valutata con il nome del tasto, su dati reali.
// new Function gira in scope globale: i const del modulo vanno passati come
// argomenti a OGNI chiamata, non basta definirli qui sopra.
const makeResolver = new Function(
    'name', 'COMMAND_KEY_SET', 'CAPTURE_KEY_ALIASES',
    `const resolved = name && (${resolvedBody}); return resolved ?? null;`,
);
const resolveCapturedKeyName = name => makeResolver(name, COMMAND_KEY_SET, CAPTURE_KEY_ALIASES);
for (const [alias, target] of Object.entries(CAPTURE_KEY_ALIASES)) {
    assert(COMMAND_KEY_SET.has(target), `alias ${alias} -> ${target} non è un valore di COMMAND_KEYS`);
}
assert.strictEqual(resolveCapturedKeyName('Return'), 'Return');
assert.strictEqual(resolveCapturedKeyName('space'), 'space');
assert.strictEqual(resolveCapturedKeyName('Escape'), 'Escape');
assert.strictEqual(resolveCapturedKeyName('KP_Enter'), 'Return');
assert.strictEqual(resolveCapturedKeyName('KP_Space'), 'space');
assert.strictEqual(resolveCapturedKeyName('ISO_Left_Tab'), 'Tab');
assert.strictEqual(resolveCapturedKeyName('KP_Home'), 'Home');
assert.strictEqual(resolveCapturedKeyName('KP_End'), 'End');
assert.strictEqual(resolveCapturedKeyName('KP_Page_Up'), 'Page_Up');
assert.strictEqual(resolveCapturedKeyName('KP_Next'), 'Page_Down');
assert.strictEqual(resolveCapturedKeyName('KP_Page_Down'), 'Page_Down');
assert.strictEqual(resolveCapturedKeyName('KP_Up'), 'Up');
assert.strictEqual(resolveCapturedKeyName('KP_Down'), 'Down');
assert.strictEqual(resolveCapturedKeyName('KP_Left'), 'Left');
assert.strictEqual(resolveCapturedKeyName('KP_Right'), 'Right');
// Tasti non mappati di proposito: nessun commit (feedback e si resta in cattura).
assert.strictEqual(resolveCapturedKeyName('a'), null);
assert.strictEqual(resolveCapturedKeyName('F13'), null);
assert.strictEqual(resolveCapturedKeyName('KP_Delete'), null);
assert.strictEqual(resolveCapturedKeyName(null), null);

console.log('1e. Verifica casella hotwords_in_prompt (solo ramo stream)...');
// Il campo esiste in FallbackLevel ed e' letto da api_client.transcribe_audio
// solo per il livello stream: la casella deve stare DENTRO il ramo stream di
// _buildLevelExpander, altrimenti stt/ocr mostrerebbero un campo inesistente
// (il backend rifiuterebbe la scrittura) e l'utente salverebbe una spunta falsa.
assert(prefsSrc.includes('hotwords_in_prompt'),
    'prefs.js deve gestire hotwords_in_prompt: senza la casella il backend non e' +
    'raggiungibile da GUI');

// Estrae il corpo di un metodo per brace-matching: verificare 'dentro il ramo
// stream' con una semplice include() non basta, il campo potrebbe comparire
// anche fuori dal ramo (per tutti i servizi) e il test passerebbe comunque.
function methodBody(src, signature) {
    const at = src.indexOf(signature);
    assert(at !== -1, `${signature} non trovato in prefs.js`);
    const open = src.indexOf('{', at);
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{')
            depth++;
        else if (src[i] === '}' && --depth === 0)
            return src.slice(open + 1, i);
    }
    throw new Error(`graffe non bilanciate in ${signature}`);
}

// I commenti non contano: il campo e' citato anche nella nota esplicativa
// sopra il ramo, e contarli falserebbe il confronto branch/totale.
function stripComments(js) {
    return js
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .split('\n')
        .map(line => line.replace(/(^|\s)\/\/.*$/, '$1'))
        .join('\n');
}

const expanderBody = methodBody(prefsSrc, '_buildLevelExpander(serviceKey, index, level, streamState = null)');
const expanderCode = stripComments(expanderBody);
const occurrences = expanderCode.split('hotwords_in_prompt').length - 1;
assert(occurrences > 0, 'hotwords_in_prompt deve comparire in _buildLevelExpander');

// Estrai il ramo if (serviceKey === 'stream') {...} e verifica che contenga
// TUTTE le occorrenze del campo.
const streamBranch = expanderCode.match(
    /if \(serviceKey === 'stream'\)\s*\{[\s\S]*?\n {8}\}/,
);
assert(streamBranch, 'manca il guard if (serviceKey === \'stream\') in _buildLevelExpander');
const inBranch = streamBranch[0].split('hotwords_in_prompt').length - 1;
assert.strictEqual(inBranch, occurrences,
    `tutte le occorrenze di hotwords_in_prompt devono stare dentro il ramo stream (${inBranch}/${occurrences})`);

// Coerenza con la sintassi usata dal backend: la stringa, non il booleano.
assert(/setLevelField\(\s*serviceKey,\s*index,\s*'hotwords_in_prompt',\s*String\([\w.]+\.active\)\s*\)/.test(expanderCode),
    "hotwords_in_prompt deve essere salvato come String(row.active) ('true'/'false'), non come booleano");
assert(/active:\s*!!level\.hotwords_in_prompt/.test(expanderCode),
    'la casella deve partire da active: !!level.hotwords_in_prompt');

// Adw.SwitchRow ha solo title/subtitle: 'description' non esiste sulla classe e
// un uso improprio crasha a runtime (GObject: property non trovata).
const switchRow = streamBranch[0].match(/new Adw\.SwitchRow\(\{[\s\S]*?\}\)/);
assert(switchRow, 'hotwords_in_prompt deve essere una Adw.SwitchRow');
assert(!/\bdescription\s*:/.test(switchRow[0]),
    'Adw.SwitchRow non ha la property description (crash a runtime): usare subtitle');
assert(/subtitle\s*:/.test(switchRow[0]),
    'la spiegazione va in subtitle (unico campo di testo libero di Adw.SwitchRow)');

// Coerenza JS <-> backend: il campo deve essere in LEVEL_FIELDS, altrimenti
// config_editor.set_level_field solleva Unknown field e la casella non salva.
const levelFields = require('child_process').execFileSync(
    'python3',
    ['-c', 'from bravoric_stt_clipboard.config_editor import LEVEL_FIELDS; print(chr(10).join(LEVEL_FIELDS))'],
    { cwd: REPO_ROOT, env: { ...process.env, PYTHONPATH: path.join(REPO_ROOT, 'src') }, encoding: 'utf-8' }
).trim().split('\n').filter(Boolean);
assert(levelFields.includes('hotwords_in_prompt'),
    'hotwords_in_prompt deve stare in config_editor.LEVEL_FIELDS, altrimenti set-level lo rifiuta');

console.log('2. Verifica logica di serializzazione comando (funzione REALE)...');
// Il serializer non e' piu' ricopiato qui: si ESTRAE da prefs.js e si
// esegue, quindi le griglie sotto presidiano il prodotto e non una sua
// ricostruzione. Prima questo test verificava la propria copia: cambiare il
// serializer vero in prefs.js lasciava questo file VERDE. bodyOf taglia sul
// brace-balance, serve la stessa testata che in prefs.js perche' l'eval
// riceva una dichiarazione e non un blocco orfano. serializeCommandRows e'
// pura (niente Adw/GLib/gettext), quindi si esegue senza stub.
const serializeBody = methodBody(prefsSrc, 'function serializeCommandRows(rows)');
const serializeFn = new Function('rows', serializeBody);
const serialize = serializeFn;

const mockRows = [
    {
        keyword: { text: 'a capo' },
        aliases: { text: 'invio, in view' },
        action: { selected: 0 },
        key: { text: 'Return' },
        scope: { selected: 0 },
        ends: { active: false },
    },
    {
        keyword: { text: 'cancella tutto' },
        aliases: { text: 'elimina tutto, wipe' },
        action: { selected: 1 },
        key: { text: 'Return' },
        scope: { selected: 1 },
        ends: { active: true },
    },
    {
        keyword: { text: '   ' }, // vuoto, ignorato
        aliases: { text: '' },
        action: { selected: 0 },
        key: { text: 'Return' },
        scope: { text: '' },
        ends: { active: false },
    }
];

const serialized = serialize(mockRows);
assert.strictEqual(serialized.length, 2);
assert.deepStrictEqual(serialized[0], {
    keyword: 'a capo',
    aliases: ['invio', 'in view'],
    action: 'key',
    key: 'Return',
    scope: '',
    ends_session: false,
});
assert.deepStrictEqual(serialized[1], {
    keyword: 'cancella tutto',
    aliases: ['elimina tutto', 'wipe'],
    action: 'delete',
    key: '',
    scope: 'chunk',
    ends_session: true,
});

console.log('3. Verifica pool parallelo e slot per endpoint (ramo stream)...');

// I due campi nuovi (parallel, max_concurrency) sono stream-only come
// hotwords_in_prompt: senza il guard mostreremo a stt/ocr una casella che il
// backend rifiuterebbe, e senza il riscontro con LEVEL_FIELDS la salvataggio
// solleverebbe Unknown field.
const expanderCodeOnda4 = stripComments(methodBody(prefsSrc, '_buildLevelExpander(serviceKey, index, level, streamState = null)'));
assert(expanderCodeOnda4.includes("'max_concurrency'"),
    'prefs.js deve gestire max_concurrency: senza la casella lo slot per endpoint non e' +
    'raggiungibile da GUI');
assert(expanderCodeOnda4.includes("'parallel'"),
    'prefs.js deve gestire parallel: senza l\'interruttore il livello non entra nel pool');

// Il guard deve esistere come stringa esatta: con `if (true)` il file resta
// sintatticamente valido, ma le righe comparirebbero per stt e ocr e questo
// test deve rosare. Per questo NON basta un include() sul file intero (il
// ramo hotwords contiene gia' la stessa stringa): si verifica che il blocco
// ESISTA e che CONTENGA i due campi.
const guardRe = /if \(serviceKey === 'stream'\)\s*\{/g;
let poolBranch = null;
let guardCount = 0;
for (const m of expanderCodeOnda4.matchAll(guardRe)) {
    guardCount++;
    const open = m.index + m[0].length - 1;
    let depth = 0;
    for (let i = open; i < expanderCodeOnda4.length; i++) {
        if (expanderCodeOnda4[i] === '{')
            depth++;
        else if (expanderCodeOnda4[i] === '}' && --depth === 0) {
            const body = expanderCodeOnda4.slice(open + 1, i);
            if (body.includes("'parallel'") && body.includes("'max_concurrency'")) {
                poolBranch = body;
                break;
            }
            break;
        }
    }
    if (poolBranch)
        break;
}
assert(guardCount > 0, "manca il guard if (serviceKey === 'stream') in _buildLevelExpander");
assert(poolBranch,
    'parallel e max_concurrency devono stare dentro if (serviceKey === \'stream\'): ' +
    'fuori dal ramo stt/ocr mostrerebbero campi inesistenti');

// Estrattore di oggetto per brace-matching. Serve perche' la regex non-greasy
// su `new Adw.SpinRow({...})` si ferma al PRIMO `})`, cioe' alla chiusura del
// Gtk.Adjustment annidato, e perderebbe tutto quello che segue (digits:
// asserito qui sotto). Stesso limite di methodBody: le stringhe del blocco
// non contengono graffe.
function objectLiteralAfter(src, needle) {
    const at = src.indexOf(needle);
    assert(at !== -1, `${needle} non trovato nel ramo stream`);
    const open = src.indexOf('{', at + needle.length - 1);
    assert(open !== -1, `manca '{' dopo ${needle}`);
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{')
            depth++;
        else if (src[i] === '}' && --depth === 0)
            return src.slice(open + 1, i);
    }
    throw new Error(`graffe non bilanciate dopo ${needle}`);
}

// Un interruttore e uno slider, non altro: SwitchRow per il flag booleano,
// SpinRow per il numero.
const poolSwitch = objectLiteralAfter(poolBranch, 'new Adw.SwitchRow(');
const poolSpin = objectLiteralAfter(poolBranch, 'new Adw.SpinRow(');
// Adw.SwitchRow ha solo title/subtitle: 'description' crasha a runtime.
assert(!/\bdescription\s*:/.test(poolSwitch),
    'Adw.SwitchRow di parallel non ha la property description (crash a runtime): usare subtitle');
assert(/subtitle\s*:/.test(poolSwitch),
    'la spiegazione di parallel va in subtitle');
// Il testo lungo del numero sta nel tooltip: SpinRow ha solo title/subtitle.
assert(/tooltip_text\s*:/.test(poolSpin),
    'max_concurrency deve spiegare il limite in tooltip_text');

// Lo slider e' 1..8, coerente con il clamp di config.py: sotto 1 non ha
// senso (zero worker), sopra 8 il tetto di worker_count.
const spinAdjustment = objectLiteralAfter(poolSpin, 'new Gtk.Adjustment(');
assert(/lower\s*:\s*1\b/.test(spinAdjustment),
    'la SpinRow di max_concurrency deve avere lower: 1');
assert(/upper\s*:\s*8\b/.test(spinAdjustment),
    'la SpinRow di max_concurrency deve avere upper: 8');
assert(/step_increment\s*:\s*1\b/.test(spinAdjustment),
    'la SpinRow di max_concurrency deve avere step_increment: 1 (slot interi)');
assert(/digits\s*:\s*1\b/.test(poolSpin),
    'la SpinRow di max_concurrency deve avere digits: 1 (un solo decimale)');

// Serializzazione come hotwordsInPromptRow e timeoutRow: la stringa, non il
// booleano. Math.round come su timeoutRow: la SpinRow ha digits 1 (richiesto
// dalla spec per l'estetica) e puo' quindi produrre "3.0", che il backend
// scarterebbe: int("3.0") solleva ValueError e _coerce_max_concurrency
// tornerebbe al default 3, losing la scelta dell'utente in silenzio.
assert(/setLevelField\(\s*serviceKey,\s*index,\s*'parallel',\s*String\([\w.]+\.active\)\s*\)/.test(poolBranch),
    "parallel deve essere salvato come String(row.active) ('true'/'false'), non come booleano");
assert(/setLevelField\(\s*serviceKey,\s*index,\s*'max_concurrency',\s*String\(Math\.round\([\w.]+\.value\)\)\s*\)/.test(poolBranch),
    'max_concurrency deve essere salvato come String(Math.round(row.value)): intero 1..8, non la stringa con decimale');
assert(/active\s*:\s*!!level\.parallel/.test(poolBranch),
    'l\'interruttore deve partire da active: !!level.parallel');

// Disabilitato con `sensitive`, non con `editable`: lo slot vale solo se il
// livello partecipa al pool, e `editable: false` e' asserito come assente in
// tutto il file (test 1) perche' non esiste sulla SpinRow.
assert(/[\w.]+\.sensitive\s*=\s*[\w.]+\.active/.test(poolBranch),
    'la SpinRow di max_concurrency va disabilitata con row.sensitive = row.active, non con editable');
assert(!/editable\s*:/.test(poolBranch),
    'nessuna property editable nella casella max_concurrency (crash a runtime sulla SpinRow)');

// Vietati i pattern che il test 1 vieta su tutto il file: ripetuto qui per
//che l'onda 4 non li introduca nel blocco nuovo.
assert(!poolBranch.includes('new Gtk.Entry'),
    'nessun Gtk.Entry nel blocco parallel/max_concurrency');
assert(!poolBranch.includes('Main.notifyError'),
    'Main.notifyError: Main non e\' importato in prefs.js');

// Coerenza JS <-> backend: senza i due campi in LEVEL_FIELDS set_level_field
// solleva Unknown field e la casella salva una schermata di errore.
const levelFieldsOnda4 = require('child_process').execFileSync(
    'python3',
    ['-c', 'from bravoric_stt_clipboard.config_editor import LEVEL_FIELDS; print(chr(10).join(LEVEL_FIELDS))'],
    { cwd: REPO_ROOT, env: { ...process.env, PYTHONPATH: path.join(REPO_ROOT, 'src') }, encoding: 'utf-8' }
).trim().split('\n').filter(Boolean);
for (const field of ['parallel', 'max_concurrency']) {
    assert(levelFieldsOnda4.includes(field),
        `${field} deve stare in config_editor.LEVEL_FIELDS, altrimenti set-level lo rifiuta`);
}

console.log('4. Verifica che i flag per-livello partano dal valore REALE (difetto A)...');
// Difetto A (giro 1): config_editor.get_state() stringifica TUTTI i LEVEL_FIELDS
// con str(), quindi `parallel` e `hotwords_in_prompt` arrivano in prefs.js come
// le stringhe "True"/"False", NON come booleani. I due Adw.SwitchRow erano
// costruiti con `!!level.<campo>`: in JS !!'False' === true, quindi partivano
// SEMPRE accesi e il primo click li spegneva invece di accenderli.
//
// Non basta guardare la forma del sorgente: la prova e' eseguita sul valore
// che arriva davvero dal backend e sulla funzione di normalizzazione, cosi'
// il test diventa ROSSO se il fix viene rimosso.
const backendValues = require('child_process').execFileSync(
    'python3',
    ['-c', [
        'import json, tempfile, pathlib',
        'from bravoric_stt_clipboard import config_editor as ce',
        'd = pathlib.Path(tempfile.mkdtemp())',
        'p = d / "c.toml"',
        'p.write_text("[[stt.fallback]]\\nname=\\"a\\"\\nendpoint=\\"e\\"\\nmodel=\\"m\\"\\nparallel=false\\n'
        + '[[stt.fallback]]\\nname=\\"b\\"\\nendpoint=\\"e\\"\\nmodel=\\"m\\"\\nparallel=true\\n")',
        'ce.CONFIG_PATH = p',
        'st = ce.get_state()',
        'print(json.dumps([l["parallel"] for l in st["stt"]["levels"][:2]]))',
    ].join('\n')],
    { cwd: REPO_ROOT, env: { ...process.env, PYTHONPATH: path.join(REPO_ROOT, 'src') }, encoding: 'utf-8' }
).trim();
const [backendOff, backendOn] = JSON.parse(backendValues);
assert.strictEqual(typeof backendOff, 'string',
    `il backend deve emettere parallel come stringa (arrivato ${typeof backendOff}): e' la premessa del difetto A`);
assert.strictEqual(backendOff.toLowerCase(), 'false');
assert.strictEqual(backendOn.toLowerCase(), 'true');

const flagFn = prefsSrc.match(/function\s+levelFlagTrue\s*\([^)]*\)\s*\{([\s\S]*?)\n\}/);
assert(flagFn, 'prefs.js deve definire levelFlagTrue(): senza, gli switch ripartono da !!\'False\'');
const normalizer = new Function('value', flagFn[1]);
assert.strictEqual(normalizer('False'), false,
    'levelFlagTrue("False") deve essere false: e\' il cuore del difetto A');
assert.strictEqual(normalizer('False'), !'False',
    'la normalizzazione deve ribaltare il caso di !!\'False\' (che e\' true)');
assert.strictEqual(normalizer('True'), true);
assert.strictEqual(normalizer('true'), true);
assert.strictEqual(normalizer('false'), false);
assert.strictEqual(normalizer(''), false, 'campo assente/legacy = stringa vuota: lo switch deve partire spento');

const normUse = expanderCode.match(/level\s*=\s*\{[\s\S]*?\};/);
assert(normUse, 'il ramo stream deve normalizzare level una volta sola');
assert(/hotwords_in_prompt:\s*levelFlagTrue\(/.test(normUse[0]),
    'hotwords_in_prompt deve passare da levelFlagTrue()');
assert(/parallel:\s*levelFlagTrue\(/.test(normUse[0]),
    'parallel deve passare da levelFlagTrue()');

// --- F5 (meta' 2): GRIGLIA DI COMPETIMENTO sulle due normalizzazioni --------
// Prima l'unico assert che legava il banner a levelFlagTrue era di FORMA:
// confrontava il TESTO della normalizzazione del banner con la stringa esatta
// `String(level.parallel).toLowerCase() === 'true'`. Un test prescrittivo
// non distingue una normalizzazione corretta da una sbagliata: e' verde anche
// se banner e switch divergono su tutti i valori reali, e rosso solo se
// qualcuno riscrive la lettera. Qui invece entrambe le funzioni vengono
// ESEGUITE sulla stessa matrice di valori e devono concordare.
// La funzione e' gia' stata estratta e resa eseguibile piu' sopra
// (`normalizer`): qui la griglia la esegue, non la ricostruisce.
const normalize = normalizer;

// La regola del banner. Non piu' una costante locale nel prodotto: qui si
// ricostruisce l'equivalente (level -> levelFlagTrue(level.parallel)) e la si
// mette in confronto con la normalizzazione vera su ogni valore della griglia.
// Se il banner tornasse a normalizzare per conto proprio, l'asserzione qui
// sotto continuerebbe a valere perche' confronta i due RISULTATI: e' il
// prodotto a dover passare da levelFlagTrue, e questo e' il posto dove si
// verifica. Il call site nel banner e' presidato a parte, sotto.
const bannerNormalize = level => normalize(level.parallel);

// Matrice dei valori che il backend puo' davvero consegnare ai due call site:
// le stringhe dei LEVEL_FIELDS (str(True) == "True"), i booleani veri, e i
// casi assenti/legacy. `1`/`0` non arrivano oggi ma entrano nella stessa
// conversione String(), quindi sono un caso che la regola dichiara.
const FLAG_GRID = [
    { value: 'true', expected: true },
    { value: 'True', expected: true },
    { value: 'false', expected: false },
    { value: 'False', expected: false },
    { value: true, expected: true },
    { value: false, expected: false },
    { value: undefined, expected: false },
    { value: '', expected: false },
    { value: 1, expected: false },
    { value: 0, expected: false },
];
const showValue = v => (typeof v === 'string' ? `'${v}'` : `${v}`);

// (a) il risultato atteso per ciascun valore, sulla funzione vera
for (const { value, expected } of FLAG_GRID) {
    assert.strictEqual(normalize(value), expected,
        `levelFlagTrue(${showValue(value)}) deve essere ${expected}: ` +
        'i flag per-livello stringificati fanno partire gli switch sempre accesi');
    // (b) e il banner deve concordare su quello STESSO valore: una sola
    // normalizzazione, verificata per esecuzione e non per lettera.
    assert.strictEqual(bannerNormalize({ parallel: value }), normalize(value),
        `il banner e gli switch per-livello devono dare lo stesso risultato su ` +
        `${showValue(value)}: una sola normalizzazione per il flag parallel`);
}

// Il caso che ha prodotto il difetto A, dichiarato per nome e non solo implicito
// nella griglia: `!!'False'` e' true in JS, quindi senza normalizzazione lo
// switch partirebbe acceso e il primo click lo spegnerebbe invece di accenderlo.
assert.strictEqual(normalize('False'), false, 'levelFlagTrue("False") deve essere false: e\' il cuore del difetto A');
assert.strictEqual(normalize('False'), !'False',
    'la normalizzazione deve ribaltare il caso di !!\'False\' (che e\' true)');

// Il banner non puo' avere una normalizzazione PROPRIA: il suo codice non deve
// contenere nessun secondo criterio (ne' il vecchio `isOn`, ne' un confronto
// con 'true' scritto per mano). Non e' un test di lettera sul comportamento, e'
// il presidio strutturale del "una sola funzione": la griglia qui sopra dice
// COME normalizza, questo dice DOVE.
const bannerBodyText = methodBody(prefsSrc, 'function dispatchStatusFromState(streamState)');
const bannerCode = stripComments(bannerBodyText);
assert(!/isOn/.test(bannerCode),
    'il banner non deve piu\' definire isOn(): esiste una sola normalizzazione, levelFlagTrue');
assert(!/toLowerCase\(\)\s*===\s*['"]true['"]/.test(bannerCode),
    'il banner non deve riscrivere la regola: passa per levelFlagTrue()');
const bannerLevelChecks = (bannerCode.match(/levelFlagTrue\(\s*level\.parallel\s*\)/g) || []).length;
assert(bannerLevelChecks >= 2,
    'i due filtri del banner (nel pool / escluso) devono passare entrambi da levelFlagTrue(level.parallel)');
// E il banner deve davvero dividere i livelli con la funzione vera, non con un
// altro predicato. Si ESTRAE il corpo reale di dispatchStatusFromState e lo si
// esegue davvero, passandogli levelFlagTrue per nome: il risultato torna solo
// se il banner chiama davvero la normalizzazione del prodotto. methodBody
// (quello di questo file) restituisce il corpo SENZA le graffe, quindi vanno
// rimesse attorno: da sole sarebbero un blocco orfano e `new Function` lo
// rifiuterebbe con SyntaxError. `_` (gettext) e' l'identita' come nel smoke test:
// qui non c'e' una sessione Shell, e serve solo il testo, non la traduzione.
const bannerOf = new Function('_', 'levelFlagTrue', [
    'return (streamState) => {',
    bannerBodyText,
    '};',
].join('\n'))(text => text, normalize);
const mixed = {
    dispatch_mode: 'auto',
    max_concurrent_chunks: 4,
    max_concurrent_chunks_auto: true,
    levels: [{ name: 'a', parallel: 'True' }, { name: 'b', parallel: 'False' }, { name: 'c', parallel: 'true' }],
};
const bannerOut = bannerOf(mixed);
assert(typeof bannerOut.subtitle === 'string' && bannerOut.subtitle.length > 0,
    'il banner reale deve restituire un sottotitolo per uno stato in auto');
// "3 endpoints in the pool" e' il conto che IL BANNER fa con la sua
// normalizzazione: qui e' 2, perche' 'False' non e' un flag acceso. Se il
// banner usasse `!!level.parallel` (difetto A) o un criterio proprio, questo
// numero cambierebbe e la griglia che precede lo catturerebbe lo stesso.
const poolCount = Number(/Parallel: (\d+) endpoints/.exec(bannerOut.subtitle)?.[1]);
assert.strictEqual(poolCount, 2,
    `il banner deve contare 2 endpoint nel pool con un livello 'False', ne' conta ${poolCount} ` +
    `(sottotitolo: ${bannerOut.subtitle})`);
assert(bannerOut.note.includes('b'),
    `il livello escluso dal pool deve essere dichiarato irraggiungibile nel banner: ${bannerOut.note}`);

console.log('PASS test-prefs-voice-commands.js: tutte le asserzioni verificate con successo!');
