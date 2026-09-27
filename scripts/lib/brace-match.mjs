// F6-resto: stesso algoritmo di scripts/lib/brace-match.cjs (vedi lì il
// motivo e i limiti), duplicato qui in ESM perché gjs -m non capisce
// require(). Prima era copiato a mano identico in 3 punti fra
// test-toml-bool-editor.js, test-shortcut-accelerator.js e
// test-smoke-gjs-prefs.js. Le due copie (.cjs qui sopra e .mjs qui) restano
// DUE file perché Node (require) e gjs (import) non condividono un formato
// di modulo: unificarle in una sola richiederebbe un loader condizionale,
// più rischio per zero beneficio su un file di 12 righe.
//
// matchBrace(src, openIndex): openIndex deve puntare a '{'. Ritorna l'indice
// della '}' che la richiude, o -1 se le graffe non sono bilanciate.
export function matchBrace(src, openIndex) {
    let depth = 0;
    for (let i = openIndex; i < src.length; i++) {
        if (src[i] === '{')
            depth++;
        else if (src[i] === '}' && --depth === 0)
            return i;
    }
    return -1;
}
