#!/usr/bin/env node
// Test logico di timeout di stato + sensitivity voci menu dell'indicatore GNOME.
//
// Le costanti sono estratte dal VERO extension.js: il test fallisce se qualcuno
// le cambia nel sorgente senza aggiornare le aspettative (guardia anti-drift).
// `decide()` è una porta fedele del blocco in _refreshStatus() +
// _timeoutLimitFor()/_timeoutMessage().
//
// Uso: node scripts/test-timeout-logic.js
// Logic test of state timeouts + menu-entry sensitivity of the GNOME
// indicator.
//
// The constants are extracted from the REAL extension.js: the test fails if
// someone changes them in the source without updating the expectations
// (anti-drift guard). `decide()` is a faithful port of the block in
// _refreshStatus() + _timeoutLimitFor()/_timeoutMessage().
//
// Usage: node scripts/test-timeout-logic.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { matchBrace } = require('./lib/brace-match.cjs');

const EXT_DIR = path.join(__dirname, '..', 'gnome-extension', 'bravoric-hear-read-write@riccardomurru.it');
const SRC = path.join(EXT_DIR, 'extension.js');
const src = fs.readFileSync(SRC, 'utf8');

// Estrae il corpo di `const NAME = { ... }`: accetta `chiave: N` e `chiave: N * M`.
// Extracts the body of `const NAME = { ... }`: accepts `key: N` and
// `key: N * M`.
function extractMap(name) {
    const m = src.match(new RegExp(`const ${name} = \\{([^}]*)\\}`));
    if (!m)
        throw new Error(`costante ${name} non trovata in extension.js`);
    const out = {};
    for (const entry of m[1].split(',')) {
        const mm = entry.match(/(\w+)\s*:\s*([0-9]+)(?:\s*\*\s*([0-9]+))?/);
        if (!mm)
            continue;
        out[mm[1]] = mm[3] ? Number(mm[2]) * Number(mm[3]) : Number(mm[2]);
    }
    return out;
}

const STATE_TIMEOUT_SECONDS = extractMap('STATE_TIMEOUT_SECONDS');
const PROCESSING_TIMEOUT_SECONDS = extractMap('PROCESSING_TIMEOUT_SECONDS');

// P4 (giro 18): il caso service='stream'. Fino al giro 18 questa porta non
// aveva ALCUN caso con service='stream' — il gate certificava la costante
// difettosa scrivendo 'recording scaduto -> idle' con service `undefined`,
// cioe' solo la registrazione STT. Il percorso streaming non era mai passato
// da qui.
// P4 (round 18): the service='stream' case. Until round 18 this gate had NO
// case with service='stream' — it certified the defective constant by
// writing 'recording expired -> idle' with service `undefined`, i.e. only the
// STT recording. The streaming path had never gone through here.
function timeoutLimitFor(state, service) {
    if (state === 'processing')
        return service ? (PROCESSING_TIMEOUT_SECONDS[service] || null) : null;
    // Replica di _timeoutLimitFor() in extension.js (P4): su 'recording' il
    // limite NON vale per il servizio 'stream'. Vedi il commento esteso nel
    // sorgente: i 15 minuti sono un watchdog da registrazione STT e non
    // hanno senso su una sessione per_chunk che continua a battere il cuore.
    // Replica of _timeoutLimitFor() in extension.js (P4): on 'recording' the
    // limit does NOT apply to the 'stream' service. See the extended comment in
    // the source: the 15 minutes are an STT-recording watchdog and make no
    // sense on a per_chunk session that keeps beating the heart.
    if (state === 'recording' && service === 'stream')
        return null;
    return STATE_TIMEOUT_SECONDS[state] || null;
}

function timeoutMessage(state) {
    if (state === 'recording')
        return 'Recording timed out';
    if (state === 'error')
        return 'Error state reset';
    return 'Processing timed out';
}

// Estrae le chiavi della mappa `const NAME = { ... }` (stessa estrazione di
// extractMap, ma solo i nomi: serve per replicare la validazione THEME_ICONS
// che _refreshStatus() fa su uno stato sconosciuto).
// Extracts the keys of the map `const NAME = { ... }` (same extraction as
// extractMap, but only the names: it serves to replicate the THEME_ICONS
// validation that _refreshStatus() does on an unknown state).
function extractKeys(name) {
    const m = src.match(new RegExp(`const ${name} = \\{([^}]*)\\}`));
    if (!m)
        throw new Error(`costante ${name} non trovata in extension.js`);
    return new Set(m[1].split(',')
        .map(entry => entry.match(/(\w+)\s*:/))
        .filter(Boolean)
        .map(mm => mm[1]));
}

const THEME_ICONS_KEYS = extractKeys('THEME_ICONS');

// Porta fedele del blocco di decisione in _refreshStatus().
//
// Replica le DUE DIFESE che il sorgente ha e che questa copia deve avere
// anch'essa, altrimenti il test passa verde mentre il codice vero reagisce
// diversamente (difetto I, giro 1):
//   1. THEME_ICONS[data.state] — uno stato sconosciuto cade su 'idle', non
//      viene propagato (diversamente _icon.gicon cercherebbe un'icona
//      inesistente);
//   2. Number.isFinite(data.timestamp) — senza, un timestamp assente o non
//      numerico darebbe NaN nella sottrazione, il confronto `NaN > limit` e'
//      False e il timeout non scatterebbe MAI per quel file di stato.
// Faithful port of the decision block in _refreshStatus().
//
// It replicates the TWO DEFENSES that the source has and that this copy must
// have too, otherwise the test passes green while the real code reacts
// differently (defect I, round 1):
//   1. THEME_ICONS[data.state] — an unknown state falls on 'idle', it is not
//      propagated (otherwise _icon.gicon would look for a non-existent icon);
//   2. Number.isFinite(data.timestamp) — without it, a missing or
//      non-numeric timestamp would give NaN in the subtraction, the
//      comparison `NaN > limit` is False and the timeout would NEVER fire
//      for that state file.
// Modulo puro caricato SENZA riscriverlo: si tolgono solo gli `export`.
// Pure module loaded WITHOUT rewriting it: only the `export` keywords are removed.
const QB = new Function(require('fs').readFileSync(require('path').join(__dirname, '..', 'gnome-extension', 'bravoric-hear-read-write@riccardomurru.it', 'quick-buttons.mjs'), 'utf8')
    .replace(/^export /gm, '') + '\nreturn { QUICK_BUTTONS, quickButtonMode };')();

function decide(data, warnedBefore, now) {
    const reported = data.state && THEME_ICONS_KEYS.has(data.state) ? data.state : 'idle';
    let state = reported;
    let warned = warnedBefore;
    let notified = null;
    const limit = timeoutLimitFor(reported, data.service);
    if (limit && Number.isFinite(data.timestamp)) {
        if (now - data.timestamp > limit) {
            state = 'idle';
            if (!warned) { warned = true; notified = timeoutMessage(reported); }
        } else {
            warned = false;
        }
    } else {
        warned = false;
    }
    const idle = state === 'idle';
    // Giro 3 (F7): le tre voci di avvio seguono la STESSA variabile di
    // guardia, calcolata dal sorgente come `state === 'idle' || state ===
    // 'error'` (extension.js:898, B3 del giro 2: in stato error l'utente deve
    // poter riprovare). Il modello replicava prima solo `idle` e ignorava il
    // ramo error: con F7 le tre voci devono essere per costruzione uguali, e
    // un modello che ne fotografava due su tre non poteva presidiarlo.
    // Round 3 (F7): the three start entries follow the SAME guard variable,
    // computed by the source as `state === 'idle' || state === 'error'`
    // (extension.js:898, B3 of round 2: in error state the user must be able to
    // retry). The model used to replicate only `idle` and ignore the error
    // branch: with F7 the three entries must be equal by construction, and a
    // model that photographed two out of three could not guard it.
    const canStart = idle || state === 'error';
    // Contratto start/stop: la sensibilita' viene dal modulo REALE quick-buttons.mjs
    // (quickButtonMode), lo stesso usato dal menu e dai bottoni rapidi. canStart
    // resta per i casi "avvio" (idle/error).
    // Start/stop contract: sensitivity comes from the REAL quick-buttons.mjs module
    // (quickButtonMode), the same used by the menu and the quick buttons. canStart
    // stays for the "start" cases (idle/error).
    const sens = key => QB.quickButtonMode(QB.QUICK_BUTTONS.find(b => b.key === key), state, data.service, data.cancellable) !== 'busy';
    return {
        state, warned, notified,
        dictationSensitive: sens('dictation'),
        ocrSensitive: sens('ocr'),
        // F7: la voce Streaming è un avvio di cattura come le altre due. Prima
        // il sorgente non le chiamava mai setSensitive: restava cliccabile
        // durante recording/processing e il click partiva a vuoto.
        // F7: the Streaming entry is a capture start like the other two. Before, the
        // source never called setSensitive on it: it stayed clickable during
        // recording/processing and the click went off empty.
        streamSensitive: sens('stream'),
    };
}

let pass = 0, fail = 0;
function check(name, cond) {
    if (cond) { pass++; console.log(`  PASS  ${name}`); }
    else { fail++; console.log(`  FAIL  ${name}`); }
}
const NOW = 1_000_000_000; // epoch fisso

console.log('== costanti ==');
check('STATE_TIMEOUT_SECONDS ha recording e error', !!STATE_TIMEOUT_SECONDS.recording && !!STATE_TIMEOUT_SECONDS.error);
check('PROCESSING_TIMEOUT_SECONDS ha stt e ocr', !!PROCESSING_TIMEOUT_SECONDS.stt && !!PROCESSING_TIMEOUT_SECONDS.ocr);
check('ocr più lento di stt', PROCESSING_TIMEOUT_SECONDS.ocr > PROCESSING_TIMEOUT_SECONDS.stt);

console.log('== _timeoutLimitFor ==');
check("idle → nessun limite", timeoutLimitFor('idle') === null);
check("recording → limite", timeoutLimitFor('recording') === STATE_TIMEOUT_SECONDS.recording);
check("error → limite", timeoutLimitFor('error') === STATE_TIMEOUT_SECONDS.error);
check("processing stt → limite", timeoutLimitFor('processing', 'stt') === PROCESSING_TIMEOUT_SECONDS.stt);
check("processing ocr → limite", timeoutLimitFor('processing', 'ocr') === PROCESSING_TIMEOUT_SECONDS.ocr);
check("processing senza service → nessun limite", timeoutLimitFor('processing') === null);
check("processing service ignoto → nessun limite", timeoutLimitFor('processing', 'boh') === null);
// P4: recording con service='stream' → nessun limite. Era il caso difettoso
// che il gate non copriva affatto.
// P4: recording with service='stream' → no limit. It was the defective case
// that the gate did not cover at all.
check("recording stream → nessun limite (P4)", timeoutLimitFor('recording', 'stream') === null);
check("recording stt → limite invariato", timeoutLimitFor('recording', 'stt') === STATE_TIMEOUT_SECONDS.recording);

console.log('== decisione di stato ==');
const fresh = (state, service, ageSec) => ({ state, service, timestamp: NOW - ageSec });

let r = decide(fresh('idle', undefined, 0), false, NOW);
check("idle → idle, voci attive", r.state === 'idle' && r.dictationSensitive && r.ocrSensitive && !r.notified);

r = decide(fresh('processing', 'stt', 60), false, NOW);
check("processing fresco → resta processing", r.state === 'processing' && !r.notified);
check('processing fresco → voci disabilitate', !r.dictationSensitive && !r.ocrSensitive);

r = decide(fresh('processing', 'stt', PROCESSING_TIMEOUT_SECONDS.stt + 60), false, NOW);
check("processing scaduto → idle", r.state === 'idle');
check("processing scaduto → avviso una volta", r.notified === 'Processing timed out' && r.warned === true);
check('processing scaduto → voci riattivate', r.dictationSensitive && r.ocrSensitive);

r = decide(fresh('processing', 'stt', PROCESSING_TIMEOUT_SECONDS.stt + 60), true, NOW);
check('processing scaduto già avvisato → nessun secondo avviso', r.notified === null);

r = decide(fresh('recording', undefined, STATE_TIMEOUT_SECONDS.recording + 60), false, NOW);
check("recording scaduto → idle + avviso", r.state === 'idle' && r.notified === 'Recording timed out');

r = decide(fresh('recording', undefined, 60), false, NOW);
check("recording fresco → resta recording", r.state === 'recording' && !r.notified);

r = decide(fresh('error', undefined, STATE_TIMEOUT_SECONDS.error + 60), false, NOW);
check("error scaduto → idle + avviso", r.state === 'idle' && r.notified === 'Error state reset');

r = decide(fresh('error', undefined, 60), false, NOW);
check("error fresco → resta error", r.state === 'error' && !r.notified);

r = decide(fresh('processing', 'stt', 60), true, NOW);
check('stato fresco → flag avviso riarmato', r.warned === false);

console.log('== difese del sorgente replicate ==');
// Stato sconosciuto: il sorgente lo scarta su 'idle' via THEME_ICONS.
// Unknown state: the source discards it to 'idle' via THEME_ICONS.
r = decide({ state: 'boh', service: 'stt', timestamp: NOW }, false, NOW);
check('stato sconosciuto -> idle (validazione THEME_ICONS, non propagato)',
    r.state === 'idle' && r.dictationSensitive && !r.notified);
r = decide({ state: 'idle', service: 'stt', timestamp: NOW }, false, NOW);
check("stato noto ma non attivo resta com'era", r.state === 'idle');
// Timestamp assente/non numerico: nessun timeout spurio.
// Missing/non-numeric timestamp: no spurious timeout.
r = decide({ state: 'recording', service: 'stt' }, false, NOW);
check('timestamp assente -> nessun timeout, stato intatto',
    r.state === 'recording' && r.notified === null && !r.warned);
r = decide({ state: 'recording', service: 'stt', timestamp: 'spazzatura' }, false, NOW);
check('timestamp non numerico -> nessun timeout (Number.isFinite)',
    r.state === 'recording' && r.notified === null);
r = decide({ state: 'recording', service: 'stt', timestamp: null }, false, NOW);
check('timestamp null -> nessun timeout (Number.isFinite)',
    r.state === 'recording' && r.notified === null);
r = decide({ state: 'recording', service: 'stt', timestamp: NaN }, false, NOW);
check("timestamp NaN -> nessun timeout (Number.isFinite)", r.state === 'recording');

// I casi in cui il sorgente e una verita' semplice DIVERGISCONO davvero.
// Number.isFinite accetta 0 e -0 (sono numeri finiti), quindi con
// timestamp = 0 il timeout scatta: e' corretto, il file di stato e' vecchio
// di 55 anni. Quello che la guardia RESPINGE sono i valori truthy ma non
// finiti/non numerici, che con `if (data.timestamp)` passerebbero e
// produrrebbero `now - Infinity`/`now - 'x'` (NaN) o, peggio, con `true`
// un now - 1 falsissimo. Qui il test separa le due implementazioni: senza
// Number.isFinite i casi 'x', true e -Infinity cambiano esito.
// Nota: status.py scrive sempre time.time() (mai 0), quindi questi sono
// solo ingressi corrotti — ma e' esattamente il caso che la difesa presidia.
// The cases where the source and a simple truthiness really DIVERGE.
// Number.isFinite accepts 0 and -0 (they are finite numbers), so with
// timestamp = 0 the timeout fires: it is correct, the state file is 55 years
// old. What the guard REJECTS are the truthy but non-finite/non-numeric
// values, which with `if (data.timestamp)` would pass and produce
// `now - Infinity`/`now - 'x'` (NaN) or, worse, with `true` a completely
// false now - 1. Here the test separates the two implementations: without
// Number.isFinite the cases 'x', true and -Infinity change outcome.
// Note: status.py always writes time.time() (never 0), so these are only
// corrupt inputs — but it is exactly the case the defense guards.
r = decide({ state: 'recording', service: 'stt', timestamp: true }, false, NOW);
check('timestamp true (booleano truthy) -> respinto dalla guardia (Number.isFinite)',
    r.state === 'recording' && r.notified === null);
r = decide({ state: 'recording', service: 'stt', timestamp: -Infinity }, false, NOW);
check('timestamp -Infinity -> respinto dalla guardia (Number.isFinite)',
    r.state === 'recording' && r.notified === null);
r = decide({ state: 'recording', service: 'stt', timestamp: Infinity }, false, NOW);
check('timestamp +Infinity -> respinto dalla guardia (Number.isFinite)',
    r.state === 'recording' && r.notified === null);
// timestamp 0: Number.isFinite(0) e' true, quindi il timeout SCATTA (il file
// e' del 1970). Fissarlo qui evita che un futuri cambiamento "correttivo"
// della guardia venga scambiato per un invariante del test.
// timestamp 0: Number.isFinite(0) is true, so the timeout FIRES (the file is
// from 1970). Pinning it here prevents a future "corrective" change of the
// guard from being mistaken for an invariant of the test.
r = decide({ state: 'recording', service: 'stt', timestamp: 0 }, false, NOW);
check('timestamp 0: la guardia lo ACCETTA e il timeout scatta (file del 1970)',
    r.state === 'idle' && r.notified === 'Recording timed out');


// Con timestamp finito e scaduto il timeout scatta: la difesa non deve
// spegnere il percorso vero.
// With a finite and expired timestamp the timeout fires: the defense must
// not switch off the real path.
r = decide(fresh('recording', undefined, STATE_TIMEOUT_SECONDS.recording + 60), false, NOW);
check('timestamp finito e scaduto -> il timeout scatta ancora',
    r.state === 'idle' && r.notified === 'Recording timed out');
check('le due difese esistono davvero nel sorgente (anti-drift)',
    src.includes('data.state && THEME_ICONS[data.state] ? data.state : \'idle\'') &&
    src.includes('if (limit && Number.isFinite(data.timestamp))'));

console.log('== struttura del sorgente ==');
check('il sorgente usa _timeoutLimitFor e _timeoutMessage',
    src.includes('_timeoutLimitFor(reportedState, data.service)') &&
    src.includes('this._timeoutMessage(reportedState)'));
check('il sorgente sincronizza le voci di menu dal modello start/stop (_syncMenuItem)',
src.includes("this._syncMenuItem(this._dictationItem, 'dictation')") &&
src.includes("this._syncMenuItem(this._ocrItem, 'ocr')"));
check("_init inizializza i flag", src.includes('this._statusParseErrors = 0') && src.includes('this._timeoutWarned = false'));
check('_refreshStatus ha il ramo else per output assente (evita "transcribing…" bloccato)',
    src.includes('this._lastOutputItem.setSensitive(preview !== null)'));
check('_lastOutputItem torna a "(none)" quando non c\'è output né storico',
    src.includes("_('Last output: (none)')"));

// ---------------------------------------------------------------------
// Difetto F7 (giro 3): la voce "Streaming" non veniva MAI disabilitata.
//
// Meccanismo: in _refreshStatus() le tre voci di avvio esistono
// (_dictationItem, _ocrItem, _streamItem) ma solo le prime due ricevevano
// setSensitive(canStart). La terza restava cliccabile durante recording e
// processing: il click lanciava bravoric-stream-toggle, che rispondeva False
// senza mostrare nulla all'utente.
//
// Due livelli di presidio, come per il difetto F sopra:
//  1. strutturale sul sorgente vero (se la riga sparisce, l'asserzione vira);
//  2. logico sulla porta `decide()`, che replica il blocco di sensitivity.
// ---------------------------------------------------------------------
// ---------------------------------------------------------------------
// Defect F7 (round 3): the "Streaming" entry was NEVER disabled.
//
// Mechanism: in _refreshStatus() the three start entries exist
// (_dictationItem, _ocrItem, _streamItem) but only the first two received
// setSensitive(canStart). The third stayed clickable during recording and
// processing: the click launched bravoric-stream-toggle, which answered
// False without showing anything to the user.
//
// Two levels of guard, as for defect F above:
//  1. structural on the real source (if the line disappears, the assertion
//     turns);
//  2. logical on the `decide()` port, which replicates the sensitivity
//     block.
// ---------------------------------------------------------------------
console.log('== difetto F7: la voce Streaming segue la stessa guardia ==');
check('F7 il sorgente sincronizza anche _streamItem dallo stesso modello',
    src.includes("this._syncMenuItem(this._streamItem, 'stream')"));
// Anti-drift: le tre chiamate devono stare nello stesso blocco di _refreshStatus,
// non essere sparpagliate (una fuori dal blocco non avrebbe la stessa variabile).
// Anti-drift: the three calls must be in the same block of _refreshStatus,
// not scattered (one outside the block would not have the same variable).
const sensBlock = src.match(/this\._uiState = state;[\s\S]*?this\._syncMenuItem\(this\._streamItem, 'stream'\);/);
check('F7 le tre voci stanno nello stesso blocco (stesso stato letto)',
    !!sensBlock && sensBlock[0].includes("this._syncMenuItem(this._dictationItem, 'dictation')")
    && sensBlock[0].includes("this._syncMenuItem(this._ocrItem, 'ocr')"));

r = decide(fresh('recording', 'stt', 5), false, NOW);
check('F7 recording stt -> Streaming disabilitata; la dettatura e\' cliccabile (e\' lo stop)',
    r.streamSensitive === false && r.dictationSensitive === true && r.ocrSensitive === false);
r = decide(fresh('processing', 'stt', 5), false, NOW);
check('F7 processing -> la voce Streaming è disabilitata', r.streamSensitive === false);
r = decide(fresh('idle', undefined, 5), false, NOW);
check('F7 idle -> la voce Streaming è attiva',
    r.streamSensitive === true && r.dictationSensitive === true);
r = decide(fresh('error', undefined, 5), false, NOW);
check('F7 error -> la voce Streaming torna attiva (come le altre due)',
    r.streamSensitive === true && r.streamSensitive === r.dictationSensitive);
// Coerenza con le altre due su TUTTI gli stati: è la guardia unica, quindi le
// tre voci non possono divergere (è il senso della variabile canStart).
// Consistency with the other two on ALL states: it is the single guard, so
// the three entries cannot diverge (that is the point of the canStart
// variable).
const ALL_STATES = ['idle', 'recording', 'processing', 'error'];
check('F7 le tre voci divergono solo per lo stop: idle/error tutte, altrimenti solo il servizio che lavora',
    ALL_STATES.every(s => {
        const d = decide(fresh(s, 'stt', 1), false, NOW);
        if (s === 'idle' || s === 'error')
            return d.streamSensitive && d.dictationSensitive && d.ocrSensitive;
        const open = [d.dictationSensitive, d.ocrSensitive, d.streamSensitive].filter(Boolean).length;
        return open === (s === 'recording' ? 1 : 0) && (s !== 'recording' || d.dictationSensitive);
    }));
// E il caso di danno vero: recording in corso, la voce era cliccabile.
// And the case of real damage: recording in progress, the entry was
// clickable.
check('F7 il click durante una cattura di un ALTRO servizio non parte più a vuoto',
    decide(fresh('recording', 'stt', 1), false, NOW).streamSensitive === false);



// ===============================================================
// Difetto P4 (watchdog): i 15 minuti di STATE_TIMEOUT_SECONDS.recording
// venivano applicati anche a una sessione streaming per_chunk VIVA.
// Misurato a caldo con le costanti REALI del sorgente: da 14m30s
// `recording`, da 15m01s `idle` + "Recording timed out", con il
// supervisore ancora vivo e ffmpeg ancora sul microfono. Inoltre la
// voce Streaming tornava cliccabile, quindi il click avviava un
// SECONDO ffmpeg (vedi punto 2: l'esclusione reciproca, ora chiusa in
// stt._start).
//
// Il test che chiudeva il difetto era proprio quello che lo certificava:
// asseriva "recording scaduto → idle" e non conteneva MAI 'stream' come
// service. Qui il caso è invertito: sessione viva appena sopra il limite
// resta recording, silenziosa e con le voci disabilitate.
// ===============================================================
// ===============================================================
// Defect P4 (watchdog): the 15 minutes of STATE_TIMEOUT_SECONDS.recording
// were also applied to a LIVE per_chunk streaming session. Measured hot with
// the REAL constants of the source: at 14m30s `recording`, at 15m01s `idle`
// + "Recording timed out", with the supervisor still alive and ffmpeg still
// on the microphone. In addition the Streaming entry became clickable again,
// so the click started a SECOND ffmpeg (see point 2: the mutual exclusion,
// now closed in stt._start).
//
// The test that closed the defect was the very one that certified it: it
// asserted "recording expired → idle" and NEVER contained 'stream' as
// service. Here the case is inverted: a live session just above the limit
// stays recording, silent and with the entries disabled.
// ===============================================================
console.log('== difetto P4: il watchdog non uccide una sessione stream viva ==');
const HALF_HOUR = 30 * 60;

// 1. Il caso che oggi mancava: sessione per_chunk viva, eta' SOVRA il limite.
// 1. The case that was missing today: live per_chunk session, age OVER the
// limit.
r = decide(fresh('recording', 'stream', STATE_TIMEOUT_SECONDS.recording + 60), false, NOW);
check('P4 sessione stream viva oltre 15 min -> resta recording',
    r.state === 'recording');
check('P4 ... e senza la notifica falsa "Recording timed out"',
    r.notified === null && r.warned === false);

// 2. E i casi estremi: mezz'ora, quaranta minuti. Era qui che il difetto
// colpiva l'uso ordinario (dettatura continua), non un angolo.
// 2. And the extreme cases: half an hour, forty minutes. This is where the
// defect hit ordinary use (continuous dictation), not a corner.
r = decide(fresh('recording', 'stream', HALF_HOUR), false, NOW);
check('P4 sessione stream viva da 30 min -> resta recording, nessun avviso',
    r.state === 'recording' && r.notified === null);
r = decide(fresh('recording', 'stream', 40 * 60), false, NOW);
check('P4 sessione stream viva da 40 min -> resta recording, nessun avviso',
    r.state === 'recording' && r.notified === null);

// 3. Le tre voci restano disabilitate: senza questo, l'utente premeva la
// scorciatoia e partiva un secondo ffmpeg sopra la sessione viva.
// 3. The three entries stay disabled: without this, the user pressed the
// shortcut and a second ffmpeg started on top of the live session.
r = decide(fresh('recording', 'stream', 40 * 60), false, NOW);
check('P4 sessione stream viva -> dettatura e OCR non avviabili; solo Streaming e\' cliccabile (stop)',
    r.dictationSensitive === false && r.ocrSensitive === false && r.streamSensitive === true);

// 4. L'INVARIANTE che il fix non deve rompere: la registrazione STT resta
// governata dai 15 minuti, e un STT morto torna idle con avviso.
// 4. The INVARIANT the fix must not break: the STT recording stays governed
// by the 15 minutes, and a dead STT goes back to idle with a warning.
r = decide(fresh('recording', 'stt', STATE_TIMEOUT_SECONDS.recording + 60), false, NOW);
check('P4 registrazione STT scaduta -> torna ancora idle con avviso',
    r.state === 'idle' && r.notified === 'Recording timed out');
r = decide(fresh('recording', 'stt', STATE_TIMEOUT_SECONDS.recording + 60), true, NOW);
check('P4 registrazione STT scaduta -> avviso una volta sola',
    r.notified === null);
r = decide(fresh('recording', 'stt', 60), false, NOW);
check('P4 registrazione STT fresca -> resta recording',
    r.state === 'recording' && r.notified === null);
r = decide(fresh('recording', undefined, STATE_TIMEOUT_SECONDS.recording + 60), false, NOW);
check('P4 recording senza service -> limite applicato (comportamento preesistente)',
    r.state === 'idle' && r.notified === 'Recording timed out');

// 5. Anti-drift sul sorgente VERO: se _timeoutLimitFor perde la clausola
// stream, questo test continua a misurare una copia e il gate resta verde
// sul codice rotto. Le due righe devono stare dentro il metodo reale.
// 5. Anti-drift on the REAL source: if _timeoutLimitFor loses the stream
// clause, this test keeps measuring a copy and the gate stays green on the
// broken code. The two lines must be inside the real method.
const limitSrc = (src.match(/_timeoutLimitFor\(state, service\) \{[\s\S]*?\n {4}\}/) || [''])[0];
check('P4 il sorgente reale esclude stream dal limite recording',
    limitSrc.includes("if (state === 'recording' && service === 'stream')")
    && limitSrc.includes('return null;'));
check('P4 la clausola stream sta DAVANTI al fallback generico sul limite',
    limitSrc.indexOf("service === 'stream'") > 0
    && limitSrc.indexOf("service === 'stream'") < limitSrc.indexOf('STATE_TIMEOUT_SECONDS[state]'));
check('P4 il limite STT e il limite processing NON sono stati toccati',
    limitSrc.includes('settingInt(PROCESSING_TIMEOUT_KEYS[service], PROCESSING_TIMEOUT_SECONDS[service] / 60) * 60')
    && limitSrc.includes('settingInt(STATE_TIMEOUT_KEYS[state], STATE_TIMEOUT_SECONDS[state] / 60) * 60'));
// Una sessione stream non deve nemmeno ARMARE il flag di avviso: senza
// limite la porta entra nel ramo `else`, che lo tiene a false. Così se più
// tardi la sessione finisce davvero e scatta un timeout vero, l'utente
// viene avvisato una volta (non resta silente perché il flag era già armato).
// A stream session must not even ARM the warning flag: with no limit the
// port enters the `else` branch, which keeps it false. So if later the
// session really ends and a real timeout fires, the user is warned once (it
// does not stay silent because the flag was already armed).
r = decide(fresh('recording', 'stream', 40 * 60), true, NOW);
check('P4 una sessione stream non arm mai _timeoutWarned (il flag resta riarmabile)',
    r.state === 'recording' && r.warned === false && r.notified === null);

// 6. Il cuore: il backend batte il timestamp mentre la sessione gira, e' il
// secondo pezzo del fix (a). Senza di esso l'eta' letta qui sarebbe sempre
// quella dell'avvio.
// 6. The heart: the backend beats the timestamp while the session runs, it is
// the second piece of fix (a). Without it the age read here would always be
// that of the start.
const streamSrc = fs.readFileSync(
    path.join(__dirname, '..', 'src', 'bravoric_stt_clipboard', 'stream.py'), 'utf8');
// Giro 18, difetto di copertura: il check precedente confrontava la scrittura
// con TUTTO stream.py. La stessa identica riga compare in altri 3 punti di
// avvio (1350, 1471, ...), quindi svuotare il CORPO di heartbeat() non faceva
// fallire niente: la suite restava verde sul codice che non batte piu' il
// cuore. Un test che non puo' fallire non e' un test.
// Qui si legge il corpo della funzione: la porzione fra la firma e il prossimo
// `def` di primo livello, con la docstring rimossa (perche' nella docstring la
// parola write_status compare descrivendo il guard, non chiamandolo).
// Round 18, coverage defect: the previous check compared the write with the
// WHOLE stream.py. The very same line appears in 3 other start points (1350,
// 1471, ...), so emptying the BODY of heartbeat() failed nothing: the suite
// stayed green on code that no longer beats the heart. A test that cannot
// fail is not a test. Here the body of the function is read: the portion
// between the signature and the next top-level `def`, with the docstring
// removed (because in the docstring the word write_status appears describing
// the guard, not calling it).
function pyFuncBody(text, signature) {
    const at = text.indexOf(signature);
    if (at === -1)
        return '';
    const rest = text.slice(at + signature.length);
    const cut = rest.search(/^def \w/m);
    return (cut === -1 ? rest : rest.slice(0, cut)).replace(/^\s*"""[\s\S]*?"""\s*/, '');
}
const heartbeatBody = pyFuncBody(streamSrc, 'def heartbeat() -> None:');
check('P4 il corpo di heartbeat() riscrive RECORDING con service=stream (non basta la presenza nel file)',
    heartbeatBody.trim().length > 0
    && /status\.write_status\(\s*status\.STATE_RECORDING,\s*service="stream"\s*\)/.test(heartbeatBody));
const vadLoop = (streamSrc.match(/while True:\s*\n\s*# P4[\s\S]*?pcm_queue\.get\(timeout=0\.5\)/) || [''])[0];
check('P4 il battito e nel loop VAD, non altrove',
    !!vadLoop && vadLoop.includes('heartbeat()')
    && vadLoop.includes('STREAM_HEARTBEAT_SECONDS'));
check('P4 il battito sta PRIMA del ramo continue del silenzio (battte anche in pausa)',
    vadLoop.includes('heartbeat()')
    && vadLoop.indexOf('heartbeat()') < vadLoop.indexOf('pcm_queue.get')
    && vadLoop.indexOf('heartbeat()') !== -1);
const BEAT = (streamSrc.match(/^STREAM_HEARTBEAT_SECONDS = ([\d.]+)$/m) || [])[1];
check("P4 il periodo del battito e molto piu corto del limite (non puo attraversarlo)",
    Number(BEAT) > 0 && Number(BEAT) < STATE_TIMEOUT_SECONDS.recording / 3);

// 7. La parte LOGICA legata al sorgente VERO, non alla copia qui sopra.
// Le asserzioni 1-4 misurano `timeoutLimitFor`, che e' una copia fedele ma
// comunque una copia: se il fix venisse rimosso dal sorgoso e la copia
// dimenticata, il test continuerebbe a misurare la copia e resterebbe
// verde sul codice rotto. Solo le due asserzioni strutturali sopra
// (5) coprirebbero quel caso, e in modo grossolano. Qui il metodo REALE e'
// estratto da extension.js e valutato, esattamente come _runStreamCommand
// piu' in basso: se la clausola stream sparisce, questo blocco vira.
// 7. The LOGICAL part tied to the REAL source, not to the copy above.
// Assertions 1-4 measure `timeoutLimitFor`, which is a faithful copy but a
// copy nonetheless: if the fix were removed from the source and the copy
// forgotten, the test would keep measuring the copy and stay green on the
// broken code. Only the two structural assertions above (5) would cover that
// case, and coarsely. Here the REAL method is extracted from extension.js
// and evaluated, exactly like _runStreamCommand further below: if the stream
// clause disappears, this block turns.
console.log('== P4: il metodo REALE di _timeoutLimitFor ==');
const limitSIG = '    _timeoutLimitFor(state, service) {';
const atLimit = src.indexOf(limitSIG);
check('P4 _timeoutLimitFor presente in extension.js', atLimit !== -1);
let realLimitSrc = '';
if (atLimit !== -1) {
    const openL = src.indexOf('{', atLimit);
    const endL = matchBrace(src, openL);
    realLimitSrc = endL === -1 ? '' : src.slice(atLimit, endL + 1);
}
check('P4 il corpo di _timeoutLimitFor e\' stato estratto', realLimitSrc.length > 0);

// Chiavi GSettings che ridefiniscono i limiti (in minuti), estratte dal
// sorgente vero: `const NAME = { chiave: 'stringa', ... }`.
// GSettings keys that override the limits (in minutes), extracted from the
// real source: `const NAME = { key: 'string', ... }`.
function extractKeyMap(name) {
    const m = src.match(new RegExp(`const ${name} = \\{([^}]*)\\}`));
    if (!m)
        throw new Error(`costante ${name} non trovata in extension.js`);
    const out = {};
    for (const mm of m[1].matchAll(/(\w+)\s*:\s*'([^']+)'/g))
        out[mm[1]] = mm[2];
    return out;
}
const STATE_TIMEOUT_KEYS = extractKeyMap('STATE_TIMEOUT_KEYS');
const PROCESSING_TIMEOUT_KEYS = extractKeyMap('PROCESSING_TIMEOUT_KEYS');
// settingInt finto: registra le chiavi richieste e restituisce l'override
// (in minuti) se presente, altrimenti il fallback come farebbe il vero.
// Fake settingInt: records the requested keys and returns the override
// (in minutes) when present, otherwise the fallback like the real one.
const settingAsked = [];
let settingOverrides = {};
const limitSandbox = {
    STATE_TIMEOUT_SECONDS,
    PROCESSING_TIMEOUT_SECONDS,
    STATE_TIMEOUT_KEYS,
    PROCESSING_TIMEOUT_KEYS,
    settingInt: (key, fallback) => {
        settingAsked.push(key);
        return key in settingOverrides ? settingOverrides[key] : fallback;
    },
};
vm.createContext(limitSandbox);
if (realLimitSrc) {
    vm.runInContext('class _LimitExt {' + realLimitSrc
        + '\n}\nvar _realLimit = _LimitExt.prototype._timeoutLimitFor;', limitSandbox);
}
const realLimit = limitSandbox._realLimit;
// Ogni limite e' regolabile da GUI: la chiave giusta viene chiesta e il valore
// (in minuti) diventa secondi. Chiave assente = default storico.
// Every limit is GUI-tunable: the right key is asked for and the value (in
// minutes) becomes seconds. Missing key = historical default.
if (realLimit) {
    for (const [state, key] of Object.entries(STATE_TIMEOUT_KEYS)) {
        settingAsked.length = 0;
        settingOverrides = { [key]: 7 };
        check(`GUI: ${state} legge ${key} e lo converte in secondi`,
            realLimit.call({}, state, undefined) === 7 * 60 && settingAsked.includes(key));
        settingOverrides = {};
        check(`GUI: ${state} senza override = default storico`,
            realLimit.call({}, state, undefined) === STATE_TIMEOUT_SECONDS[state]);
    }
    for (const [service, key] of Object.entries(PROCESSING_TIMEOUT_KEYS)) {
        settingAsked.length = 0;
        settingOverrides = { [key]: 11 };
        check(`GUI: processing ${service} legge ${key} e lo converte in secondi`,
            realLimit.call({}, 'processing', service) === 11 * 60 && settingAsked.includes(key));
        settingOverrides = {};
        check(`GUI: processing ${service} senza override = default storico`,
            realLimit.call({}, 'processing', service) === PROCESSING_TIMEOUT_SECONDS[service]);
    }
    settingOverrides = { 'recording-timeout-minutes': 1 };
    check('GUI: la sessione streaming resta senza limite anche con override',
        realLimit.call({}, 'recording', 'stream') === null);
    settingOverrides = {};
    check('GUI: idle e servizio ignoto restano senza limite',
        realLimit.call({}, 'idle') === null && realLimit.call({}, 'processing', 'boh') === null);
}
if (realLimit) {
    // Il caso del difetto, misurato sul codice vero.
    // The defect case, measured on the real code.
    check('P4 REALE: recording con service stream -> nessun limite',
        realLimit('recording', 'stream') === null);
    // E gli invarianti: il metodo vero continua a rispondere come prima per
    // tutti gli altri casi (se un if fosse scritto male, riporterebbe null
    // anche qui e questi lo mostrerebbero).
    // And the invariants: the real method keeps answering as before for all the
    // other cases (if an if were badly written, it would return null here too
    // and these would show it).
    check('P4 REALE: recording con service stt -> limite recording',
        realLimit('recording', 'stt') === STATE_TIMEOUT_SECONDS.recording);
    check('P4 REALE: recording senza service -> limite recording (invariato)',
        realLimit('recording', undefined) === STATE_TIMEOUT_SECONDS.recording);
    check('P4 REALE: recording con service ignoto -> limite recording (invariato)',
        realLimit('recording', 'boh') === STATE_TIMEOUT_SECONDS.recording);
    check('P4 REALE: processing stt -> limite stt (invariato)',
        realLimit('processing', 'stt') === PROCESSING_TIMEOUT_SECONDS.stt);
    check('P4 REALE: processing ocr -> limite ocr (invariato)',
        realLimit('processing', 'ocr') === PROCESSING_TIMEOUT_SECONDS.ocr);
    check('P4 REALE: processing senza service -> nessun limite (invariato)',
        realLimit('processing') === null);
    check('P4 REALE: idle -> nessun limite (invariato)',
        realLimit('idle', undefined) === null);
    check('P4 REALE: error -> limite error (invariato)',
        realLimit('error', undefined) === STATE_TIMEOUT_SECONDS.error);
    // La copia e il metodo reale devono concordare su TUTTA la griglia: e'
    // quello che rende lecito fidarsi dei casi 1-4 scritti sulla copia.
    // The copy and the real method must agree on the WHOLE grid: it is what
    // makes it legitimate to trust cases 1-4 written on the copy.
    const GRID = [];
    for (const st of ['idle', 'recording', 'processing', 'error'])
        for (const sv of [undefined, 'stt', 'ocr', 'stream', 'boh'])
            GRID.push([st, sv]);
    check('P4 la copia e il metodo reale concordano su tutta la griglia stato x servizio',
        GRID.every(([st, sv]) => realLimit(st, sv) === timeoutLimitFor(st, sv)),
        );
}



// ===============================================================
// Difetto F (giro 1): _runStreamCommand() non rilasciava
// _streamWorkerActive nei suoi rami di uscita per errore.
// Meccanismo: _startStreamPasteWorker() mette _streamWorkerActive = true e
// poi, per un item 'command', delega. Se il metodo esce per errore con
// `return`, il flag restava True e la coda non veniva piu' svuotata per
// tutta la sessione GNOME (la sessione successiva azzera solo
// _streamPasteBlocked, non questo latch).
//
// Il metodo e' ESTRATTO dal vero extension.js e valutato con stub fedeli di
// Clutter/Main/logError: se il fix viene rimosso, queste asserzioni virano
// rosse. Stanno qui, e non in un file nuovo, perche' il gate del progetto
// esegue questo file: un test separato non verrebbe mai lanciato.
// ===============================================================
// ===============================================================
// Defect F (round 1): _runStreamCommand() did not release
// _streamWorkerActive in its error exit branches.
// Mechanism: _startStreamPasteWorker() sets _streamWorkerActive = true and
// then, for a 'command' item, delegates. If the method exits on error with
// `return`, the flag stayed True and the queue was no longer emptied for the
// whole GNOME session (the next session resets only _streamPasteBlocked, not
// this latch).
//
// The method is EXTRACTED from the real extension.js and evaluated with
// faithful stubs of Clutter/Main/logError: if the fix is removed, these
// assertions turn red. They live here, and not in a new file, because the
// project's gate runs this file: a separate test would never be launched.
// ===============================================================
const SIG = '    _runStreamCommand(item) {';
const atF = src.indexOf(SIG);
check("F _runStreamCommand presente in extension.js", atF !== -1);
let methodSrc = '';
if (atF !== -1) {
    const openF = src.indexOf('{', atF);
    const endF = matchBrace(src, openF);
    methodSrc = endF === -1 ? '' : src.slice(atF, endF + 1);
}
check("F il corpo di _runStreamCommand e' stato estratto", methodSrc.length > 0);

const notifications = [];
const sandbox = {
    Clutter: new Proxy({ KeyState: { PRESSED: 1, RELEASED: 0 } },
        { get: (t, k) => (k in t ? t[k] : 'KEY_' + String(k)) }),
    Main: { notifyError: (a, b) => notifications.push([a, b]) },
    // helper di modulo di extension.js: qui inoltrano al Main finto
    // module helpers of extension.js: here they forward to the fake Main
    notifyErrorIfEnabled: (a, b) => notifications.push([a, b]),
    notifyStatusIfEnabled: () => {},
    GLib: { SOURCE_REMOVE: false },
    logError: () => {},
    _: s => s,
    computeStreamDelete: () => ({ count: 0, segments: [] }),
};
vm.createContext(sandbox);
if (methodSrc) {
    vm.runInContext(
        'class _Ext {' + methodSrc + '\n}\nvar _runStreamCommand = _Ext.prototype._runStreamCommand;',
        sandbox);
}
const runCommand = sandbox._runStreamCommand;

function makeExt(sendKeyOk) {
    return {
        _streamSegments: ['ciao mondo'],
        _streamQueue: [{ action: 'command', sessionId: 's1', text: 'x',
                         command: { action: 'key', key: 'Return' } }],
        _streamWorkerActive: true,     // messo dal worker prima di delegare | set by the worker before delegating
        _streamPasteBlocked: false,
        _sendKey: () => sendKeyOk,
        _writeStreamLiveText: () => {},
        _requestStreamEnd(sid) { this._endCalled = sid; },
        _startStreamPasteWorker() { this._restartCalled = true; },
    };
}

console.log('== difetto F: i rami di errore rilasciano _streamWorkerActive ==');
if (runCommand) {
    // 1. tasto non inviabile: era il caso piu' grave (bloccava la coda per sempre)
    // 1. key that cannot be sent: it was the most serious case (it blocked the
    // queue forever)
    let e = makeExt(false);
    runCommand.call(e, e._streamQueue[0]);
    check("F tasto fallito: _streamWorkerActive rilasciato", e._streamWorkerActive === false);
    check("F tasto fallito: coda bloccata (il blocco e' voluto)", e._streamPasteBlocked === true);
    check("F tasto fallito: l'utente e' avvisato", notifications.length === 1);
    check("F tasto fallito: il worker NON e' rilanciato (niente duplicati)",
        e._restartCalled === undefined);

    // 2. scope di delete invalido (computeStreamDelete -> count -1)
    // 2. invalid delete scope (computeStreamDelete -> count -1)
    notifications.length = 0;
    e = makeExt(true);
    sandbox.computeStreamDelete = () => ({ count: -1, segments: [] });
    runCommand.call(e, { action: 'command', sessionId: 's1', text: 'x',
        command: { action: 'delete', scope: 'boh' } });
    check("F delete con scope invalido: latch rilasciato", e._streamWorkerActive === false);
    check("F delete con scope invalido: coda bloccata", e._streamPasteBlocked === true);

    // 3. delete parziale (un backspace non parte)
    // 3. partial delete (a backspace does not go out)
    notifications.length = 0;
    e = makeExt(true);
    sandbox.computeStreamDelete = () => ({ count: 2, segments: ['a', 'b'] });
    e._sendKey = k => String(k) !== 'KEY_KEY_BackSpace';
    runCommand.call(e, { action: 'command', sessionId: 's1', text: 'x',
        command: { action: 'delete', scope: 'word' } });
    check("F delete parziale: latch rilasciato", e._streamWorkerActive === false);
    check("F delete parziale: segmenti azzerati", e._streamSegments === null);

    // 4. azione sconosciuta (ramo else finale)
    notifications.length = 0;
    e = makeExt(true);
    runCommand.call(e, { action: 'command', sessionId: 's1', text: 'x',
        command: { action: 'magi', key: 'Return' } });
    check("F azione sconosciuta: latch rilasciato", e._streamWorkerActive === false);
    check("F azione sconosciuta: coda bloccata", e._streamPasteBlocked === true);

    // 5. il percorso di SUCCESSO continua a funzionare
    // 5. the SUCCESS path keeps working
    sandbox.computeStreamDelete = () => ({ count: 0, segments: [] });
    e = makeExt(true);
    runCommand.call(e, { action: 'command', sessionId: 's1', text: 'x',
        command: { action: 'key', key: 'Return', ends_session: true } });
    check("F percorso OK: latch rilasciato e worker rilanciato",
        e._streamWorkerActive === false && e._restartCalled === true);
    check("F percorso OK: la coda avanza di un elemento", e._streamQueue.length === 0);
    check("F percorso OK: ends_session richiesta", e._endCalled === 's1');
}

// Contatore statico sul sorgente vero: ogni blocco _streamPasteBlocked = true
// deve essere seguito dal reset del latch prima del return successivo.
// Static counter on the real source: every _streamPasteBlocked = true block
// must be followed by the latch reset before the next return.
const linesF = methodSrc.split('\n');
const blockIdx = [];
linesF.forEach((l, i) => { if (l.includes('this._streamPasteBlocked = true')) blockIdx.push(i); });
check("F i rami che bloccano e escono sono 4 (rilevati " + blockIdx.length + ')', blockIdx.length === 4);
check("F nessun ramo di blocco lascia il latch acceso", blockIdx.every(i => {
    for (let j = i; j < linesF.length; j++) {
        if (linesF[j].includes('this._streamWorkerActive = false')) return true;
        if (linesF[j].trim().startsWith('return')) return false;
    }
    return false;
}));

// Difetto F-bis: il re-arm a 100ms di _requestStreamEnd era infinito con la
// coda bloccata (la coda non si svuota da sola). Deve cedere sul blocco.
// Defect F-bis: the 100 ms re-arm of _requestStreamEnd was infinite with the
// queue blocked (the queue does not empty by itself). It must yield on the
// block.
const endCheck = (src.match(/_requestStreamEnd\(sessionId\) \{[\s\S]*?\n {4}\}/) || [''])[0];
check("F-bis _requestStreamEnd ha una condizione di fine-coda sulla coda bloccata",
    endCheck.includes('this._streamPasteBlocked'));
check("F-bis la condizione di fine-coda e' dichiarata in un commento",
    endCheck.includes('DICHIARATO'));

console.log(`\n${pass} PASS / ${fail} FAIL`);
process.exit(fail === 0 ? 0 : 1);
