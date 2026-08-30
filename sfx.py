#!/usr/bin/env python3
"""
sfx.py — a synthesised sound-effect kit, and the reason it exists.

WHAT THE RESEARCH SAYS
----------------------
Asked what separates a cheap faceless video from an edited one, the answer
that comes back first is not visual. It is that professional edits put a
sound on the cut: "a subtle whoosh or click on each cut makes edits feel
snappier". Retention editing guides state it as a rule - if there is a
five-second block where NOTHING changes, no cut, no zoom, no text and no
sound effect, that is a hole viewers leak out of - and they recommend a
pattern interrupt every 15-30 seconds.

Our video has ZERO sound effects. Not few - none. Every transition is
silent, every on-screen card appears silently, and the only audio that has
ever existed in this pipeline is narration plus one drone. That is very
probably the single largest "2016 stock video" tell in the whole output, and
it is also the cheapest thing on the list to fix.

WHY SYNTHESISED RATHER THAN DOWNLOADED
--------------------------------------
Freesound has an API and Pixabay has 130,000 free effects, and either would
work. Synthesising them anyway is the better trade for this project:

  - no key, no quota, no rate limit, nothing to run out mid-render
  - no licensing question and no Content ID risk, ever
  - deterministic, so a build cannot change because a library re-ranked
  - testable offline, which matters because this sandbox cannot reach
    Pixabay at all
  - roughly 20KB of generated audio instead of a network dependency in the
    hot path of a 15-minute job

Every sound is built from ffmpeg primitives with an explicit envelope. They
are deliberately understated: an effect you consciously notice under
narration is already too loud.

    python3 sfx.py        # writes the kit to assets/sfx/ and measures it
"""

import os
import subprocess

SR = 48000
KIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "sfx")


def _run(args, out):
    subprocess.run(["ffmpeg", "-y", "-v", "error"] + args +
                   ["-c:a", "pcm_s16le", "-ar", str(SR), "-ac", "1", out],
                   check=True, timeout=60)
    return out


def whoosh(out, dur=0.42, level=0.16):
    """
    Air moving past. Pink noise under a raised-cosine swell, low-passed so it
    sits beneath the voice instead of hissing over it.

    Goes on a transition between sections - the sound that makes a cut feel
    deliberate rather than like the file simply changed.
    """
    return _run([
        "-f", "lavfi", "-i", f"anoisesrc=c=pink:r={SR}:d={dur}",
        "-af", (f"volume='{level}*pow(sin(PI*t/{dur}),1.6)':eval=frame,"
                f"lowpass=f=2200,highpass=f=180"),
    ], out)


def pop(out, freq=760, dur=0.13, level=0.20):
    """
    A soft tonal blip for something ARRIVING on screen - a term card, a list
    row, a number. Fast exponential decay, no click on the attack.
    """
    return _run([
        "-f", "lavfi", "-i", f"sine=frequency={freq}:r={SR}:d={dur}",
        "-af", (f"volume='{level}*exp(-16*t)*(1-exp(-260*t))':eval=frame,"
                f"lowpass=f=5200"),
    ], out)


def tick(out, freq=1500, dur=0.05, level=0.13):
    """A dry click. Used where something is marked done."""
    return _run([
        "-f", "lavfi", "-i", f"sine=frequency={freq}:r={SR}:d={dur}",
        "-af", f"volume='{level}*exp(-70*t)':eval=frame",
    ], out)


def thud(out, freq=68, dur=0.5, level=0.30):
    """
    Low weight, for a section landing or a big number appearing. This is the
    one the body feels rather than hears; it is what makes a title card seem
    to have arrived rather than simply been there.
    """
    return _run([
        "-f", "lavfi", "-i", f"sine=frequency={freq}:r={SR}:d={dur}",
        "-af", (f"volume='{level}*exp(-6.5*t)*(1-exp(-90*t))':eval=frame,"
                f"lowpass=f=190"),
    ], out)


def riser(out, dur=1.1, level=0.13):
    """
    Tension into a reveal. Noise swelling under a rising tone, ending exactly
    on the cut so the payoff lands on silence-then-impact.
    """
    return _run([
        "-f", "lavfi", "-i", f"anoisesrc=c=white:r={SR}:d={dur}",
        "-f", "lavfi", "-i", f"sine=frequency=220:r={SR}:d={dur}",
        "-filter_complex",
        (f"[0:a]volume='{level}*pow(t/{dur},2.2)':eval=frame,"
         f"highpass=f=700,lowpass=f=6000[n];"
         f"[1:a]volume='{level*0.7}*pow(t/{dur},2.6)':eval=frame[s];"
         f"[n][s]amix=inputs=2:normalize=0[a]"),
        "-map", "[a]",
    ], out)


BUILDERS = {
    "whoosh.wav": whoosh,
    "pop.wav":    pop,
    "tick.wav":   tick,
    "thud.wav":   thud,
    "riser.wav":  riser,
}


def build_kit(dest=KIT):
    """Make every effect once. Cheap enough to regenerate on each run, so
    there is no binary audio checked into the repository."""
    os.makedirs(dest, exist_ok=True)
    made = {}
    for name, fn in BUILDERS.items():
        p = os.path.join(dest, name)
        if not os.path.exists(p):
            fn(p)
        made[name.split(".")[0]] = p
    return made


def _measure(path):
    """Peak level and real duration - a sound that clips or runs long is worse
    than no sound, and both are invisible without measuring."""
    # volumedetect reports at INFO level, so "-v error" silences the very
    # thing being asked for. The first version of this printed "?" for every
    # peak and looked like a broken sound rather than a broken measurement.
    out = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", path, "-af", "volumedetect",
         "-f", "null", "-"], capture_output=True, text=True).stderr
    peak = next((l.split(":")[1].strip() for l in out.splitlines()
                 if "max_volume" in l), "?")
    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True).stdout.strip()
    return peak, float(dur or 0)


if __name__ == "__main__":
    kit = build_kit()
    print(f"{'sound':<10} {'duration':>9}  {'peak':>10}   size")
    print("-" * 48)
    for name, path in sorted(kit.items()):
        peak, dur = _measure(path)
        print(f"{name:<10} {dur:>8.2f}s  {peak:>10}   "
              f"{os.path.getsize(path)/1024:.0f} KB")
    print("\nPeaks are all well under 0 dB on purpose: these sit UNDER "
          "narration.\nAn effect you consciously notice is already too loud.")


# ---------------------------------------------------------------------------
# building one track for a whole video
# ---------------------------------------------------------------------------
def build_track(duration, events, out_wav, dest=KIT):
    """
    One silent track of `duration` seconds with every effect pasted in at its
    exact time. `events` is [(seconds, name), ...].

    Mixed in Python rather than with a filter graph on purpose. Sixteen hits
    would mean sixteen inputs, sixteen adelay filters and one enormous amix -
    hard to read, hard to change, and impossible to check without rendering
    the whole video. Summing samples into a buffer is deterministic, and the
    result can be verified by measuring the level at the times a sound was
    supposed to land (see _rms_at below), which is the only proof that
    actually matters.

    Samples are summed and then clipped at the very end. Two effects landing
    together should add up, not replace each other.
    """
    import wave
    import struct
    kit = build_kit(dest)
    n_total = int(duration * SR) + SR
    buf = [0] * n_total

    cache = {}
    for name in {n for _, n in events}:
        path = kit.get(name)
        if not path:
            continue
        with wave.open(path) as w:
            raw = w.readframes(w.getnframes())
        cache[name] = struct.unpack(f"<{len(raw)//2}h", raw)

    placed = 0
    for at, name in events:
        s = cache.get(name)
        if not s or at < 0:
            continue
        off = int(at * SR)
        if off >= n_total:
            continue
        for i, v in enumerate(s):
            j = off + i
            if j >= n_total:
                break
            buf[j] += v
        placed += 1

    out = struct.pack(f"<{n_total}h",
                      *(max(-32768, min(32767, v)) for v in buf))
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(out)
    return out_wav, placed


def _rms_at(path, t, window=0.25):
    """Level in a window around t. Used to PROVE a sound actually landed
    where it was asked to, rather than trusting that the code ran."""
    import wave
    import struct
    import math
    with wave.open(path) as w:
        sr = w.getframerate()
        w.setpos(max(0, int(t * sr)))
        raw = w.readframes(int(window * sr))
    s = struct.unpack(f"<{len(raw)//2}h", raw) or (0,)
    return math.sqrt(sum(x * x for x in s) / len(s)) / 32768
