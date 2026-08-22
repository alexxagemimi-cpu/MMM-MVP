#!/usr/bin/env python3
"""
engine.py — MMM Factory video assembler.

Reads script.json, produces final_video.mp4 (720p / 25fps).

Pipeline:
  per scene : Edge-TTS voice (+ word timings) -> image -> Ken Burns render
  assemble  : stream-copy concat (fast, lossless)
  finish    : burn word-synced subtitles + side-chain-ducked music bed

Design notes:
  - Ken Burns runs on a 2x supersampled frame. This is what kills the
    sub-pixel jitter that makes naive zoompan look cheap.
  - The PNG is fed WITHOUT -loop 1. zoompan expands one input frame into d
    output frames. With -loop 1 you get d frames PER looped frame, which is
    the classic reason these renders come out minutes too long.
  - Every scene is encoded with identical codec parameters so the final
    concat can stream-copy instead of re-encoding.
  - Any single failed image degrades to a slate instead of killing a
    20-minute run.
"""

import os
import sys
import json
import math
import time
import shutil
import asyncio
import subprocess
import urllib.parse
import urllib.request

from PIL import Image
import edge_tts

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
VOICE       = os.environ.get("VOICE", "en-US-GuyNeural")
W, H, FPS   = 1280, 720, 25
SS          = 2                       # supersample factor for Ken Burns
KB_W, KB_H  = W * SS, H * SS          # 2560x1440
FETCH_W     = 1920                    # what we ask Pollinations for
FETCH_H     = 1080

CRF_SCENE, PRESET_SCENE = "18", "veryfast"
CRF_FINAL, PRESET_FINAL = "20", "medium"

MUSIC_FILE  = "assets/music.mp3"      # optional; a bed is synthesised if absent
MUSIC_GAIN  = 0.20
WORDS_PER_CUE = 6
MAX_CUE_GAP   = 0.65

WORK = "build"

# Applied to every image request. Keeps the whole video visually coherent.
VISUAL_STYLE = (
    "cinematic 35mm film still, anamorphic widescreen, low-key moody lighting, "
    "volumetric haze, muted desaturated teal and amber palette, shallow depth "
    "of field, fine film grain, photorealistic, highly detailed, "
    "no text, no watermark, no logo, no caption"
)

SUB_STYLE = (
    "Fontname=DejaVu Sans,Fontsize=17,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&HD0000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=2.4,Shadow=0.8,Alignment=2,MarginV=46,Spacing=0.3"
)


# ----------------------------------------------------------------------------
# SHELL HELPERS
# ----------------------------------------------------------------------------
def run(cmd, what="command"):
    """Run argv list (no shell -> no quoting bugs). Raise with real stderr."""
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        print(f"\n❌ {what} failed")
        print("   cmd: " + " ".join(cmd)[:1500])
        print("   err: " + (p.stderr or "")[-3000:])
        raise RuntimeError(what)
    return p.stdout.strip()


def probe(path):
    return float(run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path
    ], f"probe {path}"))


# ----------------------------------------------------------------------------
# 1. VOICE  (audio + real word timings, no SubMaker version roulette)
# ----------------------------------------------------------------------------
async def synth(text, mp3_path):
    """
    Stream Edge-TTS. Returns [(start_s, end_s, word), ...].

    WordBoundary offset/duration are in 100-nanosecond ticks. Reading the raw
    chunks instead of SubMaker avoids the API break between edge-tts v6 and v7.
    """
    comm = edge_tts.Communicate(text, VOICE)
    words = []
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 10_000_000
                dur = chunk["duration"] / 10_000_000
                words.append((start, start + dur, chunk["text"]))
    if os.path.getsize(mp3_path) == 0:
        raise RuntimeError("Edge-TTS produced an empty audio file")
    return words


# ----------------------------------------------------------------------------
# 2. SUBTITLES
# ----------------------------------------------------------------------------
def group_cues(words, offset):
    """Word timings -> readable caption cues, shifted onto the global timeline."""
    if not words:
        return []

    chunks, buf = [], []
    for w in words:
        if buf:
            too_long = len(buf) >= WORDS_PER_CUE
            big_gap = (w[0] - buf[-1][1]) > MAX_CUE_GAP
            sentence_end = buf[-1][2].rstrip().endswith((".", "!", "?", ":", "—"))
            if too_long or big_gap or sentence_end:
                chunks.append(buf)
                buf = []
        buf.append(w)
    if buf:
        chunks.append(buf)

    cues = []
    for c in chunks:
        start = c[0][0] + offset
        end = max(c[-1][1] + offset, start + 0.45)
        text = " ".join(x[2] for x in c).strip()
        if text:
            cues.append([start, end, text])

    # stop cues overlapping after the min-duration clamp
    for i in range(len(cues) - 1):
        if cues[i][1] > cues[i + 1][0]:
            cues[i][1] = max(cues[i][0] + 0.2, cues[i + 1][0] - 0.02)
    return cues


def ts(t):
    t = max(0.0, t)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, (a, b, txt) in enumerate(cues, 1):
            f.write(f"{i}\n{ts(a)} --> {ts(b)}\n{txt}\n\n")
    print(f"   ✓ {len(cues)} subtitle cues -> {path}")


# ----------------------------------------------------------------------------
# 3. IMAGES
# ----------------------------------------------------------------------------
def fetch_image(keyword, seed, out_png):
    """Pollinations -> sanitised KB_W x KB_H RGB PNG. Never raises."""
    prompt = urllib.parse.quote(f"{keyword}. {VISUAL_STYLE}", safe="")
    url = (f"https://image.pollinations.ai/prompt/{prompt}"
           f"?width={FETCH_W}&height={FETCH_H}&seed={seed}"
           f"&model=flux&nologo=true&enhance=false")
    tmp = os.path.join(WORK, "_dl.bin")

    for attempt in range(1, 5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=200) as r:
                data = r.read()
            if len(data) < 4096:
                raise RuntimeError(f"suspiciously small payload ({len(data)}B)")
            with open(tmp, "wb") as f:
                f.write(data)
            with Image.open(tmp) as im:
                im = im.convert("RGB").resize((KB_W, KB_H), Image.Resampling.LANCZOS)
                im.save(out_png, "PNG")
            os.remove(tmp)
            return True
        except Exception as e:
            print(f"      retry {attempt}/4 — {e}")
            time.sleep(6 * attempt)

    print("      ⚠️  image unavailable — using fallback slate")
    Image.new("RGB", (KB_W, KB_H), (11, 12, 16)).save(out_png, "PNG")
    return False


# ----------------------------------------------------------------------------
# 4. KEN BURNS
# ----------------------------------------------------------------------------
def ken_burns(idx, frames):
    """
    Deterministic motion. zoom is a linear function of `on` (output frame
    number) rather than the self-accumulating `zoom+0.001` idiom, which drifts
    and stutters. Rotating four moves stops the video feeling mechanical.
    """
    n = max(frames - 1, 1)
    mode = idx % 4
    zmax = 1.22

    if mode == 0:      # push in
        z, x, y = f"min(1+0.00040*on,{zmax})", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif mode == 1:    # pull out
        z, x, y = f"max({zmax}-0.00040*on,1.0)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif mode == 2:    # pan left -> right
        z, x, y = "1.16", f"(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"
    else:              # pan right -> left
        z, x, y = "1.16", f"(iw-iw/zoom)*(1-on/{n})", "ih/2-(ih/zoom/2)"

    return (
        f"scale={KB_W}:{KB_H}:flags=lanczos,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},"
        f"eq=contrast=1.06:saturation=0.90:gamma=0.98,"
        f"vignette=PI/5,"
        f"format=yuv420p"
    )


def render_scene(idx, png, mp3, out_mp4, first, last, audio_dur):
    frames = math.ceil(audio_dur * FPS) + 3
    vf = ken_burns(idx, frames)

    # gentle fade at the very top and tail of the film only
    if first:
        vf += ",fade=t=in:st=0:d=1.0"
    if last:
        vf += f",fade=t=out:st={max(audio_dur - 1.2, 0):.3f}:d=1.2"

    af = ("loudnorm=I=-16:TP=-1.5:LRA=11,"
          "aresample=48000:resampler=soxr,aformat=channel_layouts=stereo")

    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", png,
        "-i", mp3,
        "-filter_complex", f"[0:v]{vf}[v];[1:a]{af}[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", PRESET_SCENE, "-crf", CRF_SCENE,
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-shortest", "-movflags", "+faststart",
        out_mp4,
    ], f"render scene {idx + 1}")


# ----------------------------------------------------------------------------
# 5. MUSIC BED
# ----------------------------------------------------------------------------
def music_bed(duration, out_mp3):
    """
    Synthesise a dark ambient drone. Zero cost, zero licensing risk, and it
    never gets a Content ID claim. Drop a file at assets/music.mp3 to override.
    """
    d = duration + 2
    filt = (
        "[0:a]volume=0.55[a0];"
        "[1:a]volume=0.30,tremolo=f=0.11:d=0.55[a1];"
        "[2:a]volume=0.16,tremolo=f=0.10:d=0.40[a2];"
        "[a0][a1][a2]amix=inputs=3:normalize=0,"
        "lowpass=f=420,"
        "aecho=0.8:0.88:70|190|420:0.30|0.18|0.09,"
        f"afade=t=in:st=0:d=3,afade=t=out:st={max(d - 4, 0):.2f}:d=4,"
        "volume=0.9[out]"
    )
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=55:sample_rate=48000:duration={d}",
        "-f", "lavfi", "-i", f"sine=frequency=82.41:sample_rate=48000:duration={d}",
        "-f", "lavfi", "-i", f"sine=frequency=110:sample_rate=48000:duration={d}",
        "-filter_complex", filt,
        "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "192k",
        out_mp3,
    ], "synthesise music bed")


# ----------------------------------------------------------------------------
# 6. FINAL PASS — burn subtitles + duck music under narration
# ----------------------------------------------------------------------------
def finish(body, srt, music, out_mp4):
    srt_arg = srt.replace("\\", "/").replace(":", r"\:")

    filt = (
        f"[0:v]subtitles=filename='{srt_arg}':force_style='{SUB_STYLE}'[v];"
        f"[1:a]volume={MUSIC_GAIN},aresample=48000,"
        f"aformat=channel_layouts=stereo[m];"
        # main = music, sidechain = narration -> music dips when the voice talks
        f"[m][0:a]sidechaincompress="
        f"threshold=0.030:ratio=9:attack=12:release=380:makeup=1[duck];"
        f"[0:a][duck]amix=inputs=2:duration=first:normalize=0,"
        f"alimiter=limit=0.95[a]"
    )

    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", body,
        "-stream_loop", "-1", "-i", music,
        "-filter_complex", filt,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", PRESET_FINAL, "-crf", CRF_FINAL,
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        out_mp4,
    ], "final composite")


# ----------------------------------------------------------------------------
async def build():
    t0 = time.time()
    os.makedirs(WORK, exist_ok=True)

    with open("script.json", encoding="utf-8") as f:
        data = json.load(f)

    scenes = data["scenes"] if isinstance(data, dict) else data
    total = len(scenes)
    print("=" * 62)
    print(f"  MMM ENGINE | {total} scenes | {W}x{H} @ {FPS}fps | voice={VOICE}")
    print("=" * 62)

    cues, parts, timeline, failed_images = [], [], 0.0, 0

    for i, sc in enumerate(scenes):
        narration = (sc.get("narration") or "").strip()
        keyword = (sc.get("image_keyword") or "abstract dark texture").strip()
        if not narration:
            print(f"--- scene {i+1}/{total}: empty narration, skipping")
            continue

        print(f"\n--- scene {i+1}/{total} [{sc.get('beat','?')}] "
              f"{len(narration.split())} words")

        mp3 = os.path.join(WORK, f"s{i:03d}.mp3")
        png = os.path.join(WORK, f"s{i:03d}.png")
        mp4 = os.path.join(WORK, f"s{i:03d}.mp4")

        print("   ▸ voice...")
        words = await synth(narration, mp3)
        dur = probe(mp3)
        print(f"     {dur:.1f}s, {len(words)} word timings")

        print(f"   ▸ image: {keyword[:52]}")
        if not fetch_image(keyword, 1000 + i * 137, png):
            failed_images += 1

        print("   ▸ render...")
        render_scene(i, png, mp3, mp4, first=(i == 0), last=(i == total - 1),
                     audio_dur=dur)

        actual = probe(mp4)
        cues += group_cues(words, timeline)
        timeline += actual
        parts.append(mp4)

        # free disk on the runner as we go
        os.remove(png)
        os.remove(mp3)

    if not parts:
        raise RuntimeError("no scenes rendered")

    print(f"\n▸ assembling {len(parts)} scenes ({timeline/60:.1f} min)...")
    listfile = os.path.join(WORK, "concat.txt")
    with open(listfile, "w") as f:
        for p in parts:
            f.write(f"file '{os.path.abspath(p)}'\n")

    body = os.path.join(WORK, "body.mp4")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", listfile,
         "-c", "copy", "-movflags", "+faststart", body], "concat")

    srt = "subtitles.srt"
    write_srt(cues, srt)

    if os.path.exists(MUSIC_FILE):
        print(f"▸ music: {MUSIC_FILE}")
        music = MUSIC_FILE
    else:
        print("▸ music: synthesising ambient bed (no assets/music.mp3 found)")
        music = os.path.join(WORK, "bed.mp3")
        music_bed(timeline, music)

    print("▸ final composite (subtitles + ducked music)...")
    finish(body, srt, music, "final_video.mp4")

    for p in parts:
        os.remove(p)
    if os.path.exists(body):
        os.remove(body)

    size = os.path.getsize("final_video.mp4") / 1_048_576
    final_dur = probe("final_video.mp4")
    print("\n" + "=" * 62)
    print(f"✅ final_video.mp4")
    print(f"   duration : {final_dur/60:.1f} min ({final_dur:.0f}s)")
    print(f"   size     : {size:.1f} MB")
    print(f"   subtitles: {len(cues)} cues")
    if failed_images:
        print(f"   ⚠️  {failed_images} scene(s) fell back to a slate — "
              f"check before publishing")
    print(f"   build    : {(time.time()-t0)/60:.1f} min")
    print("=" * 62)


if __name__ == "__main__":
    try:
        asyncio.run(build())
    except Exception as e:
        print(f"\n❌ FATAL: {e}", file=sys.stderr)
        sys.exit(1)

