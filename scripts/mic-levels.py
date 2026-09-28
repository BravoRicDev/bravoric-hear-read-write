#!/usr/bin/env python3
"""Misura i livelli RMS del microfono per calibrare il VAD dello streaming.

Cattura N secondi da ffmpeg (sorgente PulseAudio "default") e stampa min/max/
media in dB, più una soglia consigliata per `[stream].noise_db`.

Uso:
    python3 scripts/mic-levels.py [--seconds 8] [--source default]

Nota: il VAD dello streaming è adattivo (stima il noise floor da solo), ma
conoscere i livelli reali aiuta a capire se il microfono è troppo basso.

Measures the microphone's RMS levels to calibrate the streaming VAD.

It captures N seconds from ffmpeg (PulseAudio source "default") and prints
min/max/mean in dB, plus a recommended threshold for `[stream].noise_db`.

Usage:
    python3 scripts/mic-levels.py [--seconds 8] [--source default]

Note: the streaming VAD is adaptive (it estimates the noise floor by
itself), but knowing the real levels helps to tell whether the microphone
is too low.
"""
from __future__ import annotations

import argparse
import math
import os
import statistics
import subprocess
import sys
import time

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
FRAME_SIZE = 480  # ~30ms
BYTES_PER_FRAME = FRAME_SIZE * BYTES_PER_SAMPLE
MARGIN_DB = 6.0

# Lingua dei messaggi: italiano se la lingua di sistema inizia per "it",
# inglese altrimenti (stessa regola di scripts/install.sh).
# Message language: Italian if the system language starts with "it",
# English otherwise (same rule as scripts/install.sh).
_LOCALE = os.environ.get("LANGUAGE") or os.environ.get("LC_ALL") \
    or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
_IT = _LOCALE.startswith("it")


def _t(italian: str, english: str) -> str:
    return italian if _IT else english


def _rms_db(frame: bytes) -> float:
    n = len(frame) // BYTES_PER_SAMPLE
    if n == 0:
        return -200.0
    total = 0
    for i in range(n):
        sample = int.from_bytes(frame[i * 2:i * 2 + 2], "little", signed=True)
        total += sample * sample
    rms = math.sqrt(total / n) / 32768.0
    return 20.0 * math.log10(rms) if rms > 0 else -200.0


def main() -> int:
    parser = argparse.ArgumentParser(description=_t("Misura livelli mic per calibrare il VAD", "Measure mic levels to calibrate the VAD"))
    parser.add_argument("--seconds", type=float, default=8.0, help=_t("Durata cattura (default 8)", "Capture length in seconds (default 8)"))
    parser.add_argument("--source", default="default", help=_t("Sorgente PulseAudio (default 'default')", "PulseAudio source (default 'default')"))
    args = parser.parse_args()

    cmd = [
        "ffmpeg", "-y", "-f", "pulse", "-i", args.source,
        "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-acodec", "pcm_s16le", "-",
    ]
    print(_t(f"Cattura {args.seconds:.0f}s da '{args.source}'... PARLA ORA se vuoi misurare la voce.",
             f"Capturing {args.seconds:.0f}s from '{args.source}'... SPEAK NOW if you want to measure your voice."))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    levels: list[float] = []
    t0 = time.time()
    try:
        while time.time() - t0 < args.seconds:
            frame = proc.stdout.read(BYTES_PER_FRAME)
            if not frame:
                break
            levels.append(_rms_db(frame))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    if not levels:
        print(_t("Nessun frame catturato: ffmpeg non ha prodotto audio (sorgente errata?)",
                 "No frame captured: ffmpeg produced no audio (wrong source?)"), file=sys.stderr)
        return 1

    ordered = sorted(levels)
    floor = ordered[max(0, len(ordered) // 10)]  # 10° percentile = noise floor
    recommended = max(-55.0, min(-15.0, floor + MARGIN_DB))

    print(f"\n{_t('Frame', 'Frames')}: {len(levels)}  (~{len(levels) * FRAME_SIZE / SAMPLE_RATE:.1f}s)")
    print(f"Min:  {ordered[0]:7.1f} dB")
    print(f"P10:  {floor:7.1f} dB  ({_t('noise floor stimato', 'estimated noise floor')})")
    print(f"Med:  {statistics.median(ordered):7.1f} dB")
    print(f"P90:  {ordered[min(len(ordered) - 1, len(ordered) * 9 // 10)]:7.1f} dB")
    print(f"Max:  {ordered[-1]:7.1f} dB")
    print(_t(f"\nSoglia VAD consigliata (P10 + {MARGIN_DB:.0f} dB): {recommended:.1f} dB",
             f"\nRecommended VAD threshold (P10 + {MARGIN_DB:.0f} dB): {recommended:.1f} dB"))
    print(_t("(il VAD dello streaming è adattivo: questo valore è solo diagnostico)",
             "(the streaming VAD is adaptive: this value is diagnostic only)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
