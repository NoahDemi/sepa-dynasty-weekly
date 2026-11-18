"""Placeholder intro/outro music and cover art, generated locally (so they are royalty-free).

Swap in your own files by replacing assets/intro.wav, assets/outro.wav, assets/cover.jpg,
or by pointing config.yaml at different paths. Run `python -m podcast.assets` to regenerate.
"""
from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path

from .config import ROOT

RATE = 44100
NOTE = {"C": 261.63, "D": 293.66, "E": 329.63, "F": 349.23, "G": 392.00, "A": 440.00, "B": 493.88}


def _freq(name: str) -> float:
    return NOTE[name[0]] * 2 ** (int(name[1]) - 4)


def _add(buf, start: float, samples) -> None:
    i0 = int(start * RATE)
    for i, s in enumerate(samples):
        if i0 + i < len(buf):
            buf[i0 + i] += s


def _pluck(freq: float, dur: float, vol: float):
    n = int(dur * RATE)
    for i in range(n):
        t = i / RATE
        env = min(1, t / 0.005) * math.exp(-3.5 * t / dur)
        yield vol * env * (math.sin(2 * math.pi * freq * t) + 0.35 * math.sin(4 * math.pi * freq * t)
                           + 0.15 * math.sin(6 * math.pi * freq * t))


def _kick(vol: float = 0.9):
    for i in range(int(0.22 * RATE)):
        t = i / RATE
        yield vol * math.exp(-18 * t) * math.sin(2 * math.pi * (50 + 90 * math.exp(-30 * t)) * t)


def _hat(vol: float = 0.12):
    rnd = random.Random(7)
    for i in range(int(0.05 * RATE)):
        yield vol * math.exp(-90 * i / RATE) * rnd.uniform(-1, 1)


def make_jingle(path: Path, bars: int, bpm: int = 112, seed_chords=None) -> None:
    """A simple upbeat four-chord groove: kick, hats, bass, and plucked chords."""
    chords = seed_chords or [("C3", ["C4", "E4", "G4"]), ("A2", ["C4", "E4", "A4"]),
                             ("F2", ["C4", "F4", "A4"]), ("G2", ["D4", "G4", "B4"])]
    beat = 60 / bpm
    total = bars * 4 * beat + 1.5
    buf = [0.0] * int(total * RATE)
    for bar in range(bars):
        bass, notes = chords[bar % len(chords)]
        t0 = bar * 4 * beat
        for b in range(4):
            _add(buf, t0 + b * beat, _kick())
            _add(buf, t0 + (b + 0.5) * beat, _hat())
            _add(buf, t0 + b * beat, _pluck(_freq(bass), beat * 0.9, 0.35))
        for k, off in enumerate([0, 0.75, 1.5, 2.5, 3.0, 3.5]):  # syncopated chord stabs
            for n in notes:
                _add(buf, t0 + off * beat, _pluck(_freq(n), beat * 1.2, 0.14))
            if k % 2 == 0:
                _add(buf, t0 + off * beat, _pluck(_freq(notes[-1]) * 2, beat * 0.8, 0.08))
    peak = max(abs(s) for s in buf) or 1
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(32767 * 0.8 * s / peak)) for s in buf))


def make_cover(path: Path, title: str, tagline: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    size = 1400
    img = Image.new("RGB", (size, size), "#0b3d2e")
    d = ImageDraw.Draw(img)
    for y in range(0, size, 140):  # yard lines
        d.line([(0, y), (size, y)], fill="#145c45", width=6)
    d.rectangle([60, 60, size - 60, size - 60], outline="#f4f1e8", width=10)

    def font(px: int):
        for f in ("/System/Library/Fonts/Supplemental/Arial Black.ttf",
                  "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            if Path(f).exists():
                return ImageFont.truetype(f, px)
        return ImageFont.load_default(px)

    words = title.upper().split()
    y = 300
    for word in words:
        px = 250
        while d.textlength(word, font=font(px)) > size - 240:
            px -= 10
        f = font(px)
        d.text((size / 2, y), word, font=f, fill="#f4f1e8", anchor="mm")
        y += px + 30
    d.rectangle([180, y - 20, size - 180, y - 8], fill="#e9b44c")
    d.text((size / 2, y + 70), "DYNASTY FANTASY FOOTBALL", font=font(58), fill="#e9b44c", anchor="mm")
    img.save(path, "JPEG", quality=90)


def ensure_assets(cfg: dict) -> None:
    a = cfg["audio"]
    intro, outro, cover = (ROOT / a[k] for k in ("intro_music", "outro_music", "cover"))
    if not intro.exists():
        make_jingle(intro, bars=6)
    if not outro.exists():
        make_jingle(outro, bars=8, seed_chords=[("F2", ["C4", "F4", "A4"]), ("G2", ["D4", "G4", "B4"]),
                                                ("A2", ["C4", "E4", "A4"]), ("C3", ["C4", "E4", "G4"])])
    if not cover.exists():
        make_cover(cover, cfg["show"]["name"], cfg["show"]["tagline"])


if __name__ == "__main__":
    from .config import load_config
    ensure_assets(load_config())
    print("assets ready in", ROOT / "assets")
