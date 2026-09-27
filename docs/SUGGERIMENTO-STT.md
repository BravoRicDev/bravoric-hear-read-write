# SUGGERIMENTO-STT — contesto (`prompt`) e campi per lo STT via scrocco-llm

> Guida pratica per la dettatura a chunk: **quali campi inviare**, **come costruire
> il `prompt`** ("prompt personalizzato + ultimi 2-3 chunk") e **cosa supporta
> davvero** ogni provider dietro scrocco-llm.
> Riferimenti al codice del gateway: `app/main.py:6186`, `app/forwarder.py:3266`.

---

## 0. TL;DR

- **Sì**: scrocco-llm accetta il campo `prompt` sul multipart di
  `/v1/audio/transcriptions` e lo **inoltra identico** al provider scelto.
- Il `prompt` è **l'`initial_prompt` di Whisper**: contesto *soft* (stile, nomi
  propri, vocabolario, "resto della frase"), **non** un override del contenuto.
- **Budget: 224 token.** Se più lungo, Whisper considera **solo gli ultimi 224
  token** (taglia da sinistra). Quindi: contesto sempre in **coda**.
- Oggi `api_client.transcribe_audio()` manda **solo** `file` + `model`: per usare
  il contesto va aggiunto `prompt` (e conviene aggiungere anche `language`).
- Chi è dietro: **Groq `whisper-large-v3-turbo`** e **Speaches** (faster-whisper
  locale). Entrambi supportano `prompt`.
- Il gateway **inoltra** anche `hotwords` e `vad_filter` (supportati dai server
  whisper self-hosted); li **rimuove** automaticamente per i cloud che li
  rifiutano (Groq → 400). Vedi §6.

---

## 1. Come scrocco-llm gestisce lo STT

Endpoint OpenAI-compatibili:

- `POST /v1/audio/transcriptions` — audio → testo (lingua originale)
- `POST /v1/audio/translations`   — audio → testo in inglese

Il gateway legge dal **multipart form** una **allowlist** e inoltra questi campi
al deployment con capacità `stt`:

| Campo form     | Inoltrato? | Note |
| -------------- | ---------- | ---- |
| `file`         | ✅ obblig. | il binario audio |
| `model`        | ✅ (riscritto) | il gateway rimette il modello reale del deployment |
| `language`     | ✅         | codice ISO-639-1 (`it`, `en`, …). **Consigliato sui chunk corti** |
| `prompt`       | ✅         | contesto/`initial_prompt` (questo è il punto della guida) |
| `response_format` | ✅      | `json` (default) → `{"text": ...}`; oppure `text`, `srt`, `vtt`, `verbose_json` |
| `temperature`  | ✅         | default `0.0` |
| `hotwords`     | ✅ (vedi §6) | boosting del vocabolario. Inoltrato **solo** ai whisper self-hosted (Speaches); rimosso sui cloud (Groq) |
| `vad_filter`   | ✅ (vedi §6) | idem |
| `stream`, `timestamp_granularities` | ❌ scartato | non necessari qui |

> Qualsiasi altro campo viene **ignorato in silenzio** (nessun errore).

I campi "da inviare come se li aspetta" sono quindi, in pratica:

```
file=<audio>            (obbligatorio)
model=scrocco-llm-fissone
language=it
prompt=<personalizzato + ultimi 2-3 chunk>
response_format=json
temperature=0
hotwords=<termini da spingere>   (opzionale; efficace sul whisper locale, §6)
```

---

## 2. Costruzione del `prompt` (il cuore del suggerimento)

Regola d'oro per la dettatura a chunk:

```
prompt = [PROMPT PERSONALIZZATO] + "\n" + [CODA DEGLI ULTIMI 2-3 CHUNK]
```

- **Prompt personalizzato**: fisso, dominio/stile/vocabolario. Es.
  `"Dettatura in italiano tecnico. Termini: scrocco-llm, gateway, deployment, FastAPI, chunk."`
- **Resto della frase**: il **testo trascritto degli ultimi 2-3 chunk**, così il
  modello "riprende" il filo (punteggiatura, coerenza, nomi propri già visti).
- **Tronca da sinistra**: mantieni la coda entro ~**224 token** (~**800-900
  caratteri** in italiano). Il testo più recente è il più importante.

Esempio di funzione:

```python
def build_prompt(personal: str, last_chunks: list[str], max_chars: int = 800) -> str:
    """personal + coda degli ultimi chunk, troncando da sinistra."""
    tail = " ".join(c.strip() for c in last_chunks[-3:] if c.strip())
    text = (personal.strip() + "\n" + tail).strip()
    return text[-max_chars:]  # la parte più recente resta
```

Note pratiche:

- **Chunk di poche parole**: imposta sempre `language="it"` — evita che su audio
  brevissimo la lingua venga rilevata male.
- Il `prompt` **non** obbliga il contenuto: serve a ortografia/punteggiatura/stile.
- Non incollare l'intera trascrizione: oltre 224 token viene tagliata comunque
  (tieni tu la coda, così controlli *cosa* resta).
- Utile anche per **hotword occasionali** (nomi di file, sigle): mettile nel
  prompt personalizzato. Per un boosting *forte* serve `hotwords` (§6).

---

## 3. Esempi di chiamata

### 3.1 curl (multipart)

```bash
curl -sS https://<gateway>/v1/audio/transcriptions \
  -H "Authorization: Bearer $SCROCCO_FISSONE_API_KEY" \
  -F "file=@chunk.ogg;type=audio/ogg" \
  -F "model=scrocco-llm-fissone" \
  -F "language=it" \
  -F "response_format=json" \
  -F "temperature=0" \
  -F 'prompt=Dettatura in italiano tecnico. Termini: scrocco-llm, gateway, deployment.
     ...e adesso continuiamo con la parte del routing dei deployment'
```

### 3.2 Patch consigliata a `src/bravoric_stt_clipboard/api_client.py`

`transcribe_audio` oggi manda solo `file` + `model`. Estensione additiva
(retro-compatibile) con `language` e `prompt`:

```python
def transcribe_audio(level: FallbackLevel, audio_path: Path,
                     language: str | None = None,
                     prompt: str | None = None,
                     hotwords: str | None = None) -> str:
    url = f"{level.endpoint.rstrip('/')}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {level.resolved_api_key()}"}
    verify = level.ca_cert_path() or True
    _MIME_MAP = {"ogg": "audio/ogg", "wav": "audio/wav", "mp3": "audio/mpeg",
                 "m4a": "audio/mp4", "flac": "audio/flac"}
    mime = _MIME_MAP.get(audio_path.suffix.lstrip("."), "audio/ogg")
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    data = {"model": level.model}
    if language:
        data["language"] = language          # es. "it"
    if prompt:
        data["prompt"] = prompt[-800:]       # coda (budget ~224 token)
    # hotwords: inoltrato dal gateway ai whisper self-hosted (Speaches);
    # rimosso sui cloud che non lo accettano (Groq). Vedi §6.
    if hotwords:
        data["hotwords"] = hotwords

    resp = requests.post(
        url, headers=headers,
        files={"file": (audio_path.name, audio_bytes, mime)},
        data=data, timeout=level.timeout_seconds, verify=verify,
    )
    if resp.status_code != 200:
        raise ApiError(f"[{level.name}] transcribe failed: {resp.status_code} {resp.text}")
    text = _response_json(resp, level, "transcribe").get("text", "")
    return text.strip() if isinstance(text, str) else ""
```

Uso nella modalità `per_chunk` (`stream.py`), mantenendo gli ultimi chunk:

```python
last_chunks: list[str] = []          # stato di sessione
PERSONAL = "Dettatura in italiano tecnico. Termini: scrocco-llm, gateway, deployment."

ogg = <utterance OGG>
text = try_with_fallback(
    cfg.stream_fallback,
    lambda level: transcribe_audio(
        level, ogg, language="it",
        prompt=build_prompt(PERSONAL, last_chunks),
    ),
)
if text:
    last_chunks.append(text)         # alimenta il contesto del prossimo chunk
    last_chunks[:] = last_chunks[-3:]
```

---

## 4. Cosa fa ogni provider dietro scrocco-llm

| Provider | Modello (nel CSV) | `prompt` | `language` | `hotwords`/`vad_filter` |
| -------- | ----------------- | -------- | ---------- | ----------------------- |
| **Groq** | `whisper-large-v3-turbo` | ✅ | ✅ | ✗ (li **rifiuta**: il gateway li rimuove) |
| **Speaches** (faster-whisper locale) | `Systran/faster-whisper-base` | ✅ | ✅ | ✅ (inoltrati) |

- Il routing STT sceglie uno di questi deployment (capacità `stt`), con fallback
  automatico se uno è in cooldown. Il `prompt` viaggia su entrambi.
- `hotwords`/`vad_filter` sono inoltrati **solo** ai server whisper self-hosted
  (`_stt_extras_supported`: provider/host `speaches`, `faster-whisper`,
  `whisper.cpp`, `whisper-server`). Per i cloud (Groq) vengono **rimossi** prima
  dell'inoltro, altrimenti l'upstream risponde **400 "unknown param"**.
- ⚠️ **Per usare davvero `hotwords`** devi far finire la richiesta sul whisper
  locale, non su Groq: punta il modello STT a
  **`scrocco-llm-fissone-stt-fallback`** (Speaches). Con il default
  `scrocco-llm-fissone-stt` (Groq) il campo viene scartato: nessun boosting.
- Su Speaches il `prompt` è mappato a `initial_prompt` di faster-whisper: stesso
  comportamento di Whisper (contesto soft, budget 224 token).

---

## 5. Perché aiuta sui chunk corti

Whisper su pochi decimi di secondo ha pochissimo segnale: senza contesto tende a
"tirare a indovinare" (punteggiatura sbagliata, omofoni, nomi propri storpi).
Passare gli **ultimi 2-3 chunk** gli dà il filo del discorso e la forma della
frase in corso. Il `prompt personalizzato` fissa stile e vocabolario.

Effetto tipico: maggiore stabilità di maiuscole/punteggiatura e migliore
resa dei termini tecnici. **Non** aspettarti che "inventi" parole assenti
dall'audio.

---

## 6. `hotwords` (boosting forte del vocabolario) — già supportato

`hotwords` (faster-whisper) è **più incisivo** del `prompt` per spingere termini
specifici. **Implementato nel gateway:**

- `hotwords` e `vad_filter` sono nell'**allowlist STT** (`app/main.py`).
- `forwarder.transcribe` li inoltra **solo** ai server whisper self-hosted
  (`_stt_extras_supported`: `speaches`/`faster-whisper`/`whisper.cpp`/
  `whisper-server`); per i cloud (Groq) li **rimuove** (altrimenti → 400).
- Test: `tests/test_stt_fields.py` (allowlist + gating provider).
- Deploy 6/6; verificato e2e: Groq `scrocco-llm-fissone-stt` → 200 (campi
  rimossi), Speaches `scrocco-llm-fissone-stt-fallback` → 200 (campi inoltrati).

Uso lato client: vedi lo snippet §3.2, e ricorda la nota §4 (per avere il
boosting devi far risolvere il modello STT sul whisper locale, non su Groq).

---

## 7. Gotcha

- **Ordine del contesto**: il testo più recente deve stare in **coda**; il
  troncamento di Whisper tiene gli ultimi 224 token.
- **`response_format`**: lascia `json` (il client legge `.get("text")`). Con
  `text`/`srt`/`vtt` il gateway risponde in plain text.
- **`temperature=0`**: deterministico, adatto a dettatura.
- **Chunk vuoti / silenzio**: lo STT può restituire `""` — non aggiungerlo a
  `last_chunks` (non sporca il contesto).
- **Privacy**: il `prompt` contiene testo dettato; non loggarlo per intero.

---

## 8. Riferimenti

- Gateway — allowlist STT: `app/main.py` (`_audio_transcribe`).
- Gateway — inoltro multipart + gating extras: `app/forwarder.py`
  (`transcribe`, `_stt_extras_supported`).
- App — client STT: `src/bravoric_stt_clipboard/api_client.py:43` (`transcribe_audio`).
- App — flusso STT a blocco: `src/bravoric_stt_clipboard/stt.py:75`.
- Dettatura a chunk: `ROADMAP-CHUNK.md` (§5.2 usa `transcribe_audio`).
