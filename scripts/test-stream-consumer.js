#!/usr/bin/env node
// Regressione dello snapshot consumer reale usato dall'estensione GNOME.
// Regression of the real snapshot consumer used by the GNOME extension.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { matchBrace } = require('./lib/brace-match.cjs');

// I metodi estratti da extension.js notificano tramite notifyErrorIfEnabled /
// notifyStatusIfEnabled (funzioni di modulo, controllate da GSettings). Nei
// test i metodi restano quelli REALI e ricevono un `Main` finto: qui i due
// helper inoltrano a quel Main. Il comportamento degli helper veri (interruttore
// spento, chiave assente) e' provato a parte, estraendoli dal sorgente.
// The methods extracted from extension.js notify through
// notifyErrorIfEnabled / notifyStatusIfEnabled (module functions, controlled
// by GSettings). In the tests the methods stay the REAL ones and receive a
// fake `Main`: here the two helpers forward to that Main. The behavior of
// the real helpers (switch off, missing key) is proven separately, by
// extracting them from the source.
const NOTIFY_PRELUDE = 'const notifyErrorIfEnabled = (t, b) => Main.notifyError(t, b);\n'
    + 'const notifyStatusIfEnabled = (t, b) => Main.notify(t, b);\n'
    + 'const settingInt = (key, fallback) => fallback;\n';

(async () => {
    const extensionPath = path.join(__dirname, '..', 'gnome-extension', 'bravoric-indicator@local', 'extension.js');
    const helperPath = path.join(__dirname, '..', 'gnome-extension', 'bravoric-indicator@local', 'stream-consumer.mjs');
    const source = fs.readFileSync(extensionPath, 'utf8');
    const { consumeStreamSnapshot, classifyStreamItem, normalizeCommandKeyword, computeStreamDelete, parseBlacklist } = await import(pathToFileURL(helperPath));
    // S4: il modulo puro del monitor di cache, importato ed ESEGUITO come stream-consumer.mjs
    // S4: the pure module of the cache monitor, imported and RUN like
    // stream-consumer.mjs
    const watchPath = path.join(__dirname, '..', 'gnome-extension', 'bravoric-indicator@local', 'watch-cache.mjs');
    const { watchCacheFile, watchStatusFile, watchStreamStateFile } = await import(pathToFileURL(watchPath));
    // S4: il modulo puro del lampeggio, importato ed ESEGUITO come gli altri
    // S4: the pure module of the blink, imported and RUN like the others
    const blinkPath = path.join(__dirname, '..', 'gnome-extension', 'bravoric-indicator@local', 'recording-blink.mjs');
    const { setRecordingBlink } = await import(pathToFileURL(blinkPath));
    let pass = 0;
    function check(name, condition) {
        assert.ok(condition, name);
        pass++;
        console.log(`  PASS  ${name}`);
    }
    const consumer = () => ({ _streamSessionId: null, _streamIndex: 0, _streamWasActive: false, _streamFinalObserved: false });
    const state = (active, chunks, mode = 'per_chunk', session_id = 's1') => ({ active, chunks, mode, session_id });

    let c = consumer();
    let r = consumeStreamSnapshot(c, state(true, ['a']));
    check('snapshot attivo accoda un chunk', r.items.map(x => x.text).join() === 'a' && c._streamIndex === 1);

    c = consumer();
    r = consumeStreamSnapshot(c, state(true, ['a', 'b', 'c']));
    check('snapshot multiplo drenato in ordine in una callback', r.items.map(x => x.text).join() === 'a,b,c' && c._streamIndex === 3);

    c = consumer();
    // Più scritture coalesciate: consumer osserva solo lo snapshot risultante.
    // Several coalesced writes: the consumer observes only the resulting
    // snapshot.
    r = consumeStreamSnapshot(c, state(true, ['a', 'b', 'c']));
    check('coalescenza debounce non perde i chunk presenti nello snapshot', r.items.length === 3);

    c = consumer();
    consumeStreamSnapshot(c, state(true, []));
    r = consumeStreamSnapshot(c, state(false, ['a', 'b']));
    check('snapshot finale per_chunk inattivo drena i chunk pendenti', r.items.map(x => x.text).join() === 'a,b' && c._streamFinalObserved);

    r = consumeStreamSnapshot(c, state(false, ['a', 'b']));
    check('rilettura snapshot finale non duplica', r.items.length === 0);

    c = consumer();
    r = consumeStreamSnapshot(c, state(true, ['a']));
    const r2 = consumeStreamSnapshot(c, state(true, ['a', 'b']));
    check('snapshot successivi accodano solo il nuovo chunk', r.items.map(x => x.text).join() === 'a' && r2.items.map(x => x.text).join() === 'b');

    c = consumer();
    r = consumeStreamSnapshot(c, state(true, ['a', '', null, 'b']));
    check('chunk non validi scartati senza bloccare i successivi', r.items.map(x => x.text).join() === 'a,b' && r.invalid === 2 && c._streamIndex === 4);

    c = consumer();
    const previous = consumeStreamSnapshot(c, state(true, ['old']));
    const next = consumeStreamSnapshot(c, state(true, ['new'], 'per_chunk', 's2'));
    check('sessione nuova riparte da zero senza cancellare gli item già accodati dal chiamante', previous.items[0].text === 'old' && next.items[0].text === 'new' && c._streamIndex === 1);

    c = consumer();
    consumeStreamSnapshot(c, state(true, []));
    r = consumeStreamSnapshot(c, state(false, ['final'], 'at_end'));
    check('at_end consente il chunk finale dopo sessione attiva', r.items[0]?.text === 'final');
    c = consumer();
    r = consumeStreamSnapshot(c, state(false, ['final'], 'at_end'));
    check('at_end inattivo mai osservato attivo resta rifiutato', !r.accepted && r.items.length === 0);

    check('consumer reale importato dal modulo usato in extension.js', source.includes("from './stream-consumer.mjs'") && source.includes('computeStreamDelete') && source.includes('consumeStreamSnapshot(this, state)'));
    // Difetto G (giro 1): `finalObserved` (senza underscore) era un campo morto
    // — inizializzato a false al cambio sessione e MAI letto, mentre il flag
    // vero `_streamFinalObserved` restava True dalla sessione precedente. Il
    // test esistente presidiava solo il campo corretto e lasciava il morto
    // senza guardia. Qui il campo morto non deve piu' esistere e il flag vero
    // deve essere davvero azzerato al cambio di sessione.
    // Defect G (round 1): `finalObserved` (without underscore) was a dead field
    // — initialized to false at the session change and NEVER read, while the
    // real flag `_streamFinalObserved` stayed True from the previous session.
    // The existing test guarded only the correct field and left the dead one
    // unguarded. Here the dead field must no longer exist and the real flag must
    // really be reset at the session change.
    const helperSrc = fs.readFileSync(helperPath, 'utf8');
    check('nessun campo morto finalObserved senza underscore',
        !/\bfinalObserved\b/.test(helperSrc.replace(/_streamFinalObserved/g, '')));
    check('il flag letto e\' _streamFinalObserved, inizializzato al cambio sessione',
        /consumer\._streamFinalObserved = false;/.test(helperSrc) &&
        /consumer\._streamFinalObserved = true;/.test(helperSrc));
    // Comportamento: dopo aver osservato una sessione finale, il cambio di
    // sessione deve ripulire il flag (con il campo morto il reset non
    // arrivava da nessuna parte).
    // Behavior: after observing a final session, the session change must clear
    // the flag (with the dead field the reset arrived from nowhere).
    c = consumer();
    consumeStreamSnapshot(c, state(true, []));
    consumeStreamSnapshot(c, state(false, ['x']));
    check('dopo snapshot finale il flag e\' true', c._streamFinalObserved === true);
    consumeStreamSnapshot(c, state(true, ['y'], 'per_chunk', 's3'));
    check('il cambio di sessione azzera _streamFinalObserved', c._streamFinalObserved === false);
    check('il campo morto non viene creato sul consumer',
        !Object.prototype.hasOwnProperty.call(c, 'finalObserved'));
    check('niente righe che sovrascrivono lo stato consumer con undefined', !source.includes('this._streamIndex = this.nextIndex') && !source.includes('this._streamSessionId = this.sessionId'));
    c = consumer();
    const queue = [];
    const snap1 = consumeStreamSnapshot(c, state(true, ['a'])); queue.push(...snap1.items);
    const snap2 = consumeStreamSnapshot(c, state(true, ['a', 'b'])); queue.push(...snap2.items);
    check('snapshot successivi non accodano duplicati', queue.map(x => x.text).join(',') === 'a,b');
    c = consumer();
    const pendingQueue = [];
    const s1 = consumeStreamSnapshot(c, state(true, ['old'])); pendingQueue.push(...s1.items);
    const s2 = consumeStreamSnapshot(c, state(true, ['new'], 'per_chunk', 's2')); pendingQueue.push(...s2.items);
    check('cambio sessione non cancella coda preesistente', pendingQueue.map(x => x.sessionId).join(',') === 's1,s2');
    check('pacing include settle prima del successivo set_text', source.includes('STREAM_SETTLE_MS') && source.includes('this._streamQueue.shift()'));
    check('paste path branches on paste channel', source.includes("this._pasteChannel === 'type'") && source.includes("this._pasteChannel = state.paste_channel === 'type' ? 'type' : 'clipboard'"));
    const typeBranch = source.slice(source.indexOf("if (this._pasteChannel === 'type')"), source.indexOf('St.Clipboard.get_default().set_text', source.indexOf("if (this._pasteChannel === 'type')")));
    check('type branch contains no clipboard write or paste chord', !typeBranch.includes('set_text') && !typeBranch.includes('KEY_Control_L'));
    // Il terzo termine di questa asserzione era `source.includes('this._streamDestroyed')`:
    // presidiava la guardia per NOME, quindi verificava che una LETTERA ci fosse e non che
    // il teardown fermasse il lavoro. Sostituito dalla sezione S3B in fondo al file, che
    // esegue la guardia vera e misura che nessun timer di digitazione riparta.
    // The third term of this assertion was
    // `source.includes('this._streamDestroyed')`: it guarded the guard by NAME,
    // so it verified that a LETTER was there and not that the teardown stopped
    // the work. Replaced by section S3B at the bottom of the file, which runs the
    // real guard and measures that no typing timer restarts.
    check('type branch uses paced tracked timer and destroy cleanup', source.includes('this._streamTypeTimerId = GLib.timeout_add') && source.includes('GLib.source_remove(this._streamTypeTimerId)'));
    check('callback accoda in FIFO e usa worker seriale', source.includes('this._streamQueue.push(...result.items.map(item => classifyStreamItem(item, this._streamRules, this._streamBlacklist)))') && source.includes('_startStreamPasteWorker()') && source.includes('this._streamQueue[0]'));
    check('device mancante non avvia worker né consuma la coda', source.includes('if (!this._virtualDevice)') && source.includes('un prossimo evento può riprovare'));
    check('tasti segnalano success/failure al worker', /_sendKey\(keyval, state\)[\s\S]{0,260}return false;[\s\S]{0,260}return true;/.test(source));
    check('shortcut ctrl+shift+v preme Shift e sempre rilascia i tasti', source.includes("this._pasteShortcut === 'ctrl+shift+v'") && source.includes('pressed.reverse()') && source.includes('Clutter.KEY_Shift_L'));
    check('consumer usa i campi reali del worker', helperPath && source.includes('consumeStreamSnapshot(this, state)') && fs.readFileSync(helperPath, 'utf8').includes('consumer._streamIndex'));
    check('timer paste serializzato e cleanup in destroy', source.includes('this._streamPasteTimerId = GLib.timeout_add') && source.includes('GLib.source_remove(this._streamPasteTimerId)'));
    check('cambio sessione sblocca la coda paste se bloccata', source.includes('this._streamPasteBlocked = false'));
    check('worker drena i chunk drop con ciclo bounded', source.includes("while (this._streamQueue.length && this._streamQueue[0].action === 'drop')"));

    // B5: il timer ricorsivo di _requestStreamEnd deve essere tracciato,
    // annullato in destroy() e interrompersi se l'estensione è distrutta.
    // B5: the recursive timer of _requestStreamEnd must be tracked, cancelled in
    // destroy() and stop if the extension is destroyed.
    const reqEndBody = (source.match(/_requestStreamEnd\(sessionId\)\s*\{([\s\S]*?)\n    \}/) || [])[1] || '';
    check('_requestStreamEnd traccia il timer ricorsivo', reqEndBody.includes('this._streamEndTimerId = GLib.timeout_add'));
    // Qui c'era `reqEndBody.includes('this._streamDestroyed')`: la stessa verifica per
    // NOME. L'intento ("il polling di fine sessione non si ri-arma dopo destroy()") e'
    // ora misurato per comportamento nella sezione S3B, che esegue _requestStreamEnd
    // vero, chiama destroy() e poi fa scadere il timer gia' armato.
    // Here there was `reqEndBody.includes('this._streamDestroyed')`: the same
    // check by NAME. The intent ("the end-of-session polling does not re-arm
    // after destroy()") is now measured by behavior in section S3B, which runs
    // the real _requestStreamEnd, calls destroy() and then lets the already armed
    // timer expire.
    check('_init inizializza _streamEndTimerId', source.includes('this._streamEndTimerId = null'));
    check('destroy rimuove _streamEndTimerId', source.includes('GLib.source_remove(this._streamEndTimerId)'));
    // Giro 18: la chiusura ora passa per il sottocomando idempotente `stop`.
    // Prima l'asserzione pretendeva la stringa esatta
    // spawnBackground('bravoric-stream-toggle'), cioe' il chiamata SENZA
    // argomenti che e' la chiusura silenziosa: con nessuna sessione viva il
    // toggle ne AVVIA una nuova invece di chiudere (misurato in
    // stream_toggle_main). La stringa e' quindi cambiata per scelta, non
    // per indebolimento: l'intento dell'asserzione — la fine sessione passa
    // dalla porta giusta e resta agganciata al drenaggio della coda — e'
    // PRESERVATO, e i due pezzi sono verificati separatamente.
    // Round 18: the close now goes through the idempotent `stop` subcommand.
    // Before, the assertion demanded the exact string
    // spawnBackground('bravoric-stream-toggle'), i.e. the call WITHOUT arguments
    // which is the silent close: with no live session the toggle STARTS a new one
    // instead of closing (measured in stream_toggle_main). The string therefore
    // changed by choice, not by weakening: the intent of the assertion — the end
    // of the session goes through the right door and stays hooked to the queue
    // drain — is PRESERVED, and the two pieces are verified separately.
    check('_requestStreamEnd conserva la logica di fine stream',
        reqEndBody.includes("spawnBackground('bravoric-stream-toggle', 'stop')")
        && reqEndBody.includes('this._streamQueue.length || this._streamWorkerActive'));
    // Anti-drift sul punto esatto del difetto: la condizione di partenza
    // `state.active === true` (che faceva tornare in silenzio quando la
    // lettura dello stato era obsoleta) non deve piu' esserci, e la
    // sessione diversa va lasciata in pace invece di chiusa.
    // Anti-drift on the exact point of the defect: the start condition
    // `state.active === true` (which made it return silently when the state read
    // was obsolete) must no longer be there, and a different session must be
    // left alone instead of closed.
    check('_requestStreamEnd non usa piu active===true come condizione di partenza',
        !reqEndBody.includes('state.active === true')
        && reqEndBody.includes('state.session_id !== sessionId'));
    // La chiusura non puo' fallire in silenzio: ogni uscita terminale che
    // NON spara il toggle lascia una traccia (logError).
    // The close cannot fail silently: every terminal exit that does NOT fire the
    // toggle leaves a trace (logError).
    check('_requestStreamEnd non torna mai in silenzio senza traccia',
        reqEndBody.includes('nessuna chiusura necessaria')
        && reqEndBody.includes('stream end abbandonato')
        && reqEndBody.includes("logError(e, 'stream end state verification')"));
    // E la coda che non si svuota non tiene piu' la chiusura appesa: c'e' un
    // tetto oltre il quale si chiude comunque (pallino rosso segnalato).
    // And the queue that does not empty no longer keeps the close hanging: there
    // is a cap beyond which it closes anyway (red dot reported).
    check('_requestStreamEnd ha un tetto che forza la chiusura',
        reqEndBody.includes('STREAM_END_TIMEOUT_MS') && reqEndBody.includes('chiusura forzata'));

    const rules = [{ keyword: 'invio', aliases: ['invito', 'in view'], action: 'key', key: 'Return' }, { keyword: 'cancella', aliases: ['elimina'], action: 'delete', scope: 'word' }];
    check('normalizzazione NFC e punteggiatura finale', normalizeCommandKeyword(' Invio... ') === normalizeCommandKeyword('invio'));
    for (const text of ['invio.', 'Invio!', 'invio...', 'invio,'])
        check(`keyword punteggiata ${text}`, classifyStreamItem({text}, rules).action === 'key');
    for (const text of ['invito.', 'Invito!', 'in view...', 'In View!'])
        check(`alias punteggiato ${text}`, classifyStreamItem({text}, rules).action === 'key');
    check('alias delete classificato delete', classifyStreamItem({text: 'elimina!'}, rules).command.scope === 'word');
    check('match esatto, niente sottostringhe', classifyStreamItem({text: 'premi invio'}, rules).action === 'paste');
    check('seconda regola classificata delete', classifyStreamItem({text: 'cancella!'}, rules).command.scope === 'word');

    // Blacklist tests
    const blacklistSet = new Set(['grazie', 'thank you']);
    check('blacklist drops exact normalized phrase', classifyStreamItem({text: 'Grazie!'}, rules, blacklistSet).action === 'drop');
    check('blacklist drops multi-word exact match', classifyStreamItem({text: 'THANK YOU...'}, rules, blacklistSet).action === 'drop');
    check('blacklist does not drop phrase containing word as substring', classifyStreamItem({text: 'Grazie a tutti'}, rules, blacklistSet).action === 'paste');
    check('blacklist has precedence over rules', classifyStreamItem({text: 'invio!'}, [{keyword: 'invio', action: 'key', key: 'Return'}], new Set(['invio'])).action === 'drop');
    // Regressione del bug regex doppio-escaped: la normalizzazione deve trattare \s
    // come whitespace e NON rimuovere per errore "s" o backslash finali.
    // Regression of the double-escaped regex bug: the normalization must treat
    // \s as whitespace and must NOT remove a trailing "s" or backslash by
    // mistake.
    check('normalizzazione non rimuove una "s" finale', normalizeCommandKeyword('thanks') === 'thanks');
    check('normalizzazione non rimuove un backslash finale', normalizeCommandKeyword('foo\\') === 'foo\\');
    check('normalizzazione rimuove solo punteggiatura e spazi finali', normalizeCommandKeyword('cancella...') === 'cancella');
    const consumerSource = fs.readFileSync(helperPath, 'utf8');
    check('stream-consumer.mjs usa regex whitespace finale', consumerSource.includes("replace(/\\s+$/u, '')") && consumerSource.includes("match(/\\S+$/u)"));
    check('stream-consumer.mjs senza regex doppio-escaped', !consumerSource.includes('\\\\s+$') && !consumerSource.includes('\\\\S+$'));
    check('extension.js senza regex doppio-escaped', !source.includes('\\\\s+$') && !source.includes('\\\\S+$'));
    check('stream-consumer.mjs senza classe doppio-escaped', !consumerSource.includes('[\\\\s.'));

    // Test computeStreamDelete
    let delRes = computeStreamDelete(['ciao ', 'mondo '], 'chunk');
    check("(['ciao ', 'mondo '], 'chunk') => count 6, ['ciao ']", delRes.count === 6 && JSON.stringify(delRes.segments) === JSON.stringify(['ciao ']));

    delRes = computeStreamDelete(['mondo '], 'chunk');
    check("(['mondo '], 'chunk') => count 5, [' ']", delRes.count === 5 && JSON.stringify(delRes.segments) === JSON.stringify([' ']));

    delRes = computeStreamDelete(['ciao ', 'mondo '], 'word');
    check("(['ciao ', 'mondo '], 'word') => count 6, ['ciao ']", delRes.count === 6 && JSON.stringify(delRes.segments) === JSON.stringify(['ciao ']));

    delRes = computeStreamDelete(['mondo '], 'word');
    check("(['mondo '], 'word') => count 5, [' ']", delRes.count === 5 && JSON.stringify(delRes.segments) === JSON.stringify([' ']));

    delRes = computeStreamDelete(['ciao mondo '], 'word');
    check("(['ciao mondo '], 'word') => count 6, ['ciao ']", delRes.count === 6 && JSON.stringify(delRes.segments) === JSON.stringify(['ciao ']));

    delRes = computeStreamDelete(['ciao mondo '], 'chunk');
    check("(['ciao mondo '], 'chunk') => count 10, [' ']", delRes.count === 10 && JSON.stringify(delRes.segments) === JSON.stringify([' ']));

    delRes = computeStreamDelete([' '], 'chunk');
    check("([' '], 'chunk') => count 0", delRes.count === 0);

    delRes = computeStreamDelete([], 'word');
    check("([], 'word') => count 0", delRes.count === 0);

    delRes = computeStreamDelete(['ciao '], 'invalid_scope');
    check("scope invalido => count -1", delRes.count === -1);

    // ====================================================================
    // Giro 2 (F1, C2): _onStreamStateChanged eseguito davvero.
    // Prima questo file guardava solo stringhe in extension.js: nessuna prova
    // comportamentale sul metodo che decide cosa finisce nel campo e cosa
    // finisce nel file di contesto. Qui il metodo REALE viene estratto per
    // brace-matching ed eseguito, con this legato all'istanza.
    // ====================================================================
    // ====================================================================
    // Round 2 (F1, C2): _onStreamStateChanged really run.
    // Before, this file only looked at strings in extension.js: no behavioral
    // proof on the method that decides what ends up in the field and what ends
    // up in the context file. Here the REAL method is extracted by
    // brace-matching and run, with this bound to the instance.
    // ====================================================================
    console.log('== giro 2: _onStreamStateChanged reale (F1, C2) ==');

    function methodBody(src, signature) {
        const at = src.indexOf(signature);
        assert.ok(at !== -1, `${signature} non trovato in extension.js`);
        const open = src.indexOf('{', at);
        const end = matchBrace(src, open);
        if (end === -1)
            throw new Error('graffe non bilanciate');
        return src.slice(open + 1, end);
    }

    // Ancoraggio sulla DEFINIZIONE, non sulla chiamata: la stringa
    // '_onStreamStateChanged()' compare PRIMA alla riga 436, dentro un'altra
    // funzione, e il brace-matching avrebbe estratto il corpo di quella.
    // Anchored on the DEFINITION, not on the call: the string
    // '_onStreamStateChanged()' appears FIRST at line 436, inside another
    // function, and the brace-matching would have extracted the body of that one.
    const body = methodBody(source, '\n    _onStreamStateChanged() {');
    // Il corpo usa GLib/Gio, gli helper del consumer, logError, _sm e Main:
    // si forniscono tutti, e si chiama la funzione con this = istanza.
    // The body uses GLib/Gio, the consumer's helpers, logError, _sm and Main: we
    // provide them all, and the function is called with this = instance.
    const realMethod = new Function(
        'GLib', 'Gio', 'consumeStreamSnapshot', 'classifyStreamItem', 'parseBlacklist',
        'logError', 'TextDecoder', 'JSON', 'STREAM_STATE_PATH',
        body,
    );

    function newIndicator() {
        return {
            _streamSessionId: null,
            _streamIndex: 0,
            _streamWasActive: false,
            _streamFinalObserved: false,
            _streamQueue: [],
            _streamSegments: null,
            _streamPasteBlocked: false,
            _streamRules: [],
            _streamBlacklist: new Set(),
            _pasteDelayMs: 250,
            _pasteChannel: 'clipboard',
            _pasteShortcut: 'ctrl+v',
            _streamWorkerActive: false,
            _workerRuns: 0,
            _liveTextWrites: [],
            _writeStreamLiveText() { this._liveTextWrites.push((this._streamSegments || []).slice()); },
            _startStreamPasteWorker() { this._workerRuns++; },
        };
    }

    function feed(indicator, snapshot) {
        const GLibStub = {
            file_get_contents: () => [true, new TextEncoder().encode(JSON.stringify(snapshot))],
        };
        return realMethod.call(indicator, GLibStub, null, consumeStreamSnapshot,
            classifyStreamItem, parseBlacklist, () => {}, TextDecoder, JSON,
            '/tmp/stream_state.json');
    }

    // --- F1: coda bloccata della sessione precedente -------------------
    // --- F1: blocked queue of the previous session -------------------
    const ind = newIndicator();
    feed(ind, state(true, ['PRIMO CHUNK']));
    check('F1: il chunk della sessione 1 finisce in coda',
        ind._streamQueue.length === 1 && ind._streamQueue[0].text === 'PRIMO CHUNK');
    // invio fallito: il blocco scatta e il chunk resta in testa
    // failed send: the block fires and the chunk stays at the head
    ind._streamPasteBlocked = true;
    ind._streamSegments = ['PRIMO CHUNK'];

    feed(ind, state(true, ['NUOVA SESSIONE'], 'per_chunk', 's2'));
    check('F1: al cambio di sessione il blocco della coda viene sbloccato',
        ind._streamPasteBlocked === false,
        );
    check('F1: il chunk mai consegnato della sessione 1 NON viene riconsegnato',
        ind._streamQueue.every(x => x.text !== 'PRIMO CHUNK'),
        );
    check('F1: la coda contiene solo i chunk della sessione nuova',
        ind._streamQueue.length === 1 && ind._streamQueue[0].text === 'NUOVA SESSIONE',
        );
    check('F1: i segmenti sono azzerati al cambio di sessione',
        ind._streamSegments === null);
    check('F1: il worker parte con la coda nuova', ind._workerRuns >= 2);

    // Caso con comando in testa: il comando della sessione vecchia, se
    // sopravvivesse, agirebbe sui segmenti NUOVI (BackSpace distruttivi).
    // Case with a command at the head: the old session's command, if it
    // survived, would act on the NEW segments (destructive BackSpaces).
    const indCmd = newIndicator();
    const snapshotCmd = (active, chunks, session) => ({
        active, chunks, mode: 'per_chunk', session_id: session,
        commands: [{ keyword: 'cancella', aliases: [], action: 'delete', scope: 'word' }],
    });
    feed(indCmd, snapshotCmd(true, ['cancella'], 's1'));
    indCmd._streamPasteBlocked = true;
    indCmd._streamSegments = ['testo sessione 1'];
    const queueBefore = indCmd._streamQueue.length;
    feed(indCmd, snapshotCmd(true, ['nuova frase qui'], 's2'));
    check('F1: il comando delete della sessione vecchia non sopravvive al cambio',
        indCmd._streamQueue.every(x => x.action !== 'delete'),
        );
    check('F1: i segmenti nuovi non sono bersagli di un delete distruttivo',
        indCmd._streamSegments === null && indCmd._streamQueue.length === queueBefore,
        );

    // Il blocco SENZA cambio di sessione deve invece restare: un errore di
    // invio non viene dimenticato al primo chunk successivo.
    // The block WITHOUT a session change must instead stay: a send error is not
    // forgotten at the first following chunk.
    const indSticky = newIndicator();
    feed(indSticky, state(true, ['uno']));
    indSticky._streamPasteBlocked = true;
    feed(indSticky, state(true, ['due']));
    check('F1: senza cambio di sessione il blocco persiste (niente auto-sblocco)',
        indSticky._streamPasteBlocked === true,
        );

    // --- C2: il file di contesto non riceve segmenti mai consegnati -----
    // Anche qui si esegue il metodo REALE (_writeStreamLiveText), non una
    // copia: la copia precedente scriveva anche quando i segmenti erano null,
    // cioe' non presidiava la guardia che il difetto riguarda. Gio e' finto,
    // la scrittura viene catturata.
    // --- C2: the context file does not receive never-delivered segments -----
    // Here too the REAL method (_writeStreamLiveText) is run, not a copy: the
    // previous copy also wrote when the segments were null, i.e. it did not
    // guard the guard the defect is about. Gio is fake, the write is captured.
    const liveBody = methodBody(source, '\n    _writeStreamLiveText() {');
    const realLive = new Function(
        'Gio', 'STREAM_LIVE_TEXT_PATH', 'TextEncoder', 'logError', liveBody,
    );

    function makeLiveIndicator(segments) {
        const ind = newIndicator();
        ind._streamSessionId = 's1';
        ind._streamSegments = segments;
        ind._written = [];
        const GioStub = {
            FileCreateFlags: { REPLACE_DESTINATION: 1 },
            File: {
                new_for_path: () => ({
                    replace_contents_async: (bytes) => {
                        ind._written.push(JSON.parse(new TextDecoder().decode(bytes)));
                    },
                }),
            },
        };
        ind._write = () => realLive.call(ind, GioStub, '/tmp/live.json',
            TextEncoder, () => {});
        return ind;
    }

    // Caso normale: i segmenti consegnati vengono scritti.
    // Normal case: the delivered segments are written.
    const ok = makeLiveIndicator(['PRIMO']);
    ok._write();
    check('C2: il file di contesto registra i segmenti consegnati',
        ok._written.length === 1
        && JSON.stringify(ok._written[0].segments) === JSON.stringify(['PRIMO'])
        && ok._written[0].session_id === 's1');
    // Il caso del difetto: segmenti sconosciuti (null) dopo il cambio di
    // sessione -> il metodo deve NON scrivere. E' la guardia che chiude C2.
    // The defect case: unknown segments (null) after the session change -> the
    // method must NOT write. It is the guard that closes C2.
    const unknown = makeLiveIndicator(null);
    unknown._write();
    check('C2: con segmenti sconosciuti non viene scritto nulla',
        unknown._written.length === 0);
    // Nessuna sessione: idem, non si scrive.
    // No session: same, nothing is written.
    const noSession = makeLiveIndicator(['X']);
    noSession._streamSessionId = null;
    noSession._write();
    check('C2: senza sessione non viene scritto nulla', noSession._written.length === 0);

    // Il percorso completo: al cambio di sessione i segmenti sono azzerati
    // (null) e quindi il file di contesto NON contiene piu' i segmenti della
    // sessione precedente, che non erano mai stati consegnati.
    // The full path: at the session change the segments are reset (null) and
    // therefore the context file NO LONGER contains the segments of the previous
    // session, which had never been delivered.
    const indFlow = newIndicator();
    feed(indFlow, state(true, ['PRIMO'], 'per_chunk', 's1'));
    indFlow._streamSegments = ['PRIMO'];
    const flow = makeLiveIndicator(indFlow._streamSegments);
    flow._streamSessionId = 's1';
    flow._write();
    feed(indFlow, state(true, ['DOPO'], 'per_chunk', 's2'));
    check('F1/C2: al cambio di sessione i segmenti precedenti spariscono dallo stato',
        indFlow._streamSegments === null);
    const after = makeLiveIndicator(indFlow._streamSegments);
    after._streamSessionId = indFlow._streamSessionId;
    after._write();
    check('C2: il file di contesto non riceve i segmenti della sessione precedente',
        after._written.length === 0,
        );

    // ====================================================================
    console.log('== giro 3: _runStreamCommand reale, tasto distruttivo (DIF-1) ==');
    // Il difetto: nel ramo action="key" il tasto veniva premuto e rilasciato e
    // poi si usciva, senza toccare _streamSegments e senza chiamare
    // _writeStreamLiveText. Il BackSpace cancellava davvero i caratteri dal
    // campo, ma il modello — che rappresenta cosa c'e' nel campo — restava
    // invariato: il backend leggeva come presenti parole appena cancellate.
    // Ancoraggio sulla DEFINIZIONE, per la stessa ragione di sopra.
    // The defect: in the action="key" branch the key was pressed and released
    // and then it exited, without touching _streamSegments and without calling
    // _writeStreamLiveText. The BackSpace really deleted the characters from the
    // field, but the model — which represents what is in the field — stayed
    // unchanged: the backend read as present words that had just been deleted.
    // Anchored on the DEFINITION, for the same reason as above.
    const cmdBody = methodBody(source, '\n    _runStreamCommand(item) {');
    // Il corpo usa Clutter (la keyMap), Main, logError, computeStreamDelete e
    // gli helper dell'istanza: si forniscono tutti come parametri.
    // The body uses Clutter (the keyMap), Main, logError, computeStreamDelete and
    // the instance's helpers: we provide them all as parameters.
    const realCommand = new Function(
        'Clutter', 'Main', 'logError', 'computeStreamDelete', 'GLib', '_', 'item', NOTIFY_PRELUDE + cmdBody,
    );
    const KEY = (name) => `KEY_${name}`;
    const ClutterStub = {
        KeyState: { PRESSED: 1, RELEASED: 0 },
        ...Object.fromEntries(['Return', 'Enter', 'Tab', 'space', 'Escape', 'BackSpace',
            'Delete', 'Home', 'End', 'Page_Up', 'Page_Down', 'Left', 'Right', 'Up', 'Down',
            ...Array.from({ length: 12 }, (_, i) => `F${i + 1}`)].map(k => [KEY(k), k])),
    };
    function commandIndicator(segments) {
        const ind = newIndicator();
        ind._streamSessionId = 's1';
        ind._streamSegments = segments;
        ind._sentKeys = [];
        ind._sendKey = (key) => { ind._sentKeys.push(key); return true; };
        ind._requestStreamEnd = () => {};
        return ind;
    }
    // Il metodo legge il comando da `item.command`, quindi l'item passato
    // avvolge la regola: e' la stessa forma che produce classifyStreamItem.
    // The method reads the command from `item.command`, so the item passed wraps
    // the rule: it is the same shape that classifyStreamItem produces.
    const runCommand = (ind, command) => realCommand.call(
        ind, ClutterStub,
        { notifyError: () => { ind._notified = true; } },
        () => {}, computeStreamDelete, { SOURCE_REMOVE: false },
        // gettext di test: le stringhe restano inglese, la sola cosa che
        // conta qui e' che i rami di uscita per errore restino eseguibili.
        // Test gettext: the strings stay English, the only thing that matters here
        // is that the error exit branches stay runnable.
        s => s, { sessionId: 's1', command },
    );

    // CASO A: BackSpace su due segmenti. Il segmento cancellato sparisce dal
    // modello E il file di contesto viene riscritto.
    // CASE A: BackSpace on two segments. The deleted segment disappears from the
    // model AND the context file is rewritten.
    const indA = commandIndicator(['Parole da cancellare. ']);
    runCommand(indA, { action: 'key', key: 'BackSpace' });
    check('DIF-1: BackSpace toglie il chunk dal modello dei segmenti',
        JSON.stringify(indA._streamSegments) === JSON.stringify([' ']),
        );
    check('DIF-1: BackSpace riscrive il file di contesto (non solo la memoria)',
        indA._liveTextWrites.length === 1
        && JSON.stringify(indA._liveTextWrites[0]) === JSON.stringify([' ']),
        );
    check('DIF-1: il tasto BackSpace e\' stato inviato una volta sola',
        indA._sentKeys.length === 2
        && indA._sentKeys.every(k => k === ClutterStub[KEY('BackSpace')]),
        );
    // Il testo che il backend legge non deve piu' contenere la frase cancellata.
    // The text the backend reads must no longer contain the deleted sentence.
    check('DIF-1: il testo cancellato non resta nel file letto dal backend',
        !JSON.stringify(indA._liveTextWrites[0]).includes('da cancellare'),
        );

    // CASO B: il ramo delete, come riferimento: il comportamento noto resta.
    // CASE B: the delete branch, as a reference: the known behavior stays.
    const indB = commandIndicator(['Parole da cancellare. ']);
    runCommand(indB, { action: 'delete', scope: 'chunk' });
    check('DIF-1: il ramo delete continua a svuotare il modello',
        JSON.stringify(indB._streamSegments) === JSON.stringify([' ']),
        );

    // CASO C: tasto NON distruttivo. Nessuna modifica al modello: premere
    // "a capo" o "freccia" non cancella testo, e toccare i segmenti qui
    // inventerebbe una cancellazione che non e' avvenuta.
    // CASE C: NON-destructive key. No change to the model: pressing "newline" or
    // "arrow" does not delete text, and touching the segments here would invent
    // a deletion that did not happen.
    const indC = commandIndicator(['Testo intatto. ']);
    runCommand(indC, { action: 'key', key: 'Return' });
    check('DIF-1: un tasto non distruttivo lascia i segmenti come sono',
        JSON.stringify(indC._streamSegments) === JSON.stringify(['Testo intatto. '])
        && indC._liveTextWrites.length === 0,
        );

    // CASO D: piu' segmenti. La cancellazione di chunk toglie l'ULTIMO, e il
    // file di contesto deve riflettere quello che resta, non la lista intera.
    // CASE D: several segments. The chunk deletion removes the LAST one, and the
    // context file must reflect what remains, not the whole list.
    const indD = commandIndicator(['Primo. ', 'Secondo. ']);
    runCommand(indD, { action: 'key', key: 'Delete' });
    check('DIF-1: con piu\' segmenti la cancellazione toglie solo l\'ultimo',
        JSON.stringify(indD._streamSegments) === JSON.stringify(['Primo. '])
        && indD._liveTextWrites.length === 1,
        );

    // ====================================================================
    // giro 4 (F10): _init REALE su un Indicatore appena creato.
    //
    // Prima questa campagna non aveva nessuna prova che un Indicatore appena
    // creato reggesse: i campi che _init non inizializzava (_streamSegments,
    // _streamRules, _streamBlacklist, _monitor, ...) esistevano solo dopo la
    // prima lettura riuscita, e un accesso in quel buco dava
    // "Cannot read properties of undefined" invece di un valore sentinella.
    // Qui il metodo VERO viene eseguito con stub fedeli di St/PopupMenu/
    // Gio/GLib/Clutter: se _init inizializza un campo con un valore che non
    // regge il primo uso (null dove serve .push, array dove serve .size),
    // questa sezione va ROSSA.
    // ====================================================================
    // ====================================================================
    // round 4 (F10): REAL _init on a freshly created Indicator.
    //
    // Before, this campaign had no proof that a freshly created Indicator held
    // up: the fields that _init did not initialize (_streamSegments,
    // _streamRules, _streamBlacklist, _monitor, ...) existed only after the first
    // successful read, and an access in that hole gave "Cannot read properties of
    // undefined" instead of a sentinel value. Here the REAL method is run with
    // faithful stubs of St/PopupMenu/Gio/GLib/Clutter: if _init initializes a
    // field with a value that does not hold up at first use (null where .push is
    // needed, array where .size is needed), this section goes RED.
    // ====================================================================
    console.log('== giro 4: _init reale, un Indicatore appena creato non deve esplodere ==');

    // Il corpo di _init dipende da super._init() e chiama metodi fratelli
    // (_setAccessibleState, _idleGicon, _buildDiagnosticsSubmenu, i watcher,
    // _startVirtualDevice, _refreshStatus, _refreshHistory), quindi non puo'
    // girare come funzione libera: viene montato in una classe che eredita da
    // una base finta che fa da PanelMenu.Button. I fratelli sono presi dal
    // SORGENTE VERO con lo stesso brace-matching dei casi precedenti: quello
    // che si eseguisce qui dentro e' il prodotto, non una sua ricostruzione.
    // The body of _init depends on super._init() and calls sibling methods
    // (_setAccessibleState, _idleGicon, _buildDiagnosticsSubmenu, the watchers,
    // _startVirtualDevice, _refreshStatus, _refreshHistory), so it cannot run as
    // a free function: it is mounted in a class that inherits from a fake base
    // acting as PanelMenu.Button. The siblings are taken from the REAL SOURCE
    // with the same brace-matching as the previous cases: what runs in here is
    // the product, not a reconstruction of it.
    const initBody = methodBody(source, '\n    _init(extension) {');
    const sibling = (signature) => methodBody(source, `\n    ${signature} {`);

    const menuItem = (label) => ({
        label: { text: label },
        _sensitive: true,
        _handlers: {},
        setSensitive(v) { this._sensitive = v; },
        connect(sig, cb) { (this._handlers[sig] ||= []).push(cb); return 1; },
        activate() { (this._handlers.activate || []).forEach(cb => { cb(); }); },
    });
    const menuOf = () => ({ _items: [], addMenuItem(i) { this._items.push(i); } });
    class FakeMenuButton {
        _init(alignment, name) { this._alignment = alignment; this._name = name; this.menu = menuOf(); }
        add_child(child) { this._children = this._children || []; this._children.push(child); }
        get_accessible() { return (this._accessible ||= { accessible_description: '' }); }
        destroy() { this._destroyedByStub = true; }
    }
    // I metodi che _init richiama e che qui NON girano: leggono davvero il
    // disco o parlano con la tastiera virtuale. Non sono il soggetto di questa
    // sezione (che guarda i campi che _init lascia inizializzati), e sostituirli
    // e' esattamente il tipo di copia che i casi precedenti rifiutano: qui si
    // dichiara cosa non gira, non si finge che giri.
    // The methods that _init calls and that do NOT run here: they really read
    // the disk or talk to the virtual keyboard. They are not the subject of this
    // section (which looks at the fields that _init leaves initialized), and
    // replacing them is exactly the kind of copy the previous cases refuse: here
    // we declare what does not run, we do not pretend it does.
    const STUBBED_IO = `
        _watchStatusFile() { this._monitor = null; this._monitorId = 0; }
        _watchStreamStateFile() { this._streamMonitor = null; this._streamMonitorId = 0; }
        _startVirtualDevice() { this._virtualDevice = null; }
        _refreshStatus() {}
        _refreshHistory() {}
    `;
    const InitClass = new Function(
        'GObject', 'PanelMenu', 'PopupMenu', 'St', 'GLib', 'Gio', 'Clutter', 'Main',
        '_', 'logError', 'spawnBackground', 'spawnConfigEditor', 'showCopiedOsd',
        'STATUS_PATH', 'HISTORY_PATH', 'STREAM_STATE_PATH', 'STREAM_LIVE_TEXT_PATH',
        'VENV_BIN', 'THEME_ICONS', 'BLINK_INTERVAL_MS', 'REFRESH_DEBOUNCE_MS',
        'TIMEOUT_CHECK_INTERVAL_SECONDS', 'STREAM_PACING_DEFAULT_MS', 'STREAM_SETTLE_MS',
        'STREAM_DEBOUNCE_MS', 'STREAM_END_TIMEOUT_MS',
        'consumeStreamSnapshot', 'classifyStreamItem', 'parseBlacklist', 'computeStreamDelete',
        `${NOTIFY_PRELUDE}return class extends PanelMenu.Button {
            _init(extension) { super._init(0.0, 'Bravoric STT/OCR');${initBody}
            }
            _setAccessibleState(state) {${sibling('_setAccessibleState(state)')}}
            _idleGicon() {${sibling('_idleGicon()')}}
            _diagnosticsFacts() {${sibling('_diagnosticsFacts()')}}
            _historyTagLabel(service, kind) {${sibling('_historyTagLabel(service, kind)')}}
            _buildDiagnosticsSubmenu() {${sibling('_buildDiagnosticsSubmenu()')}}
            _diagnosticsReport() {${sibling('_diagnosticsReport()')}}
            ${STUBBED_IO}
        };`)(null,
        { Button: FakeMenuButton }, {
            PopupMenuItem: function (l) { return menuItem(l); },
            PopupImageMenuItem: function (l) { return menuItem(l); },
            PopupSeparatorMenuItem: function () { return menuItem('---'); },
            PopupSubMenuMenuItem: function (l) { return Object.assign(menuItem(l), { menu: menuOf() }); },
        },
        { Icon: class { constructor() { this.gicon = null; this._classes = new Set(); }
            add_style_class_name(c) { this._classes.add(c); }
            remove_style_class_name(c) { this._classes.delete(c); } } },
        { PRIORITY_DEFAULT: 0, SOURCE_CONTINUE: true, SOURCE_REMOVE: false,
            FileTest: { EXISTS: 1 },
            timeout_add: () => 1, timeout_add_seconds: () => 1, source_remove: () => {},
            file_test: () => true, build_filenamev: parts => parts.join('/'),
            get_home_dir: () => '/tmp', get_user_data_dir: () => '/tmp',
            path_get_dirname: p => p.split('/').slice(0, -1).join('/') },
        { FileCreateFlags: { REPLACE_DESTINATION: 1 }, FileMonitorFlags: { NONE: 0 },
            IOErrorEnum: { EXISTS: 1 },
            icon_new_for_string: path => ({ path }),
            Cancellable: class { cancel() { this._c = true; } is_cancelled() { return !!this._c; } },
            File: { new_for_path: () => ({
                make_directory_with_parents() {},
                load_contents_async(_c, cb) { cb({ load_contents_finish: () => [true, new TextEncoder().encode('{}')] }); },
                monitor_directory: () => ({ connect: () => 1, cancel() {} }),
            }) } },
        { KEY_Return: 36, KeyState: { PRESSED: 1, RELEASED: 0 },
            ThemedIcon: { new: name => ({ name }) },
            get_default_backend: () => ({ get_default_seat: () => ({ create_virtual_device: () => ({}) }) }),            get_current_event_time: () => 0, unicode_to_keysym: () => 0,
            InputDeviceType: { KEYBOARD_DEVICE: 1 } },
        { notify() {}, notifyError() {} },
        s => s, () => {}, () => {}, () => {}, () => {},
        '/tmp/status.json', '/tmp/history.json', '/tmp/stream_state.json', '/tmp/live.json',
        '/tmp/venv/bin', { idle: 'idle-icon' }, 500, 250, 30, 250, 30, 250, 5000,
        consumeStreamSnapshot, classifyStreamItem, parseBlacklist, computeStreamDelete);

    // L'estensione finta serve solo a openPreferences()/path, che _init non chiama.
    // The fake extension only serves openPreferences()/path, which _init does
    // not call.
    let fresh = null;
    let initError = null;
    try {
        fresh = new InitClass({ openPreferences() {}, path: '/tmp' });
        fresh._init({ openPreferences() {}, path: '/tmp' });
    } catch (e) {
        initError = e;
    }
    if (initError)
        console.error('    _init ha fallito con:', initError && initError.stack || initError);
    check('F10: _init reale su un indicatore vuoto non lancia', initError === null
        && fresh !== null);

    check('F10: la coda parte vuota e davvero un array', Array.isArray(fresh._streamQueue));
    check('F10: i segmenti partono da null (buffer sconosciuto), non da []',
        fresh._streamSegments === null);
    check('F10: le regole e la blacklist partono da valori utilizzabili',
        Array.isArray(fresh._streamRules) && fresh._streamBlacklist instanceof Set);
    // I contatori generazionali devono essere 0 e NON undefined: e' il campo su
    // cui _refreshStatus/_refreshHistory fanno `x += 1`. Se tornassero
    // undefined, il primo giro produrrebbe NaN e la guardia `gen !== this._x`
    // lascerebbe passare la risposta vecchia: tornerebbe il difetto stale.
    // The generation counters must be 0 and NOT undefined: it is the field on
    // which _refreshStatus/_refreshHistory do `x += 1`. If they came back
    // undefined, the first round would produce NaN and the guard `gen !==
    // this._x` would let the old answer through: the stale defect would come
    // back.
    check('F10: i contatori generazionali sono 0, non undefined',
        fresh._statusRefreshGen === 0 && fresh._historyRefreshGen === 0);
    // I campi che destroy() legge: se non esistessero, il teardown si fermerebbe
    // al primo `if (this._monitor)` che trova undefined come falso.
    // The fields that destroy() reads: if they did not exist, the teardown would
    // stop at the first `if (this._monitor)` that finds undefined as false.
    check('F10: i monitor sono inizializzati a null prima di essere creati',
        fresh._monitor === null && fresh._streamMonitor === null
        && fresh._monitorId === 0 && fresh._streamMonitorId === 0);

    // Il punto per cui il null conta: se _streamSegments fosse [] un comando
    // delete partirebbe da un campo "vuoto" e cancellerebbe a caso. Con null
    // computeStreamDelete non trova nulla da cancellare (fail-safe no-op).
    // The point why null matters: if _streamSegments were [] a delete command
    // would start from an "empty" field and delete at random. With null
    // computeStreamDelete finds nothing to delete (fail-safe no-op).
    check('F10: un delete su un campo sconosciuto non cancella nulla',
        computeStreamDelete(fresh._streamSegments || [], 'chunk').count === 0
        && computeStreamDelete(fresh._streamSegments || [], 'word').count === 0);

    // destroy() appena acceso l'estensione: l'unica sorgente che esiste e'
    // quella che _init ha appena armato in fondo, il timer periodico di
    // controllo timeout (GLib.timeout_add_seconds). Tutto il resto e' gia'
    // a 0/null e NON deve generare rimozioni: source_remove su un id
    // inesistente e' un avviso a runtime in GLib vero, ed e' esattamente il
    // sintomo che una riga di teardown copiata male produce.
    //
    // Il numero e' 1 e non 0 perche' _init davvero arma quel timer: questo
    // test gira sull'indicatore REALE, quindi conta anche quello. Se il
    // teardown smettesse di ripulirlo, il valore sarebbe 0 e il verde di
    // sotto sparirebbe.
    // destroy() right after the extension is switched on: the only source that
    // exists is the one that _init has just armed at the end, the periodic
    // timeout check timer (GLib.timeout_add_seconds). Everything else is already
    // 0/null and must NOT generate removals: source_remove on a non-existent id
    // is a runtime warning in the real GLib, and it is exactly the symptom that
    // a badly copied teardown line produces.
    //
    // The number is 1 and not 0 because _init really arms that timer: this test
    // runs on the REAL indicator, so it counts that one too. If the teardown
    // stopped cleaning it up, the value would be 0 and the green below would
    // vanish.
    const removedIds = [];
    const destroyBody = methodBody(source, '\n    destroy() {');
    // destroy() chiama super.destroy() in fondo: la super classe e' una base
    // finta che non fa niente, perche' qui si prova il TEARDOWN dell'estensione,
    // non quello di PanelMenu.Button (che vuole Mutter).
    // destroy() calls super.destroy() at the end: the superclass is a fake base
    // that does nothing, because here the extension's TEARDOWN is proven, not
    // that of PanelMenu.Button (which needs Mutter).
    const makeDestroy = (onRemove) => new Function('GLib', 'Base',
        `return class extends Base { destroy() {${destroyBody}\n} }`)(
        { source_remove: onRemove },
        class { destroy() { /* base finta: super.destroy() non deve fare nulla */ } });
/*
 * fake base: super.destroy() must do nothing
 */
    let destroyError = null;
    // destroy() azzera il campo dopo averlo usato: l'id va letto PRIMA.
    // destroy() resets the field after using it: the id must be read BEFORE.
    const expectedId = fresh._timeoutCheckId;
    try {
        // Girando sull'indicatore davvero prodotto da _init (non su un'istanza
        // vuota), il teardown trova i campi nello stato in cui _init li ha
        // lasciati: e' esattamente il percorso che fa disable() su un
        // indicatore appena acceso.
        // Running on the indicator really produced by _init (not on an empty
        // instance), the teardown finds the fields in the state in which _init left
        // them: it is exactly the path that disable() takes on a freshly switched-on
        // indicator.
        makeDestroy(id => { removedIds.push(id); }).prototype.destroy.call(fresh);
    } catch (e) {
        destroyError = e;
    }
    if (destroyError)
        console.error('    destroy ha fallito con:', destroyError && destroyError.stack || destroyError);
    check('F10: destroy ripulisce solo il timer che _init ha armato, nessun id inesistente',
        destroyError === null
        && removedIds.length === 1
        && removedIds[0] === expectedId);

    // Prova di non-vacuità: lo stesso destroy() su un'istanza con un timer
    // fittizio in corso DEVE rimuoverlo. Se il test di sopra passasse per
    //che' il teardown non gira affatto, questa riga se ne accorgerebbe: il
    // GUARDIANO può essere verde solo perche' la rimozione c'e' davvero.
    // Non-vacuity proof: the same destroy() on an instance with a fictitious
    // timer in progress MUST remove it. If the test above passed because the
    // teardown does not run at all, this line would notice: the GUARDIAN can be
    // green only because the removal really exists.
    let removedLive = 0;
    try {
        // Secondo indicatore con i campi che _init scrive e DUE timer vivi:
        // quello di incolla e quello periodico (che _init aveva gia' azzerato
        // sopra, chiamando destroy su fresh, quindi va rimesso qui).
        // Second indicator with the fields that _init writes and TWO live timers: the
        // paste one and the periodic one (which _init had already reset above,
        // calling destroy on fresh, so it must be put back here).
        const live = Object.create(Object.getPrototypeOf(fresh));
        Object.assign(live, fresh, { _streamPasteTimerId: 42, _timeoutCheckId: 7 });
        makeDestroy(() => { removedLive++; }).prototype.destroy.call(live);
    } catch (e) {
        destroyError = e;
    }
    check('F10: CONTRO — destroy rimuove anche i timer vivi (il verde sopra non e\' vacuo)',
        destroyError === null && removedLive === 2);

    // ====================================================================
    // S3B: dopo destroy() la coda accodata non viene piu' elaborata e il
    // timer di scrittura non riparte.
    //
    // Il gate presidiava una LETTERA (`source.includes('this._streamDestroyed')`):
    // poteva restare verde con la guardia svuotata, o con il flag rimosso e
    // la guardia lasciata senza condizione. Qui girano i metodi VERI estratti
    // da extension.js — _startStreamPasteWorker, _requestStreamEnd e destroy —
    // e la verifica e' sugli EFFETTI: nessun tasto inviato, nessun timer
    // ri-armato, nessun toggle sparato, coda svuotata.
    // ====================================================================
    // ====================================================================
    // S3B: after destroy() the queued queue is no longer processed and the write
    // timer does not restart.
    //
    // The gate used to guard a LETTER (`source.includes('this._streamDestroyed')`):
    // it could stay green with the guard emptied, or with the flag removed and
    // the guard left with no condition. Here the REAL methods extracted from
    // extension.js run — _startStreamPasteWorker, _requestStreamEnd and destroy —
    // and the check is on the EFFECTS: no key sent, no timer re-armed, no toggle
    // fired, queue emptied.
    // ====================================================================
    console.log('== S3B: teardown — coda svuotata e timer di scrittura non riparte ==');

    const workerBody = methodBody(source, '\n    _startStreamPasteWorker() {');
    const endBody = methodBody(source, '\n    _requestStreamEnd(sessionId) {');

    const ClutterRun = { KEY_Control_L: 37, KEY_Shift_L: 50, KEY_v: 55,
        KeyState: { PRESSED: 1, RELEASED: 0 } };
    const StRun = { ClipboardType: { CLIPBOARD: 0 }, Clipboard: { get_default: () => ({ set_text() {} }) } };

    function makeRun() {
        const armed = [];      // callback dei sorgenti GLib vivi | callbacks of the live GLib sources
        const removed = [];    // id che il teardown ha chiesto a GLib | ids the teardown asked GLib to remove
        const toggles = [];    // toggle sparati DOPO il teardown | toggles fired AFTER the teardown
        const errors = [];     // logError emessi DOPO il teardown | logError calls emitted AFTER the teardown
        const notices = [];    // notifiche mostrate all'utente DOPO il teardown | notifications shown to the user AFTER the teardown
        const GLib = {
            PRIORITY_DEFAULT: 0, SOURCE_REMOVE: false,
            timeout_add: (_p, _ms, cb) => { armed.push(cb); return armed.length; },
            source_remove: id => removed.push(id),
            file_get_contents: () => { throw new Error('stato non leggibile'); },
        };
        const ind = {
            _streamQueue: [], _streamWorkerActive: false, _streamPasteBlocked: false,
            _streamSegments: null, _streamSessionId: 's1',
            _streamEndRequested: null, _streamEndTimerId: null,
            _streamPasteTimerId: null, _streamTypeTimerId: null,
            _streamDebounceId: null, _timeoutCheckId: null,
            _blinkTimeoutId: null, _refreshDebounceId: null,
            _monitor: null, _monitorId: 0, _streamMonitor: null, _streamMonitorId: 0,
            // Gio.Cancellable vero: cancel() è irreversibile e is_cancelled()
            // resta vero. È questa la condizione che le guardie leggono, quindi
            // se lo stub mentisse il teardown misurerebbe il nulla.
            // Real Gio.Cancellable: cancel() is irreversible and is_cancelled() stays
            // true. It is this condition that the guards read, so if the stub lied the
            // teardown would measure nothing.
            _cancellable: { _c: false, cancel() { this._c = true; }, is_cancelled() { return this._c; } },
            _virtualDevice: { run_dispose() {} },
            _pasteShortcut: 'ctrl+v', _pasteChannel: 'clipboard', _pasteDelayMs: 5,
            _streamPasteDeviceWarned: false,
            _keys: [], _committed: [],
            _pacingDelayMs() { return 5; },
            _sendKey(key, state) { this._keys.push([key, state]); return true; },
            _commitStreamItem(item) { this._streamQueue.shift(); this._committed.push(item.text); },
            _typeStreamItem() {}, _runStreamCommand() {},
        };
        // logError(error, tag): il tag e' il secondo argomento, quello che
        // dice COSA e' fallito. Raccolto con un resto di argomenti per non
        // dichiarare un primo parametro che qui non serve.
        // logError(error, tag): the tag is the second argument, the one that says
        // WHAT failed. Collected with a rest of arguments so as not to declare a
        // first parameter that is not needed here.
        const collectError = (...args) => errors.push(args[1]);
        // Il corpo dichiara gia' `const item` al suo interno: non si passa
        // l'item come parametro (omonimo), il metodo lo prende dalla coda.
        // I metodi prendono il loro NOME VERO: cosi' le chiamate fra metodi
        // (il timer di pacing che rilancia il worker) risolvono come nel
        // sorgente, e non come in una ricostruzione.
        // The body already declares `const item` inside it: the item is not passed
        // as a parameter (same name), the method takes it from the queue. The
        // methods take their REAL NAME: so the calls between methods (the pacing
        // timer that relaunches the worker) resolve as in the source, and not as in
        // a reconstruction.
        ind._startStreamPasteWorker = new Function('GLib', 'Main', 'St', 'Clutter', 'logError', '_', 'STREAM_SETTLE_MS',
            `${NOTIFY_PRELUDE}return function () {${workerBody}\n};`)(
            GLib, { notifyError: title => notices.push(title) }, StRun, ClutterRun,
            collectError, s => s, 30);
        ind._requestStreamEnd = new Function('GLib', 'spawnBackground', 'logError', 'TextDecoder', 'JSON',
            'STREAM_STATE_PATH', 'STREAM_END_TIMEOUT_MS', 'STREAM_END_POLL_MS',
            `${NOTIFY_PRELUDE}return function (sessionId) {${endBody}\n};`)(
            GLib, cmd => toggles.push(cmd), collectError,
            TextDecoder, JSON, '/tmp/stream_state.json', 5000, 100);
        return { ind, armed, removed, toggles, errors, notices, GLib, collectError };
    }

    // Il teardown REALE dello stesso blocco che gira in giro 4: nessuna copia.
    // The REAL teardown of the same block that runs in round 4: no copy.
    const teardown = (run) => makeDestroy(id => run.removed.push(id)).prototype.destroy.call(run.ind);

    // --- coda accodata al teardown -------------------------------------
    // --- queue queued at the teardown -------------------------------------
    const run = makeRun();
    run.ind._streamQueue.push({ action: 'paste', sessionId: 's1', index: 0, text: 'ciao' });
    run.ind._startStreamPasteWorker();
    const armedBefore = run.armed.length;
    check('S3B: il chunk accodato arma il timer di scrittura', armedBefore === 1
        && run.ind._streamPasteTimerId === 1);

    teardown(run);
    check('S3B: il teardown svuota la coda accodata', run.ind._streamQueue.length === 0);
    check('S3B: il teardown rimuove il timer di scrittura e rilascia la tastiera',
        run.removed.includes(1) && run.ind._streamPasteTimerId === null
        && run.ind._virtualDevice === null);

    // Il timer era gia' armato: se il teardown non lo ferma, scade comunque.
    // The timer was already armed: if the teardown does not stop it, it expires
    // anyway.
    const keysAtTeardown = run.ind._keys.length;
    for (const cb of run.armed) cb();
    check('S3B: il timer gia armato NON invia piu tasti dopo il teardown',
        run.ind._keys.length === keysAtTeardown);
    check('S3B: il timer gia armato NON ri-arma il worker di scrittura',
        run.armed.length === armedBefore);
    check('S3B: nessun toggle e nessun log dopo il teardown del paste',
        run.toggles.length === 0 && run.errors.length === 0);

    // Il caso che distingue la PRIMA guardia dalle altre: destroy() svuota la
    // coda, quindi senza un chunk in arrivo tardi la guardia del worker sembra
    // non servire (la coda vuota la fa comunque uscire). Qui il chunk arriva
    // DOPO il teardown: e' la situazione che il flag `_streamDestroyed` fermava
    // e che nessuna delle altre guardie copre. Il worker deve uscire alla
    // prima riga, senza toccare la tastiera e senza notificare all'utente un
    // problema che non puo' piu' risolvere.
    // The case that tells the FIRST guard from the others: destroy() empties the
    // queue, so without a late-arriving chunk the worker's guard seems not to be
    // needed (the empty queue makes it exit anyway). Here the chunk arrives AFTER
    // the teardown: it is the situation that the `_streamDestroyed` flag stopped
    // and that none of the other guards covers. The worker must exit at the first
    // line, without touching the keyboard and without notifying the user of a
    // problem it can no longer solve.
    run.ind._streamQueue.push({ action: 'paste', sessionId: 's1', index: 1, text: 'tardivo' });
    run.ind._startStreamPasteWorker();
    check('S3B: un chunk arrivato dopo il teardown non avvia lavoro',
        run.armed.length === armedBefore
        && run.ind._keys.length === keysAtTeardown
        && run.notices.length === 0
        && run.errors.length === 0);

    // --- il poll di fine sessione ---------------------------------------
    // --- the end-of-session poll ---------------------------------------
    const runEnd = makeRun();
    runEnd.ind._streamQueue.push({ action: 'paste', sessionId: 's1', index: 0, text: 'ciao' });
    runEnd.ind._streamWorkerActive = true;
    runEnd.ind._requestStreamEnd('s1');
    const endArmed = runEnd.armed.length;
    teardown(runEnd);
    runEnd.armed[endArmed - 1]();
    check('S3B: il poll di fine sessione non si ri-arma dopo il teardown',
        runEnd.armed.length === endArmed);
    check('S3B: il poll di fine sessione non spara il toggle dopo il teardown',
        runEnd.toggles.length === 0 && runEnd.errors.length === 0);

    // Canale 'type': stessa domanda, secondo canale. `_typeStreamItem` digita
    // carattere per carattere con un timer ricorsivo: senza la guardia in
    // `tick` il teardown lascerebbe il timer battere su un'istanza distrutta
    // (invio di tasti nel vuoto, o peggio, in un'altra finestra).
    // 'type' channel: same question, second channel. `_typeStreamItem` types
    // character by character with a recursive timer: without the guard in `tick`
    // the teardown would let the timer beat on a destroyed instance (keys sent
    // into the void, or worse, into another window).
    const typeBody = methodBody(source, '\n    _typeStreamItem(item) {');
    const ClutterType = { ...ClutterRun, KEY_Return: 36, KEY_Tab: 48,
        unicode_to_keysym: code => code };
    const typeRun = makeRun();
    typeRun.ind._pasteChannel = 'type';
    typeRun.ind._streamQueue.push({ action: 'paste', sessionId: 's1', index: 0, text: 'ab' });
    typeRun.ind._typeStreamItem = new Function('GLib', 'Main', 'Clutter', 'logError', '_', 'TYPE_KEY_INTERVAL_MS',
        `${NOTIFY_PRELUDE}return function (item) {${typeBody}\n};`)(
        // Lo stesso GLib del teardown, non una copia: i timer armati qui
        // devono finire nello stesso registro che il teardown ripulisce.
        // The same GLib as the teardown, not a copy: the timers armed here must end
        // up in the same registry that the teardown cleans.
        typeRun.GLib, { notifyError: title => typeRun.notices.push(title) },
        ClutterType, typeRun.collectError, s => s, 10);
    typeRun.ind._typeStreamItem({ text: 'ab' });
    const typeArmed = typeRun.armed.length;
    check('S3B: la digitazione armata un timer di carattere', typeArmed === 1
        && typeRun.ind._streamTypeTimerId === 1);

    teardown(typeRun);
    const typeKeys = typeRun.ind._keys.length;
    for (const cb of typeRun.armed) cb();
    check('S3B: il timer di digitazione NON invia piu caratteri dopo il teardown',
        typeRun.ind._keys.length === typeKeys
        && typeRun.armed.length === typeArmed
        && typeRun.notices.length === 0
        && typeRun.errors.length === 0);

    // --- NON VACUITA': gli stessi effetti senza il teardown devono fallire.
    // Se questi controlli passassero anche a teardown spento, misurerebbero
    // una proprieta' che il codice non ha: copertura falsa.
    // Due chunk e non uno: il primo viene consegnato dal timer, il secondo
    // resta in coda, e resta li' a dimostrare che il lavoro continua.
    // --- NON-VACUITY: the same effects without the teardown must fail. If these
    // checks passed even with the teardown off, they would measure a property
    // the code does not have: false coverage. Two chunks and not one: the first
    // is delivered by the timer, the second stays in the queue, and stays there
    // to show that the work continues.
    const broken = makeRun();
    for (const text of ['uno', 'due'])
        broken.ind._streamQueue.push({ action: 'paste', sessionId: 's1', index: 0, text });
    broken.ind._requestStreamEnd('s1');
    broken.ind._startStreamPasteWorker();
    const brokenArmed = broken.armed.length;
    for (const cb of broken.armed) cb();
    check('S3B: CONTRO — senza teardown il lavoro CONTINUA (i controlli sopra mordono)',
        broken.ind._keys.length > 0
        && broken.armed.length > brokenArmed
        && broken.ind._committed.length === 2);

    // CONTRO gemello: il buco che G1 vieta e' il teardown che azzera la
    // struttura guardata solo a meta'. Qui la coda viene svuotata ma la
    // tastiera virtuale resta viva: e' esattamente il caso in cui la guardia
    // deve dire "non lavoro piu'". Se i controlli di sopra fossero verdi
    // anche in questo caso, misurerebbero una proprieta' che il codice non ha.
    // Twin CONTRA: the hole that G1 forbids is the teardown that resets the
    // guarded structure only halfway. Here the queue is emptied but the virtual
    // keyboard stays alive: it is exactly the case in which the guard must say
    // "I no longer work". If the checks above were green in this case too, they
    // would measure a property the code does not have.
    const hole = makeRun();
    hole.ind._streamQueue.push({ action: 'paste', sessionId: 's1', index: 0, text: 'ciao' });
    hole.ind._startStreamPasteWorker();
    const holeArmed = hole.armed.length;
    hole.ind._streamQueue = [];
    const holeKeys = hole.ind._keys.length;
    for (const cb of hole.armed) cb();
    check('S3B: CONTRO — con la tastiera ancora viva il timer invia e ri-arma',
        hole.ind._keys.length > holeKeys && hole.armed.length > holeArmed);



    // ====================================================================
    // S4 (G9): watch-cache.mjs ESEGUITO davvero.
    //
    // Prima di questa estrazione il monitor di directory e i due debounce
    // non avevano NESSUN test: giro 4 li evita proprio, sostituendo
    // _watchStatusFile/_watchStreamStateFile con stub finti, quindi il teardown
    // che li consuma era misurato solo su campi scritti a mano. Qui il modulo
    // puro viene importato ed eseguito, con stub di Gio/GLib che NOTANO cio'
    // che fanno: campo del monitor, collegamento dell'handler, periodo del
    // timer e reset del riferimento si vedono tutti.
    // ====================================================================
    // ====================================================================
    // S4 (G9): watch-cache.mjs REALLY RUN.
    //
    // Before this extraction the directory monitor and the two debounces had NO
    // test at all: round 4 avoids them, replacing
    // _watchStatusFile/_watchStreamStateFile with fake stubs, so the teardown
    // that consumes them was measured only on hand-written fields. Here the pure
    // module is imported and run, with Gio/GLib stubs that NOTE what they do:
    // monitor field, handler connection, timer period and reference reset are
    // all visible.
    // ====================================================================
    console.log('== S4: watch-cache.mjs reale (monitor di cache e debounce) ==');

    const watchSrc = fs.readFileSync(watchPath, 'utf8');
    // I commenti NON contano: il testo 'gi://' citato nella intestazione del
    // modulo renderebbe il controllo verde per Caso. Si misura il codice.
    // Comments do NOT count: the text 'gi://' cited in the module's header would
    // make the check green by Chance. The code is measured.
    const watchCode = watchSrc
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .split('\n').filter(line => !line.trim().startsWith('//')).join('\n');
    // Purezza per costruzione, non per promessa: il modulo non ha una riga di
    // import, quindi non puo' aver importato St/Gtk/Gio (G7/G18).
    // Purity by construction, not by promise: the module has no import line, so
    // it cannot have imported St/Gtk/Gio (G7/G18).
    check('S4: watch-cache.mjs non importa nulla (nessun gi://): la purezza e\' per costruzione',
        !/^\s*import\s/m.test(watchCode) && !watchCode.includes('gi://'));
    // Se il modulo esistesse ma restasse dormiente, i verdi sotto passerebbero
    // comunque: questo controllo lega il modulo a extension.js.
    // I DUE watcher che _init avvia davvero sono i punti di aggancio
    // misurabili qui dentro extension.js. Il terzo NON lo e': il
    // scheduleRefresh non ha una riga in questo file, e' watch-cache.mjs:67 a
    // chiamarlo dentro watchStatusFile. Un letterale `scheduleRefresh(this,
    // ioDeps, ...)` qui misurerebbe un metodo che nessuno invoca, e il gate
    // punirebbe la sua rimozione invece di premiare il percorso vero: la
    // prova del debounce sta piu' avanti, sul modulo ESEGUITO.
    // If the module existed but stayed dormant, the greens below would pass
    // anyway: this check ties the module to extension.js. The TWO watchers that
    // _init really starts are the measurable hook points inside extension.js.
    // The third is NOT: scheduleRefresh has no line in this file, it is
    // watch-cache.mjs:67 that calls it inside watchStatusFile. A literal
    // `scheduleRefresh(this, ioDeps, ...)` here would measure a method that
    // nobody invokes, and the gate would punish its removal instead of rewarding
    // the real path: the proof of the debounce comes later, on the module that
    // is RUN.
    check('S4: extension.js importa watch-cache.mjs e gli delega i due watcher che _init avvia',
        source.includes("from './watch-cache.mjs'")
        && source.includes('watchStatusFile(this, ioDeps, STATUS_PATH, REFRESH_DEBOUNCE_MS)')
        && source.includes('watchStreamStateFile(this, ioDeps, STREAM_STATE_PATH, STREAM_DEBOUNCE_MS)'));

    // Stub di Gio/GLib che REGISTRANO le operazioni. Ogni id di timer e' un
    // numero progressivo, come fa GLib: cosi' "stesso id" e' un fatto e non
    // un confronto di stringhe.
    // Gio/GLib stubs that RECORD the operations. Every timer id is a progressive
    // number, as GLib does: so "same id" is a fact and not a string comparison.
    function wcMakeIo() {
        const calls = [];
        const io = { calls, fired: [] };
        io.Gio = {
            FileMonitorFlags: { NONE: 0 },
            IOErrorEnum: { EXISTS: 17 },
            File: { new_for_path: path => wcMakeDir(io, path) },
        };
        io.GLib = {
            PRIORITY_DEFAULT: 0,
            SOURCE_REMOVE: false,
            path_get_dirname: p => p.split('/').slice(0, -1).join('/'),
            timeout_add: (_prio, ms, cb) => {
                calls.push(['timeout_add', ms]);
                io.fired.push(cb);
                return io.fired.length;
            },
            source_remove: id => calls.push(['source_remove', id]),
        };
        io.logError = (_e, tag) => calls.push(['logError', tag]);
        return io;
    }

    function wcMakeDir(io, path, { monitorThrows = false, mkdirThrows = null } = {}) {
        const dir = {
            path,
            make_directory_with_parents: () => {
                io.calls.push(['make_directory_with_parents', path]);
                if (mkdirThrows) throw mkdirThrows;
            },
            monitor_directory: () => {
                io.calls.push(['monitor_directory', path]);
                if (monitorThrows) throw new Error('monitor fallito');
                return {
                    connected: [],
                    cancelled: 0,
                    disconnected: [],
                    connect(sig, cb) { this.connected.push([sig, cb]); return 7; },
                    disconnect(id) { this.disconnected.push(id); },
                    cancel() { this.cancelled++; },
                };
            },
        };
        return dir;
    }

    const wcGioError = (isExists) => {
        const e = new Error('errore gio');
        e.matches = (_klass, code) => isExists && code === 17;
        return e;
    };

    // --- il percorso vero: creare la directory, poi osservarla ----------
    // --- the real path: create the directory, then watch it ----------
    const wcIoA = wcMakeIo();
    const wcIndA = { _monitor: null, _monitorId: 0 };
    watchCacheFile(wcIndA, wcIoA, '/tmp/cache/status.json', () => {},
        { monitorField: '_monitor', monitorIdField: '_monitorId', createLabel: 'status', watchLabel: 'status' });
    check('S4: la directory viene creata PRIMA di essere osservata',
        wcIoA.calls[0][0] === 'make_directory_with_parents' && wcIoA.calls[1][0] === 'monitor_directory');
    check('S4: si osserva la DIRECTORY, non il file (il backend scrive tmp+rename)',
        wcIndA._monitor.connected.length === 1 && wcIndA._monitor.connected[0][0] === 'changed');
    check('S4: monitor e id finiscono nei campi che destroy() legge per nome',
        wcIndA._monitor !== null && wcIndA._monitorId === 7);
    // L'handler collegato e' quello passato dal chiamante: si preme davvero.
    // The connected handler is the one passed by the caller: it is really
    // pressed.
    let wcFiredA = 0;
    const wcIoA2 = wcMakeIo();
    const wcIndA2 = { _monitor: null, _monitorId: 0 };
    watchCacheFile(wcIndA2, wcIoA2, '/tmp/cache/status.json', () => { wcFiredA++; },
        { monitorField: '_monitor', monitorIdField: '_monitorId', createLabel: 'status', watchLabel: 'status' });
    wcIndA2._monitor.connected[0][1]();
    check('S4: l\'evento del monitor esegue l\'handler del chiamante', wcFiredA === 1);

    // I campi passati per NOME: con un nome fisso uno dei due teardown
    // (quello del monitor stream) smetterebbe di disconnettere.
    // The fields passed by NAME: with a fixed name one of the two teardowns (the
    // stream monitor's) would stop disconnecting.
    const wcIoB = wcMakeIo();
    const wcIndB = { _streamMonitor: null, _streamMonitorId: 0 };
    watchCacheFile(wcIndB, wcIoB, '/tmp/cache/stream_state.json', () => {},
        { monitorField: '_streamMonitor', monitorIdField: '_streamMonitorId', createLabel: 'stream', watchLabel: 'stream_state' });
    check('S4: i campi del monitor stream restano separati da quelli dello stato',
        wcIndB._streamMonitorId === 7 && wcIndB._monitor === undefined && wcIndB._monitorId === undefined);

    // --- EXISTS sul mkdir non e\' un errore da segnalare ----------------
    // --- EXISTS on mkdir is not an error to report ----------------
    const wcIoC = wcMakeIo();
    const wcIndC = { _monitor: null, _monitorId: 0 };
    wcIoC.Gio.File.new_for_path = path =>
        wcMakeDir(wcIoC, path, { mkdirThrows: wcGioError(true) });
    watchCacheFile(wcIndC, wcIoC, '/tmp/cache/status.json', () => {},
        { monitorField: '_monitor', monitorIdField: '_monitorId', createLabel: 'status', watchLabel: 'status' });
    check('S4: EXISTS sul mkdir non viene loggato e il monitor viene creato',
        !wcIoC.calls.some(c => c[0] === 'logError') && wcIndC._monitorId === 7);

    // --- mkdir fallito davvero: si avvisa, ma si monitora lo stesso -----
    // --- mkdir really failed: a warning is given, but we monitor anyway -----
    const wcIoD = wcMakeIo();
    const wcIndD = { _monitor: null, _monitorId: 0 };
    wcIoD.Gio.File.new_for_path = path =>
        wcMakeDir(wcIoD, path, { mkdirThrows: wcGioError(false) });
    watchCacheFile(wcIndD, wcIoD, '/tmp/cache/status.json', () => {},
        { monitorField: '_monitor', monitorIdField: '_monitorId', createLabel: 'status', watchLabel: 'status' });
    check('S4: mkdir fallito: avviso con l\'etichetta di CREAZIONE e monitor lo stesso',
        wcIoD.calls.some(c => c[0] === 'logError' && c[1].includes('creare status dir'))
        && !wcIoD.calls.some(c => c[0] === 'logError' && c[1].includes('monitorare'))
        && wcIndD._monitorId === 7);

    // --- monitor non creato: il campo NON resta sporco -----------------
    // createLabel e watchLabel sono VOLUTAMENTE diversi: i due messaggi non
    // possono confondersi, e un monitor fallito non deve dirsi "creazione".
    // --- monitor not created: the field does NOT stay dirty -----------------
    // createLabel and watchLabel are DELIBERATELY different: the two messages
    // cannot be confused, and a failed monitor must not call itself "creation".
    const wcIoE = wcMakeIo();
    const wcStale = { cancelled: 0, cancel() { this.cancelled++; } };
    const wcIndE = { _monitor: wcStale, _monitorId: 9 };
    wcIoE.Gio.File.new_for_path = path => wcMakeDir(wcIoE, path, { monitorThrows: true });
    watchCacheFile(wcIndE, wcIoE, '/tmp/cache/status.json', () => {},
        { monitorField: '_monitor', monitorIdField: '_monitorId', createLabel: 'status', watchLabel: 'stream_state' });
    check('S4: monitor non creato: il campo viene svuotato e il monitor precedente cancellato',
        wcIndE._monitor === null && wcStale.cancelled === 1);
    check('S4: monitor non creato: l\'avviso porta l\'etichetta di MONITORAGGIO, distinta dalla creazione',
        wcIoE.calls.filter(c => c[0] === 'logError').length === 1
        && wcIoE.calls.some(c => c[1] === 'bravoric-indicator: impossibile monitorare stream_state dir'));

    // --- debounce del refresh ------------------------------------------
    const wcIoF = wcMakeIo();
    const wcIndF = { _monitor: null, _monitorId: 0, _refreshDebounceId: null, refreshed: 0 };
    wcIndF._refreshStatus = () => { wcIndF.refreshed++; };
    wcIndF._refreshHistory = () => { wcIndF.refreshed++; };
    watchStatusFile(wcIndF, wcIoF, '/tmp/cache/status.json', 250);
    check('S4: il monitor dello stato rimanda la lettura: nessuna lettura al collegamento',
        wcIndF._monitor !== null && wcIndF._refreshDebounceId === null && wcIndF.refreshed === 0);
    wcIndF._monitor.connected[0][1]();
    const wcFirstId = wcIndF._refreshDebounceId;
    check('S4: la scrittura arma il debounce con il periodo dichiarato',
        wcFirstId === 1 && wcIoF.calls.some(c => c[0] === 'timeout_add' && c[1] === 250));
    // Scrittura atomica = eventi ravvicinati: senza reset, due timer leggerebbero
    // lo stesso file e il lavoro raddopperebbe (G6: rimuovere PRIMA di ri-armare).
    // Atomic write = close events: without a reset, two timers would read the
    // same file and the work would double (G6: remove BEFORE re-arming).
    wcIndF._monitor.connected[0][1]();
    check('S4: un secondo evento rimuove la sorgente gia\' armata prima di ri-armare',
        wcIoF.calls.filter(c => c[0] === 'source_remove' && c[1] === wcFirstId).length === 1
        && wcIndF._refreshDebounceId === 2);
    // Il timer che scade fa il lavoro una volta sola e si rimuove dal campo.
    // The timer that expires does the work once and removes itself from the
    // field.
    wcIoF.fired[1]();
    check('S4: il debounce scaduto legge stato E cronologia, poi azzera il campo',
        wcIndF.refreshed === 2 && wcIndF._refreshDebounceId === null);

    // --- la delega di extension.js ESEGUITA, non il suo testo -----------
    // Il controllo sopra sul sorgente dice CHE extension.js nomina i due
    // watcher; questo dice che la delega FUNZIONA. I due corpi sono presi
    // dalla DEFINIZIONE con lo stesso brace-matching del destroy() piu' in
    // giù ed eseguiti contro il modulo vero, con gli stub di Gio/GLib: cio'
    // che si misura e' il periodo che ARRIVA al debounce, non una lettera.
    // Perche' la misura resti quella dichiarata dal file, i due periodi si
    // leggono dalla fonte e non si riscrivono qui.
    // --- the delegation of extension.js RUN, not its text -----------
    // The check above on the source says THAT extension.js names the two
    // watchers; this one says that the delegation WORKS. The two bodies are taken
    // from the DEFINITION with the same brace-matching as the destroy() further
    // down and run against the real module, with the Gio/GLib stubs: what is
    // measured is the period that ARRIVES at the debounce, not a letter. So that
    // the measure stays the one declared by the file, the two periods are read
    // from the source and not rewritten here.
    const wcConstMs = (name) => {
        const m = source.match(new RegExp('^const ' + name + ' = (\\d+);$', 'm'));
        assert.ok(m, `${name} non trovato in extension.js`);
        return Number(m[1]);
    };
    const wcRefreshMs = wcConstMs('REFRESH_DEBOUNCE_MS');
    const wcStreamMs = wcConstMs('STREAM_DEBOUNCE_MS');
    // makeWatcher monta il corpo vero su un `ioDeps` finto: lo stesso oggetto
    // che extension.js:121 costruisce, con dentro gli stub che annotano.
    // makeWatcher mounts the real body on a fake `ioDeps`: the same object that
    // extension.js:121 builds, with the noting stubs inside.
    const makeWatcher = (body, deps, refreshMs, streamMs) => new Function(
        'watchStatusFile', 'watchStreamStateFile', 'ioDeps',
        'STATUS_PATH', 'STREAM_STATE_PATH', 'REFRESH_DEBOUNCE_MS', 'STREAM_DEBOUNCE_MS',
        `return function () {${body}\n};`)(
        watchStatusFile, watchStreamStateFile, deps,
        '/tmp/cache/status.json', '/tmp/cache/stream_state.json', refreshMs, streamMs);
    const wcRunWatchers = (deps, refreshMs, streamMs) => {
        const ind = { _monitor: null, _monitorId: 0, _streamMonitor: null, _streamMonitorId: 0,
            _refreshDebounceId: null, _streamDebounceId: null,
            refreshed: 0, streamed: 0,
            _refreshStatus() { this.refreshed++; },
            _refreshHistory() { this.refreshed++; },
            _onStreamStateChanged() { this.streamed++; } };
        makeWatcher(methodBody(source, '\n    _watchStatusFile() {'), deps, refreshMs, streamMs).call(ind);
        makeWatcher(methodBody(source, '\n    _watchStreamStateFile() {'), deps, refreshMs, streamMs).call(ind);
        return ind;
    };
    const wcIoP = wcMakeIo();
    const wcIndP = wcRunWatchers(
        { Gio: wcIoP.Gio, GLib: wcIoP.GLib, logError: wcIoP.logError }, wcRefreshMs, wcStreamMs);
    check('S4: i due watcher di extension.js eseguiti creano i monitor che destroy() legge',
        wcIndP._monitor !== null && wcIndP._monitorId === 7
        && wcIndP._streamMonitor !== null && wcIndP._streamMonitorId === 7);
    // Il periodo che si misura e' quello ARRIVATO al GLib finto, non quello
    // scritto qui: se extension.js passasse il periodo sbagliato il verde
    // sotto andrebbe rosso anche se il sorgente restasse identico.
    // The period being measured is the one that ARRIVED at the fake GLib, not
    // the one written here: if extension.js passed the wrong period the green
    // below would go red even if the source stayed identical.
    wcIndP._monitor.connected[0][1]();
    wcIndP._streamMonitor.connected[0][1]();
    check('S4: ...e armano i due debounce con il periodo che extension.js dichiara',
        wcIndP._refreshDebounceId === 1 && wcIndP._streamDebounceId === 2
        && JSON.stringify(wcIoP.calls.filter(c => c[0] === 'timeout_add')
            .map(c => c[1])) === JSON.stringify([wcRefreshMs, wcStreamMs]));
    // CONTRO GEMELLO: stessi corpi veri, periodi DIVERSI. Se il controllo
    // sopra fosse vacuo (periodo letto dal test e non dal codice eseguito),
    // qui i valori registrati resterebbero quelli dichiarati e il verde
    // seguente non potrebbe esistere. Il percorso e' identico: cambia solo
    // la costante passata, quindi e' la prova che la misura legge il
    // codice e non la propria aspettativa.
    // TWIN CONTRA: same real bodies, DIFFERENT periods. If the check above were
    // vacuous (period read by the test and not by the executed code), here the
    // recorded values would stay the declared ones and the following green could
    // not exist. The path is identical: only the passed constant changes, so it
    // is the proof that the measure reads the code and not its own expectation.
    const wcIoQ = wcMakeIo();
    const wcIndQ = wcRunWatchers(
        { Gio: wcIoQ.Gio, GLib: wcIoQ.GLib, logError: wcIoQ.logError }, 120, 340);
    wcIndQ._monitor.connected[0][1]();
    wcIndQ._streamMonitor.connected[0][1]();
    check('S4: CONTRO — con altri periodi il codice eseguito registra QUELLI (il verde sopra non e\' vacuo)',
        JSON.stringify(wcIoQ.calls.filter(c => c[0] === 'timeout_add')
            .map(c => c[1])) === JSON.stringify([120, 340]));

    // --- debounce dello stream: campo e periodo suoi -------------------
    // --- stream debounce: its own field and period -------------------
    const wcIoG = wcMakeIo();
    const wcIndG = { _streamMonitor: null, _streamMonitorId: 0, _streamDebounceId: null,
        _refreshDebounceId: null, streamReads: 0 };
    wcIndG._onStreamStateChanged = () => { wcIndG.streamReads++; };
    watchStreamStateFile(wcIndG, wcIoG, '/tmp/cache/stream_state.json', 300);
    wcIndG._streamMonitor.connected[0][1]();
    check('S4: il monitor stream arma il PROPRIO debounce, non quello del refresh',
        wcIndG._streamDebounceId === 1 && wcIndG._refreshDebounceId === null
        && wcIoG.calls.some(c => c[0] === 'timeout_add' && c[1] === 300));
    wcIoG.fired[0]();
    check('S4: il debounce stream chiama la lettura dello stato e si azzera',
        wcIndG.streamReads === 1 && wcIndG._streamDebounceId === null);

    // --- PROVA DI NON VACUITA': i campi scritti dal modulo sono gli stessi
    // che il teardown REALE di destroy() legge. Gli id sono letti PRIMA del
    // teardown: leggerli dopo darebbe null e il controllo passerebbe sempre.
    // --- NON-VACUITY PROOF: the fields written by the module are the same ones
    // that the REAL teardown of destroy() reads. The ids are read BEFORE the
    // teardown: reading them after would give null and the check would always
    // pass.
    const wcIoH = wcMakeIo();
    const wcIndH = { _cancellable: { _c: false, cancel() { this._c = true; }, is_cancelled() { return this._c; } },
        _monitor: null, _monitorId: 0, _streamMonitor: null, _streamMonitorId: 0,
        _refreshDebounceId: null, _streamDebounceId: null };
    watchStatusFile(wcIndH, wcIoH, '/tmp/cache/status.json', 250);
    watchStreamStateFile(wcIndH, wcIoH, '/tmp/cache/stream_state.json', 300);
    wcIndH._monitor.connected[0][1]();
    wcIndH._streamMonitor.connected[0][1]();
    const wcArmedIds = [wcIndH._refreshDebounceId, wcIndH._streamDebounceId];
    const removed = [];
    let wcDestroyError = null;
    try {
        makeDestroy(id => removed.push(id)).prototype.destroy.call(wcIndH);
    } catch (e) {
        wcDestroyError = e;
    }
    check('S4: destroy() (metodo reale) rimuove i due debounce armati dal modulo',
        wcDestroyError === null
        && wcArmedIds[0] === 1 && wcArmedIds[1] === 2
        && removed.includes(1) && removed.includes(2));
    check('S4: destroy() azzera i due riferimenti ai debounce dopo averli usati',
        wcIndH._refreshDebounceId === null && wcIndH._streamDebounceId === null);

    // --- CONTRO GEMELLO: la stessa domanda dal lato opposto. Se il modulo
    // smettesse di scrivere i campi, il teardown non avrebbe nulla da
    // rimuovere: questo controllo se ne accorgerebbe mentre il verde sopra
    // resterebbe vero (copertura falsa). Nessun modulo e' chiamato: il campo
    // nasce gia' null, come su un indicatore appena inizializzato.
    // Il cancellable e' quello vero (cancel irreversibile): e' la prima
    // istruzione di destroy(), quindi senza questo oggetto il teardown nemmeno
    // arriva ai campi che il modulo scrive.
    // --- TWIN CONTRA: the same question from the opposite side. If the module
    // stopped writing the fields, the teardown would have nothing to remove:
    // this check would notice while the green above would stay true (false
    // coverage). No module is called: the field is born null already, like on a
    // freshly initialized indicator. The cancellable is the real one (cancel is
    // irreversible): it is the first instruction of destroy(), so without this
    // object the teardown does not even reach the fields the module writes.
    const wcCancellable = () => ({
        _c: false,
        cancel() { this._c = true; },
        is_cancelled() { return this._c; },
    });
    const wcIndU = { _cancellable: wcCancellable(), _refreshDebounceId: null };
    const removedU = [];
    makeDestroy(id => removedU.push(id)).prototype.destroy.call(wcIndU);
    check('S4: CONTRO — un campo mai armato non produce rimozioni (il verde sopra non e\' vacuo)',
        removedU.length === 0);
    // E il contrario: se il modulo lasciasse il campo ARMATO dopo la
    // scadenza, il teardown successivo rimuoverebbe un id morto. GLib
    // avvisa su source_remove di un id inesistente: qui si vede che il
    // reset e'cio' che lo impedisce, misurato sul campo vero.
    // And the opposite: if the module left the field ARMED after the expiry, the
    // following teardown would remove a dead id. GLib warns on source_remove of
    // a non-existent id: here it is seen that the reset is what prevents it,
    // measured on the real field.
    const wcIoV = wcMakeIo();
    const wcIndV = { _cancellable: wcCancellable(), _monitor: null, _monitorId: 0,
        _refreshDebounceId: null, refreshed: 0 };
    wcIndV._refreshStatus = () => { wcIndV.refreshed++; };
    wcIndV._refreshHistory = () => {};
    watchStatusFile(wcIndV, wcIoV, '/tmp/cache/status.json', 250);
    wcIndV._monitor.connected[0][1]();
    wcIoV.fired[0]();
    const removedV = [];
    makeDestroy(id => removedV.push(id)).prototype.destroy.call(wcIndV);
    check('S4: CONTRO — il campo azzerato alla scadenza evita di rimuovere un id morto',
        removedV.length === 0 && wcIndV._refreshDebounceId === null);


    // ====================================================================
    // S4 (G9): recording-blink.mjs ESEGUITO davvero.
    //
    // _setRecordingBlink era l'unico metodo di extension.js che nessun test
    // nominava: giro 4 lo esercita solo per EFFETTO (se lo accende, l'id
    // finisce nel teardown), nessuna asserzione guardava cosa fa. Qui il
    // modulo puro gira con uno St.Icon finto che registra opacity e classi.
    // ====================================================================
    // ====================================================================
    // S4 (G9): recording-blink.mjs REALLY RUN.
    //
    // _setRecordingBlink was the only method of extension.js that no test named:
    // round 4 exercises it only by EFFECT (if it switches it on, the id ends up
    // in the teardown), no assertion looked at what it does. Here the pure module
    // runs with a fake St.Icon that records opacity and classes.
    // ====================================================================
    console.log('== S4: recording-blink.mjs reale (lampeggio) ==');

    const blinkSrc = fs.readFileSync(blinkPath, 'utf8');
    const blinkCode = blinkSrc
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .split('\n').filter(line => !line.trim().startsWith('//')).join('\n');
    check('S4: recording-blink.mjs non importa nulla: la purezza e\' per costruzione',
        !/^\s*import\s/m.test(blinkCode) && !blinkCode.includes('gi://'));
    check('S4: extension.js importa recording-blink.mjs e gli delega _setRecordingBlink',
        source.includes("from './recording-blink.mjs'")
        && source.includes("setRecordingBlink(this, ioDeps, active, BLINK_CLASS, settingInt('blink-interval-ms', BLINK_INTERVAL_MS))"));

    // Stub fedele a GLib su un punto che conta: `source_remove` REVOCA la
    // sorgente, quindi `fire(id)` dopo la rimozione non deve eseguire nulla.
    // Con uno stub che conserva i callback, l'asserzione "dopo lo spegnimento
    // l'icona non si tocca" misurerebbe lo stub e non il codice.
    // Stub faithful to GLib on a point that matters: `source_remove` REVOKES the
    // source, so `fire(id)` after the removal must not execute anything. With a
    // stub that keeps the callbacks, the assertion "after switching off the icon
    // is not touched" would measure the stub and not the code.
    function makeBlinkIo() {
        const calls = [];
        const sources = new Map();
        const io = { calls, sources, live: () => sources.size };
        io.GLib = {
            PRIORITY_DEFAULT: 0,
            SOURCE_CONTINUE: true,
            timeout_add: (prio, ms, cb) => {
                calls.push(['timeout_add', ms]);
                const id = sources.size + 1;
                sources.set(id, cb);
                return id;
            },
            source_remove: id => {
                calls.push(['source_remove', id]);
                sources.delete(id);
            },
        };
        // Fa scadere la sorgente se e' ancora viva; dice se e' successo.
        // Makes the source expire if it is still alive; says whether it happened.
        io.fire = id => {
            const cb = sources.get(id);
            if (!cb) return false;
            cb();
            return true;
        };
        return io;
    }

    const makeBlinkIndicator = () => ({
        _blinkTimeoutId: null,
        opacity: 255,
        classes: [],
        _icon: {
            opacity: 255,
            add_style_class_name(c) { this.owner.classes.push(['add', c]); },
            remove_style_class_name(c) { this.owner.classes.push(['remove', c]); },
        },
    });
    // Il fake _icon deve sapere chi e' il suo proprietario per annotare le classi.
    // The fake _icon must know who its owner is in order to note the classes.
    const newBlink = () => {
        const ind = makeBlinkIndicator();
        ind._icon.owner = ind;
        return ind;
    };

    // --- accendere -----------------------------------------------------
    const bIo = makeBlinkIo();
    const bInd = newBlink();
    setRecordingBlink(bInd, bIo, true, 'bravoric-recording-icon', 500);
    check('S4: accendere aggiunge la classe e arma il timer con il periodo dichiarato',
        bInd._blinkTimeoutId === 1
        && JSON.stringify(bInd.classes) === JSON.stringify([['add', 'bravoric-recording-icon']])
        && bIo.calls.some(c => c[0] === 'timeout_add' && c[1] === 500));
    // Il callback continua a girare: e' un lampeggio, non un one-shot.
    // The callback keeps running: it is a blink, not a one-shot.
    bIo.fire(1);
    check('S4: il primo giro porta l\'opacita\' a 80 (il toggle parte da 255)',
        bInd._icon.opacity === 80 && bIo.calls.length === 1);
    bIo.fire(1);
    check('S4: il giro successivo la riporta a 255, senza armare altri timer',
        bInd._icon.opacity === 255 && bIo.calls.length === 1);

    // Riaccendere mentre lampeggia non deve creare un SECONDO timer: due
    // timer sullo stesso campo significa che lo spegnimento ne lascia uno vivo.
    // Switching on again while blinking must not create a SECOND timer: two
    // timers on the same field means the switch-off leaves one alive.
    setRecordingBlink(bInd, bIo, true, 'bravoric-recording-icon', 500);
    check('S4: riaccendere mentre lampeggia NON arma un secondo timer',
        bInd._blinkTimeoutId === 1 && bIo.calls.filter(c => c[0] === 'timeout_add').length === 1
        && bIo.live() === 1);

    // --- spegnere: il timer viene rimosso e il campo azzerato ---------
    // Prima di spegnere l'icona e' a 80 (un giro fatto di proposito): se il
    // ripristino dell'opacita' sparisse dal ramo di spegnimento, qui
    // l'asserzione diventerebbe ROSSA. Con l'opacita' gia' a 255 il controllo
    // passerebbe comunque e non misurerebbe niente.
    // --- switching off: the timer is removed and the field reset ---------
    // Before switching off the icon is at 80 (a round done on purpose): if the
    // opacity restore disappeared from the switch-off branch, here the assertion
    // would turn RED. With the opacity already at 255 the check would pass
    // anyway and measure nothing.
    bIo.fire(1);
    check('S4: prima dello spegnimento l\'opacita\' e\' davvero cambiata (80)',
        bInd._icon.opacity === 80);
    setRecordingBlink(bInd, bIo, false, 'bravoric-recording-icon', 500);
    check('S4: spegnere rimuove il timer e azzera il campo',
        bIo.calls.some(c => c[0] === 'source_remove' && c[1] === 1)
        && bInd._blinkTimeoutId === null);
    check('S4: spegnere toglie la classe e riporta l\'opacita\' a 255',
        JSON.stringify(bInd.classes[1]) === JSON.stringify(['remove', 'bravoric-recording-icon'])
        && bInd._icon.opacity === 255);
    // Dopo lo spegnimento la sorgente NON esiste piu': questo e' il fatto che
    // impedisce all'icona di continuare a lampeggiare da sola, e si misura
    // sul registro delle sorgenti vive, non chiamando il callback a mano.
    // After the switch-off the source NO LONGER exists: this is the fact that
    // prevents the icon from continuing to blink by itself, and it is measured
    // on the registry of live sources, not by calling the callback by hand.
    check('S4: dopo lo spegnimento non resta NESSUNA sorgente viva',
        bIo.live() === 0 && bIo.fire(1) === false);

    // CONTRO gemello: la stessa sorgente, PRIMA dello spegnimento, gira
    // davvero. Se il verde sopra fosse vacuo (stub che non esegue nulla),
    // questo diventa falso.
    // Twin CONTRA: the same source, BEFORE the switch-off, really runs. If the
    // green above were vacuous (a stub that executes nothing), this becomes
    // false.
    const cIo = makeBlinkIo();
    const cInd = newBlink();
    setRecordingBlink(cInd, cIo, true, 'bravoric-recording-icon', 500);
    cIo.fire(1);
    check('S4: CONTRO — la sorgente accesa gira e tocca l\'icona (il verde sopra non e\' vacuo)',
        cInd._icon.opacity === 80 && cIo.live() === 1);

    // --- spegnere da fermo non genera rimozioni fantasma --------------
    // source_remove su un id inesistente e' un CRITICAL di GLib: qui si vede
    // che la guardia e' il campo, non il ramo.
    // --- switching off from idle generates no phantom removals --------
    // source_remove on a non-existent id is a GLib CRITICAL: here it is seen
    // that the guard is the field, not the branch.
    const sIo = makeBlinkIo();
    const sInd = newBlink();
    setRecordingBlink(sInd, sIo, false, 'bravoric-recording-icon', 500);
    check('S4: spegnere un indicatore fermo non rimuove nessun id (niente CRITICAL GLib)',
        sIo.calls.length === 0 && sInd._blinkTimeoutId === null);

    // --- il teardown reale deve poter rimuovere il timer del lampeggio --
    // --- the real teardown must be able to remove the blink timer --
    const tIo = makeBlinkIo();
    const tInd = {
        _cancellable: wcCancellable(),
        _icon: { opacity: 255, add_style_class_name() {}, remove_style_class_name() {} },
        _blinkTimeoutId: null, _monitor: null, _monitorId: 0,
        _streamMonitor: null, _streamMonitorId: 0,
    };
    setRecordingBlink(tInd, tIo, true, 'bravoric-recording-icon', 500);
    const blinkId = tInd._blinkTimeoutId;
    const removedBlink = [];
    makeDestroy(id => removedBlink.push(id)).prototype.destroy.call(tInd);
    check('S4: destroy() (metodo reale) rimuove il timer del lampeggio armato dal modulo',
        blinkId === 1 && removedBlink.includes(1) && tInd._blinkTimeoutId === null);

    // ====================================================================
    // Notifiche dell'estensione: ogni notifica ha il suo interruttore
    // GSettings (notify-errors / notify-status). Gli helper VERI sono estratti
    // dal sorgente ed eseguiti con un Main e un GSettings finti.
    // ====================================================================
    // ====================================================================
    // Extension notifications: each notification has its own GSettings switch
    // (notify-errors / notify-status). The REAL helpers are extracted from the
    // source and run with a fake Main and a fake GSettings.
    // ====================================================================
    console.log('== notifiche estensione: interruttori GSettings (helper reali) ==');
    const helpersStart = source.indexOf('let notificationSettings = null;');
    const helpersEndMarker = 'function notifyStatusIfEnabled(title, body) {';
    const helpersEndAt = source.indexOf(helpersEndMarker);
    check('helper notifiche presenti nel sorgente', helpersStart !== -1 && helpersEndAt > helpersStart);
    const helpersEnd = matchBrace(source, source.indexOf('{', helpersEndAt)) + 1;
    const helpersSrc = source.slice(helpersStart, helpersEnd);
    const makeHelpers = (Main, logged) => new Function('Main', 'logError',
        `${helpersSrc}\nreturn { setSettings: v => { notificationSettings = v; }, notifyErrorIfEnabled, notifyStatusIfEnabled, settingInt };`)(
        Main, (e, m) => logged.push(m));
    const mkSettings = (values, keys = Object.keys(values)) => ({
        settings_schema: { has_key: k => keys.includes(k) },
        get_boolean: k => values[k],
    });
    const sent = [];
    const Main = { notifyError: (t, b) => sent.push(['err', t, b]), notify: (t, b) => sent.push(['st', t, b]) };
    const logged = [];
    const H = makeHelpers(Main, logged);

    H.setSettings(mkSettings({ 'notify-errors': true, 'notify-status': true }));
    H.notifyErrorIfEnabled('E', 'b'); H.notifyStatusIfEnabled('S', 'b');
    check('notifiche: con entrambi gli interruttori accesi arrivano errore e stato',
        sent.length === 2 && sent[0][0] === 'err' && sent[1][0] === 'st');

    sent.length = 0;
    H.setSettings(mkSettings({ 'notify-errors': false, 'notify-status': true }));
    H.notifyErrorIfEnabled('E', 'b'); H.notifyStatusIfEnabled('S', 'b');
    check('notifiche: notify-errors spento -> l\'errore NON arriva, lo stato si',
        sent.length === 1 && sent[0][0] === 'st');

    sent.length = 0;
    H.setSettings(mkSettings({ 'notify-errors': true, 'notify-status': false }));
    H.notifyErrorIfEnabled('E', 'b'); H.notifyStatusIfEnabled('S', 'b');
    check('notifiche: notify-status spento -> lo stato NON arriva, l\'errore si',
        sent.length === 1 && sent[0][0] === 'err');

    sent.length = 0;
    H.setSettings(mkSettings({}, []));
    H.notifyErrorIfEnabled('E', 'b'); H.notifyStatusIfEnabled('S', 'b');
    check('notifiche: schema STANTIO (chiave assente) non spegne nulla e non solleva',
        sent.length === 2);

    // settingInt: valori numerici regolabili da GUI (helper REALE).
    // settingInt: GUI-tunable numeric values (REAL helper).
    const mkInts = (values, keys = Object.keys(values)) => ({
        settings_schema: { has_key: k => keys.includes(k) },
        get_int: k => values[k],
    });
    H.setSettings(mkInts({ 'history-preview-chars': 25 }));
    check('settingInt: legge il valore impostato', H.settingInt('history-preview-chars', 50) === 25);
    H.setSettings(mkInts({}, []));
    check('settingInt: schema stantio (chiave assente) -> fallback', H.settingInt('history-preview-chars', 50) === 50);
    H.setSettings(null);
    check('settingInt: impostazioni non ancora create -> fallback', H.settingInt('x', 7) === 7);
    H.setSettings(mkInts({ k: 0 }));
    check('settingInt: valore 0 (non positivo) -> fallback', H.settingInt('k', 9) === 9);
    H.setSettings(mkInts({ k: NaN }));
    check('settingInt: valore non finito -> fallback', H.settingInt('k', 9) === 9);
    logged.length = 0;
    H.setSettings({ settings_schema: { has_key: () => true }, get_int: () => { throw new Error('boom'); } });
    check('settingInt: lettura che solleva -> fallback e errore loggato',
        H.settingInt('k', 4) === 4 && logged.length === 1);
    logged.length = 0;  // stato pulito per i controlli seguenti / clean state for the next checks

    sent.length = 0;
    H.setSettings(null);
    H.notifyErrorIfEnabled('E', 'b'); H.notifyStatusIfEnabled('S', 'b');
    check('notifiche: impostazioni non ancora impostate (prima di enable / dopo disable) -> acceso',
        sent.length === 2);

    sent.length = 0;
    H.setSettings({ settings_schema: { has_key: () => true }, get_boolean: () => { throw new Error('boom'); } });
    H.notifyErrorIfEnabled('E', 'b');
    check('notifiche: lettura che solleva -> notifica comunque inviata e errore loggato',
        sent.length === 1 && logged.length === 1);

    // enable/disable: le impostazioni vengono passate agli helper PRIMA
    // dell'indicatore (la sua costruzione puo' notificare) e azzerate dopo.
    // enable/disable: the settings are passed to the helpers BEFORE the
    // indicator (its construction can notify) and reset afterwards.
    const enableAt = source.indexOf('    enable() {');
    const enableBody = source.slice(enableAt, source.indexOf('    disable() {', enableAt));
    check('enable(): notificationSettings impostato PRIMA di creare l\'indicatore',
        enableBody.indexOf('notificationSettings = this._settings') !== -1
        && enableBody.indexOf('notificationSettings = this._settings') < enableBody.indexOf('new BravoricIndicator'));
    const disableAt = source.indexOf('    disable() {');
    check('disable(): notificationSettings azzerato',
        source.slice(disableAt, disableAt + 700).includes('notificationSettings = null'));
    const rawCalls = (source.match(/Main\.notify(Error)?\(/g) || []).length;
    check('extension.js: nessuna notifica diretta fuori dagli helper (2 sole chiamate Main.notify*)',
        rawCalls === 2);

    console.log(`\n${pass} PASS / 0 FAIL`);
})().catch(error => { console.error(error); process.exitCode = 1; });
