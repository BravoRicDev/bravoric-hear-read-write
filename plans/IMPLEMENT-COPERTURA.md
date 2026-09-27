# IMPLEMENT-COPERTURA — due assert che non possono fallire

Progetto: `/home/riccardo/Progetti/bravoric-stt-clipboard` (non git, un solo writer: questo worker).
Brief: `~/.local/share/bravoric-stt-clipboard/plans/BRIEF-COPERTURA-2b.md`.
File di sorgente toccati: **uno solo**, `scripts/test-backend.py`. Nessun `src/` è stato modificato.

---

## 0. OSTACOLO INIZIALE, FUORI DAL BRIEF: il gate era ROSSO, non verde

Il brief dichiarava "gate verde 617 asserzioni". **Non era vero**: il primo
`check-extension.sh` ha dato **3 controlli falliti** (`test-toml-bool-editor.js`,
`test-install-extension-dir.sh`, `test-backend.py`).

Causa misurata, non ipotizzata: **`/tmp` era al 100%** (tmpfs 2,0 G, 0 disponibili).
`test-install-extension-dir.sh` lo diceva letteralmente: `cp: errore scrivendo ...:
Spazio esaurito sul device`. I due FAIL del test Python erano lo stesso effetto
collaterale, non difetti di codice.

Liberato `/tmp` (rimossi le copie mutanti abbandonate del worker morto
`prova-hb`, `cop-cop-A`, `cop-cop-B`, ~522 M, piu' le 379 directory scratch
`brv-test-*` piu' vecchie di 24 h e le cache `node-compile-cache`/`jiti`/
`pi-lens-ast-grep`; **non toccati** `opencode`, `pi-subagents-uid-1000`,
`pvenv`, `g4`, `fixchk` — c'era un `opencode` vivo, toccarlo non era un rischio
che si doveva correre), il gate e' tornato verde da solo: nessuna asserzione
era da correggere li'.

Nessun marker `MUTAZIONE` e nessun `.bak`/`.orig`/`.rej` nell'albero vero: verificato
prima e dopo. `~/.config/bravoric-stt-clipboard/config.toml` **intatto**
(mtime 2026-09-26 09:27:10, anteriore a questo lavoro).

---

## LAVORO 1 — il cuore si misura nel CORPO di `heartbeat()`

**Difetto.** L'asserzione confrontava la scrittura di stato con **tutto** `stream.py`.
La stessa identica riga `status.write_status(status.STATE_RECORDING, service="stream")`
compare anche nei `write_status` di avvio: nella copia mutata ne restavano **5** dopo
lo svuotamento del cuore. Con quel rumore di fondo, svuotare `heartbeat()` non poteva
produrre un FAIL — coerente con la misura del precedente (617 PASS / 0 FAIL).

**Correzione.** Due funzioni pure a livello di modulo, prima di `check()`:

- `py_func_body(text, signature)` — ritorna il corpo fra la firma e il prossimo
  `def` di primo livello, con la **docstring rimossa** (dentro la docstring la parola
  `write_status` *descrive* il guard, non lo chiama: contarla farebbe passare il test
  sul codice che non scrive piu` niente). Firma assente -> corpo vuoto, mai `IndexError`.
- `heartbeat_verdict(text)` -> `(ok, motivo)`: fallisce se la funzione non esiste,
  se il corpo e' vuoto, o se dentro il corpo non c'e' la riscrittura con
  `service="stream"`. Cattura ogni eccezione: **non puo' sollevare**.

Il test nuovo e' in **coda** a `scripts/test-backend.py`, sezione
`== giro 19: P4 il cuore si misura nel CORPO di heartbeat() ==`: 1 assert sul sorgente
vero + 6 contro-prove.

## LAVORO 2 — l'ordine della guardia non deve sollevare

**Stato di partenza, misurato:** la parte grossa del difetto era **gia' a terra**
nel ramo corrente (giro 18): c'erano gia' `find()` e una lista `_missing` al posto
dei due `.index()`. Ma restava **un crash in attesa** subito sopra, non segnalato
dal brief:

```python
_start_body = (_stt_src.split("def _start(cfg: Config) -> None:")[1]   # <- IndexError
```

Se la firma di `_start` fosse sparita dal sorgente, `[1]` avrebbe sollevato
`IndexError` e abortito la suite: **esattamente la classe di difetto che il brief
chiedeva di chiudere**, solo su un'altra riga.

**Correzione.** Estratta la verifica in `guard_order_verdict(body)` -> `(ok, mancanti)`,
funzione pura che cerca con `find()`, restituisce sempre un verdetto e non solleva
mai. L'asserzione esistente l'ora richiama; l'estrazione del corpo verifica prima
che la firma ci sia e, se manca, registra un FAIL pulito. Aggiunte 5 contro-prove
in coda, compresa quella che distingue i due FAIL possibili con lo stesso messaggio:
*corda mancante* (nomi elencati) vs *ordine invertito* (zero mancanti, FAIL per
l'ordine).

## DICHIARAZIONE RICHIESTA: MODIFICATO UN ASSERT ESISTENTE (voluto)

`scripts/test-backend.py:4334-4352`, giro 18 P4 ("il guard sullo stato stream sta
PRIMA di audio.start_recording"): l'asserzione e' stata **riscritta**, non
aggiunta. Non e' stata indebolita ne' resa tautologica — la semantica richiesta e'
identica (guardia presente **e** prima di `start_recording`), cambia solo il
meccanismo: niente piu' `.index()`/`[1]` che possano sollevare. Tutti gli altri
assert esistenti, incluso il check JS gemello in `scripts/test-timeout-logic.js`,
**non sono stati toccati**.

Nessun assert e' stato cancellato o allentato per far verde il gate.

---

## NON-VACUITA (obbligatoria) — numeri ESATTI, su copie in /tmp

Copie parziali (solo `src/ scripts/ gnome-extension/ config/ po/ assets/ bin/`),
mai sull'albero vero. Nessuna prova ha toccato `src/` del progetto.

| Voce | Mutazione | Esito |
|---|---|---|
| LAVORO 1 | corpo di `heartbeat()` svuotato (firma + docstring tenute); **5** `write_status` rimasti nel file | **628 PASS / 1 FAIL** |
| LAVORO 2 | invocazione della guardia `_is_stream_active()` rimossa da `stt._start` (resto del corpo intatto, file sintatticamente valido) | **626 PASS / 3 FAIL** |

I due FAIL sono **diversi fra loro** (1 e 3) e diversi dal FAIL atteso in verde (0).

- LAVORO 1: l'unico FAIL e `P4: il CORPO di heartbeat() riscrive RECORDING con service=stream`.
  Sulla stessa identica mutazione il vecchio test dava 617 PASS / 0 FAIL: **il test
  prima non poteva fallire, ora fallisce**.
- LAVORO 2: i 3 FAIL sono i due comportamentali gia' esistenti
  (`STT non avvia nessun ffmpeg`, `il rifiuto e' esplicito`) piu' quello dell'asserzione
  riscritta, con messaggio che **dichiara cosa manca**:
  `mancano da stt._start: la guardia _is_stream_active()`. **Nessun `ValueError`,
  nessun abort**: la suite e' arrivata in fondo stampando `626 PASS / 3 FAIL` (exit 1).
  I `Traceback` presenti nell'output sono i rami d'errore gia' loggati che esistono
  anche nella run verde, non crash della suite.

**Dichiarazione onesta sulle tecniche usate.** Per LAVORO 1 la prima prova e' riuscita
al primo tentativo. Per LAVORO 2 sono serviti **tre** tentativi invece dei due previsti,
e i primi due l'hanno fatto male, non il test:
1. rinominato `_is_stream_active()` in `__removed_guard__` -> `NameError` a runtime,
   la suite e' morta prima di arrivare all'asserzione (misura invalida);
2. regex su blocco -> ha mangiato tutto il corpo di `_start`, `IndentationError`;
3. rimozione di **una sola riga** (`if _is_stream_active():` -> `if False:`), lasciando
   il resto del corpo intatto: questo e' quello valido, e il codice mutato resta
   sintatticamente valido ed eseguibile.

Non ho inventato numeri: sono le tre run reali, e la terza e' l'unica riportata come
prova perche' e' l'unica in cui la mutazione rappresenta il difetto descritto senza
rompere il programma. Nessun `/tmp/nv-*` e' rimasto indietro; `/tmp` e' al 70%.

---

## GATE (eseguito per ultimo, sull'albero vero)

```
find . -name __pycache__ -exec rm -rf {} +   # poi:
bash scripts/check-extension.sh | tail -3
```

```
  PASS  test-backend.py (629 asserzioni)
TUTTO OK
EXIT_GATE=0
```

**617 -> 629 asserzioni (+12), 0 FAIL.** Nessun numero e' sceso: le 617 di base ci
sono tutte, piu' le 12 nuove. Base degli altri controlli invariata: stream-consumer,
timeout-logic (27), install-extension-dir (20), icon completeness (11).

## RISCHI RESIDUI

- `py_func_body` usa lo stesso criterio di taglio del gemello JS (`^def \w`): se un
  giorno `heartbeat()` diventasse l'ultima funzione del file, il corpo arriverebbe
  fino a fine file. Oggi non e' il caso, e il regex sulla chiamata continua a valere.
- Il docstring di `heartbeat()` resta descrittivo: se un giorno vi si aggiungesse
  codice vero, il docstring-strip lo ignorerebbe. Difetto voluto, gia' commentato.
- La rimozione di `/tmp` ha liberato ~600 M ma `/tmp` e' un tmpfs da 2 G: se altri
  worker lasciano copie indietro, il gate si rosera' di nuovo **per motivi di spazio,
  non di codice**. Da rifare prima di sospettare i test.
