#!/usr/bin/env node
// test-quick-buttons.js — bottoni rapidi della top bar (un click, niente menu).
// Quick buttons of the top bar (one click, no menu).
//
// Il modulo quick-buttons.mjs e' puro: qui viene importato ed ESEGUITO con
// widget finti che annotano cio' che gli succede, senza una sessione GNOME
// Shell. Le verifiche sul sorgente di extension.js (cablaggio) sono
// separate e strutturali.
// The quick-buttons.mjs module is pure: here it is imported and RUN with fake
// widgets that note what happens to them, without a GNOME Shell session. The
// checks on the extension.js source (wiring) are separate and structural.
'use strict';
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

const EXT = path.join(__dirname, '..', 'gnome-extension', 'bravoric-hear-read-write@riccardomurru.it');
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

(async () => {
    const { QUICK_BUTTONS, quickButtonActive, quickButtonSensitive, createQuickButtons } =
        await import(pathToFileURL(path.join(EXT, 'quick-buttons.mjs')));
    const by = key => QUICK_BUTTONS.find(s => s.key === key);

    console.log('== tabella dei bottoni / button table ==');
    check('tre bottoni: dettatura, OCR, streaming',
        QUICK_BUTTONS.map(s => s.key).join(',') === 'dictation,ocr,stream');
    check('ogni bottone ha comando del backend e chiave GSettings distinti',
        new Set(QUICK_BUTTONS.map(s => s.command)).size === 3
        && new Set(QUICK_BUTTONS.map(s => s.setting)).size === 3);
    check('i comandi sono gli stessi delle scorciatoie',
        by('dictation').command === 'bravoric-stt-toggle'
        && by('ocr').command === 'bravoric-ocr-capture'
        && by('stream').command === 'bravoric-stream-toggle');

    console.log('== sensibilita\' e stato attivo / sensitivity and active state ==');
    for (const spec of QUICK_BUTTONS) {
        check(`${spec.key}: cliccabile da idle e da error`,
            quickButtonSensitive(spec, 'idle', null) && quickButtonSensitive(spec, 'error', null));
        check(`${spec.key}: non cliccabile durante l'elaborazione`,
            !quickButtonSensitive(spec, 'processing', 'stt') && !quickButtonSensitive(spec, 'processing', 'ocr'));
    }
    check('dettatura: registrando con service stt e\' cliccabile (il click e\' lo stop) e attiva',
        quickButtonSensitive(by('dictation'), 'recording', 'stt') && quickButtonActive(by('dictation'), 'recording', 'stt'));
    check('streaming: registrando con service stream e\' cliccabile e attivo',
        quickButtonSensitive(by('stream'), 'recording', 'stream') && quickButtonActive(by('stream'), 'recording', 'stream'));
    check('dettatura non e\' cliccabile mentre registra lo streaming (altro servizio)',
        !quickButtonSensitive(by('dictation'), 'recording', 'stream') && !quickButtonActive(by('dictation'), 'recording', 'stream'));
    check('streaming non e\' cliccabile mentre registra la dettatura',
        !quickButtonSensitive(by('stream'), 'recording', 'stt'));
    check('OCR non ha uno stop: mai cliccabile ne\' attivo mentre si registra',
        !quickButtonSensitive(by('ocr'), 'recording', 'ocr') && !quickButtonActive(by('ocr'), 'recording', 'ocr'));
    check('service assente durante recording: nessun bottone attivo',
        QUICK_BUTTONS.every(s => !quickButtonActive(s, 'recording', null)));

    console.log('== controller: sync / update / destroy ==');
    const log = [];
    let flags = {};
    const spawned = [];
    const handles = {};
    const deps = {
        readBool: key => !!flags[key],
        makeButton: (spec, onClick) => {
            const h = {
                spec, onClick, sensitive: null, active: null, destroyed: false,
                setSensitive(b) { this.sensitive = b; },
                setActive(b) { this.active = b; },
                destroy() { this.destroyed = true; log.push(`destroy:${spec.key}`); },
            };
            handles[spec.key] = h;
            log.push(`make:${spec.key}`);
            return h;
        },
        spawn: cmd => spawned.push(cmd),
    };
    const ctl = createQuickButtons(deps);

    ctl.sync();
    check('tutte spente (default): nessun bottone costruito', ctl.keys().length === 0 && log.length === 0);

    flags = { 'show-dictation-button': true, 'show-stream-button': true };
    ctl.sync();
    check('solo i bottoni voluti vengono costruiti', ctl.keys().sort().join(',') === 'dictation,stream');
    check('ordine di creazione inverso (il primo della lista resta il piu\' a sinistra)',
        log.join('|') === 'make:stream|make:dictation');

    log.length = 0;
    ctl.sync();
    check('sync senza cambiamenti non ricostruisce nulla (niente sfarfallio)', log.length === 0);

    handles.dictation.onClick();
    handles.stream.onClick();
    check('il click lancia il comando del backend giusto',
        spawned.join(',') === 'bravoric-stt-toggle,bravoric-stream-toggle');

    check('appena costruiti seguono lo stato iniziale idle: cliccabili e non attivi',
        handles.dictation.sensitive === true && handles.dictation.active === false);

    ctl.update('recording', 'stt');
    check('update: la dettatura in registrazione e\' attiva e cliccabile, lo streaming no',
        handles.dictation.active === true && handles.dictation.sensitive === true
        && handles.stream.active === false && handles.stream.sensitive === false);

    ctl.update('processing', 'stt');
    check('update: in elaborazione nessuno e\' cliccabile',
        handles.dictation.sensitive === false && handles.stream.sensitive === false
        && handles.dictation.active === false);

    ctl.update('idle', undefined);
    check('update: di nuovo idle, tutti cliccabili e non attivi',
        handles.dictation.sensitive && handles.stream.sensitive && !handles.dictation.active);

    log.length = 0;
    flags = { 'show-ocr-button': true };
    ctl.sync();
    check('cambio dell\'insieme: prima si distrugge il vecchio, poi si costruisce il nuovo',
        log.join('|') === 'destroy:dictation|destroy:stream|make:ocr'
        || log.join('|') === 'destroy:stream|destroy:dictation|make:ocr'
        || (log.length === 3 && log[2] === 'make:ocr' && log.slice(0, 2).every(l => l.startsWith('destroy:'))));

    ctl.update('recording', 'stt');
    check('lo stato corrente si applica anche ai bottoni appena ricreati',
        handles.ocr.sensitive === false);

    log.length = 0;
    ctl.destroy();
    check('destroy rimuove tutti i bottoni e li dimentica',
        ctl.keys().length === 0 && log.join('|') === 'destroy:ocr');
    ctl.sync();
    check('dopo destroy un nuovo sync ricostruisce dal nulla', ctl.keys().join(',') === 'ocr');

    console.log('== cablaggio in extension.js (struttura) / wiring in extension.js (structure) ==');
    const ext = fs.readFileSync(path.join(EXT, 'extension.js'), 'utf8');
    check('extension.js importa il modulo puro',
        ext.includes("from './quick-buttons.mjs'"));
    check('_refreshStatus passa stato e servizio al controller',
        ext.includes('this._extension?._quick?.update(state, data.service);'));
    check('il widget e\' un PanelMenu.Button SENZA menu (nessun grab modale)',
        /new PanelMenu\.Button\(0\.0, `\$\{uuid\} \$\{spec\.key\}`, true\)/.test(ext));
    check('il click passa da St.Button (mouse e tocco)',
        ext.includes("inner.connect('clicked', () => onClick());"));
    check('disable() disconnette i segnali e distrugge i bottoni',
        /disable\(\) \{[\s\S]*?this\._settings\?\.disconnect\(id\)[\s\S]*?this\._quick\?\.destroy\(\)/.test(ext));
    check('i segnali sono collegati solo se la chiave esiste nello schema (schema stantio)',
        ext.includes('.filter(spec => quickSchema?.has_key(spec.setting))'));
    check('senza la chiave (schema stantio) il default e\' falso: nessun bottone',
        ext.includes('readBool: key => settingBool(key, false)'));

    console.log('');
    console.log(`${pass} PASS / ${fail} FAIL`);
    process.exit(fail ? 1 : 0);
})();
