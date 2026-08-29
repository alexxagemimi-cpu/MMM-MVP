#!/usr/bin/env python3
"""
thumbnail.py — draws the "here is the list" thumbnail.

WHY THIS SHAPE
--------------
Modelled on the layout that works for taxonomy explainers: a heavy condensed
uppercase headline with exactly ONE phrase in red, then a grid of labelled
circles, on white. No face, no dark background, no photo behind the text.

The important part is that this needs almost no new information. An explainer
script already carries one `key_term` per scene - "fixed costs", "variable
costs", "runway" - and those ARE the grid. The thumbnail is a picture of the
list the video is about, and we were already writing the list.

Text is measured and fitted, never assumed. An earlier on-screen card in this
project hardcoded a 560px box and long text ran off the edge of its own
background; every size here is derived from the real glyph extents instead,
so a long headline shrinks rather than overflowing.

    python3 thumbnail.py            # renders a sample to thumbnail.png
"""

import io
import os
import math
import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1280, 720

# Anton is vendored under assets/fonts (SIL Open Font License, redistributable)
# so the render has no network dependency and no apt package to hope for. The
# runner's DejaVu Bold is not a substitute: it is wide, not condensed, and the
# whole look depends on the headline being narrow enough to be big.
HERE       = os.path.dirname(os.path.abspath(__file__))
FONT_DISPLAY = os.path.join(HERE, "assets", "fonts", "Anton-Regular.ttf")
FONT_LABEL   = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

BG        = (255, 255, 255)
INK       = (17, 17, 17)
ACCENT    = (225, 27, 21)      # the one red phrase
LABEL_INK = (34, 34, 34)

# Saturated, clearly distinct ring colours. Cycled, so a 6-tile and an
# 11-tile thumbnail both look deliberate rather than randomly coloured.
RING = [
    (240, 78, 66), (247, 181, 41), (66, 168, 92), (58, 150, 221),
    (156, 92, 200), (236, 118, 173), (54, 194, 190), (243, 137, 54),
    (120, 176, 60), (95, 118, 232), (222, 92, 138), (86, 190, 143),
]

MARGIN     = 44
GAP_MIN    = 14
LABEL_SIZE = 21
LABEL_LINES = 2


UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch_tile_images(queries, out_dir, api_key=None, workers=6):
    """
    One picture per tile, from Pixabay. Returns a list of paths, None where
    nothing came back - render() draws a plain coloured circle for those, so
    a failed fetch costs one tile's picture and never the thumbnail.

    ILLUSTRATIONS FIRST, PHOTOS SECOND. The reference thumbnails use drawn
    icons, and a photograph cropped into a small circle reads as a stock
    collage rather than a diagram. Pixabay's illustration/vector categories
    are the closest free equivalent, so they are tried before photos.

    The QUERY IS NOT THE LABEL. Labels are the scene's key_term, which is
    often an abstraction that means something else to an image search -
    "runway" returns aircraft, not a company's months of remaining cash. So
    the caller passes the scene's first image_keyword instead, which the
    script already writes as a concrete photographable subject.
    """
    api_key = (api_key if api_key is not None
               else os.environ.get("PIXABAY_API_KEY", "")).strip()
    if not api_key:
        return [None] * len(queries)
    os.makedirs(out_dir, exist_ok=True)

    def one(item):
        i, q = item
        for img_type in ("illustration", "vector", "photo"):
            try:
                url = ("https://pixabay.com/api/"
                       f"?key={api_key}&q={urllib.parse.quote(q.strip())}"
                       f"&image_type={img_type}&safesearch=true&per_page=5")
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    hits = json.loads(r.read()).get("hits", [])
                if not hits:
                    continue
                src = (hits[0].get("largeImageURL")
                       or hits[0].get("webformatURL"))
                if not src:
                    continue
                req = urllib.request.Request(src, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                if len(data) < 2048:
                    continue
                path = os.path.join(out_dir, f"tile{i:02d}.png")
                with Image.open(io.BytesIO(data)) as im:
                    im.convert("RGB").save(path, "PNG")
                return i, path
            except Exception:
                continue
        return i, None

    out = [None] * len(queries)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, path in pool.map(one, list(enumerate(queries))):
            out[i] = path
    return out


def from_script(script_path, out_png, work_dir="build/thumb"):
    """
    script.json -> thumbnail.png. The whole point of the layout: everything
    it needs is already in the script.

    Falls back to the title when no thumb_headline was written, so an older
    script (or one from a model that skipped the field) still renders.
    """
    with open(script_path, encoding="utf-8") as f:
        data = json.load(f)
    scenes = data.get("scenes", [])

    headline = (data.get("thumb_headline") or data.get("title") or "").strip()
    accent = (data.get("thumb_accent") or "").strip()
    labels, queries = [], []
    for s in scenes:
        term = (s.get("key_term") or "").strip()
        if not term:
            continue
        labels.append(term)
        kws = [k for k in (s.get("image_keywords") or []) if k and k.strip()]
        queries.append(kws[0] if kws else term)

    images = fetch_tile_images(queries, work_dir)
    got = sum(1 for p in images if p)
    print(f"   thumbnail: {len(labels)} tiles, {got} with a picture")
    return render(headline, accent, labels, out_png, images=images)


def _font(path, size):
    return ImageFont.truetype(path, size)


def _text_w(draw, text, font):
    return draw.textbbox((0, 0), text, font=font)[2]


def _wrap_words(draw, words, font, max_w):
    """Greedy wrap on a word list. Returns list of word-index lists."""
    lines, cur = [], []
    for i, w in enumerate(words):
        trial = cur + [i]
        s = " ".join(words[j] for j in trial)
        if cur and _text_w(draw, s, font) > max_w:
            lines.append(cur)
            cur = [i]
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def _accent_flags(words, accent):
    """
    Which words fall inside `accent`. Matched as a consecutive run so a
    two-word accent ("MENTAL DISORDERS") colours both and only together -
    marking every occurrence of a common word would scatter red across
    the line.
    """
    flags = [False] * len(words)
    if not accent:
        return flags
    norm = lambda s: "".join(ch for ch in s.lower() if ch.isalnum())
    want = [norm(a) for a in accent.split() if norm(a)]
    if not want:
        return flags
    have = [norm(w) for w in words]
    for i in range(len(have) - len(want) + 1):
        if have[i:i + len(want)] == want:
            for j in range(i, i + len(want)):
                flags[j] = True
            return flags
    return flags


def _fit_headline(draw, words, max_w, max_h, max_lines=2):
    """
    Largest Anton size at which the headline fits the box in <= max_lines.

    Steps down from a size that is deliberately too big. Fitting by
    measurement rather than by a guessed constant is the whole point: the
    headline is written by a model, so its length is not knowable in advance.
    """
    for size in range(104, 39, -2):
        f = _font(FONT_DISPLAY, size)
        lines = _wrap_words(draw, words, f, max_w)
        if len(lines) > max_lines:
            continue
        lh = int(size * 1.06)
        if lh * len(lines) <= max_h:
            return f, lines, lh
    f = _font(FONT_DISPLAY, 40)
    return f, _wrap_words(draw, words, f, max_w)[:max_lines], 44


def _circle_image(src_path, d, ring_rgb):
    """
    One tile: the source image cropped to a centred square, scaled to d, and
    masked to a circle with a coloured ring. Crop-to-fill, never resize-to-
    fit: forcing an image to a shape it is not is how faces come out stretched.
    """
    ring_w = max(4, d // 22)
    tile = Image.new("RGBA", (d, d), (0, 0, 0, 0))

    inner_d = d - ring_w * 2
    inner = None
    if src_path and os.path.exists(src_path):
        try:
            with Image.open(src_path) as im:
                im = im.convert("RGB")
                sw, sh = im.size
                side = min(sw, sh)
                im = im.crop(((sw - side) // 2, (sh - side) // 2,
                              (sw - side) // 2 + side, (sh - side) // 2 + side))
                inner = im.resize((inner_d, inner_d), Image.Resampling.LANCZOS)
        except Exception:
            inner = None
    if inner is None:
        inner = Image.new("RGB", (inner_d, inner_d),
                          tuple(min(255, c + 42) for c in ring_rgb))

    # supersampled mask -> a clean circle edge instead of a stair-stepped one
    ss = 4
    big = Image.new("L", (inner_d * ss, inner_d * ss), 0)
    ImageDraw.Draw(big).ellipse((0, 0, inner_d * ss - 1, inner_d * ss - 1), fill=255)
    mask = big.resize((inner_d, inner_d), Image.Resampling.LANCZOS)

    ringed = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    rbig = Image.new("L", (d * ss, d * ss), 0)
    ImageDraw.Draw(rbig).ellipse((0, 0, d * ss - 1, d * ss - 1), fill=255)
    rmask = rbig.resize((d, d), Image.Resampling.LANCZOS)
    ringed.paste(Image.new("RGB", (d, d), ring_rgb), (0, 0), rmask)

    tile.paste(ringed, (0, 0))
    tile.paste(inner, (ring_w, ring_w), mask)
    return tile


def render(headline, accent, tiles, out_png, images=None, subtitle=None):
    """
    headline : the short thumbnail line, e.g. "TYPES OF BUSINESS EXPENSES"
               (shorter than the video title - that difference is deliberate)
    accent   : the phrase inside it drawn in red, e.g. "BUSINESS EXPENSES"
    tiles    : the labels under each circle - one per scene's key_term
    images   : optional list of image paths, same length as tiles
    """
    tiles = [t.strip() for t in tiles if t and t.strip()][:12]
    if not tiles:
        raise ValueError("no tiles to draw")
    images = (images or []) + [None] * (len(tiles) - len(images or []))

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # ---- headline -------------------------------------------------------
    #
    # Headline width is deliberately narrower when there are few tiles.
    #
    # Six circles in a row plus their labels is about 230px of a 720px frame,
    # so a single-line headline above them leaves half the picture empty.
    # Squeezing the headline box makes it wrap to two lines - and a two-line
    # headline is set MUCH larger to fill that width, so the text ends up
    # bigger, the frame ends up balanced, and the thing that has to be
    # readable on a phone is the thing that grew.
    n_tiles = len(tiles)
    words = headline.upper().split()
    flags = _accent_flags(words, (accent or "").upper())
    box_w = (W - MARGIN * 2) * (1.0 if n_tiles >= 8 else 0.74)
    font, lines, lh = _fit_headline(d, words, box_w, 300, max_lines=2)

    y = MARGIN - 6
    for ln in lines:
        text = " ".join(words[i] for i in ln)
        x = (W - _text_w(d, text, font)) // 2
        for i in ln:
            w = words[i]
            d.text((x, y), w, font=font, fill=ACCENT if flags[i] else INK)
            x += _text_w(d, w + " ", font)
        y += lh

    # Guaranteed air under the headline. Without it a tall grid packs itself
    # right up against the type - at ten tiles the top row of circles was
    # touching the letters' baselines.
    head_bottom = y + 24

    # ---- grid -----------------------------------------------------------
    # Six across still reads as a row; past that the circles get too small to
    # carry an image, so it breaks to two. Keeping small counts on ONE row
    # matters for balance - three-and-three leaves a narrow block of circles
    # sitting under a headline that spans the whole frame.
    n = len(tiles)
    rows = 1 if n <= 6 else 2
    per_row = math.ceil(n / rows)

    label_f = _font(FONT_LABEL, LABEL_SIZE)
    label_h = int(LABEL_SIZE * 1.18) * LABEL_LINES + 8

    avail_h = H - head_bottom - MARGIN
    cell_w = (W - MARGIN * 2 - GAP_MIN * (per_row - 1)) / per_row
    cell_h = (avail_h - GAP_MIN * (rows - 1)) / rows
    diam = int(min(cell_w * 0.92, cell_h - label_h))
    diam = max(diam, 64)

    # Space the circles off their OWN diameter, not off the cell width. When
    # the diameter ends up limited by height - which it is whenever there are
    # two rows - cell-width spacing leaves the circles floating in white with
    # big gaps, reading as an unfinished slide rather than a dense list.
    gap_x = max(GAP_MIN, int(diam * 0.16))
    pitch = diam + gap_x

    # A label may never be wider than the spacing to the next circle, or two
    # neighbouring labels overlap into each other - "COST OF GOODS SOLD" ran
    # straight through "OPERATING EXPENSES" when this was allowed to exceed
    # the pitch. Long labels wrap instead; a single word too long to wrap
    # shrinks the label font rather than overflowing.
    label_w = pitch - 8
    while LABEL_SIZE > 12 and label_f.size > 12 and any(
            _text_w(d, w, label_f) > label_w
            for t in tiles for w in t.upper().split()):
        label_f = _font(FONT_LABEL, label_f.size - 1)
    label_h = int(label_f.size * 1.18) * LABEL_LINES + 8

    row_h = diam + label_h
    grid_h = row_h * rows + GAP_MIN * (rows - 1)
    top = head_bottom + max(0, (avail_h - grid_h) // 2)

    # every shadow onto one layer, composited once - compositing the whole
    # frame per tile is 12 full-size conversions for no visible difference
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    placed = []
    for idx, label in enumerate(tiles):
        r, c = divmod(idx, per_row)
        in_row = min(per_row, n - r * per_row)
        row_w = in_row * diam + (in_row - 1) * gap_x
        cx = (W - row_w) / 2 + c * pitch + diam / 2
        cy = top + r * (row_h + GAP_MIN)

        tile = _circle_image(images[idx], diam, RING[idx % len(RING)])
        shadow.paste(Image.new("RGBA", (diam, diam), (0, 0, 0, 48)),
                     (int(cx - diam / 2), int(cy) + 6), tile.split()[3])
        placed.append((tile, int(cx - diam / 2), int(cy), cx, cy, label))

    img = Image.alpha_composite(
        img.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(7))).convert("RGB")
    d = ImageDraw.Draw(img)

    for tile, px, py, cx, cy, label in placed:
        img.paste(tile, (px, py), tile)

        # label, wrapped to LABEL_LINES and centred under its circle
        lwords = label.upper().split()
        llines = _wrap_words(d, lwords, label_f, label_w)[:LABEL_LINES]
        ly = cy + diam + 7
        for ln in llines:
            t = " ".join(lwords[i] for i in ln)
            d.text((cx - _text_w(d, t, label_f) / 2, ly),
                   t, font=label_f, fill=LABEL_INK)
            ly += int(LABEL_SIZE * 1.18)

    img.save(out_png, "PNG")
    return out_png


if __name__ == "__main__":
    render(
        "Types of Business Expenses",
        "Business Expenses",
        ["fixed costs", "variable costs", "one-off costs", "runway",
         "cost of goods sold", "operating expenses"],
        "thumbnail.png",
    )
    print("wrote thumbnail.png")
