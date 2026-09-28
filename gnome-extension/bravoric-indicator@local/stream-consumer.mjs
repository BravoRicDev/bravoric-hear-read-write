// Consumatore puro degli snapshot dello stream, condiviso da GNOME Shell e dai test di regressione Node.
// Pure stream snapshot consumer shared by GNOME Shell and Node regression tests.
export function computeStreamDelete(segments, scope) {
    const updated = segments.slice();
    const oldTotal = updated.reduce((a, s) => a + s.length, 0);
    if (updated.length === 0) return { count: 0, segments: updated };
    const raw = updated[updated.length - 1];
    const body = raw.replace(/\s+$/u, '');
    if (scope === 'chunk') {
        if (!body) return { count: 0, segments: updated };
        if (updated.length > 1) updated.pop(); else updated[0] = ' ';
    } else if (scope === 'word') {
        const match = body.match(/\S+$/u);
        if (!match) return { count: 0, segments: updated };
        const separator = body.length > match[0].length ? 1 : 0;
        const remainder = body.slice(0, body.length - match[0].length - separator) + ' ';
        if (remainder === ' ' && updated.length > 1) updated.pop();
        else updated[updated.length - 1] = remainder;
    } else {
        return { count: -1, segments: updated };
    }
    const newTotal = updated.reduce((a, s) => a + s.length, 0);
    return { count: oldTotal - newTotal, segments: updated };
}

export function normalizeCommandKeyword(value) {
    if (typeof value !== 'string')
        return '';
    return value.trim().replace(/[\s.,;:!?…]+$/u, '').normalize('NFC').toLowerCase();
}

export function parseBlacklist(raw) {
    const values = Array.isArray(raw) ? raw : typeof raw === 'string' ? raw.split(',') : [];
    return new Set(values.map(normalizeCommandKeyword).filter(Boolean));
}

export function classifyStreamItem(item, rules = [], blacklistSet = new Set()) {
    const text = typeof item?.text === 'string' ? item.text : '';
    const normalized = normalizeCommandKeyword(text);
    if (normalized && blacklistSet.has(normalized))
        return { ...item, action: 'drop' };
    const rule = normalized ? rules.find(candidate => candidate && [candidate.keyword, ...(Array.isArray(candidate.aliases) ? candidate.aliases : [])]
        .some(phrase => normalizeCommandKeyword(phrase) === normalized)) : null;
    if (!rule)
        return { ...item, action: 'paste' };
    return { ...item, action: rule.action, command: { ...rule } };
}

// `nextIndex` indica i chunk consumati dallo snapshot verso la coda di incolla locale;
// non è una conferma che l'applicazione di destinazione abbia inserito il testo.
// `nextIndex` means chunks consumed from the snapshot into the local paste queue;
// it is not an acknowledgement that the target application inserted the text.
export function consumeStreamSnapshot(consumer, state) {
    if (!state || typeof state !== 'object' || Array.isArray(state) ||
        typeof state.session_id !== 'string' || !state.session_id)
        return { items: [], invalid: 0, accepted: false };

    if (consumer._streamSessionId !== state.session_id) {
        consumer._streamSessionId = state.session_id;
        consumer._streamIndex = 0;
        consumer._streamWasActive = state.active === true;
        // Azzeramento per-sessione: esisteva anche un campo omonimo SENZA
        // underscore, inizializzato e mai letto, mentre il flag vero restava
        // True dalla sessione precedente. Quello e' sparito: oggi l'unico
        // lettore del campo e' il test, non il modulo, dove sotto il flag
        // viene solo riscritto.
        // Per-session reset: there also used to be a same-named field WITHOUT an
        // underscore, initialized and never read, while the real flag stayed True
        // from the previous session. That one is gone: today the only reader of the
        // field is the test, not the module, where below the flag is only rewritten.
        consumer._streamFinalObserved = false;
    } else if (state.active === true) {
        consumer._streamWasActive = true;
    }

    const atEndAllowed = state.mode === 'at_end' && state.active !== true && consumer._streamWasActive;
    const perChunkAllowed = state.mode === 'per_chunk';
    if (!perChunkAllowed && !atEndAllowed)
        return { items: [], invalid: 0, accepted: false };

    const chunks = Array.isArray(state.chunks) ? state.chunks : [];
    const items = [];
    let invalid = 0;
    while (consumer._streamIndex < chunks.length) {
        const index = consumer._streamIndex++;
        const text = chunks[index];
        if (typeof text !== 'string' || !text.trim()) {
            invalid++;
            continue;
        }
        items.push({ sessionId: state.session_id, index, text });
    }
    if (state.active !== true)
        consumer._streamFinalObserved = true;
    return { items, invalid, accepted: true };
}
