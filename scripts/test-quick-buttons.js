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
    const { QUICK_BUTTONS, quickButtonActive, quickButtonSensitive, quickButtonMode, createQuickButtons, PENDING_START_MS } =
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
        check(`${spec.key}: non cliccabile durante l'elaborazione di un ALTRO servizio`,
            !quickButtonSensitive(spec, 'processing', spec.service === 'stt' ? 'ocr' : 'stt'));
    }
    check('dettatura: registrando con service stt e\' cliccabile (il click e\' lo stop) e attiva',
        quickButtonSensitive(by('dictation'), 'recording', 'stt') && quickButtonActive(by('dictation'), 'recording', 'stt'));
    check('streaming: registrando con service stream e\' cliccabile e attivo',
        quickButtonSensitive(by('stream'), 'recording', 'stream') && quickButtonActive(by('stream'), 'recording', 'stream'));
    check('dettatura non e\' cliccabile mentre registra lo streaming (altro servizio)',
        !quickButtonSensitive(by('dictation'), 'recording', 'stream') && !quickButtonActive(by('dictation'), 'recording', 'stream'));
    check('streaming non e\' cliccabile mentre registra la dettatura',
        !quickButtonSensitive(by('stream'), 'recording', 'stt'));
    check('OCR: in elaborazione (selezione o richiesta) e\' attivo e cliccabile: il click ANNULLA',
        quickButtonActive(by('ocr'), 'processing', 'ocr') && quickButtonSensitive(by('ocr'), 'processing', 'ocr'));
    check('OCR: con cancellable=false (gia\' alla scrittura negli appunti) non e\' piu\' annullabile',
        !quickButtonSensitive(by('ocr'), 'processing', 'ocr', false) && quickButtonMode(by('ocr'), 'processing', 'ocr', false) === 'busy');
    check('dettatura: elaborazione (trascrizione) non annullabile: non cliccabile',
        quickButtonMode(by('dictation'), 'processing', 'stt') === 'busy');
    check('recording senza service (status vecchio): la dettatura resta fermabile, gli altri no',
        quickButtonActive(by('dictation'), 'recording', null) && quickButtonActive(by('dictation'), 'recording', undefined)
        && !quickButtonActive(by('ocr'), 'recording', null) && !quickButtonActive(by('stream'), 'recording', null));
    check('recording con service errato: la dettatura non e\' attiva (e\' un altro servizio)',
        quickButtonMode(by('dictation'), 'recording', 'stream') === 'busy');
    check('start/stop ESPLICITI: nessun comando e\' un toggle cieco (tranne lo start dello streaming)',
        by('dictation').startArgs.join() === 'start' && by('dictation').stopArgs.join() === 'stop'
        && by('ocr').startArgs.join() === 'start' && by('ocr').stopArgs.join() === 'cancel'
        && by('stream').startArgs.length === 0 && by('stream').stopArgs.join() === 'stop');

    console.log('== controller: sync / update / destroy ==');
    const log = [];
    let flags = {};
    const spawned = [];
    const handles = {};
    let spawnResult = true;
    let clock = 1000;
    const timersLog = [];
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
        spawn: (cmd, args) => { spawned.push([cmd, ...args].join(' ')); return spawnResult; },
        now: () => clock,
        later: (ms, fn) => { const t = { ms, fn, cancelled: false }; timersLog.push(t); return () => { t.cancelled = true; }; },
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
    check('il click da idle lancia il comando ESPLICITO giusto (start / toggle per lo streaming)',
        spawned.join('|') === 'bravoric-stt-toggle start|bravoric-stream-toggle');

    check('dopo il click di avvio la dettatura mostra SUBITO "stop" (finestra prima che status.json dica recording)',
        handles.dictation.active === true && handles.dictation.sensitive === true && ctl.mode('dictation') === 'stop');
    check('lo streaming non ha finestra di avvio: resta start finche\' non arriva lo stato',
        handles.stream.active === false && ctl.mode('stream') === 'start');
    spawned.length = 0;
    handles.dictation.onClick();
    check('secondo click PRIMA di recording in status.json = stop esplicito (mai un secondo start)',
        spawned.join('|') === 'bravoric-stt-toggle stop');
    check('dopo lo stop la finestra si chiude: torna start (nessun flag latched)',
        ctl.mode('dictation') === 'start' && handles.dictation.active === false);
    check('la finestra e\' chiusa dal timer, non solo dal prossimo stato: 1 timer annullato e 1 attivo',
        timersLog.filter(t => t.cancelled).length >= 1);
    spawned.length = 0;
    handles.dictation.onClick();
    ctl.update('recording', 'stt');
    handles.dictation.onClick();
    check('recording + service stt: il click e\' stop esplicito',
        spawned.join('|') === 'bravoric-stt-toggle start|bravoric-stt-toggle stop');
    ctl.update('idle', undefined);
    spawned.length = 0;
    handles.dictation.onClick();
    ctl.update('recording', undefined);
    handles.dictation.onClick();
    check('recording SENZA service: il click e\' comunque stop (mai start)',
        spawned.join('|') === 'bravoric-stt-toggle start|bravoric-stt-toggle stop');
    ctl.update('idle', undefined);
    spawned.length = 0;
    handles.dictation.onClick();
    ctl.update('error', 'stt');
    check('errore dopo lo start: la finestra "in corso" cade, il bottone e\' di nuovo start (riprova)',
        ctl.mode('dictation') === 'start' && handles.dictation.active === false && handles.dictation.sensitive === true);
    spawned.length = 0;
    spawnResult = false;
    ctl.update('idle', undefined);
    handles.dictation.onClick();
    check('spawn fallito (binario mancante): nessuna finestra latched, resta start',
        ctl.mode('dictation') === 'start' && handles.dictation.active === false);
    spawnResult = true;
    handles.dictation.onClick();
    clock += PENDING_START_MS + 1;
    check('finestra scaduta senza stato: il click torna start (mai bloccato su stop)', ctl.mode('dictation') === 'start');
    const timer = timersLog[timersLog.length - 1];
    timer.fn();
    check('il timer scaduto riallinea il bottone senza aspettare il prossimo evento',
        handles.dictation.active === false);
    ctl.update('idle', undefined);
    spawned.length = 0;
    ctl.update('recording', 'stream');
    handles.stream.onClick();
    handles.dictation.onClick();
    check('streaming attivo: il suo click e\' `stop`; la dettatura (altro servizio) non parte',
        spawned.join('|') === 'bravoric-stream-toggle stop');
    ctl.update('idle', undefined);
    spawned.length = 0;
    ctl.update('processing', 'stt');
    handles.dictation.onClick();
    check('processing stt: il click non lancia nulla (elaborazione non annullabile)', spawned.length === 0);
    ctl.update('idle', undefined);

    ctl.update('recording', 'stt');
    check('update: la dettatura in registrazione e\' attiva e cliccabile, lo streaming no',
        handles.dictation.active === true && handles.dictation.sensitive === true
        && handles.stream.active === false && handles.stream.sensitive === false);

    ctl.update('processing', 'stt');
    check('update: in elaborazione stt nessuno e\' cliccabile',
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
    spawned.length = 0;
    ctl.update('processing', 'ocr');
    handles.ocr.onClick();
    check('OCR in elaborazione: il click lancia `bravoric-ocr-capture cancel`',
        spawned.join('|') === 'bravoric-ocr-capture cancel' && handles.ocr.active === true);
    ctl.update('processing', 'ocr', false);
    spawned.length = 0;
    handles.ocr.onClick();
    check('OCR non annullabile (cancellable=false): non cliccabile, nessun comando',
        handles.ocr.sensitive === false && spawned.length === 0);
    ctl.update('idle', undefined);
    handles.ocr.onClick();
    check('OCR idle: il click lancia `bravoric-ocr-capture start`', spawned.join('|') === 'bravoric-ocr-capture start');

    log.length = 0;
    ctl.destroy();
    check('destroy rimuove tutti i bottoni e li dimentica',
        ctl.keys().length === 0 && log.join('|') === 'destroy:ocr');
    ctl.sync();
    check('dopo destroy un nuovo sync ricostruisce dal nulla', ctl.keys().join(',') === 'ocr');

    // Reload/disable con un bottone in attesa: destroy() deve annullare i timer (nessun flag latched).
    // Reload/disable with a button waiting: destroy() must cancel the timers (no latched flag).
    flags = { 'show-dictation-button': true };
    ctl.sync();
    ctl.update('idle', undefined);
    const timersBefore = timersLog.length;
    handles.dictation.onClick();
    check('click di avvio: un timer di scadenza della finestra e\' armato', timersLog.length === timersBefore + 1 && !timersLog[timersLog.length - 1].cancelled);
    ctl.destroy();
    check('destroy (disable/reload dell\'estensione) annulla il timer: nulla resta armato', timersLog[timersLog.length - 1].cancelled === true);
    ctl.sync();
    check('dopo il reload il bottone riparte da start, senza stato ereditato', ctl.mode('dictation') === 'start');
    ctl.destroy();

    console.log('== cablaggio in extension.js (struttura) / wiring in extension.js (structure) ==');
    const ext = fs.readFileSync(path.join(EXT, 'extension.js'), 'utf8');
    check('extension.js importa il modulo puro',
        ext.includes("from './quick-buttons.mjs'"));
    check('_refreshStatus passa stato, servizio e cancellable al controller',
        ext.includes('this._extension?._quick?.update(state, data.service, this._uiCancellable);'));
    check('il menu usa lo stesso modello dei bottoni (quickButtonMode)',
        ext.includes("quickButtonMode(spec, this._uiState, this._uiService, this._uiCancellable)") && ext.includes('_runMenuAction'));
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
