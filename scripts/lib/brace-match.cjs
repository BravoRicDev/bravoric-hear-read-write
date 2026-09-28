'use strict';
// F6-resto: unico algoritmo di brace-matching, prima duplicato identico in
// 7 punti fra test-timeout-logic.js, test-prefs-voice-commands.js e
// test-stream-consumer.js (stesso ciclo depth++/depth-- copiato a mano, una
// copia dimenticata in un fix futuro sarebbe rimasta indietro senza che
// nessun gate se ne accorgesse). Contatore naive: NON è quote/comment-aware
// (una '{' dentro una stringa o un commento del blocco estratto sballa il
// conteggio). Va bene qui perché i sorgenti su cui opera oggi (extension.js,
// prefs.js nei punti usati da questi test) non hanno graffe spaiate dentro
// stringhe/commenti in quei blocchi: il chiamante che estrae da un punto
// nuovo deve verificarlo di persona, come già faceva ogni copia manuale.
// L'estrattore quote/comment-aware di test-smoke-gjs-prefs.js (voce
// _buildShortcutsPage) resta apposta separato: algoritmo diverso, non una
// copia di questo.
//
// matchBrace(src, openIndex): openIndex deve puntare a '{'. Ritorna l'indice
// della '}' che la richiude, o -1 se le graffe non sono bilanciate.
// F6-rest: single brace-matching algorithm, before duplicated identically in
// 7 places among test-timeout-logic.js, test-prefs-voice-commands.js and
// test-stream-consumer.js (the same depth++/depth-- loop copied by hand, a
// copy forgotten in a future fix would have stayed behind without any gate
// noticing). Naive counter: it is NOT quote/comment-aware (a '{' inside a
// string or a comment of the extracted block throws the count off). It is
// fine here because the sources it operates on today (extension.js,
// prefs.js at the points used by these tests) have no unpaired braces
// inside strings/comments in those blocks: a caller extracting from a new
// point must verify it in person, as every manual copy already did. The
// quote/comment-aware extractor of test-smoke-gjs-prefs.js (the
// _buildShortcutsPage entry) stays separate on purpose: different
// algorithm, not a copy of this one.
//
// matchBrace(src, openIndex): openIndex must point to '{'. It returns the
// index of the '}' that closes it, or -1 if the braces are not balanced.
function matchBrace(src, openIndex) {
    let depth = 0;
    for (let i = openIndex; i < src.length; i++) {
        if (src[i] === '{')
            depth++;
        else if (src[i] === '}' && --depth === 0)
            return i;
    }
    return -1;
}

module.exports = { matchBrace };
