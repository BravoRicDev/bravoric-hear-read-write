// Osservazione dei file di cache scritti dal backend: il monitor di
// directory, il debounce di rilettura e i due punti di aggancio.
//
// Modulo puro: non importa NESSUN modulo gi://, e perche' i test possano
// eseguirlo davvero (come stream-consumer.mjs) invece di estrarne il
// sorgente e trasformarlo, Gio, GLib e logError arrivano come dipendenze
// dal chiamante. Dato che non c'e' nessun import, la purezza richiesta da
// G7/G18 e' garantita per costruzione, non per promessa.

/**
 * Rimanda una lettura: la sorgente gia' armata viene rimossa PRIMA di
 * crearne una nuova e il riferimento resta accanto alla creazione, cosi'
 * destroy() ha un solo campo da leggere per questo debounce (G6).
 */
function armDebounce(instance, deps, idField, debounceMs, onFire) {
    if (instance[idField])
        deps.GLib.source_remove(instance[idField]);
    instance[idField] = deps.GLib.timeout_add(deps.GLib.PRIORITY_DEFAULT, debounceMs, () => {
        instance[idField] = null;
        onFire();
        return deps.GLib.SOURCE_REMOVE;
    });
}

// Monitoraggio di una directory della cache: crea la directory se manca e
// la osserva, invocando onChanged a ogni scrittura. Il monitor e' quello
// della DIRECTORY, non del file: il backend scrive in modo atomico
// (tmp + rename) e il tmp non apparterrebbe al monitor del file finale.
//
// I due campi che destroy() disconnette (_monitor/_monitorId per lo stato,
// _streamMonitor/_streamMonitorId per lo stream) sono passati per NOME:
// qui non si sa quale sia, e con un nome fisso uno dei due teardown
// smetterebbe di disconnettere. Per lo stesso motivo createLabel e
// watchLabel sono distinti: i due file non usano lo stesso suffisso nei due
// messaggi ('stream' alla creazione, 'stream_state' al monitoraggio).
export function watchCacheFile(instance, deps, filePath, onChanged,
    { monitorField, monitorIdField, createLabel, watchLabel }) {
    const dir = deps.Gio.File.new_for_path(deps.GLib.path_get_dirname(filePath));
    try {
        dir.make_directory_with_parents(null);
    } catch (e) {
        if (!e.matches(deps.Gio.IOErrorEnum, deps.Gio.IOErrorEnum.EXISTS))
            deps.logError(e, `bravoric-indicator: impossibile creare ${createLabel} dir`);
    }
    try {
        const monitor = dir.monitor_directory(deps.Gio.FileMonitorFlags.NONE, null);
        instance[monitorField] = monitor;
        instance[monitorIdField] = monitor.connect('changed', onChanged);
    } catch (e) {
        deps.logError(e, `bravoric-indicator: impossibile monitorare ${watchLabel} dir`);
        if (instance[monitorField]) { instance[monitorField].cancel(); instance[monitorField] = null; }
    }
}

// status.json e output_history.json possono essere scritti in rapida
// successione (es. doppia iniezione clipboard): rimanda la lettura
// effettiva e si resetta a ogni evento, legge solo dopo l'ultimo.
export function scheduleRefresh(instance, deps, debounceMs) {
    armDebounce(instance, deps, '_refreshDebounceId', debounceMs, () => {
        instance._refreshStatus();
        instance._refreshHistory();
    });
}

export function watchStatusFile(instance, deps, statusPath, debounceMs) {
    watchCacheFile(instance, deps, statusPath,
        () => scheduleRefresh(instance, deps, debounceMs), {
            monitorField: '_monitor',
            monitorIdField: '_monitorId',
            createLabel: 'status',
            watchLabel: 'status',
        });
}

// stream_state.json per incollare i chunk appena pronti (modalita'
// per_chunk). Stesso pattern di watchStatusFile, con debounce dedicato per
// assorbire la scrittura atomica (tmp + rename).
export function watchStreamStateFile(instance, deps, streamStatePath, debounceMs) {
    watchCacheFile(instance, deps, streamStatePath, () => {
        armDebounce(instance, deps, '_streamDebounceId', debounceMs,
            () => instance._onStreamStateChanged());
    }, {
        monitorField: '_streamMonitor',
        monitorIdField: '_streamMonitorId',
        createLabel: 'stream',
        watchLabel: 'stream_state',
    });
}
