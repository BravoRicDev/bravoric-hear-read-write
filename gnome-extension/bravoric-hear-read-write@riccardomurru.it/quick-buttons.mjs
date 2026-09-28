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
// status.json mentre quell'azione registra; `stoppable` dice se un secondo click
// la ferma (dettatura e streaming si', l'OCR e' un'azione singola).
// One button per service. `setting` is the boolean GSettings key that shows it
// (default: off). `service` is the value the backend writes in status.json while
// that action is recording; `stoppable` says whether a second click stops it
// (dictation and streaming yes, OCR is a single action).
export const QUICK_BUTTONS = [
    { key: 'dictation', setting: 'show-dictation-button', command: 'bravoric-stt-toggle', icon: 'audio-input-microphone-symbolic', service: 'stt', stoppable: true },
    { key: 'ocr', setting: 'show-ocr-button', command: 'bravoric-ocr-capture', icon: 'camera-photo-symbolic', service: 'ocr', stoppable: false },
    { key: 'stream', setting: 'show-stream-button', command: 'bravoric-stream-toggle', icon: 'microphone-sensitivity-high-symbolic', service: 'stream', stoppable: true },
];

// Questo bottone sta registrando ora (e quindi un click lo ferma)?
// Is this button recording right now (so that a click stops it)?
export function quickButtonActive(spec, state, service) {
    return spec.stoppable && state === 'recording' && service === spec.service;
}

// Cliccabile? Come le voci di menu: solo da idle o error (una cattura alla volta,
// niente race su audio/clipboard). In piu' il bottone che sta registrando resta
// cliccabile, perche' il suo click e' proprio lo stop.
// Clickable? Like the menu entries: only from idle or error (one capture at a
// time, no races on audio/clipboard). In addition the button that is recording
// stays clickable, because its click is precisely the stop.
export function quickButtonSensitive(spec, state, service) {
    if (state === 'idle' || state === 'error')
        return true;
    return quickButtonActive(spec, state, service);
}

/*
 * Controller dei bottoni. Dipendenze / dependencies:
 *   readBool(key)            -> bool   chiave GSettings (falso se assente / false if missing)
 *   makeButton(spec, onClick) -> { setSensitive(b), setActive(b), destroy() }
 *   spawn(command)           -> avvia il binario del backend / launches the backend binary
 */
export function createQuickButtons(deps) {
    const built = new Map();
    let signature = null;
    let lastState = 'idle';
    let lastService = null;

    function apply() {
        for (const spec of QUICK_BUTTONS) {
            const handle = built.get(spec.key);
            if (!handle)
                continue;
            handle.setSensitive(quickButtonSensitive(spec, lastState, lastService));
            handle.setActive(quickButtonActive(spec, lastState, lastService));
        }
    }

    function destroyAll() {
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
                built.set(spec.key, deps.makeButton(spec, () => deps.spawn(spec.command)));
            signature = next;
            apply();
        },

        // Nuovo stato letto da status.json.
        // New state read from status.json.
        update(state, service) {
            lastState = state;
            lastService = service ?? null;
            apply();
        },

        // Chiavi dei bottoni attualmente costruiti (per i test).
        // Keys of the currently built buttons (for the tests).
        keys() {
            return [...built.keys()];
        },

        destroy: destroyAll,
    };
}
