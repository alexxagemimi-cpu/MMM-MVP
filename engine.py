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
import re
import asyncio
import subprocess
import urllib.parse
import urllib.request
import urllib.error

from PIL import Image
import edge_tts

# Both are optional. A missing drawing or sound module must cost the polish
# it provides, never the whole video - the engine has to keep working on a
# machine where only ffmpeg and Pillow are present.
try:
    import graphics
except Exception as _e:                       # pragma: no cover
    graphics = None
    print(f"!! graphics unavailable ({_e}) - no section cards or headers")
try:
    import sfx
except Exception as _e:                       # pragma: no cover
    sfx = None
    print(f"!! sfx unavailable ({_e}) - no sound effects")

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
# The kit is already levelled well under 0 dB (see sfx.py), so this is a trim
# rather than a fader. An effect the viewer consciously notices is too loud.
SFX_GAIN    = 0.9
WORDS_PER_CUE = 6
MAX_CUE_GAP   = 0.65

# SHOT PACING - set from retention research, not taste.
#
# Explainer editing runs about 4-6 seconds per visual; high-performing
# videos sit around one cut every 2-4s, and b-roll is typically held 3-7s.
# The old settings here (SHOTS_MAX=4, no target) put one visual every 11-22
# seconds on a 45-65s scene - a talking-head pace on an explainer, and a
# large part of why the first output felt like an advert playing under a
# voice rather than an edit.
#
# Shot count is now derived from the scene's real length divided by
# TARGET_SHOT_SEC, bounded by MIN_SHOT_SEC and SHOTS_MAX, and limited in
# practice by how many distinct keywords the script supplies.
TARGET_SHOT_SEC = float(os.environ.get("TARGET_SHOT_SEC", "5.0"))
SHOTS_MAX     = int(os.environ.get("SHOTS_MAX", "12"))
MIN_SHOT_SEC  = 3.0                   # never slice a shot shorter than this

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

# LOOK
# ----
# An explainer and an advert are lit differently, and the old settings here
# were the advert: "low-key moody lighting, volumetric haze, muted
# desaturated teal and amber" is a literal description of a coffee
# commercial, and that is exactly what the first real output looked like.
# Explainers are bright, flat and high-contrast, because the viewer is
# reading the screen, not admiring it.
#
# STYLE=explainer (default) or STYLE=cinematic for narrative/story videos.
STYLE = os.environ.get("STYLE", "explainer").strip().lower()

if STYLE == "cinematic":
    VISUAL_STYLE = (
        "cinematic 35mm film still, anamorphic widescreen, low-key moody lighting, "
        "volumetric haze, muted desaturated teal and amber palette, shallow depth "
        "of field, fine film grain, photorealistic, highly detailed, "
        "no text, no watermark, no logo, no caption")
    GRADE    = "eq=contrast=1.06:saturation=0.90:gamma=0.98"
    VIGNETTE = "vignette=PI/5,"
else:
    VISUAL_STYLE = (
        "bright clean editorial photograph, natural daylight, crisp focus, "
        "high key lighting, simple uncluttered background, clear subject, "
        "vivid but natural colour, sharp detail, "
        "no text, no watermark, no logo, no caption")
    # A touch more contrast and saturation, no gamma crush and NO vignette:
    # a vignette darkens the corners, which is where term cards sit.
    GRADE    = "eq=contrast=1.10:saturation=1.06:gamma=1.02"
    VIGNETTE = ""

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
def split_multiword(entries):
    """
    Break any timing entry holding several words into one entry per word,
    dividing its span by word length.

    Edge-TTS sentence boundaries arrive in the same shape as word
    boundaries, so a whole sentence can turn up as a single timed "word".
    Left alone that produces one caption per scene and a karaoke highlight
    with nothing to step through.
    """
    out = []
    for start, end, text in entries:
        parts = text.split()
        if len(parts) <= 1:
            out.append((start, end, text))
            continue
        span = max(end - start, 0.001)
        weights = [max(len(p), 1) for p in parts]
        total = sum(weights)
        t = start
        for p, w in zip(parts, weights):
            step = span * w / total
            out.append((t, t + step, p))
            t += step
    return out


async def synth(text, mp3_path, attempts=3):
    """
    Stream Edge-TTS. Returns [(start_s, end_s, word), ...], or [] when the
    service sends no boundary metadata (which it sometimes does not).

    Boundary offset/duration are in 100-nanosecond ticks. We match on the
    presence of offset+text rather than an exact type string, because that
    label has changed between edge-tts releases.

    That tolerance has a cost: the service also emits SENTENCE boundaries,
    which look identical in shape. A real run produced 4 caption cues for a
    4-scene script - one whole sentence per scene, dumped on screen at once,
    with nothing for the karaoke highlight to step through. Any entry
    carrying more than one word is therefore split back into words here,
    sharing its span out by word length, so downstream always sees real
    per-word timing regardless of which boundary type arrived.
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
            return split_multiword(words)
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
Style: Term,DejaVu Sans,30,&H00FFFFFF,&H00FFFFFF,&H14101010,&H14101010,-1,0,0,0,100,100,0.6,0,3,16,0,1,54,54,120,1
Style: TermSub,DejaVu Sans,17,&H00D8D8D8,&H00D8D8D8,&H28101010,&H28101010,0,0,0,0,100,100,0.3,0,3,13,0,1,54,54,92,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

TERM_HOLD    = 3.4    # seconds a term card stays up
TERM_LEAD    = 0.15   # appear a beat before the word is actually said


def find_term_time(words, term):
    """
    When is `term` first spoken? Returns its start time, or None.

    Matches on a normalised word sequence so "401(k)" or "compound
    interest" line up with however TTS chopped them. Returning None simply
    means no card for that scene - a missing card is invisible, a wrongly
    timed one is worse than none.
    """
    if not term or not words:
        return None
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    want = [norm(t) for t in term.split() if norm(t)]
    if not want:
        return None
    have = [norm(w[2]) for w in words]
    for i in range(len(have) - len(want) + 1):
        if have[i:i + len(want)] == want:
            return words[i][0]
    # single distinctive word anywhere is good enough to anchor the card
    if len(want) == 1:
        for i, h in enumerate(have):
            if h and want[0] in h:
                return words[i][0]
    return None


def ass_escape(s):
    """ASS treats { } as override blocks and \\N as a line break."""
    return (s or "").replace("\\", "").replace("{", "(").replace("}", ")").strip()


def write_ass(groups, path, term_cards=None):
    """
    Word-synced karaoke captions plus on-screen TERM CARDS.

    Captions: each word is white (SecondaryColour) until spoken, then
    sweeps to gold (PrimaryColour) via \\kf - matched to the real per-word
    TTS timing, not estimated. \\k durations run from each word's own start
    to the NEXT word's start (last word to the group's end), so gaps
    between words are absorbed into the sweep instead of leaving a dead
    pause where nothing is highlighted - durations always sum exactly to
    the line length, so the sweep can't drift out of sync.

    Term cards are the thing that makes this read as an explainer rather
    than an advert. In a money/business explainer there is nothing to
    photograph - stock footage of an office carries no information - so the
    words on screen have to carry it: the concept is named, in large type,
    at the moment the narration says it, with one line of detail under it.

    The card sits at the BOTTOM LEFT, just above the captions.

    The top of the frame is no longer free: the persistent section header
    owns the first 108px, and on a section card the whole upper area is the
    list itself. Verified on rendered frames - at the old top-left position
    the term printed through the header's numbered badge, and moving it down
    only pushed its definition inside the red highlight box instead. Header
    at the top, term card above the captions, captions at the very bottom:
    three bands, no overlap.

    Note the \\move override sets an ABSOLUTE position and therefore beats
    the style's MarginV entirely - changing the margin alone did nothing,
    which is why the first attempt at this appeared not to work. It
    used to start at y=54, which is inside the 108px the persistent header
    now owns - verified on a rendered frame, where "COMPOUND INTEREST" and
    its definition were printed straight through the header's own numbered
    badge. The header names the section; the card names the term and defines
    it; they are different jobs and must not share a row.

    The card uses ASS BorderStyle=3 (opaque box), which sizes its own
    background to the text. An earlier attempt at this drew the box with a
    hardcoded 560px width and long definitions ran straight off the edge of
    their own background; letting the renderer measure the glyphs removes
    that failure mode entirely rather than fixing it arithmetically.
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

    n_cards = 0
    for start, term, fact in (term_cards or []):
        a, b = max(start - TERM_LEAD, 0), start - TERM_LEAD + TERM_HOLD
        # layer 1 so a card always sits above the caption layer
        lines.append(
            f"Dialogue: 1,{ass_ts(a)},{ass_ts(b)},Term,,0,0,0,,"
            f"{{\\fad(180,260)\\move({-260},{H-120},{54},{H-120},0,220)}}"
            f"{ass_escape(term).upper()}\n")
        if fact:
            lines.append(
                f"Dialogue: 1,{ass_ts(a + 0.12)},{ass_ts(b)},TermSub,,0,0,0,,"
                f"{{\\fad(220,260)}}{ass_escape(fact)}\n")
        n_cards += 1

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"   ✓ {len(groups)} karaoke caption cues, {n_cards} term card(s) -> {path}")


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
    How many visuals this scene gets, and which keywords they use.

    Driven by TARGET_SHOT_SEC (~5s, the explainer editing norm) rather than
    a flat cap: a long scene earns more cuts, a short one does not get
    chopped below MIN_SHOT_SEC. If the script supplied fewer keywords than
    the scene has room for, the keywords are CYCLED rather than the cut rate
    being abandoned - a second look at the same subject still reads as an
    edit, whereas holding one clip for twenty seconds reads as a screensaver.
    """
    kws = [k.strip() for k in (keywords or []) if k and k.strip()] \
        or ["abstract dark texture"]
    want = int(round(audio_dur / TARGET_SHOT_SEC)) or 1
    room = int(audio_dur // MIN_SHOT_SEC) or 1
    n = max(1, min(want, room, SHOTS_MAX))
    return [kws[i % len(kws)] for i in range(n)]


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
        f"{GRADE},"
        f"{VIGNETTE}"
        f"format=yuv420p"
    )


def render_shot(motion_idx, asset_kind, asset_path, out_mp4, frames, fade_in,
                fade_out, overlay_png=None):
    """
    One visual -> one silent clip, `frames` frames long.

    Three kinds now:
      "video"  real footage - already has motion, so scaled/cropped to fill
               and trimmed (looped if too short) rather than Ken-Burns'd
      "image"  a still - gets the Ken Burns move
      "card"   a DRAWN card from graphics.py - held perfectly still, with no
               grade and no motion. Both would be wrong: the card was
               designed at exactly this size with its own colours, so a
               colour grade fights the design and a Ken Burns move drifts
               the type and makes it unreadable.

    `overlay_png` is the persistent section header, composited over footage
    shots so they carry the same orientation as the cards. This is the single
    device the reference explainer uses most - the section name never leaves
    the screen, and it works precisely BECAUSE it does not move.
    """
    dur = frames / FPS
    grade = f"{GRADE},{VIGNETTE}format=yuv420p"

    if asset_kind == "card":
        # zoompan with the zoom pinned at 1.0 expands ONE input frame into
        # `frames` output frames, holding the card perfectly still. This is
        # the same mechanism ken_burns uses, and it is deliberately not
        # `-loop 1`: the module note at the top of this file is about
        # unbounded inputs, and there is no reason to introduce one when a
        # bounded expansion already exists and is proven.
        vf = (f"scale={W}:{H}:flags=lanczos,"
              f"zoompan=z=1:d={frames}:s={W}x{H}:fps={FPS},format=yuv420p")
    elif asset_kind == "video":
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

    # The persistent header rides on top of footage shots. A card already
    # draws its own, so it never gets one composited over it.
    if overlay_png and asset_kind != "card" and os.path.exists(overlay_png):
        cmd += ["-i", overlay_png]
        cmd += ["-filter_complex",
                f"[0:v]{vf}[b];[b][1:v]overlay=0:0:format=auto[v]",
                "-map", "[v]"]
    else:
        cmd += ["-vf", vf]

    cmd += [
        "-an",
        "-c:v", "libx264", "-preset", PRESET_SCENE, "-crf", CRF_SCENE,
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
        "-movflags", "+faststart",
        out_mp4,
    ]
    run(cmd, f"render shot {out_mp4}", timeout=shot_timeout(dur))


def render_shot_safe(motion_idx, asset_kind, asset_path, out_mp4, frames,
                      fade_in, fade_out, overlay_png=None):
    """
    render_shot, but one unusable clip costs that shot and nothing more.

    Without this, a single bad source clip fails the whole build after every
    other scene has already been paid for. Falls back to a slate rendered
    through the still-image path, which has no external input to go wrong.
    """
    try:
        render_shot(motion_idx, asset_kind, asset_path, out_mp4, frames,
                    fade_in, fade_out, overlay_png)
        return True
    except Exception as e:
        print(f"      !! shot failed ({str(e)[:80]}) - substituting slate",
              flush=True)
        slate = out_mp4 + ".slate.png"
        Image.new("RGB", (KB_W, KB_H), (11, 12, 16)).save(slate, "PNG")
        try:
            # the slate still carries the header - losing a picture should
            # not also lose the viewer's place in the video
            render_shot(motion_idx, "image", slate, out_mp4, frames,
                        fade_in, fade_out, overlay_png)
            return False
        finally:
            if os.path.exists(slate):
                os.remove(slate)


def assemble_scene(shot_mp4s, mp3, out_mp4, listfile, target_dur):
    """
    Shots + narration -> one finished scene, in a SINGLE ffmpeg call.

    This replaced a two-step concat(-c copy) -> mux(-c:v copy) pipeline that
    hung three separate CI runs, always on the same scene, whose source was a
    slow-motion stock clip. Both `-shortest` and an explicit `-t` failed to
    stop it, which rules out the flag and points at the thing they had in
    common: `-c:v copy` inheriting that clip's timestamps through an
    intermediate file.

    So neither survives here. The shots are concatenated and the video is
    RE-ENCODED in the same pass, which forces ffmpeg to decode and re-stamp
    every frame - there is no timestamp left to inherit and no intermediate
    file to inherit it from. `target_dur` comes from our own frame budget
    (frames / FPS), not from probing a file, so the output length is a number
    we computed rather than one ffmpeg has to infer.

    The extra encode costs a few seconds per scene. Three dead runs cost 50
    minutes, so this trade is not close.

    NO loudnorm HERE - IT IS THE THING THAT HUNG
    --------------------------------------------
    Four theories about ffmpeg flags were wrong (-shortest, a missing -t,
    timestamp copying, stream copying). The tiered fallback below caught the
    real answer in a single run: with `loudnorm` in the chain the scene was
    killed at 73s; without it the same scene assembled in 1.5 seconds.
    loudnorm stalls on some particular narration clip, and no flag was ever
    going to fix that.

    Loudness is now normalised ONCE over the finished programme in finish()
    instead of per scene - which is also simply the correct place for it.
    Normalising each scene separately re-levels every scene against itself,
    so a deliberately quiet beat gets pushed up and a loud one pulled down,
    flattening exactly the dynamics between scenes that make narration feel
    edited rather than machine-processed.

    The remaining tiers still degrade, ending in silence, so one awkward
    narration clip can never kill a build again.
    """
    with open(listfile, "w") as f:
        for p in shot_mp4s:
            f.write(f"file '{os.path.abspath(p)}'\n")

    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", listfile]
    venc = ["-c:v", "libx264", "-preset", PRESET_SCENE, "-crf", CRF_SCENE,
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
            "-fps_mode", "cfr"]
    aenc = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    tail = ["-t", f"{target_dur:.3f}", "-movflags", "+faststart", out_mp4]

    # apad is bounded by whole_dur - a bare apad generates silence forever,
    # which is exactly the kind of unbounded input this file has been bitten
    # by twice already.
    tiers = [
        ("standard", f"aresample=48000,aformat=channel_layouts=stereo,"
                     f"apad=whole_dur={target_dur:.3f}"),
        ("plain", "aresample=48000,aformat=channel_layouts=stereo"),
    ]

    last = None
    for name, af in tiers:
        try:
            run(base + ["-i", mp3, "-filter_complex", f"[1:a]{af}[a]",
                        "-map", "0:v", "-map", "[a]"] + venc + aenc + tail,
                f"assemble scene [{name}]", timeout=shot_timeout(target_dur))
            if name != tiers[0][0]:
                print(f"      !! audio chain degraded to '{name}' for this scene",
                      flush=True)
            return name
        except Exception as e:
            last = e
            print(f"      !! assemble tier '{name}' failed: {str(e)[:90]}",
                  flush=True)

    # Last resort: keep the picture, lose this scene's narration, keep the
    # build alive. Silent is recoverable in an edit; a dead run is not.
    print("      !! all audio tiers failed - rendering scene SILENT", flush=True)
    run(base + ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-map", "0:v", "-map", "1:a"] + venc + aenc + tail,
        "assemble scene [silent]", timeout=shot_timeout(target_dur))
    return "silent"


def render_scene(scene_idx, assets, mp3, out_mp4, work_dir, motion_start,
                  first_scene, last_scene, frames_total, overlay_png=None):
    """
    Render one scene: N visuals (each ("video"|"image", path)) -> N silent
    shots -> one assemble pass that concatenates them and lays the narration
    over the top. The scene's frame budget is split evenly across its shots,
    remainder folded into the last one.

    The target duration is computed from that frame budget rather than
    probed back off disk: it is a number we already know exactly, and every
    hang in this file so far has come from asking ffmpeg to work a duration
    out for itself.
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
                                fade_out=(last_scene and j == n - 1),
                                overlay_png=overlay_png):
            failed += 1
        shots.append(shot_mp4)

    # What these files actually ARE, before handing them to ffmpeg. Three
    # runs died on one scene while the logs showed only that a command had
    # been killed; nothing said whether its inputs were sane. Cheap to
    # print, and it turns "it hung again" into evidence.
    for j, p in enumerate(shots):
        print(f"        shot {j}: {probe_safe(p):.2f}s "
              f"{os.path.getsize(p) if os.path.exists(p) else 0}B", flush=True)
    print(f"        audio : {probe_safe(mp3):.2f}s "
          f"{os.path.getsize(mp3) if os.path.exists(mp3) else 0}B "
          f"| target {frames_total / FPS:.2f}s", flush=True)

    listfile = os.path.join(work_dir, f"s{scene_idx:03d}_list.txt")
    assemble_scene(shots, mp3, out_mp4, listfile, frames_total / FPS)

    for p in shots:
        os.remove(p)
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
def finish(body, ass, has_subs, music, out_mp4, sfx_wav=None):
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
    # loudnorm lives HERE, once, over the whole finished programme - not per
    # scene. Per-scene normalisation hung one narration clip outright (see
    # assemble_scene) and is the wrong unit anyway: it re-levels every scene
    # against itself, flattening the loud/quiet contrast between scenes that
    # makes narration sound edited. I=-14 is YouTube's own target; below it
    # the platform leaves the file alone and it simply plays quieter than
    # everything beside it.
    # The sound-effect track is mixed in as a THIRD source, and deliberately
    # NOT side-chained. Music ducks under the voice because it is competing
    # for the same space for minutes at a time; an effect is a 0.4-second
    # accent ON a transition and ducking it would remove the very moment it
    # exists to mark.
    has_sfx = bool(sfx_wav and os.path.exists(sfx_wav))
    afilt = (
        f"[1:a]volume={MUSIC_GAIN},aresample=48000,"
        f"aformat=channel_layouts=stereo[m];"
        # main = music, sidechain = narration -> music dips under the voice
        f"[m][0:a]sidechaincompress="
        f"threshold=0.030:ratio=9:attack=12:release=380:makeup=1[duck];"
    )
    if has_sfx:
        afilt += (f"[2:a]aresample=48000,aformat=channel_layouts=stereo,"
                  f"volume={SFX_GAIN}[fx];"
                  f"[0:a][duck][fx]amix=inputs=3:duration=first:normalize=0,")
    else:
        afilt += f"[0:a][duck]amix=inputs=2:duration=first:normalize=0,"
    afilt += (f"loudnorm=I=-14:TP=-1.5:LRA=11,"
              f"alimiter=limit=0.95[a]")

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
    if has_sfx:
        cmd += ["-i", sfx_wav]

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
    #
    # THE STRUCTURE THE REFERENCE EXPLAINER USES, and the thing that most
    # separates it from what this engine used to make.
    #
    # Every scene now OPENS on a drawn card showing the whole list with the
    # current item lit and the finished ones ticked, then continues over
    # footage that carries a section header which never moves. Before, a
    # scene was an unbroken run of unrelated stock clips with nothing at all
    # telling the viewer where they were.
    #
    # Both are built from data the script already carries - one key_term per
    # scene - so this costs no extra model call and no extra quota.
    members = [(s.get("key_term") or "").strip() for s in scenes]
    members = [m for m in members if m]
    use_cards = graphics is not None and len(members) >= 2
    if use_cards:
        print(f"      drawing {total} section cards + headers "
              f"({len(members)} items)", flush=True)

    print(f"\n[3/3] render x{total} scenes ({total_shots} shots) ...", flush=True)
    cues, parts, timeline, motion_cursor = [], [], 0.0, 0
    term_cards, sfx_events = [], []

    for i, sc in enumerate(scenes):
        mp4 = os.path.join(WORK, f"s{i:03d}.mp4")
        frames_total = math.ceil(durs[i] * FPS) + 3

        assets, overlay = list(asset_paths[i]), None
        if use_cards:
            try:
                card = os.path.join(WORK, f"card{i:03d}.png")
                graphics.overview_card(members, min(i, len(members) - 1),
                                       f"Section {i+1} of {total}", card)
                overlay = os.path.join(WORK, f"head{i:03d}.png")
                graphics.section_overlay(i + 1, total,
                                         members[min(i, len(members) - 1)],
                                         overlay)
                # the card REPLACES the first stock shot rather than being
                # added, so the scene still fits its narration exactly
                assets = [("card", card)] + assets[1:] if len(assets) > 1 \
                    else [("card", card)]
            except Exception as e:
                print(f"      !! card for scene {i+1} failed ({str(e)[:70]}) "
                      f"- falling back to footage only", flush=True)
                assets, overlay = list(asset_paths[i]), None

        render_scene(i, assets, mp3s[i], mp4, WORK, motion_cursor,
                     first_scene=(i == 0), last_scene=(i == total - 1),
                     frames_total=frames_total, overlay_png=overlay)
        motion_cursor += len(assets)

        words = word_lists[i] or estimate_word_times(
            scenes[i]["narration"].strip(), durs[i])
        actual = probe(mp4)
        cues += group_words(words, timeline)

        # Term card for this scene, anchored to the moment the term is
        # actually spoken. No match -> no card; a card at the wrong moment
        # is worse than none.
        term = (sc.get("key_term") or "").strip()
        if term:
            at = find_term_time(words, term)
            if at is not None:
                term_cards.append((at + timeline, term,
                                   (sc.get("key_fact") or "").strip()))
                # a card appearing silently is half an edit
                sfx_events.append((at + timeline - TERM_LEAD, "pop"))
            else:
                print(f"        (no term-card anchor for {term!r})", flush=True)

        # Sound ON the cut, which is the thing editors name first when asked
        # what separates a cheap video from an edited one. Weight under the
        # opening, air on every section change.
        sfx_events.append((timeline, "thud" if i == 0 else "whoosh"))

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
    write_ass(cues, ass, term_cards)

    if os.path.exists(MUSIC_FILE):
        print(f"> music: {MUSIC_FILE}", flush=True)
        music = MUSIC_FILE
    else:
        print("> music: synthesising ambient bed", flush=True)
        music = os.path.join(WORK, "bed.mp3")
        music_bed(timeline, music)

    sfx_wav = None
    if sfx is not None and sfx_events:
        try:
            sfx_wav = os.path.join(WORK, "sfx.wav")
            _, placed = sfx.build_track(timeline + 2, sfx_events, sfx_wav)
            print(f"> sound: {placed} effects across {timeline/60:.1f} min",
                  flush=True)
        except Exception as e:
            print(f"> sound: failed ({str(e)[:80]}) - continuing silent",
                  flush=True)
            sfx_wav = None

    print("> final composite...", flush=True)
    finish(body, ass, len(cues) > 0, music, "final_video.mp4", sfx_wav)

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
  
