// Sonda SOLO PER TEST: gira dentro un GNOME Shell vero (headless, isolato) e
// permette al test di guardare la top bar e di cliccare un bottone rapido, senza
// display, senza schermo e senza toccare la sessione dell'utente.
// Test-ONLY probe: it runs inside a real GNOME Shell (headless, isolated) and lets
// the test look at the top bar and click a quick button, with no display, no
// screen and without touching the user's session.
//
// Protocollo a file / file protocol: la directory e' in $BRV_PROBE_DIR.
//   - ogni mezzo secondo scrive `dump.json` con i widget della top bar;
//   - se esiste `cmd`, la esegue ("click <chiave>") e scrive `cmd-result`.
//   - every half second it writes `dump.json` with the top-bar widgets;
//   - if `cmd` exists, it runs it ("click <key>") and writes `cmd-result`.
import GLib from 'gi://GLib';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

// Trova il St.Button interno di un bottone rapido dentro un PanelMenu.Button.
// Finds the inner St.Button of a quick button inside a PanelMenu.Button.
function innerButton(panelButton) {
    for (let child = panelButton.get_first_child(); child; child = child.get_next_sibling()) {
        if (child.style_class && child.style_class.includes('bravoric-quick-button'))
            return child;
    }
    return null;
}

function snapshot() {
    const roles = Object.entries(Main.panel.statusArea);
    const items = [];
    // Ordine da sinistra a destra nella casella destra della top bar.
    // Left-to-right order in the right box of the top bar.
    for (const container of Main.panel._rightBox.get_children()) {
        const button = container.get_first_child?.() ?? null;
        const entry = roles.find(([, widget]) => widget === button || widget?.container === container);
        const role = entry ? entry[0] : null;
        const inner = button ? innerButton(button) : null;
        items.push({
            role,
            quick: inner !== null,
            width: Math.round(container.width),
            height: Math.round(container.height),
            reactive: inner ? inner.reactive : null,
            accessible_name: inner ? inner.accessible_name : null,
            opacity: inner ? inner.opacity : null,
        });
    }
    return items;
}

export default class ShellProbe extends Extension {
    enable() {
        this._dir = GLib.getenv('BRV_PROBE_DIR');
        this._id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
            try {
                GLib.file_set_contents(`${this._dir}/dump.json`, JSON.stringify(snapshot()));
                // Cartella home vista dall'estensione: serve al test per essere sicuro
                // che legga e scriva SOLO dentro la sua HOME temporanea.
                // Home directory seen by the extension: the test needs it to be sure it
                // reads and writes ONLY inside its temporary HOME.
                GLib.file_set_contents(`${this._dir}/home.txt`, GLib.get_home_dir());
                const cmdPath = `${this._dir}/cmd`;
                if (GLib.file_test(cmdPath, GLib.FileTest.EXISTS)) {
                    const cmd = new TextDecoder().decode(GLib.file_get_contents(cmdPath)[1]).trim();
                    GLib.unlink(cmdPath);
                    let result = 'unknown command';
                    if (cmd === 'monitor-info') {
                        const ind = Main.panel.statusArea['bravoric-indicator@local'];
                        if (ind._monitor && !this._counting) {
                            this._counting = true;
                            this._events = 0;
                            ind._monitor.connect('changed', () => { this._events++; });
                        }
                        result = JSON.stringify({ hasMonitor: !!ind._monitor, id: ind._monitorId, events: this._events ?? -1, cancelled: ind._monitor?.is_cancelled?.() });
                    }
                    // Apre il menu dell'indicatore, apre anche i sottomenu e ne elenca le voci
                    // (etichetta e sensibilita'), poi lo richiude.
                    // Opens the indicator menu, also opens the submenus and lists their
                    // entries (label and sensitivity), then closes it again.
                    if (cmd === 'menu') {
                        const ind = Main.panel.statusArea['bravoric-indicator@local'];
                        ind.menu.open(false);
                        const rows = [];
                        for (const item of ind.menu._getMenuItems()) {
                            if (item.menu && item.label)
                                item.menu.open(false);
                            rows.push({ label: item.label?.text ?? null, sensitive: item.sensitive ?? null, sub: !!item.menu });
                            for (const sub of item.menu?._getMenuItems?.() ?? [])
                                rows.push({ label: sub.label?.text ?? null, sensitive: sub.sensitive ?? null, sub: false, parent: item.label?.text ?? null });
                        }
                        ind.menu.close(false);
                        result = JSON.stringify(rows);
                    }
                    // Finestre presenti nel Shell (titolo e classe), per vedere se si e' aperta
                    // la finestra delle preferenze; `close-windows` le chiude tutte.
                    // Windows present in the Shell (title and class), to see whether the
                    // preferences window opened; `close-windows` closes them all.
                    if (cmd === 'windows' || cmd === 'close-windows') {
                        const windows = global.get_window_actors().map(a => a.meta_window);
                        result = JSON.stringify(windows.map(w => ({ title: w.get_title(), wm_class: w.get_wm_class() })));
                        if (cmd === 'close-windows') {
                            for (const w of windows)
                                w.delete(global.get_current_time());
                        }
                    }
                    if (cmd === 'labels') {
                        const ind = Main.panel.statusArea['bravoric-indicator@local'];
                        result = JSON.stringify({
                            dictation: ind._dictationItem.label.text,
                            ocr: ind._ocrItem.label.text,
                            stream: ind._streamItem.label.text,
                        });
                    }
                    if (cmd === 'refresh') {
                        Main.panel.statusArea['bravoric-indicator@local']._refreshStatus();
                        result = 'refreshed';
                    }
                    const m = cmd.match(/^click (\w+)$/);
                    if (m) {
                        const entry = Object.entries(Main.panel.statusArea).find(([role]) => role.endsWith(`-quick-${m[1]}`));
                        const inner = entry ? innerButton(entry[1]) : null;
                        if (inner) {
                            inner.emit('clicked', 1);
                            result = 'clicked';
                        } else {
                            result = 'no such quick button';
                        }
                    }
                    GLib.file_set_contents(`${this._dir}/cmd-result`, result);
                }
            } catch (e) {
                GLib.file_set_contents(`${this._dir}/probe-error`, String(e.stack ?? e));
            }
            return GLib.SOURCE_CONTINUE;
        });
    }

    disable() {
        if (this._id)
            GLib.source_remove(this._id);
        this._id = 0;
    }
}
