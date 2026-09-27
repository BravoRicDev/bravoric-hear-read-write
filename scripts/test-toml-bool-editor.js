// test-toml-bool-editor.js — giro 2 (F2), test del prodotto reale.
//
// Il difetto: TomlBoolEditor.readBool/writeBoolean usavano il pattern
// `^key = (true|false)$` (flag m). Una riga con un commento in fondo o
// indentata NON agganciava: readBool mentiva sullo stato (restituzione il
// fallback) e writeBool cadeva nel ramo di inserimento, duplicando la chiave
// dentro [notifications]. Un TOML con una chiave duplicata non si carica:
// config.py tradue TOMLDecodeError in ConfigError e TUTTO il backend
// (STT, OCR, streaming, il menu Configuration, metà delle pagine di prefs.js)
// diventa muto, senza avviso. Basta un commento su una riga sola.
//
// Qui la CLASSE VERA viene estratta da prefs.js per brace-matching ed eseguita
// sotto gjs: non una ricostruzione, quindi il test presidia il prodotto.
// (prefs.js non è importabile fuori da una sessione Shell: importa la risorsa
// resource:///org/gnome/Shell/Extensions/... .)
//
// Verifiche: (1) il TOML resta valido dopo ogni scrittura, misurato con
// tomllib Python, non a occhio; (2) readBool e writeBool concordano, cioè la
// GUI non mente sullo stato; (3) i casi che il reviewer aveva misurati come
// rotti (commento in fondo, indentazione) sono coperti.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const PREFS_PATH = GLib.build_filenamev([
    GLib.get_current_dir(), 'gnome-extension', 'bravoric-indicator@local', 'prefs.js',
]);

// Estrae una classe per brace-matching dal sorgente reale.
function classSource(src, name) {
    const at = src.indexOf(`class ${name} `);
    if (at === -1)
        throw new Error(`classe ${name} non trovata in prefs.js`);
    const open = src.indexOf('{', at);
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{')
            depth++;
        else if (src[i] === '}' && --depth === 0)
            return src.slice(at, i + 1);
    }
    throw new Error(`graffe non bilanciate in ${name}`);
}

const [ok, bytes] = GLib.file_get_contents(PREFS_PATH);
if (!ok)
    throw new Error(`prefs.js non leggibile: ${PREFS_PATH}`);
const prefsSrc = new TextDecoder().decode(bytes);

// logError serve a TomlBoolEditor: fuori da una sessione Shell non esiste.
const logError = (err, ctx) => { /* scartato: i test verificano altrove */ };
const { TomlBoolEditor } = eval(`(() => { ${classSource(prefsSrc, 'TomlBoolEditor')}; return { TomlBoolEditor }; })()`);

let passed = 0;
let failed = 0;
function check(label, condition) {
    if (condition) {
        passed++;
        console.log(`  PASS  ${label}`);
    } else {
        failed++;
        console.log(`  FAIL  ${label}`);
    }
}

// Percorso di prova in tmp, con la stessa forma della config reale.
const workdir = GLib.dir_make_tmp('tomlbool-XXXXXX');
const cfgPath = GLib.build_filenamev([workdir, 'config.toml']);
const KEY = 'stt_on_raw_ready';
const OTHER = 'stt_on_raw_ready_content';

function writeConfig(body) {
    const f = Gio.File.new_for_path(cfgPath);
    f.replace_contents(new TextEncoder().encode(body), null, false,
        Gio.FileCreateFlags.NONE, null);
}

function readConfig() {
    const [, content] = Gio.File.new_for_path(cfgPath).load_contents(null);
    return new TextDecoder().decode(content);
}

// Validazione TOML vera: un parser, non un confronto di stringhe.
function tomlValid(text) {
    // Si scrive il file su disco e lo si fa validare da tomllib Python.
    const check_path = GLib.build_filenamev([workdir, 'check.toml']);
    Gio.File.new_for_path(check_path).replace_contents(
        new TextEncoder().encode(text), null, false, Gio.FileCreateFlags.NONE, null);
    const proc = GLib.spawn_sync(null, ['python3', '-c', `
import sys, tomllib
try:
    tomllib.load(open(sys.argv[1], 'rb'))
except Exception as exc:
    print('ROTTO: %s' % exc)
    sys.exit(1)
print('OK')
`, check_path], null, GLib.SpawnFlags.SEARCH_PATH, null);
    // spawn_sync restituisce stdout/stderr come array boxed, NON come
    // ArrayBufferView: si decodifica a mano, il TextDecoder li rifiuta.
    const decode = bytes => {
        if (!bytes)
            return '';
        try {
            return new TextDecoder().decode(new Uint8Array(Array.from(bytes))).trim();
        } catch {
            return '';
        }
    };
    // Attenzione agli indici: spawn_sync restituisce
    // [ok, stdout, stderr, exit_status] (NON [ok, status, stdout, stderr]).
    const out = decode(proc[1]);
    if (!out.startsWith('OK'))
        console.log(`    (tomllib: ${out || decode(proc[2]) || 'nessun output'})`);
    return out.startsWith('OK');
}

console.log('== F2: TomlBoolEditor (classe REALE estratta da prefs.js) ==');

// --- Caso A: riga pulita, il caso che funzionava anche prima -------------
writeConfig(`[notifications]\n${KEY} = true\n${OTHER} = true\n`);
let editor = new TomlBoolEditor(cfgPath);
check('A: riga pulita letta come true', editor.readBool(KEY, false) === true);
editor.writeBool(KEY, false);
check('A: riga pulita aggiornata', readConfig().includes(`${KEY} = false`));
check('A: TOML ancora valido', tomlValid(readConfig()));

// --- Caso B: COMMENTO in fondo alla riga (il caso che rompeva) -----------
// Prima: readBool restituiva il fallback e writeBool duplicava la chiave.
writeConfig(`[notifications]\n${KEY} = true  # non toccare\n${OTHER} = true\n`);
editor = new TomlBoolEditor(cfgPath);
check('B: readBool legge il valore anche con un commento in fondo',
    editor.readBool(KEY, false) === true);
editor.writeBool(KEY, false);
const afterB = readConfig();
check('B: la chiave non viene duplicata',
    afterB.split('\n').filter(l => l.trim().startsWith(`${KEY} `)).length === 1);
check('B: TOML ancora valido dopo la scrittura', tomlValid(afterB));
check('B: readBool e writeBool concordano (GUI non mente)',
    new TomlBoolEditor(cfgPath).readBool(KEY, true) === false);

// --- Caso C: indentazione (stessa radice) -------------------------------
writeConfig(`[notifications]\n  ${KEY} = true\n  ${OTHER} = true\n`);
editor = new TomlBoolEditor(cfgPath);
check('C: riga indentata letta correttamente', editor.readBool(KEY, false) === true);
editor.writeBool(KEY, false);
check('C: TOML ancora valido dopo la scrittura su riga indentata',
    tomlValid(readConfig()));

// --- Caso D: spazi attorno all'uguale -----------------------------------
writeConfig(`[notifications]\n${KEY}=true\n${OTHER}   =   true\n`);
editor = new TomlBoolEditor(cfgPath);
check('D: senza spazi attorno a =', editor.readBool(KEY, false) === true);
editor.writeBool(KEY, false);
check('D: TOML ancora valido', tomlValid(readConfig()));

// --- Caso E: la chiave non esiste -> inserimento in [notifications] -----
writeConfig(`[notifications]\n${OTHER} = true\n\n[history]\nmax_entries = 10\n`);
editor = new TomlBoolEditor(cfgPath);
editor.writeBool(KEY, true);
const afterE = readConfig();
check('E: chiave nuova inserita in [notifications]',
    afterE.includes(`${KEY} = true`));
check('E: inserimento non rompe la sezione successiva',
    tomlValid(afterE) && afterE.includes('max_entries = 10'));

// --- Caso F: chiave assente, nessuna sezione [notifications] -------------
writeConfig(`[history]\nmax_entries = 10\n`);
editor = new TomlBoolEditor(cfgPath);
editor.writeBool(KEY, true);
check('F: senza [notifications] il file resta intatto',
    tomlValid(readConfig()) && !readConfig().includes(`${KEY} = true`));

// --- Caso G: file illeggibile -> fallback, senza crash ------------------
const missing = new TomlBoolEditor(GLib.build_filenamev([workdir, 'non-esiste.toml']));
check('G: file assente -> fallback, nessuna eccezione',
    missing.readBool(KEY, false) === false);

console.log(`\n${passed} PASS / ${failed} FAIL`);
if (failed > 0)
    throw new Error('test-toml-bool-editor.js: ci sono check falliti');
