#!/usr/bin/env node
// test-extension-lifecycle.js — carica DAVVERO extension.js ed esegue
// enable()/disable() con stub di GNOME Shell.
// Loads the REAL extension.js and runs enable()/disable() with GNOME Shell stubs.
//
// PERCHE' ESISTE / WHY IT EXISTS: gli altri test estraggono singoli metodi da
// extension.js e li valutano, quindi non vedono mai il file intero. Un errore di
// sintassi o di struttura del modulo (es. due funzioni finite dentro
// GObject.registerClass( ... )) faceva sparire l'estensione all'avvio senza che
// nessun test diventasse rosso. Qui il modulo viene importato per intero (con gli
// import di GNOME riscritti verso gli stub) e il ciclo di vita e' eseguito.
// The other tests extract single methods from extension.js and evaluate them,
// so they never see the whole file. A syntax or structure error of the module
// (e.g. two functions ending up inside GObject.registerClass( ... )) made the
// extension vanish at startup without any test turning red. Here the module is
// imported whole (with the GNOME imports rewritten to the stubs) and the
// lifecycle is run.
//
// LIMITE / LIMIT: i widget sono finti, quindi non si prova il rendering; e le
// dipendenze di GNOME sono "permissive" (rispondono a tutto): serve a far girare
// il codice del modulo, non a verificare l'API di GNOME.
// The widgets are fake, so the rendering is not proven; and the GNOME
// dependencies are "permissive" (they answer anything): it serves to run the
// module's code, not to verify GNOME's API.
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { pathToFileURL } = require('url');

const EXT_DIR = path.join(__dirname, '..', 'gnome-extension', 'bravoric-indicator@local');
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

// Stub universale: qualunque proprieta', chiamata o costruzione restituisce se'
// stesso. Cosi' il codice che tocca St/Gio/Clutter/... gira senza sessione.
// Universal stub: any property, call or construction returns itself. So the
// code that touches St/Gio/Clutter/... runs without a session.
function makeAny() {
    const target = function () {};
    const any = new Proxy(target, {
        get: (t, key) => {
            if (key === Symbol.toPrimitive)
                return () => 'any';
            if (key === 'toString' || key === 'valueOf')
                return () => 'any';
            if (key === 'then')
                return undefined;
            return any;
        },
        set: () => true,
        apply: () => any,
        construct: () => any,
    });
    return any;
}
const any = makeAny();

// Impostazioni finte con i default REALI dello schema (letti dall'XML).
// Fake settings with the REAL schema defaults (read from the XML).
function makeSettings(keysPresent) {
    const xml = fs.readFileSync(
        path.join(EXT_DIR, 'schemas', 'org.gnome.shell.extensions.bravoric-indicator.gschema.xml'), 'utf8');
    const values = {};
    for (const m of xml.matchAll(/<key name="([^"]+)" type="(\w+)">\s*<default>([^<]*)<\/default>/g)) {
        const [, name, type, def] = m;
        values[name] = type === 'b' ? def === 'true' : type === 'i' ? Number(def) : def;
    }
    const handlers = new Map();
    let nextId = 1;
    const settings = {
        values,
        handlers,
        settings_schema: { has_key: k => keysPresent(k) && k in values },
        get_boolean: k => values[k],
        get_int: k => values[k],
        get_strv: () => [],
        connect: (signal, fn) => { handlers.set(nextId, [signal, fn]); return nextId++; },
        disconnect: id => handlers.delete(id),
        fire(signal) { for (const [s, fn] of [...handlers.values()]) if (s === signal) fn(); },
    };
    return settings;
}

async function loadExtensionModule() {
    // Riscrive gli import: gi:// e resource:// verso gli stub, i moduli locali
    // verso il loro URL assoluto reale (cosi' vengono eseguiti davvero).
    // Rewrites the imports: gi:// and resource:// to the stubs, the local
    // modules to their real absolute URL (so they are really run).
    let src = fs.readFileSync(path.join(EXT_DIR, 'extension.js'), 'utf8');
    src = src.replace(/^import\s+([^;]+?)\s+from\s+'([^']+)';$/gm, (whole, binding, spec) => {
        if (spec.startsWith('./'))
            return `import ${binding} from '${pathToFileURL(path.join(EXT_DIR, spec))}';`;
        const mod = `globalThis.__stubs['${spec}']`;
        if (binding.startsWith('* as '))
            return `const ${binding.slice(5)} = ${mod};`;
        if (binding.startsWith('{'))
            return `const ${binding.replace(/(\w+)\s+as\s+(\w+)/g, '$1: $2')} = ${mod};`;
        return `const ${binding} = ${mod};`;
    });
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bravoric-lifecycle-'));
    const file = path.join(dir, 'extension-under-test.mjs');
    fs.writeFileSync(file, src);
    return { module: await import(pathToFileURL(file)), dir };
}

(async () => {
    // --- registro di cio' che le stub vedono / record of what the stubs see ---
    const panelRoles = [];
    const destroyed = [];
    const keybindings = [];
    const removedKeybindings = [];
    const spawned = [];
    const widgets = [];

    class FakeWidget {
        constructor(props = {}) {
            Object.assign(this, props);
            this.handlers = {};
            this.classes = new Set();
            widgets.push(this);
        }
        connect(signal, fn) { this.handlers[signal] = fn; return 1; }
        add_child(child) { this.child = child; }
        add_style_class_name(c) { this.classes.add(c); }
        remove_style_class_name(c) { this.classes.delete(c); }
        destroy() { this.isDestroyed = true; destroyed.push(this.role ?? this.name ?? 'widget'); }
    }
    // Base finta di PanelMenu.Button: risponde a qualsiasi metodo, ma tiene i
    // campi che il codice legge (menu solo se dontCreateMenu e' falso).
    // Fake base of PanelMenu.Button: answers any method, but keeps the fields
    // the code reads (menu only if dontCreateMenu is false).
    class FakePanelButton {
        constructor(alignment, name, dontCreateMenu) {
            this.name = name;
            this.dontCreateMenu = dontCreateMenu === true;
            this.menu = this.dontCreateMenu ? null : any;
            this.handlers = {};
            this.destroyedFlag = false;
            return new Proxy(this, {
                get: (t, key) => (key in t ? t[key] : (typeof key === 'symbol' ? undefined : any)),
                set: (t, key, value) => { t[key] = value; return true; },
            });
        }
        // Stile GObject: le sottoclassi registrate chiamano super._init(...).
        // GObject style: the registered subclasses call super._init(...).
        _init(alignment, name) { this.name = name; this.menu = any; }
        connect(signal, fn) { this.handlers[signal] = fn; return 1; }
        add_child(child) { this.child = child; }
        destroy() { this.destroyedFlag = true; destroyed.push(this.name); }
    }
    class FakeExtension {
        constructor(metadata) {
            this.uuid = metadata.uuid;
            this.path = metadata.path;
            this._settings = metadata.settings;
        }
        getSettings() { return this._settings; }
        openPreferences() {}
    }
    const Main = {
        panel: {
            addToStatusArea(role, indicator) { panelRoles.push(role); indicator.role = role; },
        },
        wm: {
            addKeybinding(name) { keybindings.push(name); },
            removeKeybinding(name) { removedKeybindings.push(name); },
        },
        notify() {}, notifyError() {},
        osdWindowManager: any,
    };
    const GLib = new Proxy({
        build_filenamev: parts => parts.join('/'),
        get_home_dir: () => '/home/test',
        get_user_data_dir: () => '/home/test/.local/share',
        file_test: () => true,
        FileTest: { EXISTS: 1 },
        timeout_add_seconds: () => 1,
        timeout_add: () => 1,
        source_remove: () => true,
        SOURCE_CONTINUE: true, SOURCE_REMOVE: false, PRIORITY_DEFAULT: 0,
    }, { get: (t, k) => (k in t ? t[k] : any) });
    const Gio = new Proxy({
        Subprocess: { new: (argv) => { spawned.push(argv[0].split('/').pop()); return {}; } },
        SubprocessFlags: { NONE: 0 },
        SettingsBindFlags: { DEFAULT: 0 },
    }, { get: (t, k) => (k in t ? t[k] : any) });
    const St = new Proxy({ Icon: FakeWidget, Button: FakeWidget }, { get: (t, k) => (k in t ? t[k] : any) });
    const PanelMenu = { Button: FakePanelButton };
    const GObject = {
        registerClass: cls => class extends cls {
            constructor(...args) { super(...args); this._init(...args); }
        },
    };

    // logError e' un globale di GJS: qui raccoglie gli errori loggati.
    // logError is a GJS global: here it collects the logged errors.
    const logged = [];
    globalThis.logError = (e, msg) => logged.push(`${msg}: ${e?.message}`);

    globalThis.__stubs = {
        'gi://GObject': GObject,
        'gi://St': St,
        'gi://Gio': Gio,
        'gi://GLib': GLib,
        'gi://Meta': any,
        'gi://Shell': any,
        'gi://Clutter': any,
        'resource:///org/gnome/shell/extensions/extension.js': { Extension: FakeExtension, gettext: s => s },
        'resource:///org/gnome/shell/ui/panelMenu.js': PanelMenu,
        'resource:///org/gnome/shell/ui/popupMenu.js': any,
        'resource:///org/gnome/shell/ui/main.js': Main,
    };

    console.log('== il modulo intero si carica / the whole module loads ==');
    let loaded;
    try {
        loaded = await loadExtensionModule();
        check('extension.js e\' importabile per intero (sintassi e struttura del modulo)', true);
    } catch (e) {
        check(`extension.js e' importabile per intero: ${e.message}`, false);
        process.exit(1);
    }
    const ExtensionClass = loaded.module.default;
    check('l\'export di default e\' la classe Extension', typeof ExtensionClass === 'function'
        && typeof ExtensionClass.prototype.enable === 'function'
        && typeof ExtensionClass.prototype.disable === 'function');

    const quickRoles = () => panelRoles.filter(r => r.includes('-quick-'));
    const build = (keysPresent = () => true) => {
        const settings = makeSettings(keysPresent);
        const ext = new ExtensionClass({ uuid: 'bravoric-indicator@local', path: EXT_DIR, settings });
        return { ext, settings };
    };

    console.log('== enable() con i default (bottoni rapidi spenti) / enable() with defaults ==');
    {
        const { ext, settings } = build();
        ext.enable();
        check('l\'indicatore principale e\' aggiunto alla top bar', panelRoles.includes('bravoric-indicator@local'));
        check('con i default nessun bottone rapido viene creato', quickRoles().length === 0);
        check('le tre scorciatoie sono registrate', keybindings.join(',') === 'dictation-shortcut,ocr-shortcut,stream-shortcut');
        check('un segnale changed:: per ciascuna delle tre chiavi dei bottoni',
            [...settings.handlers.values()].filter(([s]) => /^changed::show-.*-button$/.test(s)).length === 3);

        console.log('== accendere i bottoni dalla GUI (segnale changed::) / turning buttons on ==');
        settings.values['show-dictation-button'] = true;
        settings.fire('changed::show-dictation-button');
        check('accesa la dettatura: compare solo il suo bottone', quickRoles().join(',') === 'bravoric-indicator@local-quick-dictation');
        const dictation = widgets.filter(w => w.style_class === 'bravoric-quick-button').at(-1);
        check('il bottone e\' un St.Button con stile touch e nome accessibile',
            dictation && dictation.accessible_name === 'Start dictation' && dictation.reactive === true);

        dictation.handlers.clicked();
        check('il click avvia bravoric-stt-toggle', spawned.at(-1) === 'bravoric-stt-toggle');

        settings.values['show-ocr-button'] = true;
        settings.values['show-stream-button'] = true;
        panelRoles.length = 0;
        settings.fire('changed::show-ocr-button');
        check('accese tutte e tre: ricostruite in ordine inverso (il primo resta a sinistra)',
            quickRoles().join(',') === [
                'bravoric-indicator@local-quick-stream',
                'bravoric-indicator@local-quick-ocr',
                'bravoric-indicator@local-quick-dictation',
            ].join(','));
        const byName = name => widgets.filter(w => w.style_class === 'bravoric-quick-button' && !w.isDestroyed
            && (w.accessible_name ?? '').includes(name)).at(-1);
        byName('OCR').handlers.clicked();
        byName('streaming').handlers.clicked();
        check('OCR e streaming lanciano il proprio comando',
            spawned.slice(-2).join(',') === 'bravoric-ocr-capture,bravoric-stream-toggle');

        console.log('== stato: registrazione / state: recording ==');
        ext._quick.update('recording', 'stt');
        const dictBtn = byName('dictation');
        check('in registrazione la dettatura resta cliccabile, diventa "Stop dictation" ed e\' rossa',
            dictBtn.reactive === true && dictBtn.accessible_name === 'Stop dictation'
            && dictBtn.child.classes.has('bravoric-recording-icon'));
        check('OCR e streaming non sono cliccabili durante la registrazione della dettatura',
            byName('OCR').reactive === false && byName('streaming').reactive === false);
        ext._quick.update('idle', null);
        check('tornati a idle tutti cliccabili e non piu\' rossi',
            byName('OCR').reactive === true && !byName('dictation').child.classes.has('bravoric-recording-icon'));

        console.log('== disable() ==');
        destroyed.length = 0;
        ext.disable();
        check('disable() distrugge i tre bottoni rapidi',
            destroyed.filter(n => n.includes(' dictation') || n.includes(' ocr') || n.includes(' stream')).length === 3);
        check('disable() disconnette tutti i segnali delle impostazioni', settings.handlers.size === 0);
        check('disable() rimuove le tre scorciatoie', removedKeybindings.join(',') === 'dictation-shortcut,ocr-shortcut,stream-shortcut');
        check('disable() distrugge anche l\'indicatore principale', destroyed.includes('Bravoric STT/OCR'));
    }

    console.log('== schema stantio (chiavi dei bottoni assenti) / stale schema ==');
    {
        panelRoles.length = 0;
        const staleKeys = k => !/^show-.*-button$/.test(k);
        const { ext, settings } = build(staleKeys);
        let threw = null;
        try {
            ext.enable();
        } catch (e) {
            threw = e;
        }
        check('enable() non solleva con uno schema senza le chiavi dei bottoni', threw === null);
        check('nessun bottone e nessun segnale per le chiavi assenti',
            quickRoles().length === 0
            && [...settings.handlers.values()].every(([s]) => !/show-.*-button/.test(s)));
        let threwOff = null;
        try {
            ext.disable();
        } catch (e) {
            threwOff = e;
        }
        check('disable() non solleva neppure con lo schema stantio', threwOff === null);
    }

    check('nessun errore inatteso e\' stato loggato durante tutto il ciclo di vita',
        logged.length === 0);
    if (logged.length)
        console.log(logged.map(l => '    ' + l).join('\n'));

    console.log('');
    console.log(`${pass} PASS / ${fail} FAIL`);
    process.exit(fail ? 1 : 0);
})();
