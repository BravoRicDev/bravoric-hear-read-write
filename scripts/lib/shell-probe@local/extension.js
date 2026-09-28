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
                const cmdPath = `${this._dir}/cmd`;
                if (GLib.file_test(cmdPath, GLib.FileTest.EXISTS)) {
                    const cmd = new TextDecoder().decode(GLib.file_get_contents(cmdPath)[1]).trim();
                    GLib.unlink(cmdPath);
                    let result = 'unknown command';
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
