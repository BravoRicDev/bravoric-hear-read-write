/*
 * Bottoni rapidi nella top bar: un click avvia/ferma un'azione, senza menu.
 *
 * Modulo puro come watch-cache.mjs e recording-blink.mjs: nessun import gi://.
 * Il chiamante fornisce le dipendenze (lettura delle impostazioni, costruzione
 * del widget, avvio del comando), cosi' i test eseguono davvero questa logica
 * senza una sessione GNOME Shell.
 *
 * Quick buttons in the top bar: one click starts/stops an action, no menu.
 *
 * Pure module like watch-cache.mjs and recording-blink.mjs: no gi:// import.
 * The caller supplies the dependencies (settings read, widget construction,
 * command launch), so the tests really run this logic without a GNOME Shell
 * session.
 */

// Un bottone per servizio. `setting` e' la chiave GSettings booleana che lo
// mostra (default: spento). `service` e' il valore che il backend scrive in
// status.json per quell'azione. `startArgs`/`stopArgs` sono gli argomenti ESPLICITI
// del comando: il click non lancia mai un toggle "alla cieca", quindi un click
// "Ferma" arrivato dopo lo stop non riavvia nulla (stop e' idempotente nel
// backend) e un "Avvia" arrivato con l'azione gia' attiva non la ferma.
// `stopStates` sono gli stati in cui l'azione e' fermabile/annullabile:
// dettatura e streaming mentre registrano; OCR durante tutta l'elaborazione
// (selezione, richiesta), finche' status.json non dichiara `cancellable: false`.
// `noServiceIsMine`: uno status `recording` senza `service` (file vecchio)
// e' della dettatura, l'unica che un tempo non lo scriveva.
// `pendingStart`: dopo un avvio il backend impiega ~1 s a scrivere lo stato; in
// quella finestra il click successivo e' gia' uno stop.
// Streaming: lo start resta il toggle senza argomenti (il backend non ha `start`).
// One button per service. `setting` is the boolean GSettings key that shows it
// (default: off). `service` is the value the backend writes in status.json for
// that action. `startArgs`/`stopArgs` are the EXPLICIT arguments of the command:
// a click never launches a "blind" toggle, so a "Stop" click arriving after the
// stop restarts nothing (stop is idempotent in the backend) and a "Start"
// arriving with the action already active does not stop it. `stopStates` are the
// states in which the action can be stopped/cancelled: dictation and streaming
// while recording; OCR during the whole processing (selection, request), until
// status.json declares `cancellable: false`. `noServiceIsMine`: a `recording`
// status with no `service` (old file) belongs to dictation, the only one that
// used not to write it. `pendingStart`: after a start the backend takes ~1 s to
// write the state; in that window the next click is already a stop.
// Streaming: its start stays the no-argument toggle (the backend has no `start`).
export const QUICK_BUTTONS = [
    { key: 'dictation', setting: 'show-dictation-button', command: 'bravoric-stt-toggle', icon: 'audio-input-microphone-symbolic', service: 'stt', startArgs: ['start'], stopArgs: ['stop'], stopStates: ['recording'], noServiceIsMine: true, pendingStart: true },
    { key: 'ocr', setting: 'show-ocr-button', command: 'bravoric-ocr-capture', icon: 'camera-photo-symbolic', service: 'ocr', startArgs: ['start'], stopArgs: ['cancel'], stopStates: ['processing'], noServiceIsMine: false, pendingStart: false },
    { key: 'stream', setting: 'show-stream-button', command: 'bravoric-stream-toggle', icon: 'microphone-sensitivity-high-symbolic', service: 'stream', startArgs: [], stopArgs: ['stop'], stopStates: ['recording'], noServiceIsMine: false, pendingStart: false },
];

// Finestra massima in cui, dopo un avvio, il click successivo vale come stop.
// Maximum window in which, after a start, the next click counts as a stop.
export const PENDING_START_MS = 5000;

// Modalita' del controllo per lo stato letto:
//   'stop'  = l'azione e' in corso ed e' fermabile: il click la ferma/annulla
//   'start' = idle/error: il click la avvia
//   'busy'  = altro servizio attivo, o elaborazione non annullabile: non cliccabile
// Mode of the control for the state read:
//   'stop'  = the action is running and can be stopped: the click stops/cancels it
//   'start' = idle/error: the click starts it
//   'busy'  = another service is active, or a non-cancellable processing: not clickable
export function quickButtonMode(spec, state, service, cancellable) {
    const mine = service === spec.service || (spec.noServiceIsMine && (service === null || service === undefined));
    if (spec.stopStates.includes(state) && mine && cancellable !== false)
        return 'stop';
    if (state === 'idle' || state === 'error')
        return 'start';
    return 'busy';
}

// Questo bottone sta lavorando ora (e quindi un click lo ferma)?
// Is this button working right now (so that a click stops it)?
export function quickButtonActive(spec, state, service, cancellable) {
    return quickButtonMode(spec, state, service, cancellable) === 'stop';
}

// Cliccabile? Da idle/error (start) o quando e' lui a lavorare (stop). Mai durante
// il lavoro di un altro servizio o un'elaborazione non annullabile (una cattura
// alla volta, niente race su audio/clipboard).
// Clickable? From idle/error (start) or when it is the one working (stop). Never
// during another service's work or a non-cancellable processing (one capture at a
// time, no races on audio/clipboard).
export function quickButtonSensitive(spec, state, service, cancellable) {
    return quickButtonMode(spec, state, service, cancellable) !== 'busy';
}

/*
 * Controller dei bottoni. Dipendenze / dependencies:
 *   readBool(key)            -> bool   chiave GSettings (falso se assente / false if missing)
 *   makeButton(spec, onClick) -> { setSensitive(b), setActive(b), destroy() }
 *   spawn(command, args)     -> avvia il binario del backend; `false` = non partito
 *                               launches the backend binary; `false` = did not start
 *   now()                    -> ms (opzionale / optional, default Date.now)
 *   later(ms, fn)            -> () => void  timer annullabile (opzionale) / cancellable timer (optional)
 */
export function createQuickButtons(deps) {
    const built = new Map();
    const pending = new Map();
    const timers = new Map();
    const now = deps.now ?? (() => Date.now());
    let signature = null;
    let lastState = 'idle';
    let lastService = null;
    let lastCancellable = null;

    function clearPending(key) {
        pending.delete(key);
        const cancel = timers.get(key);
        if (cancel)
            cancel();
        timers.delete(key);
    }

    // Modalita' effettiva: un avvio appena lanciato conta gia' come "in corso"
    // finche' lo stato letto e' ancora quello vecchio.
    // Effective mode: a start just launched already counts as "running" while the
    // state read is still the old one.
    function modeOf(spec) {
        const mode = quickButtonMode(spec, lastState, lastService, lastCancellable);
        if (mode === 'start' && spec.pendingStart && now() < (pending.get(spec.key) ?? 0))
            return 'stop';
        return mode;
    }

    function apply() {
        for (const spec of QUICK_BUTTONS) {
            const handle = built.get(spec.key);
            if (!handle)
                continue;
            const mode = modeOf(spec);
            handle.setSensitive(mode !== 'busy');
            handle.setActive(mode === 'stop');
        }
    }

    function click(spec) {
        const mode = modeOf(spec);
        if (mode === 'busy')
            return;
        const args = mode === 'stop' ? spec.stopArgs : spec.startArgs;
        let started;
        try {
            started = deps.spawn(spec.command, args);
        } catch {
            started = false;
        }
        if (mode === 'start' && spec.pendingStart && started !== false) {
            clearPending(spec.key);
            pending.set(spec.key, now() + PENDING_START_MS);
            if (deps.later) {
                const cancel = deps.later(PENDING_START_MS, () => {
                    timers.delete(spec.key); // gia' scattato: niente da annullare | already fired: nothing to cancel
                    clearPending(spec.key);
                    apply();
                });
                timers.set(spec.key, cancel);
            }
        } else {
            clearPending(spec.key);
        }
        apply();
    }

    function destroyAll() {
        for (const key of [...pending.keys()])
            clearPending(key);
        for (const handle of built.values())
            handle.destroy();
        built.clear();
        signature = null;
    }

    return {
        // Allinea i bottoni alle impostazioni. Ricostruisce solo se l'insieme
        // dei bottoni voluti e' cambiato, cosi' un evento inutile non li fa
        // sfarfallare.
        // Aligns the buttons with the settings. It rebuilds only if the set of
        // wanted buttons changed, so a useless event does not make them flicker.
        sync() {
            const wanted = QUICK_BUTTONS.filter(spec => deps.readBool(spec.setting));
            const next = wanted.map(spec => spec.key).join(',');
            if (next === signature)
                return;
            destroyAll();
            // Ogni bottone nuovo finisce a sinistra dei precedenti: si crea
            // in ordine inverso perche' il primo della lista resti il piu' a
            // sinistra (e tutti a sinistra dell'indicatore principale).
            // Every new button lands to the left of the previous ones: they
            // are created in reverse order so the first of the list stays the
            // leftmost (and all of them left of the main indicator).
            for (const spec of [...wanted].reverse())
                built.set(spec.key, deps.makeButton(spec, () => click(spec)));
            signature = next;
            apply();
        },

        // Nuovo stato letto da status.json.
        // New state read from status.json.
        update(state, service, cancellable) {
            lastState = state;
            lastService = service ?? null;
            lastCancellable = cancellable ?? null;
            // Lo stato e' arrivato: la finestra "avvio in corso" ha fatto il suo
            // lavoro (recording/processing = avviato; error = fallito).
            // The state arrived: the "start in progress" window did its job
            // (recording/processing = started; error = failed).
            if (state !== 'idle')
                for (const key of [...pending.keys()])
                    clearPending(key);
            apply();
        },

        // Modalita' corrente di un bottone: 'start' | 'stop' | 'busy' (per il menu e i test).
        // Current mode of a button: 'start' | 'stop' | 'busy' (for the menu and tests).
        mode(key) {
            const spec = QUICK_BUTTONS.find(s => s.key === key);
            return spec ? modeOf(spec) : 'busy';
        },

        // Chiavi dei bottoni attualmente costruiti (per i test).
        // Keys of the currently built buttons (for the tests).
        keys() {
            return [...built.keys()];
        },

        destroy: destroyAll,
    };
}
