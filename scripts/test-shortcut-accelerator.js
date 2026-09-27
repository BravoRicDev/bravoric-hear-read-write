// test-shortcut-accelerator.js — F5 (giro 4), validazione degli acceleratori.
//
// Il difetto: l'unico controllo prima della scrittura era markConflict, che
// cerca una stringa IDENTICA in cinque schemi di sistema. Non è una
// validazione di acceleratore: una scorciatoia non valida restava salvata in
// dconf per sempre e non accadeva MAI, senza un solo messaggio in nessuna
// lingua. Misurato: la GUI scriveva 'Alt+Super+R' e '<Alt><Super>r' in modo
// identico, e solo il secondo funziona.
//
// Qui si esegue la FUNZIONE REALE estratta da prefs.js (non una copia:
// una copia potrebbe divergere senza che nessun controllo lo noti), su gjs
// vero, e si misura il comportamento di Gtk.accelerator_parse invece di
// crederci. prefs.js non è importabile fuori da una sessione Shell (importa
// la risorsa resource:///org/gnome/Shell/...), quindi si estrae per
// brace-matching e si valuta, come già fatto per TomlBoolEditor.
//
// IL PUNTO DI NON-VACUITA' che questo test presidia: Gtk.accelerator_parse
// NON ritorna un booleano in GJS, ritorna [ok, keyval, mods], un array boxed
// che in JavaScript è SEMPRE truthy — anche [false, 0, 0]. Il controllo
// `if (!Gtk.accelerator_parse(t))` passerebbe quindi SEMPRE, su QUALSIASI
// stringa: sarebbe un controllo vacuo, cioè esattamente il difetto che
// questa funzione deve chiudere. I casi sotto includono percio' le due
// stringhe che differiscono solo per la forma e per l'iniziale.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import { matchBrace } from './lib/brace-match.mjs';
// Gtk 4 esplicitamente: senza questo gjs segnala "Gtk ha 2 versioni" e la
// versione scelta puo' cambiare comportamento di accelerator_parse.
imports.gi.versions.Gtk = '4.0';
const { Gtk } = imports.gi;

const PREFS_PATH = GLib.build_filenamev([
    GLib.get_current_dir(), 'gnome-extension', 'bravoric-indicator@local', 'prefs.js',
]);

let passed = 0;
let failed = 0;
function check(label, condition) {
    if (condition) {
        passed++;
        console.log(`  PASS  ${label}`);
    } else {
        failed++;
        console.log(`  FAIL  ${label}`);
    }
}

const [ok, bytes] = GLib.file_get_contents(PREFS_PATH);
if (!ok)
    throw new Error(`prefs.js non leggibile: ${PREFS_PATH}`);
const prefsSrc = new TextDecoder().decode(bytes);

// Estrae una funzione per brace-matching dal sorgente reale.
function funcSource(src, signature) {
    const at = src.indexOf(signature);
    if (at === -1)
        throw new Error(`${signature} non trovato in prefs.js`);
    const open = src.indexOf('{', at);
    const end = matchBrace(src, open);
    if (end === -1)
        throw new Error(`graffe non bilanciate in ${signature}`);
    return src.slice(at, end + 1);
}

// Se la funzione non c'eta' piu' nel prodotto (per esempio dopo un
// ripristino), il test deve DIRE PERCHE' e fallire, non abortire con uno
// stack trace: nel gate la riga di fallimento deve essere leggibile.
let acceleratorIsValid = null;
try {
    acceleratorIsValid = eval(`(() => {
        ${funcSource(prefsSrc, 'function acceleratorIsValid(')};
        return acceleratorIsValid;
    })()`);
} catch (e) {
    console.log(`  FAIL  acceleratorIsValid non trovato in prefs.js: ${e.message}`);
    console.log('\n0 PASS / 1 FAIL');
    throw new Error('test-shortcut-accelerator.js: validazione assente nel prodotto');
}
console.log('acceleratorIsValid REALE estratto da prefs.js e in esecuzione');

console.log('== F5: acceleratori validi e non validi ==');

// La forma corretta: <Mod><Mod><tasto> (angolari, tasto minuscolo o maiuscolo).
check('forma canonica <Alt><Super>r accettata',
    acceleratorIsValid('<Alt><Super>r') === true);
check('forma canonica <Control><Shift>v accettata',
    acceleratorIsValid('<Control><Shift>v') === true);
check('forma canonica <Control><Alt>Delete accettata',
    acceleratorIsValid('<Control><Alt>Delete') === true);

// Le forme che Mutter RIFIUTA. Sono quelle che la GUI scriveva prima,
// indistinguibili dalle precedenti per l'utente: una sola lettera di differenza.
check('forma senza angolari Alt+Super+R rifiutata (Mutter la scarta)',
    acceleratorIsValid('Alt+Super+R') === false);
check('forma senza angolari Ctrl+Shift+V rifiutata',
    acceleratorIsValid('Ctrl+Shift+V') === false);
check('testo libero rifiutato',
    acceleratorIsValid('ciao mamma') === false);
check('spazi iniziali/finali rifiutati (niente trim implicito)',
    acceleratorIsValid('  <Alt><Super>r  ') === false);
check('spazio finale rifiutato',
    acceleratorIsValid('<Alt><Super>r ') === false);

// Modificatore SENZA tasto: parsea, ma non e' un keybinding utilizzabile.
// serve accelerator_valid per chiudere questo buco.
check('solo modificatore <Super> rifiutato (nessun tasto)',
    acceleratorIsValid('<Super>') === false);
check('solo modificatore <Alt> rifiutato (nessun tasto)',
    acceleratorIsValid('<Alt>') === false);

// Stringa vuota = scorciatoia rimossa: NON e' un errore di sintassi, e il
// prodotto deve continuare a poterla cancellare (row.text ? ... : []).
check('stringa vuota accettata (rimozione della scorciatoia)',
    acceleratorIsValid('') === true);

// Non-vacuità: la forma che il reviewer aveva suggerito, se fosse usata
// literally, passerebbe su TUTTE le stringhe perché un array è truthy.
const boxedAlwaysTruthy = ['Alt+Super+R', 'ciao mamma', ''].every(t => {
    const res = Gtk.accelerator_parse(t);
    return Array.isArray(res) && Boolean(res) === true;
});
check('CONFIRMATO: il valore ritornato da accelerator_parse e\' sempre truthy',
    boxedAlwaysTruthy);
check('CONFIRMATO: la destrutturazione distingue i casi (il naive no)',
    Gtk.accelerator_parse('Alt+Super+R')[0] === false
    && Gtk.accelerator_parse('<Alt><Super>r')[0] === true);

// --- Il prodotto scrive solo se la riga e' valida, e lo dice -------------
// markInvalid/refreshEntryState vivono dentro _buildShortcutsPage, non sono
// estraibili singolarmente: qui si presidia l'ORDINE delle operazioni nel
// sorgente reale (validazione PRIMA di set_strv, e un return che impedisce
// la scrittura). Un test che passa anche con il codice di prima non
// presidierebbe nulla: il vecchio codice non aveva nessuna delle due.
const applyBlock = funcSource(prefsSrc, "row.connect('apply', () => {");
const validateAt = applyBlock.indexOf('refreshEntryState()');
const returnAt = applyBlock.indexOf('if (!refreshEntryState())');
const writeAt = applyBlock.indexOf('settings.set_strv(');
check('nel prodotto la validazione precede la scrittura in dconf',
    validateAt !== -1 && writeAt !== -1 && validateAt < writeAt);
check('nel prodotto una riga non valida blocca la scrittura (return)',
    returnAt !== -1 && returnAt < writeAt);
check('il prodotto chiama set_strv una sola volta nel blocco apply',
    applyBlock.split('settings.set_strv(').length === 2);

// --- i18n: la nuova stringa esiste e non passa cruda --------------------
const newMsg = 'Not a valid shortcut: %s';
check('la nuova stringa e\' passata da _() nel prodotto',
    prefsSrc.includes(`_('${newMsg}')`));
const poDir = GLib.build_filenamev([
    GLib.get_current_dir(), 'gnome-extension', 'bravoric-indicator@local', 'po',
]);
for (const file of ['bravoric-indicator.pot', 'it.po']) {
    const [pok, pbytes] = GLib.file_get_contents(GLib.build_filenamev([poDir, file]));
    const txt = new TextDecoder().decode(pbytes);
    check(`"${newMsg}" presente in ${file}`, pok && txt.includes(`msgid "${newMsg}"`));
}
const itTxt = (() => {
    const [pok, pbytes] = GLib.file_get_contents(GLib.build_filenamev([poDir, 'it.po']));
    if (!pok)
        throw new Error('it.po non leggibile');
    return new TextDecoder().decode(pbytes);
})();
const itEntry = itTxt.split('\n').find(l => l.trim().startsWith('msgstr') &&
    itTxt.includes(`msgid "${newMsg}"`) && l.includes('scorciatoia'));
check('la voce italiana e\' tradotta (non resta la stringa inglese)',
    itEntry !== undefined);
// La traduzione non vuole perdere il segnaposto %s.
const itMsgStr = itEntry ? itEntry.trim().slice('msgstr'.length).trim() : '';
check('la traduzione conserva il segnaposto %s',
    itMsgStr.includes('%s'));

console.log(`\n${passed} PASS / ${failed} FAIL`);
if (failed > 0)
    throw new Error('test-shortcut-accelerator.js: ci sono check falliti');
