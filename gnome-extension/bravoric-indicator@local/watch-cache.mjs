// Osservazione dei file di cache scritti dal backend: il monitor di
// directory, il debounce di rilettura e i due punti di aggancio.
//
// Modulo puro: non importa NESSUN modulo gi://, e perche' i test possano
// eseguirlo davvero (come stream-consumer.mjs) invece di estrarne il
// sorgente e trasformarlo, Gio, GLib e logError arrivano come dipendenze
// dal chiamante. Dato che non c'e' nessun import, la purezza richiesta da
// G7/G18 e' garantita per costruzione, non per promessa.
// Observation of the cache files written by the backend: the directory
// monitor, the re-read debounce and the two hook points.
//
// Pure module: it imports NO gi:// module, and so that the tests can really
// run it (like stream-consumer.mjs) instead of extracting its source and
// transforming it, Gio, GLib and logError arrive as dependencies from the
// caller. Since there are no imports, the purity required by G7/G18 is
// guaranteed by construction, not by promise.

/**
 * Rimanda una lettura: la sorgente gia' armata viene rimossa PRIMA di
 * crearne una nuova e il riferimento resta accanto alla creazione, cosi'
 * destroy() ha un solo campo da leggere per questo debounce (G6).
 */
/*
 * Defers a read: the already armed source is removed BEFORE creating a new
 * one and the reference stays next to the creation, so destroy() has a
 * single field to read for this debounce (G6).
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
// Monitoring of a cache directory: creates the directory if missing and
// watches it, invoking onChanged at every write. The monitor is the one of
// the DIRECTORY, not of the file: the backend writes atomically (tmp +
// rename) and the tmp would not belong to the monitor of the final file.
//
// The two fields that destroy() disconnects (_monitor/_monitorId for the
// state, _streamMonitor/_streamMonitorId for the stream) are passed by NAME:
// here we do not know which one it is, and with a fixed name one of the two
// teardowns would stop disconnecting. For the same reason createLabel and
// watchLabel are distinct: the two files do not use the same suffix in the
// two messages ('stream' at creation, 'stream_state' at monitoring).
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
// status.json and output_history.json can be written in rapid succession
// (e.g. double clipboard injection): the actual read is deferred and reset at
// every event, it reads only after the last one.
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
// stream_state.json to paste the chunks just ready (per_chunk mode). Same
// pattern as watchStatusFile, with a dedicated debounce to absorb the atomic
// write (tmp + rename).
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
