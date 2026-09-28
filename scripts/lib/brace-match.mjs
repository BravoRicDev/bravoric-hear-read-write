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
// F6-rest: same algorithm as scripts/lib/brace-match.cjs (see there for the
// reason and the limits), duplicated here in ESM because gjs -m does not
// understand require(). Before, it was copied by hand identically in 3
// places among test-toml-bool-editor.js, test-shortcut-accelerator.js and
// test-smoke-gjs-prefs.js. The two copies (.cjs above and .mjs here) stay
// TWO files because Node (require) and gjs (import) do not share a module
// format: unifying them in one would require a conditional loader, more
// risk for zero benefit on a 12-line file.
//
// matchBrace(src, openIndex): openIndex must point to '{'. It returns the
// index of the '}' that closes it, or -1 if the braces are not balanced.
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
