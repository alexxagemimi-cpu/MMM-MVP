#!/usr/bin/env python3
"""
graphics.py — the on-screen system, copied from what actually works.

STUDIED, NOT INVENTED
---------------------
Frame-by-frame analysis of "Every Operating System Explained in 8 Minutes",
plus the owner's own screen recording of one of our runs. What the reference
actually does, and what we were doing instead:

1. IT OPENS ON THE WHOLE LIST. Frame one is a grid of all eight operating
   systems, logo and name. The viewer sees everything they are about to learn
   before a word is spoken. Ours opened on a soft-focus coffee cup.

2. A SECTION HEADER NEVER LEAVES THE SCREEN. Top-left an icon, top-centre the
   section name in large type - "WINDOWS", then "LINUX". It persists across
   every shot of that section, so the viewer always knows where they are.
   Ours had no orientation at all.

3. NOTHING CUTS. Sampling one frame per second through a section shows the
   MS-DOS logo holding on the left while screenshots appear beside it and are
   swapped. Elements ACCUMULATE on a stable canvas. Scene-change detection
   finds zero hard cuts in 43 seconds. We cut between unrelated full-frame
   stock clips every five seconds.

4. WHITE BACKGROUND, BLACK TEXT. Not cinematic, not dark. It reads as a
   document, which is the point.

5. REAL ARTIFACTS OF THE SUBJECT. Actual MS-DOS boot output, the actual
   Windows 1.01 splash, the actual Windows 10 desktop, real logos. Not one
   frame of stock footage. When a picture of the real thing does not exist,
   a simple flat icon or a stick-figure doodle stands in - never a
   photograph of an unrelated office.

The dark card design that preceded this file was wrong, and the owner said so
plainly. This is the corrected system.

    python3 graphics.py     # renders one of each to graphics_demo/
"""

import os
import subprocess

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720

HERE = os.path.dirname(os.path.abspath(__file__))
F_DISPLAY = os.path.join(HERE, "assets", "fonts", "Anton-Regular.ttf")
F_BODY    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
F_TEXT    = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

PAPER   = (255, 255, 255)
INK     = (17, 17, 17)
MUTED   = (122, 128, 132)
FAINT   = (214, 219, 222)
ACCENT  = (225, 27, 21)      # the single red, as the reference thumbnails use
DONE    = (34, 148, 90)

MARGIN  = 56
HEAD_H  = 108               # the band the section header owns, on every frame

# The lowest line anything drawn here may reach. Below it belongs to the two
# things engine.py burns ON TOP of these cards afterwards: the term card
# (its box reaches ~554) and the caption block (~564 down). A card that draws
# into that region looks fine on its own and is a pile-up in the finished
# video - which is exactly how it was found.
SAFE_BOTTOM = 436


def _f(p, s):
    return ImageFont.truetype(p, s)


def _w(d, t, f):
    return d.textbbox((0, 0), t, font=f)[2]


def _wrap(d, text, f, mw):
    out, cur = [], []
    for wd in text.split():
        t = cur + [wd]
        if cur and _w(d, " ".join(t), f) > mw:
            out.append(" ".join(cur)); cur = [wd]
        else:
            cur = t
    if cur:
        out.append(" ".join(cur))
    return out


def _fit(d, text, path, mw, hi, lo=16, max_lines=1):
    for s in range(hi, lo - 1, -2):
        f = _f(path, s)
        ls = _wrap(d, text, f, mw)
        if len(ls) <= max_lines:
            return f, ls
    f = _f(path, lo)
    return f, _wrap(d, text, f, mw)[:max_lines]


def _ease(t):
    """Ease-out cubic. Linear motion is what reads as machine-made."""
    return 1 - (1 - t) ** 3


# ---------------------------------------------------------------------------
# the persistent header
# ---------------------------------------------------------------------------
def draw_header(d, index, total, name):
    """
    Number badge left, section name centred, thin rule under. Drawn onto every
    frame of a section - it is the orientation device, and it works precisely
    because it does NOT move or animate between shots.
    """
    badge = 56
    x, y = MARGIN, MARGIN - 18
    d.ellipse((x, y, x + badge, y + badge), fill=ACCENT)
    nf = _f(F_DISPLAY, 30)
    nt = f"{index:02d}"
    d.text((x + (badge - _w(d, nt, nf)) / 2, y + 10), nt, font=nf, fill=PAPER)

    sf = _f(F_BODY, 17)
    d.text((x + badge + 16, y + 4), f"OF {total:02d}", font=sf, fill=MUTED)

    f, lines = _fit(d, name.upper(), F_DISPLAY, W - MARGIN * 2 - 220, 54, 26)
    t = lines[0] if lines else name.upper()
    d.text(((W - _w(d, t, f)) / 2, y - 2), t, font=f, fill=INK)
    d.line((MARGIN, HEAD_H, W - MARGIN, HEAD_H), fill=FAINT, width=2)


def section_overlay(index, total, name, out_png):
    """
    The header alone on transparency, for compositing over stock footage or a
    photograph so those shots carry the same orientation as the drawn cards.
    A white plate sits behind it so black type stays legible over any picture.

    The plate is FULLY opaque, and that is deliberate. It was 232/255 first,
    which looks like a reasonable "barely there" choice and is not: measured
    on a rendered frame over a saturated clip, the footage read straight
    through the band, so the section name sat on whatever colour happened to
    be behind it and the header stopped being a stable white strip. The whole
    point of this design is that it reads as a document (see note 4 at the top
    of this file); a see-through header is a tinted header, and a tinted
    header changes every time the shot behind it does.
    """
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    plate = Image.new("RGBA", (W, HEAD_H + 4), (255, 255, 255, 255))
    img.paste(plate, (0, 0))
    d = ImageDraw.Draw(img)
    draw_header(d, index, total, name)
    img.save(out_png, "PNG")
    return out_png


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------
def overview_card(items, current=None, eyebrow=None, out_png="card.png"):
    """
    EVERY item at once, numbered, with the finished ones ticked and the
    current one boxed in red.

    Two jobs, which is why it earns its place: it is the opening frame (here
    is everything you are about to learn) and it is the between-sections beat
    the owner asked for (you have finished two of six, this is number three).
    The reference video opens on exactly this and it is the clearest possible
    statement of what the next eight minutes contain.
    """
    items = [i.strip() for i in items if i and i.strip()]
    if not items:
        raise ValueError("overview_card needs items")
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)

    n = len(items)
    cols = 2 if n <= 8 else 3
    rows = -(-n // cols)
    top = MARGIN + (54 if eyebrow else 8)

    if eyebrow:
        ef = _f(F_BODY, 20)
        d.text((MARGIN, MARGIN - 12), eyebrow.upper(), font=ef, fill=ACCENT)

    cw = (W - MARGIN * 2) / cols
    ch = (H - top - MARGIN) / rows
    lab_size = int(min(ch * 0.30, cw * 0.11, 40))

    for i, label in enumerate(items):
        r, c = divmod(i, cols)
        x = MARGIN + c * cw
        y = top + r * ch
        done = current is not None and i < current
        live = current is not None and i == current

        if live:
            d.rounded_rectangle((x - 6, y - 4, x + cw - 18, y + ch - 14),
                                radius=10, outline=ACCENT, width=3)

        bs = int(lab_size * 1.15)
        by = y + (ch - 22 - bs) / 2
        col = DONE if done else (ACCENT if live else FAINT)
        d.ellipse((x + 10, by, x + 10 + bs, by + bs), fill=col)
        nf = _f(F_DISPLAY, int(bs * 0.56))
        nt = "✓" if done else f"{i+1}"
        tf = _f(F_BODY, int(bs * 0.62)) if done else nf
        d.text((x + 10 + (bs - _w(d, nt, tf)) / 2, by + bs * 0.20), nt,
               font=tf, fill=PAPER)

        f, lines = _fit(d, label.upper(), F_DISPLAY, cw - bs - 46,
                        lab_size, 15, max_lines=2)
        ly = y + (ch - 22 - len(lines) * f.size * 1.08) / 2
        # current=None means "no section yet" - the opening frame, where the
        # whole list is being shown before anything has started. There every
        # item is live-to-come, so every item is set in ink. Greying them all
        # (what falling through to MUTED did) made the one frame that is
        # supposed to say "here is everything you are about to learn" look
        # like a list of things already dismissed.
        strong = live or done or current is None
        for ln in lines:
            d.text((x + 10 + bs + 20, ly), ln, font=f,
                   fill=INK if strong else MUTED)
            ly += f.size * 1.08

    img.save(out_png, "PNG")
    return out_png


def point_card(index, total, name, heading, bullets=None, note=None,
               out_png="card.png"):
    """A section's own page: the persistent header, one heading, a few short
    lines. This is what carries an explanation when no real artifact exists.

    `total` of 0 (or no name) draws it WITHOUT the section header, for the
    scenes that belong to no section - the opening before the first item, and
    the closing summary. Drawing the header anyway printed "00 OF 00" there.
    """
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    headed = bool(total) and bool(name)
    if headed:
        draw_header(d, index, total, name)

    y = (HEAD_H + 46) if headed else (MARGIN + 10)
    # With bullets underneath, the heading gets ONE line - _fit shrinks the
    # type until it fits rather than wrapping. A two-line heading pushed the
    # bullets down far enough that the SAFE_BOTTOM cut-off dropped three of
    # the four, so the card lost most of its content to make room for a
    # bigger restatement of what the header already said.
    hf, hl = _fit(d, heading, F_DISPLAY, W - MARGIN * 2, 68, 24,
                  max_lines=1 if bullets else 2)
    for ln in hl:
        d.text((MARGIN, y), ln.upper(), font=hf, fill=INK)
        y += int(hf.size * 1.10)
    # Clear the last line's descenders before the rule. At y+12 the underline
    # was being drawn through the bottom of the heading it was meant to sit
    # beneath - Anton's cap height leaves far less room than its size implies.
    y += int(hf.size * 0.22)
    d.line((MARGIN, y, MARGIN + 96, y), fill=ACCENT, width=5)
    y += 40

    for b in (bullets or [])[:4]:
        bf = _f(F_TEXT, 28)
        lines = _wrap(d, b, bf, W - MARGIN * 2 - 44)[:2]
        # Stop before SAFE_BOTTOM rather than running past it. A two-line
        # heading pushes the bullets down, and with four of them the last
        # ones were landing under the term card - seen on a rendered frame,
        # where "Deposit" was hidden behind an opaque black box. Dropping a
        # bullet is a loss; printing one where it cannot be read is a loss
        # AND a mess.
        if y + len(lines) * 38 > SAFE_BOTTOM:
            break
        for k, ln in enumerate(lines):
            if k == 0:
                d.ellipse((MARGIN + 4, y + 12, MARGIN + 14, y + 22), fill=ACCENT)
            d.text((MARGIN + 34, y), ln, font=bf, fill=INK if k == 0 else MUTED)
            y += 38
        y += 10

    if note:
        nf, nl = _fit(d, note, F_TEXT, W - MARGIN * 2, 25, 16, max_lines=2)
        # Flowing under the content, NOT anchored to the bottom of the frame.
        #
        # Bottom-anchored put it at y~647, which is inside two things that
        # get burned on top of this card later: the caption block (engine.py
        # CAP_MARGIN_V/CAP_SIZE put it from ~564 down) and the term card
        # (TERM_Y 496, its box reaching ~554). Seen on a rendered frame with
        # all three stacked on each other. SAFE_BOTTOM is the line nothing
        # drawn here may cross.
        ny = min(y + 16, SAFE_BOTTOM - len(nl) * int(nf.size * 1.3))
        for ln in nl:
            d.text((MARGIN, ny), ln, font=nf, fill=MUTED)
            ny += int(nf.size * 1.3)
    img.save(out_png, "PNG")
    return out_png


def stat_card(index, total, name, value, label, out_png="card.png"):
    """One number, as large as it fits. A figure spoken aloud is gone in a
    second; on screen it is the only thing in the frame."""
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    draw_header(d, index, total, name)
    f, _ = _fit(d, str(value), F_DISPLAY, W - MARGIN * 2, 260, 60)
    lf, ll = _fit(d, label, F_DISPLAY, W - MARGIN * 2, 52, 24, max_lines=2)

    # Advance by the value's MEASURED height, not its nominal font size. A
    # glyph like % descends well below the em box, so "38%" ran straight
    # through its own label when the step was f.size.
    vb = d.textbbox((0, 0), str(value), font=f)
    vh = vb[3] - vb[1]
    block = vh + 26 + len(ll) * lf.size * 1.12
    y = HEAD_H + max(24, (H - HEAD_H - block) / 2 - 20)
    d.text(((W - _w(d, str(value), f)) / 2, y - vb[1]), str(value),
           font=f, fill=ACCENT)
    y += vh + 26
    for ln in ll:
        d.text(((W - _w(d, ln, lf)) / 2, y), ln.upper(), font=lf, fill=INK)
        y += lf.size * 1.12
    img.save(out_png, "PNG")
    return out_png


# ---------------------------------------------------------------------------
# animation
# ---------------------------------------------------------------------------
def _cells(items, eyebrow):
    """Where each row sits on the card.

    Shared by both clip builders so an animation can never disagree with the
    still card it is animating - the geometry is computed once, here, from
    the same numbers overview_card lays out with.
    """
    n = len(items)
    cols = 2 if n <= 8 else 3
    rows = -(-n // cols)
    top = MARGIN + (54 if eyebrow else 8)
    ch = (H - top - MARGIN) / rows
    cw = (W - MARGIN * 2) / cols
    boxes = []
    for i in range(n):
        r, c = divmod(i, cols)
        boxes.append((int(MARGIN + c * cw) - 8, int(top + r * ch) - 6,
                      int(MARGIN + (c + 1) * cw), int(top + (r + 1) * ch)))
    return boxes


def _encode(tmp, pattern, fps, out_mp4):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-framerate", str(fps),
                    "-i", os.path.join(tmp, pattern), "-c:v", "libx264",
                    "-preset", "superfast", "-crf", "18", "-pix_fmt",
                    "yuv420p", "-r", str(fps), out_mp4],
                   check=True, timeout=180)
    for f in os.listdir(tmp):
        os.remove(os.path.join(tmp, f))
    os.rmdir(tmp)
    return out_mp4


def overview_clip(items, current, out_mp4, seconds=4.0, fps=25, eyebrow=None,
                  tmp="_ovtmp", frames=None):
    """
    The overview card, with rows arriving one after another. This is the
    OPENING card only - the first time the viewer sees the list.

    Composites PRE-DRAWN STRIPS rather than redrawing the card each frame.
    Redrawing text 100 times costs about 30s a card, which across a dozen
    scenes would eat the job budget on its own.

    `frames` overrides `seconds` when the caller needs an exact count. The
    engine splits a scene into whole frames and the shot has to match that
    budget exactly, so "4.0 seconds" is not a number it can use.
    """
    items = [i.strip() for i in items if i and i.strip()]
    os.makedirs(tmp, exist_ok=True)
    full_p = os.path.join(tmp, "_full.png")
    overview_card(items, current, eyebrow, full_p)
    with Image.open(full_p) as im:
        full = im.convert("RGB").copy()

    blank = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(blank)
    if eyebrow:
        d.text((MARGIN, MARGIN - 12), eyebrow.upper(), font=_f(F_BODY, 20),
               fill=ACCENT)

    cells = [(full.crop(b), b) for b in _cells(items, eyebrow)]

    total_f = int(frames) if frames else int(seconds * fps)
    for fi in range(total_f):
        t = fi / max(total_f - 1, 1)
        fr = blank.copy()
        for i, (cell, box) in enumerate(cells):
            p = _ease(max(0.0, min(1.0, (t - i * 0.05) / 0.30)))
            if p <= 0:
                continue
            dy = int((1 - p) * 26)
            lay = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), PAPER)
            lay.paste(cell, (0, dy))
            fr.paste(Image.blend(fr.crop(box), lay, p), (box[0], box[1]))
        fr.save(os.path.join(tmp, f"f{fi:04d}.png"))

    return _encode(tmp, "f%04d.png", fps, out_mp4)


def advance_clip(items, current, out_mp4, frames, fps=25, eyebrow=None,
                 tmp="_advtmp", previous=None):
    """
    The between-sections beat: the item just finished gets its tick, then the
    red box moves down to the next one.

    This is the thing the owner asked for in so many words - "show main
    screen to make user know they completed one section then explain next
    thing" - and it is a DIFFERENT animation from the opening card on
    purpose. Replaying the rows-arriving animation at every section would
    contradict the one structural finding from the reference video: elements
    ACCUMULATE on a stable canvas. By section three the list is furniture the
    viewer already knows; the only thing that should move is what changed.

    So the whole card is held still and exactly two rows are cross-faded -
    the finished one and the new one - and they are staggered, tick first,
    box second, because that is the order the sentence goes in: that one is
    done, this one is next.

    Cost is two drawn cards and `frames` composites of two small crops, which
    is a fraction of what redrawing the card per frame would take.
    """
    items = [i.strip() for i in items if i and i.strip()]
    if not 0 <= current < len(items):
        raise ValueError(f"advance_clip: current {current} outside the list")
    # `previous` is normally the item before, but the FIRST section advances
    # from "nothing selected yet" - the opening list - which is current=None,
    # not -1. Passing it explicitly keeps that case from having to be encoded
    # as an out-of-range index.
    if previous is None and current > 0:
        previous = current - 1
    os.makedirs(tmp, exist_ok=True)

    before_p = os.path.join(tmp, "_a.png")
    after_p = os.path.join(tmp, "_b.png")
    overview_card(items, previous, eyebrow, before_p)
    overview_card(items, current, eyebrow, after_p)
    with Image.open(before_p) as im:
        before = im.convert("RGB").copy()
    with Image.open(after_p) as im:
        after = im.convert("RGB").copy()

    boxes = _cells(items, eyebrow)
    cur_box = boxes[current]
    prev_box = boxes[previous] if previous is not None else None

    total_f = int(frames)
    for fi in range(total_f):
        t = fi / max(total_f - 1, 1)
        fr = before.copy()
        # tick lands over the first 45%, box arrives over the last 65%,
        # overlapping in the middle so it reads as one movement rather than
        # two separate events
        p_tick = _ease(max(0.0, min(1.0, (t - 0.12) / 0.33)))
        p_box = _ease(max(0.0, min(1.0, (t - 0.35) / 0.45)))
        for box, p in ((prev_box, p_tick), (cur_box, p_box)):
            if box is None or p <= 0:
                continue
            fr.paste(Image.blend(fr.crop(box), after.crop(box), p),
                     (box[0], box[1]))
        fr.save(os.path.join(tmp, f"f{fi:04d}.png"))

    return _encode(tmp, "f%04d.png", fps, out_mp4)


if __name__ == "__main__":
    os.makedirs("graphics_demo", exist_ok=True)
    items = ["fixed costs", "variable costs", "semi-variable costs",
             "capital expenditure", "operating expenditure", "cost of goods sold"]
    overview_card(items, None, "Business expenses",
                  "graphics_demo/1_open.png")
    overview_card(items, 2, "Section 3 of 6", "graphics_demo/2_progress.png")
    point_card(3, 6, "semi-variable costs",
               "Part fixed, part variable",
               ["A phone plan with a fixed line rental plus per-minute charges",
                "Electricity: a standing charge plus what you actually use",
                "Overtime: base salary that rises only past a threshold"],
               "Source: US Chamber of Commerce, 2026",
               "graphics_demo/3_point.png")
    stat_card(1, 6, "fixed costs", "38%",
              "of a typical small firm's monthly outgoings",
              "graphics_demo/4_stat.png")
    print("wrote graphics_demo/")
