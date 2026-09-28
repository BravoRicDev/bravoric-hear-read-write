// Lampeggio dell'icona durante la registrazione.
//
// Modulo puro come watch-cache.mjs: nessuna riga di import e GLib arriva come
// dipendenza dal chiamante, cosi' anche questo pezzo e' eseguibile dai test
// senza estrarne il sorgente e trasformarlo.
//
// Un solo blocco con una sola ragione per esistere: "l'icona pulsa' o non
// pulsa'". Accendere e spegnere stanno insieme perche' condividono la stessa
// risorsa, il timer: la sua pulizia sta accanto alla creazione (G6), e
// destroy() legge un solo campo, _blinkTimeoutId.
// Icon blink while recording.
//
// Pure module like watch-cache.mjs: no import lines and GLib arrives as a
// dependency from the caller, so this piece too can be run by the tests
// without extracting its source and transforming it.
//
// A single block with a single reason to exist: "the icon pulses or it does
// not". Turning on and off live together because they share the same
// resource, the timer: its cleanup sits next to its creation (G6), and
// destroy() reads a single field, _blinkTimeoutId.

export function setRecordingBlink(instance, deps, active, className, intervalMs) {
    if (active) {
        if (instance._blinkTimeoutId)
            return; // gia' lampeggiante
        instance._icon.add_style_class_name(className);
        instance._blinkTimeoutId = deps.GLib.timeout_add(deps.GLib.PRIORITY_DEFAULT, intervalMs, () => {
            instance._icon.opacity = instance._icon.opacity === 255 ? 80 : 255;
            return deps.GLib.SOURCE_CONTINUE;
        });
    } else {
        if (instance._blinkTimeoutId) {
            deps.GLib.source_remove(instance._blinkTimeoutId);
            instance._blinkTimeoutId = null;
        }
        instance._icon.remove_style_class_name(className);
        instance._icon.opacity = 255;
    }
}
