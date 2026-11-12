#!/usr/bin/env python3
"""Variants of the original two-note dictation chime, written to variants/<name>-start.wav and -stop.wav.

All keep the original idea (two quick notes, rising to start, falling to stop) and change one thing each:
  a-smooth  the original notes, overlapping slightly so they ring into each other, plus a tiny room
  b-glass   a bell-like partial and a longer, softer ring
  c-warm    an octave lower, rounder (marimba-ish), less piercing
  d-airy    three notes, slightly detuned for a shimmer, quickest of the set
Utter ships c-warm. To change the chime, run this, pick one, and copy it over sounds/start.wav and sounds/stop.wav.
"""
import array, math, os, wave

RATE, VOLUME = 44100, 0.18
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "variants")


def tone(freq, ms, partials, decay, attack_ms=4.0, detune=0.0):
    """partials: [(ratio, amp)]; detune adds a second copy a few cents sharp for a gentle shimmer."""
    n = int(RATE * ms / 1000)
    out = [0.0] * n
    for i in range(n):
        t = i / RATE
        env = min(1.0, i / (RATE * attack_ms / 1000)) * math.exp(-t * decay)
        v = 0.0
        for ratio, amp in partials:
            v += amp * math.sin(2 * math.pi * freq * ratio * t)
            if detune:
                v += amp * 0.5 * math.sin(2 * math.pi * freq * ratio * (1 + detune) * t)
        out[i] = env * v
    return out


def sequence(notes, step_ms, **kw):
    """Notes start every step_ms; each rings for kw['ms'], so a step shorter than ms overlaps them."""
    ms = kw.pop("ms")
    total = int(RATE * (step_ms * (len(notes) - 1) + ms) / 1000)
    mix = [0.0] * total
    for k, f in enumerate(notes):
        start = int(RATE * step_ms * k / 1000)
        for i, x in enumerate(tone(f, ms, **kw)):
            mix[start + i] += x
    return mix


def room(samples, delays_ms=(23, 37, 53), gain=0.18):
    """A very small, short reverb: a few quiet echoes, so the chime sounds less 'synthetic'."""
    pad = int(RATE * 0.12)
    out = samples + [0.0] * pad
    for d in delays_ms:
        off = int(RATE * d / 1000)
        for i, x in enumerate(samples):
            out[i + off] += x * gain
        gain *= 0.7
    return out


def write(name, samples):
    # fade the last 15 ms so nothing clicks at the end
    fade = int(RATE * 0.015)
    for i in range(fade):
        samples[-1 - i] *= i / fade
    peak = max(abs(x) for x in samples) or 1.0
    pcm = array.array("h", (int(x / peak * VOLUME * 32767) for x in samples))
    with wave.open(os.path.join(OUT, name), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(pcm.tobytes())


A5, E6, A4, E5, C6, G6 = 880.0, 1318.5, 440.0, 659.25, 1046.5, 1568.0
E6_ = 1318.5

VARIANTS = {
    "a-smooth": dict(up=[A5, E6], step=65, ms=150, partials=[(1, 1), (2, 0.2)], decay=22, reverb=True),
    "b-glass":  dict(up=[A5, E6], step=80, ms=260, partials=[(1, 1), (2.76, 0.12), (5.4, 0.04)], decay=14,
                     reverb=True),
    "c-warm":   dict(up=[A4, E5], step=75, ms=180, partials=[(1, 1), (4, 0.15), (10, 0.03)], decay=24,
                     attack_ms=2.5, reverb=True),
    "d-airy":   dict(up=[C6, E6_, G6], step=45, ms=140, partials=[(1, 1), (2, 0.12)], decay=26, detune=0.004,
                     reverb=True),
}

if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for name, v in VARIANTS.items():
        kw = dict(ms=v["ms"], partials=v["partials"], decay=v["decay"])
        for k in ("attack_ms", "detune"):
            if k in v:
                kw[k] = v[k]
        for cue, notes in (("start", v["up"]), ("stop", list(reversed(v["up"])))):
            s = sequence(notes, v["step"], **dict(kw))
            write(f"{name}-{cue}.wav", room(s) if v["reverb"] else s)
    print("\n".join(sorted(os.listdir(OUT))))
