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
