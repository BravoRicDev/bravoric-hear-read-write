#!/usr/bin/env python3
"""Misura i livelli RMS del microfono per calibrare il VAD dello streaming.

Cattura N secondi da ffmpeg (sorgente PulseAudio "default") e stampa min/max/
media in dB, più una soglia consigliata per `[stream].noise_db`.

Uso:
    python3 scripts/mic-levels.py [--seconds 8] [--source default]

Nota: il VAD dello streaming è adattivo (stima il noise floor da solo), ma
conoscere i livelli reali aiuta a capire se il microfono è troppo basso.
"""
from __future__ import annotations

import argparse
import math
import statistics
import subprocess
import sys
import time

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
FRAME_SIZE = 480  # ~30ms
BYTES_PER_FRAME = FRAME_SIZE * BYTES_PER_SAMPLE
MARGIN_DB = 6.0


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
    parser = argparse.ArgumentParser(description="Misura livelli mic per calibrare il VAD")
    parser.add_argument("--seconds", type=float, default=8.0, help="Durata cattura (default 8)")
    parser.add_argument("--source", default="default", help="Sorgente PulseAudio (default 'default')")
    args = parser.parse_args()

    cmd = [
        "ffmpeg", "-y", "-f", "pulse", "-i", args.source,
        "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-acodec", "pcm_s16le", "-",
    ]
    print(f"Cattura {args.seconds:.0f}s da '{args.source}'... PARLA ORA se vuoi misurare la voce.")
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
        print("Nessun frame catturato: ffmpeg non ha prodotto audio (sorgente errata?)", file=sys.stderr)
        return 1

    ordered = sorted(levels)
    floor = ordered[max(0, len(ordered) // 10)]  # 10° percentile = noise floor
    recommended = max(-55.0, min(-15.0, floor + MARGIN_DB))

    print(f"\nFrame: {len(levels)}  (~{len(levels) * FRAME_SIZE / SAMPLE_RATE:.1f}s)")
    print(f"Min:  {ordered[0]:7.1f} dB")
    print(f"P10:  {floor:7.1f} dB  (noise floor stimato)")
    print(f"Med:  {statistics.median(ordered):7.1f} dB")
    print(f"P90:  {ordered[min(len(ordered) - 1, len(ordered) * 9 // 10)]:7.1f} dB")
    print(f"Max:  {ordered[-1]:7.1f} dB")
    print(f"\nSoglia VAD consigliata (P10 + {MARGIN_DB:.0f} dB): {recommended:.1f} dB")
    print("(il VAD dello streaming è adattivo: questo valore è solo diagnostico)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
