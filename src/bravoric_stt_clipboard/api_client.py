"""Client OpenAI-compatible per trascrizione, chat cleanup e vision OCR.

OpenAI-compatible client for transcription, chat cleanup and vision OCR.
"""
from __future__ import annotations

import base64
import json
import logging
import math
from pathlib import Path

import requests

from .chunk_log import redact
from .config import FallbackLevel
from .i18n import _

logger = logging.getLogger(__name__)

# Limite del campo `prompt` lato provider. Vale per TUTTE le composizioni
# del prompt ( ramo semplice e ramo con vocabolario): un prompt oltre questo
# limite viene rifiutato dal provider.
# Limit of the `prompt` field on the provider side. It applies to ALL prompt
# compositions (simple branch and vocabulary branch): a prompt over this
# limit is rejected by the provider.
PROMPT_MAX_CHARS = 800

# Frase di vocabolario: cornice fissa, il riempimento sta nel mezzo. Tenerla
# (anche ridotta) evita di degradare il prompt a un elenco nudo di termini.
# Vocabulary sentence: fixed frame, the filling goes in the middle. Keeping
# it (even shortened) avoids degrading the prompt to a bare list of terms.
_VOCAB_HEAD = "Le parole"
_VOCAB_TAIL = "sono nomi proprio."


class ApiError(RuntimeError):
    pass


def _blocks_len(*blocks: str) -> int:
    """Lunghezza esatta della stringa che verrà inviata: i blocchi sono uniti
    da UNO spazio ciascuno e quegli spazi stanno nel budget. Contarne al
    massimo uno faceva uscire il prompt di 1 carattere oltre il limite (801).

    Exact length of the string that will be sent: the blocks are joined by ONE
    space each and those spaces count in the budget. Counting at most one made
    the prompt come out 1 character over the limit (801).
    """
    return len(" ".join(b for b in blocks if b))


def _drop_oldest_words(text: str, budget: int) -> str:
    """Svuota `text` dalla TESTA (dal pezzo più vecchio) finché non sta in
    `budget`, tagliando solo su confini di parola.

    Empties `text` from the HEAD (from the oldest piece) until it fits in
    `budget`, cutting only on word boundaries.
    """
    words = text.split()
    while words and len(" ".join(words)) > budget:
        words.pop(0)
    return " ".join(words)


MAX_ERROR_BODY_CHARS = 300


def _error_body(resp, level: FallbackLevel) -> str:
    """Corpo di una risposta d'errore, sicuro da mettere in un ApiError.

    L'ApiError finisce in una notifica desktop, nel journal (fallback.py
    logga "Level %s failed: %s") e nel chunk log: un backend stile OpenAI
    ripete la chiave nel 401 ("Incorrect API key provided: sk-...") e un
    corpo HTML di errore puo' essere di migliaia di caratteri. Quindi:
    valore esatto della chiave del livello e pattern noti redatti, poi
    troncato.

    Body of an error response, safe to put in an ApiError.

    The ApiError ends up in a desktop notification, in the journal
    (fallback.py logs "Level %s failed: %s") and in the chunk log: an
    OpenAI-style backend repeats the key in the 401 ("Incorrect API key
    provided: sk-...") and an HTML error body can be thousands of characters.
    So: the level's exact key value and known patterns are redacted, then it
    is truncated.
    """
    text = str(getattr(resp, "text", "") or "")
    try:
        key = level.resolved_api_key()
    except Exception:  # noqa: BLE001 - un livello anomalo non deve mascherare l'errore HTTP | an anomalous level must not mask the HTTP error
        key = ""
    if len(key) >= 6:
        text = text.replace(key, "<redacted>")
    text = redact(text).strip()
    if len(text) > MAX_ERROR_BODY_CHARS:
        text = text[:MAX_ERROR_BODY_CHARS] + "…"
    return text


def _keep_leading_words(text: str, budget: int) -> str:
    """Tiene l'inizio di `text` (solo parole intere) finché sta in `budget`.
    Serve per il prompt personale, dove la TESTA è la parte preziosa (policy
    A5, come build_prompt che tronca a destra).

    Keeps the beginning of `text` (whole words only) while it fits in
    `budget`. Needed for the personal prompt, where the HEAD is the valuable
    part (policy A5, like build_prompt which truncates on the right).
    """
    if budget <= 0:
        return ""
    kept: list[str] = []
    used = 0
    for word in text.split():
        need = len(word) + (1 if kept else 0)
        if used + need > budget:
            break
        kept.append(word)
        used += need
    return " ".join(kept)


def _vocabulary_sentence(hotwords: str, budget: int) -> str:
    """Frase di vocabolario entro `budget` caratteri. Se la frase intera non
    entra, si accorcia la lista centrale mantenendo la cornice
    "Le parole ... sono nomi proprio."; con un budget più piccolo della
    cornice si cade al semplice taglio dalla testa della frase.

    Vocabulary sentence within `budget` characters. If the whole sentence does
    not fit, the central list is shortened while keeping the frame
    "Le parole ... sono nomi proprio."; with a budget smaller than the frame we
    fall back to a plain cut from the head of the sentence.
    """
    if budget <= 0:
        return ""
    sentence = f"{_VOCAB_HEAD} {hotwords.strip()} {_VOCAB_TAIL}"
    if len(sentence) <= budget:
        return sentence
    frame = len(_VOCAB_HEAD) + 1 + 1 + len(_VOCAB_TAIL)
    kept = _keep_leading_words(hotwords.strip(), budget - frame)
    if kept:
        return f"{_VOCAB_HEAD} {kept} {_VOCAB_TAIL}"
    # `_keep_leading_words` vuoto significa che NESSUNA parola intera sta nel
    # residuo: o il budget e' sotto la cornice, o c'e' un singolo termine senza
    # spazi piu' lungo del residuo (misurato: un hotword da 1200 caratteri con
    # budget 800). Il fallback precedente tagliava dalla TESTA la frase intera e
    # restituiva il frammento finale ("sono nomi proprio.", 18 caratteri su 800:
    # 782 sprecati e grammatica mozzata — il prompt che il provider vede diceva
    # solo quello, perche' il termine non ci stava). Ora la cornice si tiene
    # SEMPRE, e il termine viene troncato a destra sul confine di carattere:
    # la frase resta intera e grammaticale, il provider vede almeno l'inizio
    # dell'elenco invece di niente.
    # An empty `_keep_leading_words` means that NO whole word fits in the
    # remainder: either the budget is below the frame, or there is a single term
    # without spaces longer than the remainder (measured: a 1200-character
    # hotword with budget 800). The previous fallback cut the whole sentence from
    # the HEAD and returned the final fragment ("sono nomi proprio.", 18
    # characters out of 800: 782 wasted and the grammar chopped — the prompt the
    # provider saw said only that, because the term did not fit). Now the frame
    # is ALWAYS kept, and the term is truncated on the right at a character
    # boundary: the sentence stays whole and grammatical, the provider sees at
    # least the start of the list instead of nothing.
    if budget <= frame:
        # Budget sotto la cornice: non esiste una frase grammaticalmente
        # intera. Si rende il massimo disponibile tagliando dalla coda (la
        # testa "Le parole" e' la parte che rende leggibile il costrutto).
        # Budget below the frame: no grammatically whole sentence exists. We render
        # the maximum available cutting from the tail (the head "Le parole" is the
        # part that makes the construct readable).
        return _drop_oldest_words(f"{_VOCAB_HEAD} {_VOCAB_TAIL}", budget)
    words = hotwords.strip().split()
    room = budget - frame
    trimmed = words[0][:room] if words else ""
    if not trimmed:
        return _drop_oldest_words(f"{_VOCAB_HEAD} {_VOCAB_TAIL}", budget)
    return f"{_VOCAB_HEAD} {trimmed} {_VOCAB_TAIL}"


def _build_vocabulary_prompt(personal: str, context: str, hotwords: str, limit: int) -> str:
    """Compone prompt personale + contesto + frase di vocabolario entro
    `limit` caratteri, senza MAI superarlo.

    Ordine di sacrificio, fisso e documentato:
      1. contesto — è l'unico blocco già troncato per età, si perde il pezzo
         più vecchio (stessa direzione del troncamento preesistente);
      2. frase di vocabolario — si accorcia a parole intere;
      3. prompt personale — si tiene la testa a parole intere. Ultimo perché è
         l'unico pezzo scritto a mano dall'utente e non è ricostruibile dagli
         altri due. Però non può mangiare tutto il budget: al vocabolario
         viene riservata prima la cornice minima
         "Le parole ... sono nomi proprio." e, se la frase intera ci sta, tutta
         la frase — meglio 30 caratteri di istruzioni persi che una frase di
         vocabolario mozzata, che il modello leggerebbe come grammatica rotta.
    Ogni passaggio ricalcola la lunghezza esatta della stringa finale con
    `_blocks_len`, quindi la garanzia non dipende da quanti spazi si contano a
    mano: per costruzione ogni blocco sta nel suo budget e aggiunge al massimo
    uno spazio, quindi la somma resta <= limit.

    Composes personal prompt + context + vocabulary sentence within `limit`
    characters, NEVER exceeding it.

    Sacrifice order, fixed and documented:
      1. context — it is the only block already truncated by age, the oldest
         piece is lost (same direction as the pre-existing truncation);
      2. vocabulary sentence — shortened to whole words;
      3. personal prompt — the head is kept, to whole words. Last because it
         is the only piece written by hand by the user and cannot be rebuilt
         from the other two. It cannot eat the whole budget though: the
         vocabulary gets the minimal frame
         "Le parole ... sono nomi proprio." reserved first and, if the whole
         sentence fits, the whole sentence — better to lose 30 characters of
         instructions than a chopped vocabulary sentence, which the model
         would read as broken grammar.
    Every step recomputes the exact length of the final string with
    `_blocks_len`, so the guarantee does not depend on how many spaces are
    counted by hand: by construction each block fits its budget and adds at
    most one space, so the sum stays <= limit.
    """
    vocabulary = _vocabulary_sentence(hotwords, limit)
    # Il contesto aggiunge due spazi di giunzione (prima e dopo sé stesso):
    # vanno riservati insieme ai blocchi fissi.
    # The context adds two joining spaces (before and after itself): they must
    # be reserved together with the fixed blocks.
    context = _drop_oldest_words(context, limit - _blocks_len(personal, vocabulary) - 1)
    if _blocks_len(personal, context, vocabulary) <= limit:
        return " ".join(b for b in (personal, context, vocabulary) if b)

    # Qui i blocchi fissi da soli superano il limite: svuotare il contesto non
    # basta più. Si tronca invece di mandare roba fuori limite, con avviso.
    # Here the fixed blocks alone exceed the limit: emptying the context is no
    # longer enough. We truncate instead of sending out-of-limit content, with a
    # warning.
    logger.warning(
        "prompt budget: blocchi fissi oltre %d caratteri (personale %d, hotwords %d): troncamento",
        limit, len(personal), len(hotwords),
    )
    context = ""  # garantito vuoto: con il contesto presente il limite era già stato superato | guaranteed empty: with the context present the limit had already been exceeded
    vocabulary = _vocabulary_sentence(hotwords, limit - _blocks_len(personal) - 1)
    if _blocks_len(personal, vocabulary) <= limit:
        return " ".join(b for b in (personal, vocabulary) if b)

    # Il personale cede il necessario al vocabolario, tenendone la testa.
    # The personal prompt yields what the vocabulary needs, keeping its head.
    full_sentence = f"{_VOCAB_HEAD} {hotwords.strip()} {_VOCAB_TAIL}"
    floor = len(full_sentence) if len(full_sentence) <= limit else len(_VOCAB_HEAD) + 2 + len(_VOCAB_TAIL)
    personal = _keep_leading_words(personal, max(0, limit - floor - 1))
    if not personal:
        logger.warning(
            "prompt budget: prompt personale scartato, non ci sta accanto al vocabolario entro %d caratteri",
            limit,
        )
    vocabulary = _vocabulary_sentence(hotwords, limit - _blocks_len(personal) - 1)
    return " ".join(b for b in (personal, vocabulary) if b)


def _response_json(resp, level: FallbackLevel, what: str) -> dict:
    """Un corpo non-JSON o non-oggetto non deve sollevare JSONDecodeError grezzo:
    try_with_fallback cattura solo ApiError/RequestException, quindi un errore di
    forma qui impedirebbe il fallback al livello successivo.

    A non-JSON or non-object body must not raise a raw JSONDecodeError:
    try_with_fallback only catches ApiError/RequestException, so a shape error
    here would prevent falling back to the next level.
    """
    try:
        data = resp.json()
    except ValueError as exc:
        raise ApiError(f"[{level.name}] {what}: response is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ApiError(f"[{level.name}] {what}: response is not a JSON object")
    return data


def _first_message_content(data: dict, level: FallbackLevel, what: str) -> str:
    """Estrae choices[0].message.content convertendo le assenze di campo in
    ApiError (stesso motivo di _response_json).

    Extracts choices[0].message.content turning missing fields into ApiError
    (same reason as _response_json).
    """
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ApiError(f"[{level.name}] {what}: response missing choices/message/content: {exc}") from exc
    if not isinstance(content, str):
        raise ApiError(f"[{level.name}] {what}: content is not a string")
    return content


def transcribe_audio(
    level: FallbackLevel,
    audio_path: Path,
    language: str | None = None,
    prompt: str | None = None,
    hotwords: str | None = None,
    session: requests.Session | None = None,
    personal_prompt: str | None = None,
    prompt_max_chars: int = PROMPT_MAX_CHARS,
) -> str:
    url = f"{level.endpoint.rstrip('/')}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {level.resolved_api_key()}"}
    verify = level.ca_cert_path() or True
    # B12: deriva il MIME dal formato reale del file (non sempre audio/ogg).
    # B12: derive the MIME from the file's real format (not always audio/ogg).
    _MIME_MAP = {"ogg": "audio/ogg", "wav": "audio/wav", "mp3": "audio/mpeg",
                 "m4a": "audio/mp4", "flac": "audio/flac"}
    mime = _MIME_MAP.get(audio_path.suffix.lstrip("."), "audio/ogg")
    # Normalizza i campi opzionali
    data: dict = {"model": level.model}
    if language is not None and language.strip():
        data["language"] = language.strip()
    # P3: il ramo del vocabolario e' staccato dalla PRESENZA del prompt
    # personale. Prima la soglia era `prompt is not None`: con prompt=None
    # (stt.py fa `cfg.stt.prompt or None`, e i primi chunk streaming) il
    # campo `prompt` non partiva e il ramo vocabolario moriva, anche con
    # hotwords_in_prompt acceso e hotwords configurati. Ora la condizione di
    # ingresso e' il vocabolario stesso, che e' cio' che il ramo serve a
    # mandare: `hotwords_in_prompt and hotwords`. Senza questo, il default di
    # config.py basterebbe finche' prefs.js non riscrive prompt = "" (P3b),
    # e il difetto tornerebbe.
    # P3: the vocabulary branch is detached from the PRESENCE of the personal
    # prompt. Before, the threshold was `prompt is not None`: with prompt=None
    # (stt.py does `cfg.stt.prompt or None`, and so do the first streaming
    # chunks) the `prompt` field did not go out and the vocabulary branch died,
    # even with hotwords_in_prompt on and hotwords configured. Now the entry
    # condition is the vocabulary itself, which is what the branch exists to
    # send: `hotwords_in_prompt and hotwords`. Without this, the config.py
    # default would suffice only until prefs.js rewrites prompt = "" (P3b), and
    # the defect would come back.
    normalized_prompt = (prompt or "").strip()
    if getattr(level, "hotwords_in_prompt", False) is True and hotwords and hotwords.strip():
        personal = (personal_prompt or "").strip()
        context = normalized_prompt if prompt is not None else ""
        if personal and context.startswith(personal):
            context = context[len(personal):].strip()
        data["prompt"] = _build_vocabulary_prompt(personal, context, hotwords, prompt_max_chars)
    elif normalized_prompt:
        data["prompt"] = normalized_prompt[:prompt_max_chars]
    if hotwords is not None and hotwords.strip():
        data["hotwords"] = hotwords.strip()
    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
    except OSError as exc:
        raise ApiError(f"[{level.name}] transcribe: audio not readable ({audio_path}): {exc}") from exc
    try:
        client = session if session is not None else requests
        # L'unico timeout della richiesta e' level.timeout_seconds: e' il dato
        # che l'utente imposta per livello dalla GUI e deve valere. Non esiste
        # piu' alcun override: chi non ha un timeout proprio (es. i livelli
        # sintetizzati dal breaker, timeout_seconds = 0) non deve poter
        # disabilitare la richiesta, quindi si ripiega su 30.0 esattamente come
        # in stream._stop_drain_budget.
        # The only request timeout is level.timeout_seconds: it is the value the
        # user sets per level from the GUI and it must hold. There is no override
        # any more: whoever has no timeout of their own (e.g. the levels synthesized
        # by the breaker, timeout_seconds = 0) must not be able to disable the
        # request, so we fall back to 30.0 exactly as in stream._stop_drain_budget.
        try:
            timeout = float(level.timeout_seconds)
        except (TypeError, ValueError):
            timeout = 0.0
        if not math.isfinite(timeout) or timeout <= 0:
            timeout = 30.0
        resp = client.post(
            url, headers=headers, files={"file": (audio_path.name, audio_bytes, mime)},
            data=data, timeout=timeout, verify=verify,
        )
    except requests.RequestException as exc:
        raise ApiError(f"[{level.name}] transcribe: network error: {exc}") from exc
    if resp.status_code != 200:
        raise ApiError(f"[{level.name}] transcribe failed: {resp.status_code} {_error_body(resp, level)}")
    text = _response_json(resp, level, "transcribe").get("text", "")
    if not isinstance(text, str):
        return ""
    # D1: una trascrizione VUOTA non è un successo. Il backend può rispondere
    # {"text": ""} anche con un file audio pieno (silenzio, rumore, endpoint
    # che filtra). Tornare con "" faceva trattare il vuoto come successo: il
    # testo finiva a wl-copy e l'appunti veniva AZZERATO, che è la cosa più
    # dannosa che possa succedere a un utente che ha appena parlato. Il
    # chiamante distingue i due casi con ApiError, che la catena di fallback
    # sa già gestire (prova il livello successivo invece di arrendersi).
    # D1: an EMPTY transcription is not a success. The backend can answer
    # {"text": ""} even with a full audio file (silence, noise, an endpoint that
    # filters). Returning "" made the empty be treated as a success: the text
    # ended up in wl-copy and the clipboard was WIPED, which is the most harmful
    # thing that can happen to a user who has just spoken. The caller tells the
    # two cases apart with ApiError, which the fallback chain already knows how
    # to handle (it tries the next level instead of giving up).
    stripped = text.strip()
    if not stripped:
        raise ApiError(f"[{level.name}] transcribe: backend returned an empty transcription")
    return stripped


CLEANUP_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "correction",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"corrected_text": {"type": "string"}},
            "required": ["corrected_text"],
            "additionalProperties": False,
        },
    },
}


def chat_cleanup(level: FallbackLevel, system_prompt: str, text: str) -> str:
    """Chiama chat_completions con response_format json_schema stretto: senza
    vincolo di schema, i modelli chat instruction-tuned rispondono conversando
    invece di restituire solo il testo corretto (verificato su 2 modelli).

    Calls chat_completions with a strict json_schema response_format: without
    a schema constraint, instruction-tuned chat models answer conversationally
    instead of returning only the corrected text (verified on 2 models).
    """
    url = f"{level.endpoint.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {level.resolved_api_key()}",
        "Content-Type": "application/json",
    }
    verify = level.ca_cert_path() or True
    payload = {
        "model": level.model,
        "response_format": CLEANUP_JSON_SCHEMA,
        # temperature=0: il cleanup deve essere una correzione deterministica,
        # non una riscrittura creativa. Senza questo, il default del modello
        # (spesso 0.7-1.0) porta a lievi parafrasi/riformulazioni anche con
        # un prompt che le vieta esplicitamente (osservato: "Log diagnosi +
        # fix" -> "Registrati diagnosi e fix" nonostante il divieto).
        # temperature=0: the cleanup must be a deterministic correction, not a
        # creative rewrite. Without this, the model default (often 0.7-1.0) leads
        # to slight paraphrases/rewordings even with a prompt that explicitly
        # forbids them (observed: "Log diagnosi + fix" -> "Registrati diagnosi e
        # fix" despite the ban).
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=level.timeout_seconds, verify=verify)
    if resp.status_code != 200:
        raise ApiError(f"[{level.name}] chat cleanup failed: {resp.status_code} {_error_body(resp, level)}")
    content = _first_message_content(_response_json(resp, level, "chat cleanup"), level, "chat cleanup")
    try:
        corrected = json.loads(content)["corrected_text"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ApiError(f"[{level.name}] chat cleanup: malformed JSON response: {exc}") from exc
    if not isinstance(corrected, str):
        # JSON valido ma di forma sbagliata (null/numero/lista): senza questo
        # controllo un AttributeError su .strip() sfuggirebbe alla catena di
        # fallback (che cattura solo ApiError/OSError/TimeoutError) fino al top
        # level, lasciando lo status bloccato su 'processing'.
        # Valid JSON but of the wrong shape (null/number/list): without this check
        # an AttributeError on .strip() would escape the fallback chain (which only
        # catches ApiError/OSError/TimeoutError) up to the top level, leaving the
        # status stuck on 'processing'.
        raise ApiError(
            f"[{level.name}] chat cleanup: 'corrected_text' is "
            f"{type(corrected).__name__}, expected string"
        )
    return corrected.strip()


def vision_extract(level: FallbackLevel, system_prompt: str, image_bytes: bytes) -> str:
    url = f"{level.endpoint.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {level.resolved_api_key()}",
        "Content-Type": "application/json",
    }
    verify = level.ca_cert_path() or True
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": level.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _("Extract the text from this image.")},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                ],
            },
        ],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=level.timeout_seconds, verify=verify)
    if resp.status_code != 200:
        raise ApiError(f"[{level.name}] vision extract failed: {resp.status_code} {_error_body(resp, level)}")
    text = _first_message_content(_response_json(resp, level, "vision extract"), level, "vision extract")
    # D1 (lato OCR, mai chiuso finora): stessa guardia gia' applicata a
    # transcribe_audio. Un 200 con content vuoto/whitespace e' un fallimento
    # muto, non un successo: senza questa eccezione try_with_fallback lo
    # accetta come esito valido e ocr.py scrive una stringa vuota negli
    # appunti, azzerandoli, al posto di riprovare il livello successivo.
    # D1 (OCR side, never closed until now): same guard already applied to
    # transcribe_audio. A 200 with empty/whitespace content is a mute failure,
    # not a success: without this exception try_with_fallback accepts it as a
    # valid outcome and ocr.py writes an empty string to the clipboard, wiping
    # it, instead of retrying the next level.
    stripped = text.strip()
    if not stripped:
        raise ApiError(f"[{level.name}] vision extract: backend returned empty text")
    return stripped
