// test-prefs-whole.js — carica DAVVERO prefs.js per intero sotto gjs, con
// widget Adw/Gtk veri e il backend Python vero su una config temporanea, ed
// esegue fillPreferencesWindow().
// Loads the REAL prefs.js whole under gjs, with real Adw/Gtk widgets and the real
// Python backend on a temporary config, and runs fillPreferencesWindow().
//
// PERCHE' ESISTE / WHY IT EXISTS: gli smoke test estraggono singoli metodi di
// prefs.js; nessuno costruiva la finestra intera, quindi un errore che si vede
// solo componendo tutte le pagine (una riga che lancia, un import mancante, una
// chiave dello schema assente) non faceva fallire niente.
// The smoke tests extract single methods of prefs.js; nothing built the whole
// window, so an error that only shows when all the pages are composed (a row
// that throws, a missing import, a missing schema key) failed nothing.
//
// ISOLAMENTO / ISOLATION: richiede XDG_DATA_HOME e GSETTINGS_BACKEND=memory
// (li imposta la gate, con una directory brv-prefs-*) e rifiuta di girare altrimenti, cosi' non tocca mai il
// venv, la config o dconf dell'utente. Il "venv" e' uno script che lancia il
// backend vero con HOME e config temporanei.
// It requires XDG_DATA_HOME and GSETTINGS_BACKEND=memory (the gate sets them, with a brv-prefs-* directory)
// and refuses to run otherwise, so it never touches the user's venv, config or
// dconf. The "venv" is a script that runs the real backend with a temporary
// HOME and config.
import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

Adw.init();

let pass = 0;
let fail = 0;
function check(name, cond) {
    if (cond) {
        pass++;
        console.log(`  PASS  ${name}`);
    } else {
        fail++;
        console.log(`  FAIL  ${name}`);
    }
}
function finish() {
    console.log('');
    console.log(`${pass} PASS / ${fail} FAIL`);
    imports.system.exit(fail ? 1 : 0);
}

const here = GLib.path_get_dirname(import.meta.url.replace('file://', ''));
const repo = GLib.path_get_dirname(here);
const extDir = `${repo}/gnome-extension/bravoric-indicator@local`;
const dataHome = GLib.getenv('XDG_DATA_HOME');

if (!dataHome || !dataHome.includes('/brv-prefs-') || GLib.getenv('GSETTINGS_BACKEND') !== 'memory') {
    console.log('  FAIL  serve XDG_DATA_HOME in una directory temporanea brv-prefs-* e GSETTINGS_BACKEND=memory (isolamento) / '
        + 'needs XDG_DATA_HOME in a temporary brv-prefs-* directory and GSETTINGS_BACKEND=memory (isolation)');
    imports.system.exit(1);
}

function writeFile(path, text, mode = null) {
    GLib.mkdir_with_parents(GLib.path_get_dirname(path), 0o755);
    GLib.file_set_contents(path, text);
    if (mode !== null)
        GLib.chmod(path, mode);
}
function readFile(path) {
    return new TextDecoder().decode(GLib.file_get_contents(path)[1]);
}

// "venv" finto: lancia il backend vero su una HOME temporanea con la config di esempio.
// Fake "venv": runs the real backend on a temporary HOME with the example config.
const fakeHome = `${dataHome}/fake-home`;
writeFile(`${fakeHome}/.config/bravoric-stt-clipboard/config.toml`, readFile(`${repo}/config/config.example.toml`), 0o600);
writeFile(`${dataHome}/bravoric-stt-clipboard/venv/bin/bravoric-config-editor`,
    `#!/usr/bin/env bash\nexport HOME='${fakeHome}' XDG_CONFIG_HOME='${fakeHome}/.config' PYTHONPATH='${repo}/src'\n`
    + 'exec python3 -m bravoric_stt_clipboard.config_editor "$@"\n', 0o755);

// gettext neutro e base finta di ExtensionPreferences con GSettings vere (backend memory).
// Neutral gettext and fake ExtensionPreferences base with real GSettings (memory backend).
writeFile(`${dataHome}/stub-prefs.mjs`, `
import Gio from 'gi://Gio';
export const gettext = s => s;
export class ExtensionPreferences {
    getSettings() {
        const source = Gio.SettingsSchemaSource.new_from_directory(
            '${extDir}/schemas', Gio.SettingsSchemaSource.get_default(), false);
        return new Gio.Settings({ settings_schema: source.lookup('org.gnome.shell.extensions.bravoric-indicator', false) });
    }
}
`);

let source = readFile(`${extDir}/prefs.js`);
source = source
    .replace("'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js'", `'file://${dataHome}/stub-prefs.mjs'`)
    .replace("'./stream-consumer.mjs'", `'file://${extDir}/stream-consumer.mjs'`);
writeFile(`${dataHome}/prefs-under-test.mjs`, source);

const logged = [];
globalThis.logError = (e, msg) => logged.push(`${msg}: ${e?.message ?? e}`);

console.log('== prefs.js intero / whole prefs.js ==');
let module;
try {
    module = await import(`file://${dataHome}/prefs-under-test.mjs`);
    check('prefs.js e\' importabile per intero (sintassi e struttura)', true);
} catch (e) {
    check(`prefs.js e' importabile per intero: ${e.message}`, false);
    finish();
}

const Prefs = module.default;
const prefs = new Prefs();
const pages = [];
const window = new Proxy({ add(page) { pages.push(page.title); } }, {
    get: (t, k) => (k in t ? t[k] : () => {}),
});

let threw = null;
try {
    prefs.fillPreferencesWindow(window);
} catch (e) {
    threw = e;
    console.log(`    ${e.stack ?? e.message}`);
}
check('fillPreferencesWindow non solleva col backend vero', threw === null);
console.log(`    pagine / pages: ${pages.join(' | ')}`);
check('tutte le pagine sono costruite (Notifiche, Generale, Scorciatoie, Stream, Archivio, Icone, Servizi)',
    pages.length >= 7 && pages.includes('General') && pages.includes('Notifications'));
check('nessun errore loggato durante la costruzione', logged.length === 0);
if (logged.length)
    console.log(logged.map(l => `    ${l}`).join('\n'));

// La pagina General usa lo schema vero: le chiavi dei bottoni rapidi devono esserci.
// The General page uses the real schema: the quick-button keys must be there.
const source2 = Gio.SettingsSchemaSource.new_from_directory(
    `${extDir}/schemas`, Gio.SettingsSchemaSource.get_default(), false);
const schema = source2.lookup('org.gnome.shell.extensions.bravoric-indicator', false);
check('lo schema compilato ha le chiavi dei tre bottoni rapidi',
    ['show-dictation-button', 'show-ocr-button', 'show-stream-button'].every(k => schema.has_key(k)));

finish();
