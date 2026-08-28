#!/usr/bin/env python3
"""
engine.py — MMM Factory video assembler.

Reads script.json, produces final_video.mp4 (720p / 25fps).

Pipeline:
  per scene : Edge-TTS voice (+ word timings) -> up to SHOTS_MAX visuals,
              each a real Pixabay stock clip/photo when one matches the
              keyword, else an AI Ken Burns image, rendered silent,
              concatenated, then muxed with the scene's narration audio
  assemble  : stream-copy concat (fast, lossless)
  finish    : burn word-synced subtitles + side-chain-ducked music bed

Visual source order, per shot (all free, no budget):
  Pixabay video -> Pixabay photo -> Pollinations AI image -> flat slate.
  PIXABAY_API_KEY is optional; if unset, every shot goes straight to the
  AI image / slate fallback, same as before this feature existed.

Design notes:
  - Ken Burns runs on a 2x supersampled frame. This is what kills the
    sub-pixel jitter that makes naive zoompan look cheap. Real Pixabay
    footage already has motion, so it skips Ken Burns and is instead
    scaled/cropped to fill the frame and trimmed (looped if too short) to
    the shot's exact duration.
  - The PNG is fed WITHOUT -loop 1. zoompan expands one input frame into d
    output frames. With -loop 1 you get d frames PER looped frame, which is
    the classic reason these renders come out minutes too long.
  - Every shot is encoded with identical codec parameters so scenes (and
    the final video) can stream-copy concat instead of re-encoding.
  - Any single failed visual degrades down the fallback chain to a slate
    instead of killing a 20-minute run.
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
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "").strip()
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

# No ffmpeg/ffprobe call may outlive these. See run() for why this matters.
#
# These are sized against the JOB cap, not picked for comfort. A flat 240s
# per shot looks safe until you multiply: 12 shots x 240s = 48 minutes, past
# the 45-minute workflow limit, so a pathological run would still die by
# timeout - just with extra steps. A shot's budget therefore scales with the
# length it has to produce (see shot_timeout) and stays well under that.
CMD_TIMEOUT   = 120                   # generic ffmpeg call ceiling
PROBE_TIMEOUT = 30
FINAL_TIMEOUT = 1800                  # whole-video composite, legitimately long
MAX_LOOPS     = 40                    # cap on finite -stream_loop repeats


def shot_timeout(seconds):
    """
    Encode budget for one shot of `seconds` output.

    superfast/CRF18 720p runs far faster than real time even on a 2-vCPU
    runner, so 6x the output length plus a fixed startup allowance is
    generous while still bounding a full render to a few minutes.
    """
    return int(min(max(20 + seconds * 6, 45), 150))

WORK = "build"

# Applied to every image request. Keeps the whole video visually coherent.
VISUAL_STYLE = (
    "cinematic 35mm film still, anamorphic widescreen, low-key moody lighting, "
    "volumetric haze, muted desaturated teal and amber palette, shallow depth "
    "of field, fine film grain, photorealistic, highly detailed, "
    "no text, no watermark, no logo, no caption"
)

# ----------------------------------------------------------------------------
# SHELL HELPERS
# ----------------------------------------------------------------------------
def run(cmd, what="command", timeout=CMD_TIMEOUT):
    """
    Run argv list (no shell -> no quoting bugs). Raise with real stderr.

    The timeout is not optional decoration. A single ffmpeg invocation that
    never returns once ate a whole 45-minute CI budget in total silence -
    two scenes rendered, then nothing, and the job was killed by the runner
    with an orphan ffmpeg still resident. Any command that cannot finish in
    `timeout` seconds is a failure we want to SEE and degrade around, not a
    process to wait on forever.
    """
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"\n⏱  {what} exceeded {timeout}s and was killed")
        print("   cmd: " + " ".join(cmd)[:1500])
        raise RuntimeError(f"{what} timed out after {timeout}s")
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
    ], f"probe {path}", timeout=PROBE_TIMEOUT))


def probe_safe(path):
    """Duration, or 0.0 if the file is unreadable/corrupt/zero-length."""
    try:
        d = probe(path)
        return d if d and d > 0 else 0.0
    except Exception:
        return 0.0


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
def group_words(words, offset):
    """
    Word timings -> caption groups, shifted onto the global timeline. Each
    group is (start, end, word_list) where word_list keeps every individual
    (start, end, text) - the shared basis for plain SRT captions and for
    ASS karaoke-highlight captions, which need each word's own timing, not
    just the group's.
    """
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

    groups = []
    for c in chunks:
        shifted = [(s + offset, e + offset, t) for s, e, t in c if t.strip()]
        if not shifted:
            continue
        start = shifted[0][0]
        end = max(shifted[-1][1], start + 0.45)
        groups.append([start, end, shifted])

    # stop groups overlapping after the min-duration clamp
    for i in range(len(groups) - 1):
        if groups[i][1] > groups[i + 1][0]:
            groups[i][1] = max(groups[i][0] + 0.2, groups[i + 1][0] - 0.02)
    return groups


def ts(t):
    t = max(0.0, t)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(groups, path):
    """Plain captions (also useful as a standalone upload-as-CC file)."""
    with open(path, "w", encoding="utf-8") as f:
        for i, (a, b, words) in enumerate(groups, 1):
            text = " ".join(w[2] for w in words).strip()
            f.write(f"{i}\n{ts(a)} --> {ts(b)}\n{text}\n\n")
    print(f"   ✓ {len(groups)} subtitle cues -> {path}")


def ass_ts(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,DejaVu Sans,20,&H0000D7FF,&H00FFFFFF,&HD0000000,&H00000000,-1,0,0,0,100,100,0.3,0,1,2.4,0.8,2,20,20,46,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def write_ass(groups, path):
    """
    Word-synced karaoke captions: each word is white (SecondaryColour)
    until spoken, then sweeps to gold (PrimaryColour) via \\kf - matched to
    the real per-word TTS timing, not estimated. \\k durations run from each
    word's own start to the NEXT word's start (last word to the group's
    end), so gaps between words are absorbed into the sweep instead of
    leaving a dead pause where nothing is highlighted - durations always
    sum exactly to the line length, so the sweep can't drift out of sync.
    A brief pop-in scale on each line's entry keeps it from feeling static.
    """
    lines = [ASS_HEADER.format(w=W, h=H)]
    for start, end, words in groups:
        parts = []
        for i, (ws, _we, wt) in enumerate(words):
            nxt = words[i + 1][0] if i + 1 < len(words) else end
            dur_cs = max(round((nxt - ws) * 100), 1)
            parts.append(f"{{\\kf{dur_cs}}}{wt} ")
        text = ("{\\fad(60,60)\\fscx118\\fscy118\\t(0,150,\\fscx100\\fscy100)}"
                 + "".join(parts).strip())
        lines.append(f"Dialogue: 0,{ass_ts(start)},{ass_ts(end)},Caption,,0,0,0,,{text}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"   ✓ {len(groups)} karaoke caption cues -> {path}")


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


def _pixabay_get(endpoint, keyword, extra):
    q = urllib.parse.quote(keyword.strip())
    url = (f"https://pixabay.com/api/{endpoint}?key={PIXABAY_API_KEY}"
           f"&q={q}&safesearch=true&per_page=6{extra}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read()).get("hits", [])


def fetch_pixabay_video(keyword, out_mp4, seen_ids):
    """
    Real stock footage for this keyword, if Pixabay has one. Picks the first
    hit not already used elsewhere in this build, to cut down on the same
    clip repeating across similar-keyword shots. Never raises.
    """
    if not PIXABAY_API_KEY:
        return False
    try:
        hits = [h for h in _pixabay_get("videos/", keyword, "&orientation=horizontal")
                if h.get("id") not in seen_ids]
        if not hits:
            return False
        hit = hits[0]
        seen_ids.add(hit["id"])
        vids = hit.get("videos", {})
        url = (vids.get("medium") or vids.get("small")
               or vids.get("large") or vids.get("tiny") or {}).get("url")
        if not url:
            return False
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=90) as r:
            data = r.read()
        if len(data) < 8192:
            return False
        with open(out_mp4, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"      pixabay video error - {e}", flush=True)
        return False


def fetch_pixabay_photo(keyword, out_png, seen_ids):
    """Real stock photo for this keyword, resized like an AI image. Never raises."""
    if not PIXABAY_API_KEY:
        return False
    try:
        hits = [h for h in _pixabay_get("", keyword, "&image_type=photo&orientation=horizontal")
                if h.get("id") not in seen_ids]
        if not hits:
            return False
        hit = hits[0]
        seen_ids.add(hit["id"])
        url = hit.get("largeImageURL") or hit.get("webformatURL")
        if not url:
            return False
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if len(data) < 4096:
            return False
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB").resize((KB_W, KB_H), Image.Resampling.LANCZOS)
            im.save(out_png, "PNG")
        return True
    except Exception as e:
        print(f"      pixabay photo error - {e}", flush=True)
        return False


def fetch_shot_asset(keyword, seed, out_stub, seen_video_ids):
    """
    Visual source chain for one shot: Pixabay video -> Pixabay photo ->
    Pollinations AI image -> flat slate. Returns ("video"|"image", path, ok)
    where ok=False only for the final flat-slate fallback. fetch_image()
    (the tail of the chain) never raises, so this never does either - worst
    case is a slate image, never a crashed build.
    """
    if PIXABAY_API_KEY:
        video_path = out_stub + ".mp4"
        if fetch_pixabay_video(keyword, video_path, seen_video_ids):
            return "video", video_path, True
        photo_path = out_stub + ".png"
        if fetch_pixabay_photo(keyword, photo_path, seen_video_ids):
            return "image", photo_path, True

    png_path = out_stub + ".png"
    ok = fetch_image(keyword, seed, png_path)
    return "image", png_path, ok


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


def render_shot(motion_idx, asset_kind, asset_path, out_mp4, frames, fade_in, fade_out):
    """
    One visual -> one silent clip, `frames` frames long.

    Real footage ("video") already has motion, so it's scaled/cropped to
    fill the frame and trimmed (looped if too short) rather than
    Ken-Burns'd. An AI still ("image") gets the usual Ken Burns move. Both
    share the same colour grade so cuts between the two never look jarring.
    """
    dur = frames / FPS
    grade = "eq=contrast=1.06:saturation=0.90:gamma=0.98,vignette=PI/5,format=yuv420p"

    if asset_kind == "video":
        # fps= FIRST, before anything else. Stock clips arrive at whatever
        # frame rate and timebase they were shot at - a slow-motion clip can
        # be 120/240fps or variable - and those timestamps otherwise survive
        # into the copied stream and break duration handling downstream.
        # Normalising here means every shot leaves this function looking
        # identical, which is also what makes the stream-copy concat legal.
        vf = (f"fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase,"
              f"crop={W}:{H},{grade}")
    else:
        vf = ken_burns(motion_idx, frames)

    # gentle fade at the very top and tail of the film only
    if fade_in:
        vf += ",fade=t=in:st=0:d=1.0"
    if fade_out:
        vf += f",fade=t=out:st={max(dur - 1.2, 0):.3f}:d=1.2"

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if asset_kind == "video":
        # Loop a FINITE number of times, computed from the clip's real
        # duration - never -1.
        #
        # `-stream_loop -1` is how this hung: given a clip ffmpeg could not
        # drain (0 decodable frames per pass), it re-looped forever, emitted
        # nothing, and never reached the `-t` cutoff. Two scenes rendered,
        # then 44 minutes of silence and a killed job. A finite loop count
        # terminates on its own even in that case; the run() timeout is the
        # second net under it.
        src_dur = probe_safe(asset_path)
        if src_dur <= 0:
            # Undecodable clip. Looping it can only ever produce nothing, so
            # fail fast and let render_shot_safe put a slate here instead of
            # spending the timeout discovering it the slow way.
            raise RuntimeError(f"unreadable clip (0s duration): {asset_path}")
        if src_dur >= dur:
            cmd += ["-i", asset_path, "-t", f"{dur:.3f}"]
        else:
            loops = min(math.ceil(dur / max(src_dur, 0.1)), MAX_LOOPS)
            cmd += ["-stream_loop", str(loops), "-i", asset_path,
                    "-t", f"{dur:.3f}"]
    else:
        cmd += ["-i", asset_path]

    cmd += [
        "-vf", vf,
        "-an",
        "-c:v", "libx264", "-preset", PRESET_SCENE, "-crf", CRF_SCENE,
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
        "-movflags", "+faststart",
        out_mp4,
    ]
    run(cmd, f"render shot {out_mp4}", timeout=shot_timeout(dur))


def render_shot_safe(motion_idx, asset_kind, asset_path, out_mp4, frames,
                      fade_in, fade_out):
    """
    render_shot, but one unusable clip costs that shot and nothing more.

    Without this, a single bad source clip fails the whole build after every
    other scene has already been paid for. Falls back to a slate rendered
    through the still-image path, which has no external input to go wrong.
    """
    try:
        render_shot(motion_idx, asset_kind, asset_path, out_mp4, frames,
                    fade_in, fade_out)
        return True
    except Exception as e:
        print(f"      !! shot failed ({str(e)[:80]}) - substituting slate",
              flush=True)
        slate = out_mp4 + ".slate.png"
        Image.new("RGB", (KB_W, KB_H), (11, 12, 16)).save(slate, "PNG")
        try:
            render_shot(motion_idx, "image", slate, out_mp4, frames,
                        fade_in, fade_out)
            return False
        finally:
            if os.path.exists(slate):
                os.remove(slate)


def concat_shots(shot_mp4s, listfile, out_mp4):
    """Stream-copy concat of a scene's silent shots (identical codec params)."""
    with open(listfile, "w") as f:
        for p in shot_mp4s:
            f.write(f"file '{os.path.abspath(p)}'\n")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", listfile,
         "-c", "copy", "-movflags", "+faststart", out_mp4], "concat shots")


def mux_audio(video_mp4, mp3, out_mp4):
    """
    Attach the scene's narration audio to an already-rendered silent clip.

    The output length is set EXPLICITLY with -t, not inferred with -shortest.
    -shortest has to work out when to stop from stream timestamps, and with
    `-c:v copy` those timestamps come straight from the source clip. Given a
    slow-motion source (high frame rate, unusual timebase) that inference
    never resolved: ffmpeg sat forever on one scene until it was killed. We
    already know exactly how long the video is, so we say so. `apad` then
    keeps the audio from ending early, since the video is deliberately a
    few frames longer than the narration.

    I=-14 is YouTube's own normalization target. At the old -16 the platform
    left it alone and it simply played quieter than every video next to it.
    """
    vdur = probe_safe(video_mp4)
    if vdur <= 0:
        raise RuntimeError(f"unreadable silent scene video: {video_mp4}")
    af = ("loudnorm=I=-14:TP=-1.5:LRA=11,"
          "aresample=48000:resampler=soxr,aformat=channel_layouts=stereo,apad")
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", video_mp4,
        "-i", mp3,
        "-filter_complex", f"[1:a]{af}[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-t", f"{vdur:.3f}",
        "-movflags", "+faststart",
        out_mp4,
    ], "mux audio", timeout=shot_timeout(vdur))


def render_scene(scene_idx, assets, mp3, out_mp4, work_dir, motion_start,
                  first_scene, last_scene, frames_total):
    """
    Render one scene: N visuals (each ("video"|"image", path)) -> N silent
    shots -> concat -> mux with the scene's narration audio. The scene's
    frame budget is split evenly across its shots, remainder folded into
    the last one.
    """
    n = len(assets)
    base = frames_total // n
    frame_counts = [base] * n
    frame_counts[-1] += frames_total - base * n

    shots, failed = [], 0
    for j, ((kind, path), frames) in enumerate(zip(assets, frame_counts)):
        shot_mp4 = os.path.join(work_dir, f"s{scene_idx:03d}_{j:02d}.mp4")
        if not render_shot_safe(motion_start + j, kind, path, shot_mp4, frames,
                                fade_in=(first_scene and j == 0),
                                fade_out=(last_scene and j == n - 1)):
            failed += 1
        shots.append(shot_mp4)

    if n == 1:
        mux_audio(shots[0], mp3, out_mp4)
        os.remove(shots[0])
        return failed

    listfile = os.path.join(work_dir, f"s{scene_idx:03d}_list.txt")
    silent = os.path.join(work_dir, f"s{scene_idx:03d}_silent.mp4")
    concat_shots(shots, listfile, silent)
    mux_audio(silent, mp3, out_mp4)

    for p in shots:
        os.remove(p)
    os.remove(silent)
    os.remove(listfile)
    return failed


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
    ], "synthesise music bed", timeout=FINAL_TIMEOUT)


# ----------------------------------------------------------------------------
# 6. FINAL PASS — burn subtitles + duck music under narration
# ----------------------------------------------------------------------------
def finish(body, ass, has_subs, music, out_mp4):
    """
    Burn word-synced karaoke captions and mix in the ducked music bed.

    has_subs comes from the caller (len(cue groups) > 0) rather than being
    sniffed from the file on disk - the ASS header alone is a few hundred
    bytes even with zero caption lines, so a size check can't tell "no
    captions" from "captions with a lot of style boilerplate". If there are
    no cues the burn-in is skipped entirely and the video stream is
    stream-copied: libass refuses a .ass with no Dialogue lines and takes
    the whole render down with it, which is not an acceptable way to lose a
    twenty-minute job over a caption file.
    """
    afilt = (
        f"[1:a]volume={MUSIC_GAIN},aresample=48000,"
        f"aformat=channel_layouts=stereo[m];"
        # main = music, sidechain = narration -> music dips under the voice
        f"[m][0:a]sidechaincompress="
        f"threshold=0.030:ratio=9:attack=12:release=380:makeup=1[duck];"
        f"[0:a][duck]amix=inputs=2:duration=first:normalize=0,"
        f"alimiter=limit=0.95[a]"
    )

    # Loop the music a finite number of times, computed from both real
    # durations. `-stream_loop -1` is nominally bounded here by
    # amix=duration=first, but that is the same infinite-input pattern that
    # hung a render for 44 minutes elsewhere in this file, and there is no
    # reason to keep an unbounded input when the arithmetic is this easy.
    body_dur = probe_safe(body)
    music_dur = probe_safe(music)
    loops = (min(math.ceil(body_dur / music_dur) + 1, MAX_LOOPS)
             if body_dur > 0 and music_dur > 0 else 1)

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", body,
        "-stream_loop", str(loops), "-i", music,
    ]

    if has_subs:
        ass_arg = ass.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        vfilt = f"[0:v]ass=filename='{ass_arg}'[v];"
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
    run(cmd, "final composite", timeout=FINAL_TIMEOUT)


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

    # ---------------- phase 2: visuals (concurrent) ---------------------------
    # Network wait dominates this phase (Pixabay lookup+download, or
    # Pollinations), so it parallelises almost for free.
    shots_per_scene = [plan_shots(s.get("image_keywords"), durs[i])
                        for i, s in enumerate(scenes)]
    total_shots = sum(len(k) for k in shots_per_scene)
    source = "Pixabay (real footage) + AI fallback" if PIXABAY_API_KEY else "AI images only"

    print(f"\n[2/3] visuals x{total_shots} ({total} scenes) | source: {source} ...",
          flush=True)
    asset_paths, work_items, gi = [], [], 0
    for i, kws in enumerate(shots_per_scene):
        stubs = []
        for j, kw in enumerate(kws):
            # "_src" keeps the fetched asset's filename distinct from the
            # rendered shot output (s{i}_{j}.mp4) render_scene() writes to -
            # without it, a Pixabay video download and its own Ken-Burns-less
            # render collide on the same .mp4 path (read+write same file).
            out_stub = os.path.join(WORK, f"s{i:03d}_{j:02d}_src")
            work_items.append((i, j, kw, 1000 + gi * 137, out_stub))
            stubs.append(None)
            gi += 1
        asset_paths.append(stubs)

    seen_pixabay_ids = set()

    async def do_asset(item):
        i, j, kw, seed, out_stub = item
        kind, path, ok = await asyncio.to_thread(
            fetch_shot_asset, kw, seed, out_stub, seen_pixabay_ids)
        asset_paths[i][j] = (kind, path)
        tag = "video" if kind == "video" else ("image" if ok else "SLATE")
        print(f"      shot scene {i+1} #{j+1} [{tag}] | {kw[:44]}", flush=True)
        return kind == "video", ok

    results = []
    for b in range(0, len(work_items), 5):
        results += await asyncio.gather(
            *(do_asset(item) for item in work_items[b:b + 5]))
    real_footage = sum(1 for is_video, _ in results if is_video)
    failed_images = sum(1 for _, ok in results if not ok)

    # ---------------- phase 3: render ----------------------------------------
    print(f"\n[3/3] render x{total} scenes ({total_shots} shots) ...", flush=True)
    cues, parts, timeline, motion_cursor = [], [], 0.0, 0

    for i, sc in enumerate(scenes):
        mp4 = os.path.join(WORK, f"s{i:03d}.mp4")
        frames_total = math.ceil(durs[i] * FPS) + 3
        render_scene(i, asset_paths[i], mp3s[i], mp4, WORK, motion_cursor,
                     first_scene=(i == 0), last_scene=(i == total - 1),
                     frames_total=frames_total)
        motion_cursor += len(asset_paths[i])

        words = word_lists[i] or estimate_word_times(
            scenes[i]["narration"].strip(), durs[i])
        actual = probe(mp4)
        cues += group_words(words, timeline)
        timeline += actual
        parts.append(mp4)
        print(f"      scene {i+1}/{total} [{sc.get('beat','?')}] "
              f"{len(asset_paths[i])} shot(s)  {actual:.1f}s  "
              f"(+{time.time()-t0:.0f}s elapsed)", flush=True)

        for _, p in asset_paths[i]:
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
         "-c", "copy", "-movflags", "+faststart", body], "concat",
         timeout=FINAL_TIMEOUT)

    write_srt(cues, "subtitles.srt")
    ass = "captions.ass"
    write_ass(cues, ass)

    if os.path.exists(MUSIC_FILE):
        print(f"> music: {MUSIC_FILE}", flush=True)
        music = MUSIC_FILE
    else:
        print("> music: synthesising ambient bed", flush=True)
        music = os.path.join(WORK, "bed.mp3")
        music_bed(timeline, music)

    print("> final composite...", flush=True)
    finish(body, ass, len(cues) > 0, music, "final_video.mp4")

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
    if PIXABAY_API_KEY:
        print(f"   visuals  : {real_footage}/{total_shots} real Pixabay footage",
              flush=True)
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
  
