#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Whisper locale come endpoint OpenAI-compatible su 127.0.0.1.

Espone `/v1/audio/transcriptions` con lo stesso formato di OpenAI usando
faster-whisper. Serve a usare un modello STT locale (veloce, offline, in
italiano) senza modificare il codice del plugin: basta puntare il fallback
`[[stream.fallback]]` (o `[[stt.fallback]]`) a questo endpoint.

Dipendenze (separate da quelle del plugin, vedi bin/whisper-requirements.txt):
    pip install faster-whisper fastapi uvicorn

Uso:
    python3 bin/whisper-server.py [--model small] [--host 127.0.0.1] [--port 8080]

Endpoint:
    POST /v1/audio/transcriptions   multipart: file=<audio>, model=<ignorato>
    GET  /v1/models                 lista minimale (compatibilità client)
    GET  /health                    stato e modello caricato

Local Whisper as an OpenAI-compatible endpoint on 127.0.0.1.

It exposes `/v1/audio/transcriptions` with the same format as OpenAI using
faster-whisper. It serves to use a local STT model (fast, offline, in
Italian) without changing the plugin code: just point the
`[[stream.fallback]]` (or `[[stt.fallback]]`) fallback to this endpoint.

Dependencies (separate from the plugin's, see bin/whisper-requirements.txt):
    pip install faster-whisper fastapi uvicorn

Usage:
    python3 bin/whisper-server.py [--model small] [--host 127.0.0.1] [--port 8080]

Endpoints:
    POST /v1/audio/transcriptions   multipart: file=<audio>, model=<ignored>
    GET  /v1/models                 minimal list (client compatibility)
    GET  /health                    status and loaded model
"""
from __future__ import annotations

import argparse
import contextlib
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel

DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "small")
DEFAULT_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "it")
DEFAULT_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")

_model: WhisperModel | None = None
_model_name: str = DEFAULT_MODEL
_language: str = DEFAULT_LANGUAGE


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    global _model
    print(f"[whisper-server] Loading model '{_model_name}' (compute={DEFAULT_COMPUTE}) ...", flush=True)
    t0 = time.time()
    _model = WhisperModel(_model_name, device="cpu", compute_type=DEFAULT_COMPUTE)
    print(f"[whisper-server] Model '{_model_name}' ready in {time.time()-t0:.1f}s", flush=True)
    yield
    _model = None


app = FastAPI(title="Whisper Local", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok" if _model is not None else "loading",
        "model": _model_name,
        "language": _language,
    }


@app.get("/v1/models")
def list_models() -> dict:
    return {"object": "list", "data": [{"id": _model_name, "object": "model"}]}


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: str | None = Form(None),  # noqa: ARG001 — accettato per compatibilità OpenAI | accepted for OpenAI compatibility
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    hotwords: str | None = Form(None),
    response_format: str | None = Form(None),  # noqa: ARG001
) -> JSONResponse:
    whisper = _model
    if whisper is None:
        raise HTTPException(status_code=503, detail="model not loaded")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio file")

    ext = Path(file.filename).suffix if file.filename else ".wav"
    fd, tmp_name = tempfile.mkstemp(suffix=ext)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        lang = language or _language
        # faster-whisper supporta sia initial_prompt (contesto di decoding) sia
        # hotwords (bias verso parole specifiche) come parametri di transcribe.
        # Li passiamo solo se valorizzati, per non alterare il comportamento
        # di default quando il client non li invia.
        # faster-whisper supports both initial_prompt (decoding context) and
        # hotwords (bias towards specific words) as transcribe parameters. We pass
        # them only if set, so as not to alter the default behavior when the client
        # does not send them.
        transcribe_kwargs: dict = {
            "language": lang,
            "beam_size": 5,
            "vad_filter": True,
            "vad_parameters": {
                "min_silence_duration_ms": 500,
                "threshold": 0.5,
            },
            "condition_on_previous_text": False,
        }
        if prompt and prompt.strip():
            transcribe_kwargs["initial_prompt"] = prompt.strip()
        if hotwords and hotwords.strip():
            transcribe_kwargs["hotwords"] = hotwords.strip()
        try:
            segments, info = whisper.transcribe(str(tmp_path), **transcribe_kwargs)
            text = " ".join(s.text for s in segments).strip()
        except Exception as exc:  # audio illeggibile/corrotto → 400, non 500 | unreadable/corrupt audio → 400, not 500
            raise HTTPException(status_code=400, detail=f"transcription failed: {exc}") from exc
        return JSONResponse({
            "text": text,
            "language": getattr(info, "language", lang),
            "duration": getattr(info, "duration", 0.0),
        })
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)


def main() -> None:
    global _model_name, _language
    parser = argparse.ArgumentParser(description="Whisper local OpenAI-compatible server")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Whisper model size (tiny/base/small/medium/large-v3)")
    parser.add_argument("--language", default=DEFAULT_LANGUAGE, help="Lingua di trascrizione")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8080, help="Bind port")
    args = parser.parse_args()
    _model_name = args.model
    _language = args.language
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
