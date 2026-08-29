#!/usr/bin/env python3
"""
graphics.py — draw the information, instead of searching a stock library for
something that vaguely rhymes with it.

WHY THIS EXISTS
---------------
The owner watched a finished run and said it looked like a 2016 stock-footage
video. Looking at the frames rather than the logs, he was right, and the
reasons were specific:

  - A shot of a giant 3D "FRIDAY" appeared in a video about business
    expenses, under narration about "total expenditures across reporting
    periods". Pixabay returned it for some keyword and the engine used it
    without ever asking whether it was relevant.
  - In 47 seconds the video passed through a soft-focus coffee cup, a macro
    keyboard, a flat 2D cartoon, an isometric 3D illustration, a live-action
    photograph, an animated documents folder and a man in a greenhouse.
    Seven visual worlds, no system.
  - Not one number, label, comparison or diagram appeared on screen. Every
    frame was decoration under a voice.

The root cause is architectural and no amount of better keywords fixes it:
searching a stock library for a phrase and pasting back whatever ranks first
cannot produce an explainer, because the picture is chosen by keyword match
rather than by what has to be communicated.

So the important visuals are DRAWN here, from the script's own data. The
same approach already works: thumbnail.py builds a whole thumbnail out of
nothing but each scene's key_term.

    python3 graphics.py      # renders one of each card to graphics_demo/
"""

import os
import math

from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1280, 720

HERE = os.path.dirname(os.path.abspath(__file__))
F_DISPLAY = os.path.join(HERE, "assets", "fonts", "Anton-Regular.ttf")
F_BODY    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
F_TEXT    = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# A deliberately dark card. It has to read as "this is the information"
# against bright stock footage either side of it, and the captions burned in
# afterwards are light text with a dark outline, so they stay legible on top.
INK       = (13, 19, 23)
INK_SOFT  = (23, 32, 38)
WHITE     = (245, 249, 248)
DIM       = (122, 141, 148)
DIMMER    = (72, 88, 95)
ACCENT    = (72, 214, 196)
ACCENT_D  = (16, 68, 64)
WARN      = (240, 119, 107)

MARGIN = 68


def _f(path, size):
    return ImageFont.truetype(path, size)


def _w(d, t, f):
    return d.textbbox((0, 0), t, font=f)[2]


def _wrap(d, text, f, max_w):
    words, lines, cur = text.split(), [], []
    for wd in words:
        trial = cur + [wd]
        if cur and _w(d, " ".join(trial), f) > max_w:
            lines.append(" ".join(cur))
            cur = [wd]
        else:
            cur = trial
    if cur:
        lines.append(" ".join(cur))
    return lines


def _fit(d, text, path, max_w, hi, lo=18, max_lines=1):
    """Largest size at which `text` fits, measured rather than assumed."""
    for s in range(hi, lo - 1, -2):
        f = _f(path, s)
        lines = _wrap(d, text, f, max_w)
        if len(lines) <= max_lines:
            return f, lines
    f = _f(path, lo)
    return f, _wrap(d, text, f, max_w)[:max_lines]


def _base(eyebrow=None):
    img = Image.new("RGB", (W, H), INK)
    d = ImageDraw.Draw(img)
    # a soft off-centre glow keeps a flat fill from looking like a dead slate
    glow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glow).ellipse((W * 0.45, -H * 0.35, W * 1.35, H * 0.95), fill=48)
    img = Image.composite(Image.new("RGB", (W, H), INK_SOFT), img,
                          glow.filter(ImageFilter.GaussianBlur(150)))
    d = ImageDraw.Draw(img)
    if eyebrow:
        f = _f(F_BODY, 19)
        d.text((MARGIN, MARGIN - 16), eyebrow.upper(), font=f, fill=ACCENT)
        wgt = _w(d, eyebrow.upper(), f)
        d.line((MARGIN, MARGIN + 14, MARGIN + wgt, MARGIN + 14), fill=ACCENT_D, width=2)
    return img, d


def rail_card(items, active, eyebrow=None, detail=None, out_png="card.png"):
    """
    THE RECURRING VISUAL SYSTEM. Every category in the video, listed, with the
    current one lit up and the rest dimmed.

    This is the single highest-value card because it is orientation: at any
    moment the viewer can see how many things there are, which one is being
    explained, and how far through they are. It is also the thing a pile of
    unrelated stock clips can never do.

    It costs no new information - `items` is every scene's key_term, which the
    script already writes for the term cards and the thumbnail.
    """
    items = [i.strip() for i in items if i and i.strip()]
    if not items:
        raise ValueError("rail_card needs at least one item")
    active = max(0, min(active, len(items) - 1))
    img, d = _base(eyebrow)

    n = len(items)
    # Reserve the detail strip BEFORE laying out rows. Sizing the list to the
    # full frame and then writing the definition into the same space put
    # "Materials, packaging, delivery..." straight through the word RUNWAY.
    detail_h = 92 if detail else 0
    top, bot = MARGIN + 46, H - MARGIN - detail_h
    row = (bot - top) / n
    # type scales with how many rows have to fit, so a 6-item and a 16-item
    # rail are both full-bleed rather than one being tiny in a big empty frame
    size_on = int(min(row * 0.62, 62))
    size_off = int(min(row * 0.44, 34))
    num_f = _f(F_BODY, max(13, int(size_off * 0.62)))

    for i, label in enumerate(items):
        y = top + row * i
        on = (i == active)
        f = _f(F_DISPLAY if on else F_BODY, size_on if on else size_off)
        col = WHITE if on else (DIM if abs(i - active) <= 2 else DIMMER)

        if on:   # accent bar marks the live row
            d.rectangle((MARGIN - 22, y + 2, MARGIN - 14, y + size_on * 0.95),
                        fill=ACCENT)
        d.text((MARGIN + 44, y - 2), f"{i+1:02d}", font=num_f,
               fill=ACCENT if on else DIMMER)
        d.text((MARGIN + 96, y - size_on * 0.10 if on else y),
               label.upper(), font=f, fill=col)

    if detail:
        f, lines = _fit(d, detail, F_TEXT, W - MARGIN * 2 - 96, 27, 17, max_lines=2)
        ly = H - MARGIN + 4
        for ln in reversed(lines):
            ly -= int(f.size * 1.34)
            d.text((MARGIN + 96, ly), ln, font=f, fill=DIM)

    img.save(out_png, "PNG")
    return out_png


def stat_card(value, label, context=None, eyebrow=None, out_png="card.png"):
    """
    One number, as big as it will go. A figure spoken aloud is gone in a
    second; on screen it is the only thing in the frame.
    """
    img, d = _base(eyebrow)
    f, _ = _fit(d, str(value), F_DISPLAY, W - MARGIN * 2, 300, 60, max_lines=1)
    vw = _w(d, str(value), f)

    # Centre the whole stack on measured heights instead of starting the
    # number at a fixed 30% of the frame - a big value pushed its own label
    # and context to the bottom edge and left the top third empty.
    lf, llines = _fit(d, label, F_DISPLAY, W - MARGIN * 2, 62, 26, max_lines=2)
    cf, clines = ((None, []) if not context else
                  _fit(d, context, F_TEXT, W - MARGIN * 3, 26, 16, max_lines=2))
    block = (f.size * 1.06 + len(llines) * lf.size * 1.12
             + (16 + len(clines) * cf.size * 1.32 if clines else 0))
    vy = max(MARGIN + 40, (H - block) / 2 - f.size * 0.16)
    d.text(((W - vw) / 2, vy), str(value), font=f, fill=ACCENT)

    ly = vy + f.size * 1.06
    for ln in llines:
        d.text(((W - _w(d, ln, lf)) / 2, ly), ln.upper(), font=lf, fill=WHITE)
        ly += int(lf.size * 1.12)

    if clines:
        ly += 16
        for ln in clines:
            d.text(((W - _w(d, ln, cf)) / 2, ly), ln, font=cf, fill=DIM)
            ly += int(cf.size * 1.32)
    img.save(out_png, "PNG")
    return out_png


def split_card(a_title, a_items, b_title, b_items, eyebrow=None,
               out_png="card.png"):
    """
    Two things, side by side. Comparison is most of what an explainer does -
    fixed against variable, before against after - and it is exactly the shape
    no stock clip can carry.
    """
    img, d = _base(eyebrow)
    mid = W / 2
    d.line((mid, MARGIN + 54, mid, H - MARGIN), fill=ACCENT_D, width=2)

    for side, (title, items) in enumerate(((a_title, a_items), (b_title, b_items))):
        x0 = MARGIN if side == 0 else mid + 40
        colw = mid - MARGIN - 40
        tf, tl = _fit(d, title, F_DISPLAY, colw, 54, 24, max_lines=2)
        y = MARGIN + 66
        for ln in tl:
            d.text((x0, y), ln.upper(), font=tf,
                   fill=ACCENT if side == 0 else WARN)
            y += int(tf.size * 1.06)
        y += 22
        bf = _f(F_TEXT, 25)
        for it in items[:5]:
            for k, ln in enumerate(_wrap(d, it, bf, colw - 30)[:2]):
                if k == 0:
                    d.ellipse((x0 + 3, y + 10, x0 + 11, y + 18),
                              fill=ACCENT if side == 0 else WARN)
                d.text((x0 + 30, y), ln, font=bf, fill=WHITE if k == 0 else DIM)
                y += 33
            y += 12
    img.save(out_png, "PNG")
    return out_png


def title_card(headline, accent=None, sub=None, out_png="card.png"):
    """The opening frame. Currently the hook is spent on whatever stock clip
    matched keyword one - in the run we looked at, a soft-focus coffee cup."""
    img, d = _base(None)
    words = headline.upper().split()
    acc = set()
    if accent:
        norm = lambda s: "".join(c for c in s.lower() if c.isalnum())
        want = [norm(a) for a in accent.split() if norm(a)]
        have = [norm(x) for x in words]
        for i in range(len(have) - len(want) + 1):
            if have[i:i + len(want)] == want:
                acc = set(range(i, i + len(want)))
                break

    for size in range(120, 39, -3):
        f = _f(F_DISPLAY, size)
        lines, cur = [], []
        for i, wd in enumerate(words):
            trial = cur + [i]
            if cur and _w(d, " ".join(words[j] for j in trial), f) > W - MARGIN * 2:
                lines.append(cur); cur = [i]
            else:
                cur = trial
        if cur:
            lines.append(cur)
        if len(lines) <= 3 and len(lines) * size * 1.1 <= H * 0.52:
            break

    y = (H - len(lines) * size * 1.1) / 2 - (30 if sub else 0)
    for ln in lines:
        txt = " ".join(words[i] for i in ln)
        x = (W - _w(d, txt, f)) / 2
        for i in ln:
            d.text((x, y), words[i], font=f, fill=ACCENT if i in acc else WHITE)
            x += _w(d, words[i] + " ", f)
        y += size * 1.1
    if sub:
        sf, sl = _fit(d, sub, F_TEXT, W - MARGIN * 3, 30, 18, max_lines=2)
        y += 18
        for ln in sl:
            d.text(((W - _w(d, ln, sf)) / 2, y), ln, font=sf, fill=DIM)
            y += int(sf.size * 1.34)
    img.save(out_png, "PNG")
    return out_png


if __name__ == "__main__":
    os.makedirs("graphics_demo", exist_ok=True)
    cats = ["fixed costs", "variable costs", "one-off costs",
            "cost of goods sold", "operating expenses", "runway"]
    title_card("Types of Business Expenses", "Business Expenses",
               "The three that decide whether you survive the year",
               "graphics_demo/1_title.png")
    rail_card(cats, 1, "Business expenses",
              "Materials, packaging, delivery - they rise and fall with every "
              "single sale you make.", "graphics_demo/2_rail.png")
    stat_card("82%", "of small businesses fail",
              "because of cash flow problems, not lack of profit",
              "Why this matters", "graphics_demo/3_stat.png")
    split_card("Fixed costs", ["Rent", "Salaries", "Insurance", "Software"],
               "Variable costs", ["Materials", "Packaging", "Delivery",
                                  "Card processing"],
               "Tell them apart", "graphics_demo/4_split.png")
    print("wrote graphics_demo/")


# ---------------------------------------------------------------------------
# ANIMATION
# ---------------------------------------------------------------------------
def _ease(t):
    """Ease-out cubic. Linear motion is the thing that reads as 'made by a
    script'; almost all of the perceived quality of a move is in its curve."""
    return 1 - (1 - t) ** 3


def rail_clip(items, active, out_mp4, seconds=5.0, fps=25, eyebrow=None,
              detail=None, tmp="_railtmp"):
    """
    The rail card, ANIMATED: rows stagger in from the left, the accent bar
    grows onto the live row, the detail line fades up.

    Rendered by compositing PRE-DRAWN LAYERS, not by redrawing the card every
    frame. Drawing text 125 times costs ~30s per card, which across a dozen
    scenes would blow the job budget on its own; caching each row as an RGBA
    tile and only moving it per frame costs milliseconds.
    """
    items = [i.strip() for i in items if i and i.strip()]
    active = max(0, min(active, len(items) - 1))
    os.makedirs(tmp, exist_ok=True)

    base_png = os.path.join(tmp, "_base.png")
    rail_card(items, active, eyebrow, detail, base_png)
    with Image.open(base_png) as im:
        full = im.convert("RGB").copy()

    # the plain backdrop, with every row erased, is what rows animate over
    bg_png = os.path.join(tmp, "_bg.png")
    rail_card(items, active, eyebrow, None, bg_png)
    bg, _ = _base(eyebrow)

    n = len(items)
    detail_h = 92 if detail else 0
    top, bot = MARGIN + 46, H - MARGIN - detail_h
    row = (bot - top) / n

    # each row lifted off the finished card as its own strip
    strips = []
    for i in range(n):
        y0 = int(top + row * i) - 6
        y1 = int(min(H, y0 + row + 12))
        strips.append((full.crop((0, y0, W, y1)), y0))
    det_y = int(bot + 10)
    det = full.crop((0, det_y, W, H)) if detail else None

    total = int(seconds * fps)
    stagger = 0.055
    for fi in range(total):
        t = fi / max(total - 1, 1)
        fr = bg.copy()
        for i, (strip, y0) in enumerate(strips):
            p = _ease(max(0.0, min(1.0, (t - i * stagger) / 0.34)))
            if p <= 0:
                continue
            dx = int((1 - p) * -90)
            lay = Image.new("RGB", (W, strip.height), INK)
            lay.paste(strip, (dx, 0))
            fr.paste(Image.blend(fr.crop((0, y0, W, y0 + strip.height)),
                                 lay, p), (0, y0))
        if det is not None:
            p = _ease(max(0.0, min(1.0, (t - n * stagger - 0.08) / 0.3)))
            if p > 0:
                fr.paste(Image.blend(fr.crop((0, det_y, W, H)), det, p),
                         (0, det_y))
        fr.save(os.path.join(tmp, f"f{fi:04d}.png"))

    import subprocess
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-framerate", str(fps),
                    "-i", os.path.join(tmp, "f%04d.png"),
                    "-c:v", "libx264", "-preset", "superfast", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-r", str(fps), out_mp4],
                   check=True, timeout=120)
    for f in os.listdir(tmp):
        os.remove(os.path.join(tmp, f))
    os.rmdir(tmp)
    return out_mp4
