#!/usr/bin/env python3
"""
engine.py — MMM Factory video assembler.

Reads script.json, produces final_video.mp4 (720p / 25fps).

Pipeline:
  per scene : Edge-TTS voice (+ word timings) -> up to SHOTS_MAX images,
              each Ken Burns rendered silent, concatenated, then muxed with
              the scene's narration audio
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

import io
import os
import sys
import json
import math
import time
import asyncio
import subprocess
import urllib.parse
import urllib.request
import urllib.error

from PIL import Image
import edge_tts

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
VOICE       = os.environ.get("VOICE", "en-US-GuyNeural")
W, H, FPS   = 1280, 720, 25
SS          = 1.5                     # supersample factor for Ken Burns
KB_W, KB_H  = int(W * SS), int(H * SS)   # 1920x1080
FETCH_W     = 1920                    # what we ask Pollinations for
FETCH_H     = 1080

CRF_SCENE, PRESET_SCENE = "18", "superfast"
CRF_FINAL, PRESET_FINAL = "20", "medium"

MUSIC_FILE  = "assets/music.mp3"      # optional; a bed is synthesised if absent
MUSIC_GAIN  = 0.20
WORDS_PER_CUE = 6
MAX_CUE_GAP   = 0.65

SHOTS_MAX     = 4                     # cap shots per scene regardless of keyword count
MIN_SHOT_SEC  = 2.5                   # never slice a shot shorter than this

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
async def synth(text, mp3_path, attempts=3):
    """
    Stream Edge-TTS. Returns [(start_s, end_s, word), ...], or [] when the
    service sends no boundary metadata (which it sometimes does not).

    Boundary offset/duration are in 100-nanosecond ticks. We match on the
    presence of offset+text rather than an exact type string, because that
    label has changed between edge-tts releases.
    """
    last = None
    for n in range(1, attempts + 1):
        words = []
        try:
            comm = edge_tts.Communicate(text, VOICE)
            with open(mp3_path, "wb") as f:
                async for chunk in comm.stream():
                    if chunk.get("type") == "audio":
                        f.write(chunk["data"])
                    elif "offset" in chunk and "text" in chunk:
                        if chunk["text"].strip():
                            start = chunk["offset"] / 10_000_000
                            dur = chunk.get("duration", 0) / 10_000_000
                            words.append((start, start + dur, chunk["text"]))
            if os.path.getsize(mp3_path) == 0:
                raise RuntimeError("empty audio file")
            return words
        except Exception as e:
            last = e
            print(f"      TTS retry {n}/{attempts} - {e}")
            await asyncio.sleep(5 * n)
    raise RuntimeError(f"Edge-TTS failed after {attempts} attempts: {last}")


def estimate_word_times(text, duration):
    """
    Fallback timing used when the TTS service returns no word boundaries.

    Spreads the measured audio duration across the words by character count
    plus punctuation pauses. Not frame-perfect, but comfortably inside the
    tolerance of a six-word caption cue.
    """
    words = text.split()
    if not words or duration <= 0:
        return []

    strip_chars = ".,;:!?-\u2014\"'()[]"
    weights = []
    for w in words:
        core = w.strip(strip_chars)
        wt = max(len(core), 1) + 1.6
        if w.endswith((",", ";", ":")):
            wt += 2.0
        if w.endswith((".", "!", "?", "\u2014")):
            wt += 3.5
        weights.append(wt)

    total = sum(weights)
    out, t = [], 0.0
    for w, wt in zip(words, weights):
        span = duration * wt / total
        out.append((t, t + span, w))
        t += span
    return out


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
    """
    Pollinations -> sanitised KB_W x KB_H RGB PNG. Never raises.

    Decodes from memory rather than a scratch file. An earlier version used a
    single shared temp path, which corrupted images the moment fetches started
    running concurrently.
    """
    prompt = urllib.parse.quote(f"{keyword}. {VISUAL_STYLE}", safe="")
    url = (f"https://image.pollinations.ai/prompt/{prompt}"
           f"?width={FETCH_W}&height={FETCH_H}&seed={seed}"
           f"&model=flux&nologo=true&enhance=false")

    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                data = r.read()
            if len(data) < 4096:
                raise RuntimeError(f"payload too small ({len(data)}B)")
            with Image.open(io.BytesIO(data)) as im:
                im = im.convert("RGB").resize(
                    (KB_W, KB_H), Image.Resampling.LANCZOS)
                im.save(out_png, "PNG")
            return True
        except urllib.error.HTTPError as e:
            # 429 = rate limited. Back off hard rather than hammering.
            wait = 30 if e.code == 429 else 5 * attempt
            print(f"      img retry {attempt}/3 - HTTP {e.code}, "
                  f"waiting {wait}s", flush=True)
            time.sleep(wait)
        except Exception as e:
            print(f"      img retry {attempt}/3 - {e}", flush=True)
            time.sleep(5 * attempt)

    print("      !! image unavailable - using fallback slate", flush=True)
    Image.new("RGB", (KB_W, KB_H), (11, 12, 16)).save(out_png, "PNG")
    return False


def plan_shots(keywords, audio_dur):
    """
    Pick up to SHOTS_MAX distinct image keywords for one scene, in the order
    the script gave them, never slicing a shot shorter than MIN_SHOT_SEC.
    """
    kws = [k.strip() for k in (keywords or []) if k and k.strip()] \
        or ["abstract dark texture"]
    n = max(1, min(len(kws), SHOTS_MAX, int(audio_dur // MIN_SHOT_SEC) or 1))
    return kws[:n]


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
    # Rate is derived from the scene length. A fixed 0.0004/frame hit zmax at
    # ~22s, so on a 35s scene the motion froze for the last 13 seconds.
    rate = (zmax - 1.0) / n

    if mode == 0:      # push in
        z, x, y = (f"min(1+{rate:.9f}*on,{zmax})",
                   "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)")
    elif mode == 1:    # pull out
        z, x, y = (f"max({zmax}-{rate:.9f}*on,1.0)",
                   "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)")
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


def render_shot(motion_idx, png, out_mp4, frames, fade_in, fade_out):
    """One still image -> one silent Ken Burns clip, `frames` frames long."""
    vf = ken_burns(motion_idx, frames)
    dur = frames / FPS

    # gentle fade at the very top and tail of the film only
    if fade_in:
        vf += ",fade=t=in:st=0:d=1.0"
    if fade_out:
        vf += f",fade=t=out:st={max(dur - 1.2, 0):.3f}:d=1.2"

    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", png,
        "-vf", vf,
        "-an",
        "-c:v", "libx264", "-preset", PRESET_SCENE, "-crf", CRF_SCENE,
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
        "-movflags", "+faststart",
        out_mp4,
    ], f"render shot {out_mp4}")


def concat_shots(shot_mp4s, listfile, out_mp4):
    """Stream-copy concat of a scene's silent shots (identical codec params)."""
    with open(listfile, "w") as f:
        for p in shot_mp4s:
            f.write(f"file '{os.path.abspath(p)}'\n")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", listfile,
         "-c", "copy", "-movflags", "+faststart", out_mp4], "concat shots")


def mux_audio(video_mp4, mp3, out_mp4):
    """Attach the scene's narration audio to an already-rendered silent clip."""
    af = ("loudnorm=I=-16:TP=-1.5:LRA=11,"
          "aresample=48000:resampler=soxr,aformat=channel_layouts=stereo")
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", video_mp4,
        "-i", mp3,
        "-filter_complex", f"[1:a]{af}[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-shortest", "-movflags", "+faststart",
        out_mp4,
    ], "mux audio")


def render_scene(scene_idx, pngs, mp3, out_mp4, work_dir, motion_start,
                  first_scene, last_scene, frames_total):
    """
    Render one scene: N still images -> N silent Ken Burns shots -> concat
    -> mux with the scene's narration audio. The scene's frame budget is
    split evenly across its shots, remainder folded into the last one.
    """
    n = len(pngs)
    base = frames_total // n
    frame_counts = [base] * n
    frame_counts[-1] += frames_total - base * n

    shots = []
    for j, (png, frames) in enumerate(zip(pngs, frame_counts)):
        shot_mp4 = os.path.join(work_dir, f"s{scene_idx:03d}_{j:02d}.mp4")
        render_shot(motion_start + j, png, shot_mp4, frames,
                    fade_in=(first_scene and j == 0),
                    fade_out=(last_scene and j == n - 1))
        shots.append(shot_mp4)

    if n == 1:
        mux_audio(shots[0], mp3, out_mp4)
        os.remove(shots[0])
        return

    listfile = os.path.join(work_dir, f"s{scene_idx:03d}_list.txt")
    silent = os.path.join(work_dir, f"s{scene_idx:03d}_silent.mp4")
    concat_shots(shots, listfile, silent)
    mux_audio(silent, mp3, out_mp4)

    for p in shots:
        os.remove(p)
    os.remove(silent)
    os.remove(listfile)


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
    """
    Burn subtitles and mix in the ducked music bed.

    If there are no subtitle cues the burn-in is skipped entirely and the
    video stream is stream-copied. libass refuses an empty .srt and takes the
    whole render down with it, which is not an acceptable way to lose a
    twenty-minute job over a caption file.
    """
    has_subs = os.path.exists(srt) and os.path.getsize(srt) > 16

    afilt = (
        f"[1:a]volume={MUSIC_GAIN},aresample=48000,"
        f"aformat=channel_layouts=stereo[m];"
        # main = music, sidechain = narration -> music dips under the voice
        f"[m][0:a]sidechaincompress="
        f"threshold=0.030:ratio=9:attack=12:release=380:makeup=1[duck];"
        f"[0:a][duck]amix=inputs=2:duration=first:normalize=0,"
        f"alimiter=limit=0.95[a]"
    )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", body,
        "-stream_loop", "-1", "-i", music,
    ]

    if has_subs:
        srt_arg = srt.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        vfilt = (f"[0:v]subtitles=filename='{srt_arg}':"
                 f"force_style='{SUB_STYLE}'[v];")
        cmd += [
            "-filter_complex", vfilt + afilt,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", PRESET_FINAL, "-crf", CRF_FINAL,
            "-pix_fmt", "yuv420p", "-r", str(FPS),
        ]
    else:
        print("   !! no subtitle cues - skipping burn-in, copying video stream")
        cmd += [
            "-filter_complex", afilt,
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy",
        ]

    cmd += [
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        out_mp4,
    ]
    run(cmd, "final composite")


# ----------------------------------------------------------------------------
async def build():
    t0 = time.time()
    os.makedirs(WORK, exist_ok=True)

    with open("script.json", encoding="utf-8") as f:
        data = json.load(f)

    scenes = [s for s in (data["scenes"] if isinstance(data, dict) else data)
              if (s.get("narration") or "").strip()]
    total = len(scenes)
    print("=" * 62, flush=True)
    print(f"  MMM ENGINE | {total} scenes | {W}x{H} @ {FPS}fps | voice={VOICE}",
          flush=True)
    print("=" * 62, flush=True)

    # ---------------- phase 1: voice (concurrent, small batches) -------------
    # Serial TTS was costing ~10s of dead network wait per scene. Batching
    # keeps us well under any sane rate limit while cutting wall time hard.
    print(f"\n[1/3] voice x{total} ...", flush=True)
    mp3s = [os.path.join(WORK, f"s{i:03d}.mp3") for i in range(total)]
    word_lists = [None] * total

    async def do_voice(i):
        word_lists[i] = await synth(scenes[i]["narration"].strip(), mp3s[i])
        print(f"      voice {i+1}/{total} ok", flush=True)

    for b in range(0, total, 3):
        await asyncio.gather(*(do_voice(i) for i in range(b, min(b + 3, total))))

    durs = [probe(m) for m in mp3s]
    real = sum(1 for w in word_lists if w)
    print(f"      audio {sum(durs)/60:.1f} min | boundaries from service: "
          f"{real}/{total}", flush=True)

    # ---------------- phase 2: images (concurrent) ---------------------------
    # Pollinations is the slowest link by far and it is pure network wait,
    # so it parallelises almost for free.
    shots_per_scene = [plan_shots(s.get("image_keywords"), durs[i])
                        for i, s in enumerate(scenes)]
    total_shots = sum(len(k) for k in shots_per_scene)

    print(f"\n[2/3] images x{total_shots} ({total} scenes) ...", flush=True)
    png_paths, work_items, gi = [], [], 0
    for i, kws in enumerate(shots_per_scene):
        paths = []
        for j, kw in enumerate(kws):
            out_png = os.path.join(WORK, f"s{i:03d}_{j:02d}.png")
            work_items.append((i, j, kw, 1000 + gi * 137, out_png))
            paths.append(out_png)
            gi += 1
        png_paths.append(paths)

    async def do_img(item):
        i, j, kw, seed, out_png = item
        ok = await asyncio.to_thread(fetch_image, kw, seed, out_png)
        print(f"      image scene {i+1} shot {j+1} {'ok' if ok else 'SLATE'}"
              f" | {kw[:44]}", flush=True)
        return ok

    results = []
    for b in range(0, len(work_items), 5):
        results += await asyncio.gather(
            *(do_img(item) for item in work_items[b:b + 5]))
    failed_images = results.count(False)

    # ---------------- phase 3: render ----------------------------------------
    print(f"\n[3/3] render x{total} scenes ({total_shots} shots) ...", flush=True)
    cues, parts, timeline, motion_cursor = [], [], 0.0, 0

    for i, sc in enumerate(scenes):
        mp4 = os.path.join(WORK, f"s{i:03d}.mp4")
        frames_total = math.ceil(durs[i] * FPS) + 3
        render_scene(i, png_paths[i], mp3s[i], mp4, WORK, motion_cursor,
                     first_scene=(i == 0), last_scene=(i == total - 1),
                     frames_total=frames_total)
        motion_cursor += len(png_paths[i])

        words = word_lists[i] or estimate_word_times(
            scenes[i]["narration"].strip(), durs[i])
        actual = probe(mp4)
        cues += group_cues(words, timeline)
        timeline += actual
        parts.append(mp4)
        print(f"      scene {i+1}/{total} [{sc.get('beat','?')}] "
              f"{len(png_paths[i])} shot(s)  {actual:.1f}s  "
              f"(+{time.time()-t0:.0f}s elapsed)", flush=True)

        for p in png_paths[i]:
            os.remove(p)
        os.remove(mp3s[i])

    if not parts:
        raise RuntimeError("no scenes rendered")

    print(f"\n> assembling {len(parts)} scenes ({timeline/60:.1f} min)...",
          flush=True)
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
        print(f"> music: {MUSIC_FILE}", flush=True)
        music = MUSIC_FILE
    else:
        print("> music: synthesising ambient bed", flush=True)
        music = os.path.join(WORK, "bed.mp3")
        music_bed(timeline, music)

    print("> final composite...", flush=True)
    finish(body, srt, music, "final_video.mp4")

    for p in parts:
        os.remove(p)
    if os.path.exists(body):
        os.remove(body)

    size = os.path.getsize("final_video.mp4") / 1_048_576
    final_dur = probe("final_video.mp4")
    print("\n" + "=" * 62, flush=True)
    print(f"DONE  final_video.mp4", flush=True)
    print(f"   duration : {final_dur/60:.1f} min", flush=True)
    print(f"   size     : {size:.1f} MB", flush=True)
    print(f"   subtitles: {len(cues)} cues", flush=True)
    if failed_images:
        print(f"   !! {failed_images} shot(s) used a fallback slate",
              flush=True)
    print(f"   build    : {(time.time()-t0)/60:.1f} min", flush=True)
    print("=" * 62, flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(build())
    except Exception as e:
        print(f"\n❌ FATAL: {e}", file=sys.stderr)
        sys.exit(1)
  
