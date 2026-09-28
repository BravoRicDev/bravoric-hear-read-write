# Icone mancanti

Stato verificato dal codice (`notify._PACKAGED_DEFAULTS` + `config.ICON_SLOT_REGISTRY`).
Ogni notifica del backend ha uno **slot icona** configurabile da GUI
(*Icone → Custom notification icons*). Uno slot senza immagine inclusa nel
pacchetto ricade su un'**icona tema GNOME generica** finché l'utente non ne
sceglie una.

## Slot senza immagine inclusa

| Slot | Quando compare | Fallback attuale (tema) | File da creare |
|---|---|---|---|
| `stt_recording_start` | "STT: Registrazione avviata" (microfono aperto) | `media-record-symbolic` | `stt-recording-start.png` |
| `stream_session_end` | "Streaming dictation — Sessione terminata" | `edit-copy-symbolic` | `stream-session-end.png` |
| `stream_chunk_delivered` | "Streaming dictation" con il testo trascritto consegnato | `edit-copy-symbolic` | `stream-chunk-delivered.png` |
| `stream_processing_start` | "Streaming dictation — Trascrizione in corso…" (modalità *at end*) | `content-loading-symbolic` | `stream-processing-start.png` |

Tutti gli slot sono personalizzabili da GUI (*Icone*). "Trascrizione in corso…"
dello streaming *at end* ha ora il suo slot (`stream_processing_start`,
"Stream — Transcribing").

Slot già coperti (nessuna azione): `stt_start`, `stt_raw`, `stt_clean`,
`ocr_start`, `ocr_raw`, `ocr_clean`, `stream_session_start`, `error_general`.

## Specifiche dei file esistenti (da imitare)

- PNG **128×128, RGBA** (sfondo trasparente), in `src/bravoric_stt_clipboard/icons/`.
- Famiglie: `mic-*` (STT) e `camera-*` (OCR) in tre stili — *neutral* (avvio),
  *wood* (testo grezzo), *cyberpunk* (testo pulito). `stream-session-start.png`
  e `error-general.png` sono singole.
- Sorgenti (base + varianti) in `assets/icons-source/`.

## Come agganciare un file nuovo

1. Copia il PNG in `src/bravoric_stt_clipboard/icons/` (e il sorgente in
   `assets/icons-source/`).
2. In `src/bravoric_stt_clipboard/notify.py`, aggiungi la voce a
   `_PACKAGED_DEFAULTS`, ad esempio:
   `"stream_session_end": "stream-session-end.png",`
3. Verifica: `scripts/check-extension.sh` (il pacchetto include già
   `icons/*.png` via `package-data` in `pyproject.toml`).

Nessun'altra modifica di codice serve: lo slot, la riga nella GUI e la
notifica esistono già.
