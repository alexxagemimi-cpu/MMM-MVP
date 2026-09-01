#!/usr/bin/env python3
"""
engine.py — MMM Factory video assembler.

Reads script.json, produces final_video.mp4 (540p by default, 25fps).

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
    import scriptbits
except Exception as _e:
    scriptbits = None
    print(f"   !! scriptbits unavailable ({_e}) - drawn shot cards will "
          f"carry only the term and its definition", flush=True)

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
# OUTPUT SIZE. One env var, because graphics.py has to agree with it - the
# cards are drawn at exactly this size and handed to ffmpeg without a scale
# filter, so a mismatch would letterbox or crop every card in the video.
#
# 540p is 960x540: exactly half of 1080p and three quarters of 720p, which
# keeps every layout number in this project a clean multiple. "520p" is not
# a real format - there is no such standard - and picking a non-standard
# height would make ffmpeg pad to an even number anyway.
_RES = {"1080": (1920, 1080), "720": (1280, 720),
        "540": (960, 540), "480": (854, 480)}
W, H = _RES.get(os.environ.get("RESOLUTION", "540").strip(), (960, 540))
FPS  = int(os.environ.get("FPS", "25"))
SS          = 1.5                     # supersample factor for Ken Burns
KB_W, KB_H  = int(W * SS), int(H * SS)
FETCH_W     = 1920                    # what we ask Pollinations for
FETCH_H     = 1080

CRF_SCENE, PRESET_SCENE = "18", "superfast"
# CRF is the second size lever after resolution, and the one with the better
# ratio: dropping 720p to 540p removes ~25% of the bytes, +2 CRF removes a
# similar amount again without changing the frame. Env-overridable so the
# trade can be measured on a real run rather than argued about.
CRF_FINAL  = os.environ.get("CRF_FINAL", "20")
PRESET_FINAL = "medium"

MUSIC_FILE  = "assets/music.mp3"      # optional; a bed is synthesised if absent
MUSIC_GAIN  = 0.20
# The kit is already levelled well under 0 dB (see sfx.py), so this is a trim
# rather than a fader. An effect the viewer consciously notices is too loud.
SFX_GAIN    = 0.9

# HOW LONG THE SECTION CARD HOLDS.
#
# Fixed, not a share of the scene. When it took an equal share, a measured
# 40-second build was 51% one motionless card - and the card is the ONE shot
# with no inherent motion, so it is the one that must not be long. The
# opening card earns a little more because it has more to show: it is the
# first sight of the whole list.
CARD_SECONDS      = float(os.environ.get("CARD_SECONDS", "2.4"))
OPEN_CARD_SECONDS = float(os.environ.get("OPEN_CARD_SECONDS", "3.6"))

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


# THE THREE BANDS, as numbers instead of scattered literals.
#
# Everything on screen has to fit between the persistent header at the top
# and the bottom edge, without any two things sharing a row. Keeping the
# positions here means the layout can be reasoned about (and re-checked)
# in one place instead of being spread across a style line and a \\move
# override that silently outranks it.
#
# Caption size was 20 on a 720-high frame - 2.8% of frame height, about half
# of what YouTube's own default burns in, and small enough that it is
# unreadable on a phone, which is where most of this audience watches. 40 is
# ~5.5% of height, in line with what explainer channels actually run, and it
# is raised off the bottom edge so it does not fight the player's own chrome.
# Written in 1280x720 units like graphics.py and scaled to the output size,
# so a caption stays the same PROPORTION of the frame at any resolution. The
# numbers were chosen against frame height - 40 is ~5.5% of 720 - and that
# ratio is what matters, not the pixel count. Left unscaled at 540p a
# caption would be 7.4% of the frame: not a smaller video, a shoutier one.
_S = H / 720.0


def _s(v):
    return max(1, int(round(v * _S)))


CAP_SIZE      = _s(40)
CAP_MARGIN_V  = _s(64)        # bottom of the caption block to frame bottom
TERM_SIZE     = _s(34)
TERM_PAD      = _s(10)        # BorderStyle=3: "Outline" is box padding
TERM_Y        = _s(496)       # ABSOLUTE position - see the move note below
TERMSUB_SIZE  = _s(20)
TERMSUB_PAD   = _s(8)
TERMSUB_MRG_V = _s(178)

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,DejaVu Sans,{cap},&H0000D7FF,&H00FFFFFF,&HD0000000,&H00000000,-1,0,0,0,100,100,0.3,0,1,3.0,1.0,2,20,20,{capv},1
Style: Term,DejaVu Sans,{term},&H00FFFFFF,&H00FFFFFF,&H14101010,&H14101010,-1,0,0,0,100,100,0.6,0,3,{termpad},0,1,54,54,120,1
Style: TermSub,DejaVu Sans,{sub},&H00D8D8D8,&H00D8D8D8,&H28101010,&H28101010,0,0,0,0,100,100,0.3,0,3,{subpad},0,1,54,54,{subv},1

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
    lines = [ASS_HEADER.format(w=W, h=H, cap=CAP_SIZE, capv=CAP_MARGIN_V,
                               term=TERM_SIZE, termpad=TERM_PAD,
                               sub=TERMSUB_SIZE, subpad=TERMSUB_PAD,
                               subv=TERMSUB_MRG_V)]
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
    for start, term, fact, until in (term_cards or []):
        a = max(start - TERM_LEAD, 0)
        b = a + TERM_HOLD
        # `until` is the moment a drawn card takes the screen. The term card
        # comes DOWN then rather than sitting on top of a card that already
        # prints the same term and the same definition in larger type - seen
        # on a rendered frame with both showing the identical sentence.
        if until is not None:
            b = min(b, until)
        # layer 1 so a card always sits above the caption layer
        lines.append(
            f"Dialogue: 1,{ass_ts(a)},{ass_ts(b)},Term,,0,0,0,,"
            f"{{\\fad(180,260)\\move({-260},{TERM_Y},{54},{TERM_Y},0,220)}}"
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


STOP_WORDS = {"a", "an", "the", "of", "on", "in", "at", "to", "and", "or",
              "with", "for", "from", "by", "up", "out", "over", "into"}


# Tags so common across Pixabay's catalogue that matching one proves
# nothing. A clip tagged "business" could be literally any clip; a clip
# tagged "warehouse" is about a warehouse.
GENERIC_TAGS = {
    "business", "person", "people", "office", "desk", "work", "working",
    "table", "hand", "hands", "man", "woman", "men", "women", "computer",
    "laptop", "technology", "background", "modern", "professional",
    "indoor", "indoors", "outdoor", "outdoors", "city", "urban", "home",
    "room", "young", "adult", "team", "meeting", "corporate", "concept",
}


# A numeral is never what a film is about. "three" turned up as a subject
# term on the business-costs script purely because "three kinds" and "all
# three" recur, and anchoring on it would have meant rejecting every clip in
# the library for the wrong reason.
_COUNTING = {"one", "two", "three", "four", "five", "six", "seven", "eight",
             "nine", "ten", "first", "second", "third", "every", "each",
             "kinds", "kind", "type", "types", "explained", "means"}


def subject_terms(scenes, title="", limit=2):
    """
    What the WHOLE video is about. From the TITLE first, narration second.

    A shot keyword describes one moment; it does not say what the film is.
    Asked for "man wearing high rise jeans", Pixabay returned an Osaka
    skyline tagged `high rise building, urban, osaka` - a perfect match on
    "high" and "rise" and nothing to do with jeans.

    THE FIRST VERSION OF THIS TOOK THE MOST FREQUENT WORDS IN THE NARRATION,
    AND THAT WAS WRONG IN A WAY WORTH RECORDING. On a real jeans script it
    chose "pattern" and "straight", because a video about leg cuts says
    "straight" constantly and a section on fabric says "pattern". Those two
    words then actively SELECTED FOR JUNK: Pixabay tags its abstract
    wallpaper clips "pattern, texture, abstract", so the anchor admitted
    coloured smoke and a particle field, admitted a highway on "straight",
    admitted a bird on "straight gourd" - and rejected the genuinely
    denim-tagged clips, because they are not tagged "pattern". 15 of 66
    shots kept footage and the ones that survived were the worst ones. An
    anchor that picks the wrong word is worse than no anchor at all.

    The title is what the film is about, by definition and by construction -
    it is written to say so. "Every Type of Men's Jeans Explained: Fits,
    Rises, and Fabrics" gives `jeans`, which is exactly the word every good
    clip carried and no bad one did. Narration frequency is kept only as a
    fallback for a script with no usable title.
    """
    from collections import Counter

    def _clean(text):
        return [w for w in re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split()
                if len(w) >= 4 and w not in STOP_WORDS
                and w not in GENERIC_TAGS and w not in _COUNTING]

    picked = _clean(title)[:limit]
    if picked:
        return set(picked)

    n = max(1, len(scenes))
    seen = Counter()
    for sc in scenes:
        seen.update(set(_clean(sc.get("narration"))))
    need = max(2, round(n * 0.5))
    return set([w for w, c in seen.most_common() if c >= need][:limit])


def _tag_list(hit):
    """Tags IN ORDER. Pixabay returns them most-relevant first."""
    return [t.strip() for t in (hit.get("tags") or "").lower().split(",")
            if t.strip()]


def _tag_set(hit):
    return set(_tag_list(hit))


# HOW FAR INTO THE TAGS THE SUBJECT MAY APPEAR.
#
# Measured on run 38's own accepted clips. Pixabay orders tags by relevance,
# so WHERE the subject appears says whether the photo is *about* it or merely
# contains it. Every genuinely good clip in that run had `jeans` or `denim` in
# the first three tags. Every bad one had it later, or not at all:
#
#   nikon, man, casio, jeans, nikon, nikon, ...      -> a CAMERA (frame 6)
#   lonely, man, sitting, shirtless, skin, ..., jeans -> a PORTRAIT (frame 3)
#   musician, country song, banjo, guitar, cowboy     -> a BANJO PLAYER
#   toddler, child, kid, infant, playing, ..., denim  -> a TODDLER
#   clothes pins, wash, laundry, clothes line, jeans  -> LAUNDRY
#   man, beach, sand, steps, jeans, vacation          -> a BEACH
#
# Three is not tuned to taste: at four the camera photo comes back.
SUBJECT_RANK_MAX = 3


def _relevant(hit, keyword, subject=None):
    """
    Does this clip have anything to do with what was asked for?

    NOTHING USED TO ASK THIS, and the result was a giant 3D "FRIDAY"
    animation appearing under narration about "total expenditures across
    reporting periods". Pixabay returned it for some keyword, the engine took
    hit number one, and no step between the search and the screen ever
    considered whether it matched.

    THE FIRST VERSION OF THIS CHECK DID NOT WORK, and it is worth being
    precise about why, because it passed a 7/7 offline test and then let 15
    out of 15 clips through on a real run - a golden retriever on a patio
    under "fixed costs, variable costs and one-off costs", a crop sprayer
    under "materials, packaging, payment processing".

    Two faults. It needed only ONE word of the search phrase to appear in the
    tags, and it matched by SUBSTRING anywhere in the tag string. Between
    them, a three-word phrase passed on one weak hit - and the word that hit
    was almost always the generic one, because Pixabay tags half its
    catalogue "business", "person", "office", "work". Matching those proves
    nothing at all: it is the stock-footage equivalent of matching "the".

    So now: tags are compared as whole tags, not substrings; a match on a
    generic tag does not count on its own; and a phrase with several
    meaningful words needs more than one of them. A clip that cannot clear
    that bar is not shown - the caller draws a card from the script's own
    words instead, which is always about the right subject.
    """
    tags = _tag_set(hit)
    if not tags:
        return False
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", keyword.lower()).split()
             if len(w) >= 4 and w not in STOP_WORDS]
    if not words:
        return False

    def hits(w):
        # whole-tag match, allowing a plural on either side. "front" must not
        # match "frontier", and "stock" must not match "stockholm".
        return any(w == t or w == t + "s" or w + "s" == t
                   or w in t.split() for t in tags)

    # THE SUBJECT ANCHOR. A clip about a different subject is wrong however
    # well it matches the words of one query - see subject_terms above.
    #
    # AND IT ASKS WHERE, NOT JUST WHETHER. The first version of this asked
    # "is there denim in this picture?" and run 38 answered honestly: a man
    # holding a Nikon, a shirtless portrait, a banjo player, a toddler, a
    # laundry line - all of them containing jeans, none of them ABOUT jeans,
    # all of them on screen under narration about jean cuts. Someone wearing
    # jeans is in a great many photographs; that is not what the shot needed.
    #
    # Pixabay sorts tags by relevance, so the position of the subject is a
    # free measurement of how central it is, and it is the one signal that
    # separates every good clip in that run from every bad one.
    if subject:
        ordered = _tag_list(hit)
        rank = next((i for i, t in enumerate(ordered)
                     if any(w == t or w == t + "s" or w + "s" == t
                            or w in t.split() for w in subject)), None)
        if rank is None or rank >= SUBJECT_RANK_MAX:
            return False

    # WITH AN ANCHOR, BEING ABOUT THE SUBJECT IS THE WHOLE TEST.
    #
    # The word rules below exist to stop one weak match carrying a clip that
    # is about something else entirely, and the anchor already settles that
    # far better. Running them anyway rejected every genuinely good clip in
    # the run this was built from: "man wearing classic straight fit"
    # against a photo tagged `jeans, pants, clothing, blue, fashion, fabric,
    # denim, denim pants` has no word in common with the query at all, and
    # is exactly the picture that shot wanted.
    #
    # Specificity is not lost by this: the query is what Pixabay searched
    # on, so a jeans-tagged clip returned for "classic straight fit" is
    # already the library's best answer to that phrase.
    if subject:
        return True

    matched = [w for w in words if hits(w)]
    strong = [w for w in matched if w not in GENERIC_TAGS]
    if not strong:
        return False                      # only generic words matched

    # HOW MANY MATCHES A PHRASE NEEDS, counted from the phrase as written -
    # not from the words left after the length filter.
    #
    # That distinction is a real bug, not a detail. "delivery van loading" is
    # three words, but "van" is three letters and got dropped, which made it
    # look like a two-word phrase and lowered the bar to a single match. It
    # then matched "loading" in `barley, field, combine, harvest, farmer,
    # loading, summer` and put a barley harvester under narration about
    # payment processing - the same crop-sprayer shot that started all of
    # this, surviving the fix that was supposed to remove it.
    asked = [w for w in re.sub(r"[^a-z0-9 ]", " ", keyword.lower()).split()
             if w not in STOP_WORDS]
    return len(matched) >= (1 if len(asked) <= 2 else 2)


def _pick(hits, keyword):
    """
    Choose from the relevant hits instead of always taking number one.

    Pixabay ranks by popularity, so hit #1 for a phrase is the single
    most-downloaded clip for it - the one every other automated channel using
    that phrase also gets. Across a channel that is a visible fingerprint.

    The choice is DETERMINISTIC, derived from the keyword, so a rebuild of
    the same script produces the same video. A random pick would make every
    render differ and every bug impossible to reproduce.
    """
    best = hits[:8]
    idx = sum(ord(c) for c in keyword) % len(best)
    return best[idx]


def _crop_to_fill(im, w=None, h=None):
    """
    Centre-crop to the target shape, then scale. Never distort.

    The previous version called resize((1920,1080)) on whatever arrived. A
    3:2 photograph - which is what most cameras produce - came out 18% wider
    than reality, so faces were visibly stretched. It only ever showed on the
    photo path, which is why a run that got real video for every shot never
    revealed it.
    """
    w = w or KB_W
    h = h or KB_H
    im = im.convert("RGB")
    sw, sh = im.size
    scale = max(w / sw, h / sh)
    nw, nh = max(w, int(round(sw * scale))), max(h, int(round(sh * scale)))
    im = im.resize((nw, nh), Image.Resampling.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return im.crop((left, top, left + w, top + h))


def _pixabay_get(endpoint, keyword, extra):
    # 20 results, not 6. Taking hit #1 of 6 every time means every channel
    # searching the same phrase gets the same clip - a wider pool leaves room
    # to discard the irrelevant ones and still have something to choose from.
    q = urllib.parse.quote(keyword.strip())
    url = (f"https://pixabay.com/api/{endpoint}?key={PIXABAY_API_KEY}"
           f"&q={q}&safesearch=true&per_page=20{extra}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read()).get("hits", [])


def fetch_pixabay_video(keyword, out_mp4, seen_ids, subject=None):
    """
    Real stock footage for this keyword, if Pixabay has one. Picks the first
    hit not already used elsewhere in this build, to cut down on the same
    clip repeating across similar-keyword shots. Never raises.
    """
    if not PIXABAY_API_KEY:
        return False
    try:
        raw = [h for h in _pixabay_get("videos/", keyword, "&orientation=horizontal")
               if h.get("id") not in seen_ids]
        hits = [h for h in raw if _relevant(h, keyword, subject)]
        if raw and not hits:
            print(f"      !! {len(raw)} clips for {keyword[:34]!r}, none "
                  f"relevant - skipping rather than showing the wrong thing",
                  flush=True)
        if not hits:
            return False
        hit = _pick(hits, keyword)
        # What we actually chose, and why it was allowed through. The first
        # version of _relevant passed 15 clips out of 15 on a real run and
        # there was no way to tell from the log WHY - the keyword was
        # printed, the tags it supposedly matched were not. Two lines of
        # logging turns "the footage is wrong again" into evidence.
        print(f"        pixabay video {keyword[:30]!r} <- "
              f"{(hit.get('tags') or '?')[:110]}", flush=True)
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


def fetch_pixabay_photo(keyword, out_png, seen_ids, subject=None):
    """Real stock photo for this keyword, resized like an AI image. Never raises."""
    if not PIXABAY_API_KEY:
        return False
    try:
        raw = [h for h in _pixabay_get("", keyword, "&image_type=photo&orientation=horizontal")
               if h.get("id") not in seen_ids]
        hits = [h for h in raw if _relevant(h, keyword, subject)]
        if not hits:
            return False
        hit = _pick(hits, keyword)
        print(f"        pixabay photo {keyword[:30]!r} <- "
              f"{(hit.get('tags') or '?')[:110]}", flush=True)
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
            _crop_to_fill(im).save(out_png, "PNG")
        return True
    except Exception as e:
        print(f"      pixabay photo error - {e}", flush=True)
        return False


def fetch_shot_asset(keyword, seed, out_stub, seen_video_ids,
                     prefer_card=False, subject=None):
    """
    Visual source chain for one shot: Pixabay video -> Pixabay photo ->
    Pollinations AI image -> flat slate. Returns ("video"|"image", path, ok)
    where ok=False only for the final flat-slate fallback. fetch_image()
    (the tail of the chain) never raises, so this never does either - worst
    case is a slate image, never a crashed build.

    With `prefer_card`, the chain STOPS when Pixabay has nothing relevant and
    returns ("none", None, False) so the caller can draw a card out of the
    script's own words instead. That is the right order for this niche and it
    is what the reference explainer does: where no real picture of the subject
    exists it puts a drawn thing on screen, never a photograph of an unrelated
    office. An AI render of "empty desk chairs" is still a photograph of an
    unrelated office - it just cost a network round trip first.
    """
    if PIXABAY_API_KEY:
        video_path = out_stub + ".mp4"
        if fetch_pixabay_video(keyword, video_path, seen_video_ids, subject):
            return "video", video_path, True
        photo_path = out_stub + ".png"
        if fetch_pixabay_photo(keyword, photo_path, seen_video_ids, subject):
            return "image", photo_path, True

    if prefer_card:
        return "none", None, False

    png_path = out_stub + ".png"
    ok = fetch_image(keyword, seed, png_path)
    return "image", png_path, ok


def _draw_shot_card(scene, out_png, section=None, n_sections=0,
                    section_name=""):
    """
    A shot drawn from the scene's own narration, for when no relevant picture
    of the subject exists.

    Best case the narration contains an actual list - "Rent, salaries,
    insurance, software" - and those four words go on screen as four bullets,
    which is precisely what the reference explainer puts there. Failing that
    it falls back to the term and its one-line definition, which is still the
    subject of the scene rather than a stranger's dog.

    Returns ("kind", path, ok, recipe). `recipe` is what the card was drawn
    FROM, so phase 3 can re-render it as an animated clip once it knows how
    many frames the shot actually gets - the bullets then arrive one at a
    time instead of the card being a still held for five seconds.

    On any failure it returns ("image", None, False, None) so the caller's
    existing slate path still catches it - a missing picture must never fail
    a build.
    """
    try:
        term = (scene.get("key_term") or "").strip()
        fact = (scene.get("key_fact") or "").strip()
        bullets = scriptbits.list_items(scene.get("narration") or "") \
            if scriptbits else []
        if not bullets and not (term or fact):
            return "image", None, False, None

        headed = section is not None and section_name
        # Don't print the same words twice on one frame. When the section
        # header already names the term, the big heading carries the line
        # that DEFINES it and the bullets carry the examples - name at the
        # top, meaning in the middle, evidence below. Rendered with both set
        # to the term, "VARIABLE COSTS" appeared twice on the same card.
        same = headed and section_name.strip().lower() == term.lower()
        heading = (fact if (same and fact) else (term or fact)).strip()
        note = None if (same or not bullets) else fact
        if not bullets and not note and heading != fact and fact:
            note = fact

        recipe = {
            "kind": "point",
            "index": (section + 1) if headed else 0,
            "total": n_sections if headed else 0,
            "name": section_name if headed else "",
            "heading": heading,
            "bullets": bullets or None,
            "note": note or None,
        }
        graphics.point_card(out_png=out_png,
                            **{k: v for k, v in recipe.items()
                               if k != "kind"})
        return "card", out_png, True, recipe
    except Exception as e:
        print(f"      !! could not draw a card ({str(e)[:60]})", flush=True)
        return "image", None, False, None


def _draw_stat_card(value, label, out_png, section=None, n_sections=0,
                    section_name=""):
    """
    One number, alone and large. graphics.stat_card has existed and been
    tested since the day this design was written and NOTHING has ever called
    it - the fourth module in this repo built and left orphaned, after
    modes.py, overview_clip and point_card. In a money channel a figure
    filling the frame is about the most edited thing on offer, and it was
    sitting unused while the engine looked for stock photographs instead.
    """
    try:
        headed = section is not None and section_name
        recipe = {
            "kind": "stat",
            "index": (section + 1) if headed else 0,
            "total": n_sections if headed else 0,
            "name": section_name if headed else "",
            "value": value,
            "label": label,
        }
        graphics.stat_card(out_png=out_png,
                           **{k: v for k, v in recipe.items() if k != "kind"})
        return "card", out_png, True, recipe
    except Exception as e:
        print(f"      !! could not draw a stat card ({str(e)[:60]})",
              flush=True)
        return "image", None, False, None


def _distrusted_scenes(script):
    """
    Scene indices (0-based) carrying an unfixed HARD red-team finding.

    Only findings that name a scene can be acted on here: a finding with
    scene=None is about the script as a whole ("too-complex", "fluff") and
    says nothing about which term is invented, so it cannot single one out.

    Reads defensively. This runs on the engine side of a file written by
    another stage, and every safety check in this project that quietly
    no-opped on a missing field (CLAUDE.md 11) did so because it trusted the
    shape of its input. An absent `red_team` key means the red team did not
    run - not that the script is clean - so that case is reported by the
    publish gate, not silently treated as a pass here.
    """
    out = set()
    findings = script.get("red_team")
    if not isinstance(findings, list):
        return out
    n = len(script.get("scenes") or [])
    for f in findings:
        if not isinstance(f, dict):
            continue
        if str(f.get("severity", "")).strip().lower() != "hard":
            continue
        s = f.get("scene")
        # redteam.py numbers scenes from 1; a 0 or a None is "whole script".
        try:
            k = int(s) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= k < n:
            out.add(k)
    return out


def _draw_compare_card(pair, out_png, eyebrow=None):
    """
    Two neighbouring members side by side, with what separates them.

    Both columns are the writer's own key_term and key_fact, already
    fact-checked - so this cannot introduce a claim. That is the whole
    safety argument for putting a diagram on screen at all: a made-up
    comparison is a made-up fact carrying a diagram's authority.
    """
    try:
        recipe = {"kind": "compare", "left": pair[0], "right": pair[1],
                  "eyebrow": eyebrow}
        graphics.compare_card(pair[0], pair[1], out_png=out_png,
                              eyebrow=eyebrow)
        return "card", out_png, True, recipe
    except Exception as e:
        print(f"      !! could not draw a comparison ({str(e)[:60]})",
              flush=True)
        return "image", None, False, None


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

    if asset_kind == "cardclip":
        # An ANIMATED card, already rendered by graphics.py at exactly this
        # frame count and exactly this size. Nothing to scale, nothing to
        # crop, no grade and no motion - it only needs its timebase
        # normalised so the stream-copy concat downstream stays legal.
        vf = f"fps={FPS},format=yuv420p"
    elif asset_kind == "card":
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
    if overlay_png and asset_kind not in ("card", "cardclip") \
            and os.path.exists(overlay_png):
        cmd += ["-i", overlay_png]
        cmd += ["-filter_complex",
                f"[0:v]{vf}[b];[b][1:v]overlay=0:0:format=auto[v]",
                "-map", "[v]"]
    else:
        cmd += ["-vf", vf]

    if asset_kind == "cardclip":
        # belt and braces: graphics.py writes exactly this many frames, and
        # this makes it impossible for a longer one to slip through and push
        # the scene out of sync with its own narration
        cmd += ["-frames:v", str(frames)]

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


def split_frames(frames_total, n, first_frames=None):
    """
    How a scene's frame budget is divided between its shots.

    Even, except that `first_frames` pins the FIRST shot and splits what is
    left across the others. The section card needs the pin, and the reason is
    measured, not stylistic: with an even split and two shots in a scene the
    card took half the scene, and sampling a finished 40-second video every
    half second showed 51% of it was one card that never changed - the exact
    failure this project already wrote down as a rule. The card is a beat,
    not a shot; it gets a beat's worth of time and the footage gets the rest.

    A function rather than inline arithmetic because build() has to know the
    same boundaries render_scene uses - to tell which shot a term card would
    land on. Two copies of this sum would drift, and the symptom would be a
    caption suppressed over the wrong shot, which nothing would ever catch.

    The counts always sum to exactly frames_total; the remainder goes to the
    last shot.
    """
    if first_frames and n > 1:
        # clamped so the pin can never starve the remaining shots, however
        # short the scene turns out to be
        head = max(1, min(int(first_frames), frames_total - (n - 1)))
        rest = frames_total - head
        base = rest // (n - 1)
        counts = [head] + [base] * (n - 1)
        counts[-1] += rest - base * (n - 1)
        return counts
    base = frames_total // n
    counts = [base] * n
    counts[-1] += frames_total - base * n
    return counts


def render_scene(scene_idx, assets, mp3, out_mp4, work_dir, motion_start,
                  first_scene, last_scene, frames_total, overlay_png=None,
                  first_frames=None):
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
    frame_counts = split_frames(frames_total, n, first_frames)

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

    # WHICH SCENES ARE ACTUALLY ITEMS IN THE LIST.
    #
    # This used to be "every scene's key_term", and that is how RUNWAY ended
    # up on screen as a type of business expense. Runway is not an expense -
    # it is months of cash left at the current burn - and it was the CLOSE
    # beat, the scene that sums up and sends the viewer off. A closing scene
    # is not a member of the taxonomy the video is listing, and neither is
    # the opening ANSWER, the FRAME, the EDGE case or the APPLY.
    #
    # modes.py already says which beat carries a list item: CATEGORY in an
    # explainer ("one scene per type") and STEP in a guide ("one scene per
    # step"). Nothing else does. A story has neither, so it gets no
    # checklist at all, which is correct - a story is not a list.
    #
    # This does not fix the taxonomy problem in section 11 of CLAUDE.md;
    # nothing here checks that the CATEGORY scenes are genuinely members of
    # the category. It fixes the narrower bug of putting scenes on the list
    # that never claimed to be items in the first place.
    MEMBER_BEATS = {"CATEGORY", "STEP"}
    # scenes that belong to no section at all - the wrap-up, whatever the
    # mode calls it
    CLOSING_BEATS = {"CLOSE", "RESONANCE"}

    # NEVER PUT A KNOWN-BAD CLAIM ON A CARD.
    #
    # Run 38 wrote "top block" as a structural measurement of jeans. The red
    # team caught it - HARD, twice, "not defined in any of the listed
    # sources" - and the publish gate said so. Then the repair could not run
    # because every provider was out of quota, and the engine, which never
    # reads the findings, printed TOP BLOCK across the screen in the largest
    # type in the video, with a definition under it.
    #
    # Rewriting the narration needs a model and there may not be one. Not
    # AMPLIFYING it needs nothing. The narration still says the sentence -
    # that is a script problem and it stays visible in the publish gate - but
    # a term the red team has already called invented does not also get a
    # card, a checklist row or a diagram column. That is 4.20's rule read the
    # other way round: a made-up term on a card carries a card's authority,
    # and the card is the part we can withhold for free.
    # Map through the narration filter above. `scenes` drops any scene with
    # empty narration, but the findings index the UNFILTERED list - so one
    # dropped scene shifts every index after it and this would gag the wrong
    # scene while leaving the invented one on screen. Match the objects, not
    # their positions.
    _raw = data["scenes"] if isinstance(data, dict) else data
    _bad = {id(_raw[k]) for k in _distrusted_scenes(data) if 0 <= k < len(_raw)}
    distrusted = {i for i, s in enumerate(scenes) if id(s) in _bad}
    if distrusted:
        print(f"   !! {len(distrusted)} scene(s) carry an unfixed HARD "
              f"red-team finding - their terms get no card, no checklist "
              f"row and no diagram column: "
              f"{', '.join(sorted(str(i + 1) for i in distrusted))}")
    member_idx = [i for i, s in enumerate(scenes)
                  if (s.get("beat") or "").strip().upper() in MEMBER_BEATS
                  and (s.get("key_term") or "").strip()
                  and i not in distrusted]
    members = [scenes[i]["key_term"].strip() for i in member_idx]
    # where each scene sits in the list, or None if it is not an item
    member_of = {si: k for k, si in enumerate(member_idx)}
    use_cards = graphics is not None and len(members) >= 2

    # The opening card's eyebrow: the video's own title, which is what the
    # list is a list OF. Falls back to a neutral line when there isn't one.
    #
    # Defined HERE, beside `members`, not down in the render phase. It used to
    # live below and the asset phase - which runs first - reads it too, so
    # every gagged shot hit "cannot access free variable 'title_eyebrow'".
    # The try/except around that call swallowed it and fell back to a slate,
    # so the build stayed green and the feature silently did nothing: 5.8's
    # shape exactly.
    title_eyebrow = ((data.get("title") if isinstance(data, dict) else "")
                     or "In this video").strip()[:60]

    # Which section each scene BELONGS to, item or not, so a drawn shot card
    # can carry the same header the footage in that section carries. None
    # means no section: before the first item, or the closing scene.
    section_of, _run = {}, None
    for _i, _s in enumerate(scenes):
        if _i in member_of:
            _run = member_of[_i]
        section_of[_i] = None if (_s.get("beat") or "").strip().upper() \
            in CLOSING_BEATS else _run

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
    card_recipes = {}

    # Drawing a card needs graphics.py; without it the old chain (AI image,
    # then slate) is still the only thing available.
    prefer_card = graphics is not None

    # WHERE THE SCRIPT ALREADY CONTAINS THE PICTURE.
    #
    # When the narration says "Rent, salaries, insurance, software", those
    # four words ARE the visual for that moment, and no stock library has
    # anything better. Two real runs in a row proved the point negatively:
    # the photo step always finds something, so it found something every
    # time and not one drawn card was ever reached - the video kept showing
    # a retro desk fan while the narration listed the four things that
    # actually matter.
    #
    # So a scene whose narration contains a genuine list spends ONE shot
    # showing it, and that shot is index 1: immediately after the section
    # card, which is where the list is spoken. The rest of the scene still
    # uses footage. This is the one place the engine stops asking Pixabay a
    # question it cannot answer.
    # A DIAGRAM, on the beat that is about how things relate.
    #
    # The owner asked for diagrams and this project has only ever drawn
    # lists. modes.py puts EDGE (and APPLY) after the categories, which is
    # where a video says how neighbouring types differ - so that is where a
    # comparison belongs. Needs two members already explained, or there is
    # nothing to compare.
    COMPARE_BEATS = {"EDGE", "APPLY"}
    comparey = {}
    _pairs_drawn = set()
    if graphics is not None:
        for i, sc_ in enumerate(scenes):
            if (sc_.get("beat") or "").strip().upper() not in COMPARE_BEATS:
                continue
            prior = [k for k in member_idx if k < i]
            if len(prior) < 2:
                continue
            # ONE COMPARISON PER PAIR, and 4.14 is why.
            #
            # Run 38 has both EDGE and APPLY after the last CATEGORY, so both
            # took the same "last two members" and drew the identical diagram
            # twice, 28 seconds apart. A diagram earns its place by saying
            # something new; the second one says exactly what the first did.
            pair_key = tuple(prior[-2:])
            if pair_key in _pairs_drawn:
                continue
            _pairs_drawn.add(pair_key)
            cols = []
            for k in prior[-2:]:
                lines = [(scenes[k].get("key_fact") or "").strip()]
                if scriptbits:
                    lines += scriptbits.list_items(
                        scenes[k].get("narration") or "")[:2]
                cols.append((scenes[k].get("key_term") or "",
                             [ln for ln in lines if ln][:3]))
            comparey[i] = tuple(cols)

    subject = subject_terms(
        scenes, (data.get("title") if isinstance(data, dict) else "") or "")
    if subject:
        print(f"      subject anchor: {', '.join(sorted(subject))} - a clip "
              f"has to be about this, not just match the query", flush=True)

    # A distrusted scene draws nothing from its own narration either: the
    # list and the figure are pulled straight out of the sentences the red
    # team called unsupported, so putting them on a card is the same act as
    # putting the term on one.
    listy = {i for i, s in enumerate(scenes)
             if scriptbits and scriptbits.list_items(s.get("narration") or "")
             and i not in distrusted}
    # A scene with no list but a real figure in it gets that number on
    # screen instead. The list wins where a scene has both: four things
    # named is more of the explanation than one number is.
    statty = {}
    if scriptbits:
        for i, sc_ in enumerate(scenes):
            if i in listy or i in comparey or i in distrusted:
                continue
            n = scriptbits.headline_number(sc_.get("narration") or "")
            if n:
                statty[i] = n

    async def do_asset(item):
        i, j, kw, seed, out_stub = item
        # Only when the scene has room for footage as well. With two shots,
        # shot 0 becomes the section card and forcing shot 1 to a list card
        # would make the whole scene two drawn stills - and the second one
        # would hold for seven seconds without moving, which is the exact
        # failure 4.9 exists to prevent. Three shots means card, list,
        # picture.
        if j == 1 and graphics is not None \
                and (i in listy or i in statty or i in comparey) \
                and len(shots_per_scene[i]) >= 3:
            sec = section_of.get(i)
            common = dict(section=sec, n_sections=len(members),
                          section_name=(members[sec] if sec is not None
                                        else ""))
            if i in comparey:
                kind, path, ok, recipe = _draw_compare_card(
                    comparey[i], out_stub + ".png",
                    eyebrow="what separates them")
                what = f"{comparey[i][0][0]} vs {comparey[i][1][0]}"
            elif i in listy:
                kind, path, ok, recipe = _draw_shot_card(
                    scenes[i], out_stub + ".png", **common)
                what = ", ".join(scriptbits.list_items(scenes[i]["narration"]))
            else:
                value, lab = statty[i]
                kind, path, ok, recipe = _draw_stat_card(
                    value, lab, out_stub + ".png", **common)
                what = f"{value} - {lab}"
            if kind == "card":
                card_recipes[(i, j)] = recipe
                asset_paths[i][j] = (kind, path)
                tag = ("DIAGRAM" if i in comparey
                       else "LIST" if i in listy else "STAT")
                print(f"      shot scene {i+1} #{j+1} [{tag}] | {what[:44]}",
                      flush=True)
                return False, True

        kind, path, ok = await asyncio.to_thread(
            fetch_shot_asset, kw, seed, out_stub, seen_pixabay_ids,
            prefer_card, subject)
        if kind == "none" and i in distrusted:
            # "ALREADY FACT-CHECKED" IS EXACTLY WHAT IS NOT TRUE HERE.
            #
            # The comment below is the whole justification for this card: the
            # script's own words are safe to show because they have been
            # checked. On a scene the red team flagged HARD, that premise is
            # false - and this is the card that printed TOP BLOCK across four
            # frames of run 39 in the largest type in the video.
            #
            # 4.22 was written for exactly this and gagged the term card, the
            # checklist, the diagram, the list card and the stat card. It
            # missed this one, which is the one that actually reached the
            # screen. The log said "term card for 'top block' withheld" while
            # TOP BLOCK was the headline. A suppression that reports success
            # and leaves the words on screen is worse than none.
            # WITHHOLD THE CLAIM, NOT THE PICTURE.
            #
            # The first version of this drew a black slate, and run 41 showed
            # what that costs: scenes 5 and 8 became long stretches of black
            # screen, 38 of 129 shots, three of the twelve contact-sheet
            # frames pure black. Trading an invented term for a void is not a
            # fix - 4.9 already says the worst thing in this video is a frame
            # that carries nothing, and black carries less than a still card.
            #
            # The section header is SAFE to show even here: headers come from
            # `members`, which is built only from scenes that are NOT gagged,
            # so the header on a gagged scene was written by a different,
            # trusted scene. It says where the viewer is without asserting
            # anything the red team doubted.
            path = out_stub + ".png"
            sec = section_of.get(i)
            drew = False
            if graphics is not None and sec is not None and len(members) >= 2:
                try:
                    # The CHECKLIST, not a bare header. A header-only card was
                    # the first attempt and it rendered as a title over four
                    # fifths of empty page - better than black and still a
                    # frame carrying almost nothing. The checklist is built
                    # entirely from ungagged scenes, so every word on it is
                    # trusted, it fills the frame, and it tells the viewer
                    # where they are in the video - which is the one useful
                    # thing that can honestly be said during a scene whose own
                    # claims are in doubt.
                    graphics.overview_card(members, current=sec,
                                           eyebrow=title_eyebrow,
                                           out_png=path)
                    drew = True
                except Exception as e:
                    print(f"      !! checklist card failed ({str(e)[:50]})",
                          flush=True)
            if not drew:
                # No section to name - the opening or the close. Nothing
                # trustworthy is left to draw, so a flat slate it is.
                Image.new("RGB", (KB_W, KB_H), (11, 12, 16)).save(path, "PNG")
            kind, ok = "image", False
            print(f"      shot scene {i+1} #{j+1} "
                  f"[{'HEADER' if drew else 'SLATE'}] | claim withheld - "
                  f"unfixed HARD red-team finding on this scene", flush=True)
        elif kind == "none":
            # NOTHING RELEVANT EXISTS FOR THIS KEYWORD, so put the script's
            # own words on screen. They are already written, already
            # fact-checked and already spoken aloud a moment later, so a card
            # built from them cannot be off-topic - which is more than could
            # be said for the golden retriever Pixabay offered for "fixed
            # costs, variable costs and one-off costs".
            sec = section_of.get(i)
            kind, path, ok, recipe = _draw_shot_card(
                scenes[i], out_stub + ".png",
                section=sec, n_sections=len(members),
                section_name=(members[sec] if sec is not None else ""))
            if kind == "card":
                card_recipes[(i, j)] = recipe
        asset_paths[i][j] = (kind, path)
        tag = {"video": "video", "card": "CARD"}.get(
            kind, "image" if ok else "SLATE")
        print(f"      shot scene {i+1} #{j+1} [{tag}] | {kw[:44]}", flush=True)
        return kind == "video", ok

    results = []
    for b in range(0, len(work_items), 5):
        results += await asyncio.gather(
            *(do_asset(item) for item in work_items[b:b + 5]))
    real_footage = sum(1 for is_video, _ in results if is_video)
    failed_images = sum(1 for _, ok in results if not ok)
    drawn = sum(1 for row in asset_paths for k, _ in row if k == "card")

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
    if use_cards:
        n_cards = len(members) + (0 if 0 in member_of else 1)
        print(f"      drawing {n_cards} cards + headers, list of "
              f"{len(members)}: {', '.join(members)}", flush=True)
    elif graphics is not None:
        print("      no list beats (CATEGORY/STEP) in this script "
              "- footage only, no checklist", flush=True)

    print(f"\n[3/3] render x{total} scenes ({total_shots} shots) ...", flush=True)
    cues, parts, timeline, motion_cursor = [], [], 0.0, 0
    term_cards, sfx_events = [], []
    last_member = None
    header_fails = []

    for i, sc in enumerate(scenes):
        mp4 = os.path.join(WORK, f"s{i:03d}.mp4")
        frames_total = math.ceil(durs[i] * FPS) + 3

        assets, overlay, card_frames = list(asset_paths[i]), None, None
        # A card is drawn on the opening scene (the whole list, before
        # anything starts - what the reference explainer does with its first
        # frame) and on every scene that IS an item. The scenes in between -
        # a FRAME, an EDGE case, the CLOSE - carry the header of the section
        # they belong to and no card, because nothing on the list changed.
        here = member_of.get(i)
        show_card = use_cards and (here is not None or i == 0)
        if show_card:
            try:
                # The card is a BEAT, so it gets a fixed short length rather
                # than an equal share of the scene. Capped at 40% of the
                # scene as well, so a very short scene cannot become mostly
                # card.
                want = OPEN_CARD_SECONDS if i == 0 else CARD_SECONDS
                card_frames = min(int(want * FPS), int(frames_total * 0.40))
                card_frames = max(card_frames, FPS)   # never below one second

                card = os.path.join(WORK, f"card{i:03d}.mp4")
                if here is None:
                    # the opening: rows arrive one after another, so the
                    # viewer watches the whole list being built before a
                    # single section has started
                    graphics.overview_clip(
                        members, None, card, frames=card_frames, fps=FPS,
                        eyebrow=title_eyebrow,
                        tmp=os.path.join(WORK, f"ovw{i:03d}"))
                else:
                    # tick the one just finished, move the box to this one.
                    # Only what changed moves.
                    graphics.advance_clip(
                        members, here, card, frames=card_frames, fps=FPS,
                        eyebrow=f"{here+1} of {len(members)}",
                        previous=(here - 1) if here else last_member,
                        tmp=os.path.join(WORK, f"adv{i:03d}"))
                    # the tick lands about a third of the way in (see the
                    # stagger in advance_clip); mark it with a sound
                    if here or last_member is not None:
                        sfx_events.append(
                            (timeline + card_frames / FPS * 0.30, "tick"))

                # the card REPLACES the first stock shot rather than being
                # added, so the scene still fits its narration exactly
                assets = [("cardclip", card)] + assets[1:] if len(assets) > 1 \
                    else [("cardclip", card)]
            except Exception as e:
                print(f"      !! card for scene {i+1} failed ({str(e)[:70]}) "
                      f"- falling back to footage only", flush=True)
                assets, card_frames = list(asset_paths[i]), None

        # The header names the SECTION, so a non-item scene keeps the header
        # of the section it sits inside rather than losing its orientation.
        #
        # Except the closing scene, which sits inside no section - it is the
        # wrap-up. Carrying the last section's header through it labelled the
        # summary "ONE-OFF COSTS 3 OF 3" while the narration had moved on to
        # something else entirely, which is worse than no orientation: it is
        # wrong orientation. Seen on a rendered frame.
        closing = (sc.get("beat") or "").strip().upper() in CLOSING_BEATS
        if use_cards and not closing \
                and (here is not None or last_member is not None):
            at = here if here is not None else last_member
            try:
                overlay = os.path.join(WORK, f"head{i:03d}.png")
                graphics.section_overlay(at + 1, len(members),
                                         members[at], overlay)
            except Exception as e:
                print(f"      !! header for scene {i+1} failed "
                      f"({str(e)[:60]})", flush=True)
                overlay = None
                header_fails.append(i + 1)
        if here is not None:
            last_member = here

        # DRAWN CARDS BECOME CLIPS, with their bullets arriving.
        #
        # This has to happen here rather than in phase 2 because it needs the
        # shot's exact frame count, and that is only known once the section
        # card has claimed its fixed dwell. A still held for five seconds is
        # the hole 4.9 exists to close, and it was reopening on every drawn
        # shot while the section card beside it animated.
        counts = split_frames(frames_total, len(assets), card_frames)
        for j, (kind, path) in enumerate(assets):
            recipe = card_recipes.get((i, j))
            if kind != "card" or not recipe:
                continue
            animate = {"stat": graphics.stat_clip,
                       "compare": graphics.compare_clip,
                       "point": graphics.point_clip}[recipe["kind"]]
            if recipe["kind"] == "point" and not recipe.get("bullets"):
                continue          # nothing to arrive; the still is correct
            try:
                clip = os.path.join(WORK, f"pt{i:03d}_{j:02d}.mp4")
                animate(out_mp4=clip, frames=counts[j], fps=FPS,
                        tmp=os.path.join(WORK, f"pt{i:03d}{j:02d}"),
                        **{k: v for k, v in recipe.items() if k != "kind"})
                assets[j] = ("cardclip", clip)
            except Exception as e:
                # the still is already drawn and correct; losing the
                # animation is not worth losing the shot
                print(f"      !! could not animate card {i+1}.{j+1} "
                      f"({str(e)[:60]}) - using the still", flush=True)

        render_scene(i, assets, mp3s[i], mp4, WORK, motion_cursor,
                     first_scene=(i == 0), last_scene=(i == total - 1),
                     frames_total=frames_total, overlay_png=overlay,
                     first_frames=card_frames)
        motion_cursor += len(assets)

        words = word_lists[i] or estimate_word_times(
            scenes[i]["narration"].strip(), durs[i])
        actual = probe(mp4)
        cues += group_words(words, timeline)

        # Term card for this scene, anchored to the moment the term is
        # actually spoken. No match -> no card; a card at the wrong moment
        # is worse than none.
        term = (sc.get("key_term") or "").strip()
        # A term the red team called invented gets no definition card. This
        # is the exact card that put TOP BLOCK on screen in run 38, in the
        # largest type in the video, with a made-up definition under it,
        # after the red team had already flagged it HARD twice.
        if term and i in distrusted:
            print(f"        (term card for {term!r} withheld - an unfixed "
                  f"HARD red-team finding on this scene)")
            term = ""
        if term:
            at = find_term_time(words, term)
            if at is not None:
                at += timeline
                # NOT while the section card is up.
                #
                # Found on a rendered frame: the term card slid in over the
                # section card and printed "GROSS MARGIN" across the
                # checklist's own GROSS MARGIN row - the same two words
                # twice, on top of each other. The three-band layout
                # (header / term / captions) assumes the middle of the frame
                # is a picture; on a section card the middle of the frame is
                # the list.
                #
                # It is redundant there anyway: the boxed row already names
                # the term. What the term card adds that the list does not
                # is the one-line definition, and that is worth keeping - so
                # it is pushed to the moment the card ends rather than
                # dropped. The term is the subject of the whole scene, so
                # landing a beat later still lands on its own subject; what
                # 4.3 forbids is a card at a moment the narration never
                # says the word at all.
                # Which shot is actually on screen at that moment.
                # NOT named `run` - that is this module's ffmpeg runner, and
                # shadowing it here made every later ffmpeg call raise
                # "'float' object is not callable" from inside build().
                bounds, acc = [], 0.0
                for cnt in split_frames(frames_total, len(assets),
                                        card_frames):
                    acc += cnt / FPS
                    bounds.append(acc)
                k = next((x for x, b in enumerate(bounds)
                          if at - timeline < b), len(assets) - 1)
                # A shot is a DRAWN card if it has a recipe. Kind alone can
                # no longer tell: animating the content cards turned them
                # into "cardclip", the same kind the section card uses, and
                # the two need opposite treatment - wait for a section card,
                # get out of the way of a drawn one. Rendered, that mistake
                # put the term card straight across "60% OF REVENUE BEFORE A
                # SINGLE SALE".
                # NOT named `drawn` - that is the run-wide count of drawn
                # shots printed in the summary, and shadowing it here made
                # the summary print a function object. Second time a local
                # has shadowed a module name in this file today.
                def is_drawn(x):
                    return (i, x) in card_recipes

                kind_here = assets[k][0]
                if is_drawn(k):
                    # A DRAWN CARD is already doing this card's job - it
                    # names the term in large type and prints the same
                    # definition under it. Rendered, the two appeared
                    # together and said exactly the same thing twice.
                    print(f"        (term card for {term!r} skipped - the "
                          f"drawn card already says it)", flush=True)
                    at = None
                elif kind_here == "cardclip":
                    # the section card: wait for it (see above)
                    at = timeline + bounds[k] + 0.15

                # If a DRAWN card starts later in this scene, the term card
                # comes down when it does.
                until = next((timeline + bounds[x - 1]
                              for x in range(k + 1, len(assets))
                              if is_drawn(x)), None)

                if at is None:
                    pass                 # deliberately suppressed just above
                elif until is not None and until - at < 1.2:
                    # too little of it would be seen to be worth showing
                    print(f"        (term card for {term!r} skipped - a "
                          f"drawn card takes the screen)", flush=True)
                elif at < timeline + actual - 0.6:
                    term_cards.append((at, term,
                                       (sc.get("key_fact") or "").strip(),
                                       until))
                    # a card appearing silently is half an edit
                    sfx_events.append((at - TERM_LEAD, "pop"))
                else:
                    print(f"        (no room for term card {term!r})",
                          flush=True)
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
        for _, p in assets:
            if p not in [q for _, q in asset_paths[i]] and os.path.exists(p):
                os.remove(p)   # the rendered card clip

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
    # MB per minute is the number that actually decides whether a long video
    # fits: GitHub Free gives 500 MB of artifact storage in total, so a
    # 16-minute cut at 13 MB/min fills a fifth of it on its own. Printing the
    # rate means the next resolution or CRF change can be judged against a
    # measurement instead of a guess.
    print(f"   size     : {size:.1f} MB  ({size / max(final_dur/60, 0.01):.1f} "
          f"MB/min -> a 16 min cut would be "
          f"~{size / max(final_dur/60, 0.01) * 16:.0f} MB)", flush=True)
    print(f"   subtitles: {len(cues)} cues", flush=True)
    if PIXABAY_API_KEY:
        print(f"   visuals  : {real_footage}/{total_shots} real Pixabay footage",
              flush=True)
    if drawn:
        # Not a failure line. A drawn card is the CORRECT answer when the
        # stock library has nothing about the subject, and the ratio is the
        # single most useful number for judging a run in this niche.
        print(f"   drawn    : {drawn}/{total_shots} shots drawn from the "
              f"script (no relevant footage existed)", flush=True)
    if header_fails:
        # LOUD, because a missing section header is invisible in a green log
        # and is the one device the whole layout is built around. It went
        # missing from an entire 8-scene video behind a caught exception.
        print(f"   !! NO SECTION HEADER on {len(header_fails)} scene(s): "
              f"{header_fails} - the video has lost its orientation device",
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
  
