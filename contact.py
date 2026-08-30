#!/usr/bin/env python3
"""
contact.py - pull frames out of a finished video and lay them on one sheet.

WHY THIS EXISTS
---------------
The working rule on this project is "look at the artifact, not the logs".
Every real bug here was caught by looking at a rendered frame: the advert
look, the see-through term card, the term card printing through the section
header, the stretched Pixabay photo. None of them showed up in a log, and
several of them ran GREEN in CI while being completely wrong on screen.

But the finished video is 13MB and lives in a GitHub artifact, which means
"look at it" costs a download, a zip, and a video player. On a tablet that
is most of the effort of reviewing a run. So runs get judged by their logs,
which is exactly the habit that shipped all four of those bugs.

This makes one JPEG - twelve frames, evenly spaced, each stamped with the
second it came from. It is ~60KB, opens instantly anywhere, and shows the
things that actually go wrong: a card that does not read, a header that
collides with something, footage that has nothing to do with the words, a
frame that is simply black.

MEASUREMENTS
------------
Numbers alongside, because some faults are easier to measure than to see:

  bright   mean luminance. A dead-black frame reads 0-8 and is nearly
           always a failed fetch or a shot that rendered empty.
  header   luminance of the top band, where the persistent section header
           lives. Our design is a white header, so a section frame should
           read high; a value near the frame mean means the header did not
           draw at all.
  ink      share of the frame that is dark on light. A card is mostly
           white with black text and reads low but non-zero; a value of
           0.00 on a frame that should be a card means the text is missing
           or the same colour as its background.

    python3 contact.py final_video.mp4
    python3 contact.py final_video.mp4 --b64      # also dump for a log
"""

import base64
import os
import subprocess
import sys

TILE_W = 320
COLS, ROWS = 4, 3
LABEL_H = 18
QUALITY = 58


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def grab(path, t, out_png, width=TILE_W):
    """One frame at t. -ss before -i seeks by keyframe and is far faster;
    a couple of frames of imprecision does not matter for a contact sheet."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", path,
         "-frames:v", "1", "-vf", f"scale={width}:-2", out_png],
        check=True, timeout=60)
    return out_png


def _stats(im):
    """bright / header / ink for one tile."""
    g = im.convert("L")
    px = list(g.getdata())
    w, h = g.size
    mean = sum(px) / len(px)
    band = px[:w * max(1, h // 7)]                 # top ~14% = header band
    header = sum(band) / len(band)
    ink = sum(1 for v in px if v < 90) / len(px)   # dark pixels
    return mean, header, ink


def sheet(video, out_jpg="contact_sheet.jpg", tmp="_contact"):
    from PIL import Image, ImageDraw, ImageFont

    dur = duration(video)
    if dur <= 0:
        raise SystemExit(f"{video}: no duration - is it a real video?")

    os.makedirs(tmp, exist_ok=True)
    n = COLS * ROWS
    # Sample inside the video, never at 0.0 or the very last frame: the
    # first frame is mid fade-in and the last is mid fade-out, so both are
    # dark for reasons that are not faults and would read as ones.
    times = [dur * (i + 0.5) / n for i in range(n)]

    tiles, rows = [], []
    for i, t in enumerate(times):
        p = grab(video, t, os.path.join(tmp, f"t{i:02d}.png"))
        im = Image.open(p).convert("RGB")
        tiles.append((t, im))
        rows.append((t,) + _stats(im))

    tw, th = tiles[0][1].size
    sheet_im = Image.new("RGB", (COLS * tw, ROWS * (th + LABEL_H)), (24, 24, 26))
    d = ImageDraw.Draw(sheet_im)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
    except Exception:
        font = ImageFont.load_default()

    for i, (t, im) in enumerate(tiles):
        c, r = i % COLS, i // COLS
        x, y = c * tw, r * (th + LABEL_H)
        sheet_im.paste(im, (x, y))
        d.text((x + 5, y + th + 3), f"{i+1:02d}   {t:6.1f}s",
               font=font, fill=(235, 235, 235))

    sheet_im.save(out_jpg, "JPEG", quality=QUALITY, optimize=True)

    for p in os.listdir(tmp):
        os.remove(os.path.join(tmp, p))
    os.rmdir(tmp)

    return out_jpg, rows, dur


def report(rows, dur, video, out_jpg):
    print(f"\ncontact sheet | {video} | {dur:.1f}s | {os.path.getsize(out_jpg)/1024:.0f} KB")
    print(f"{'#':>2} {'at':>7}  {'bright':>6} {'header':>6} {'ink':>5}   note")
    print("-" * 58)
    for i, (t, mean, header, ink) in enumerate(rows, 1):
        note = ""
        if mean < 8:
            note = "BLACK - failed fetch or empty shot"
        elif ink < 0.005 and mean > 200:
            note = "white, no ink - card with no text?"
        elif header > mean + 45:
            note = "bright top band (header present)"
        print(f"{i:>2} {t:>6.1f}s  {mean:>6.1f} {header:>6.1f} {ink:>5.2f}   {note}")

    black = sum(1 for _, m, _, _ in rows if m < 8)
    if black:
        print(f"\n!! {black} of {len(rows)} sampled frames are effectively black.")


def dump_b64(path, chunk=180):
    """Print the sheet as base64 between markers.

    This exists because the sandbox these tools run in cannot reach the
    Azure host GitHub serves artifacts from, so a video built in CI cannot
    be downloaded and looked at - only its log can be read. Putting the
    sheet IN the log is the only way to actually see a CI-built frame.
    Off by default; it costs a few hundred lines of log when switched on.
    """
    b = base64.b64encode(open(path, "rb").read()).decode()
    print(f"\n--8<--SHEET-BEGIN {os.path.basename(path)} {len(b)}")
    for i in range(0, len(b), chunk):
        print(b[i:i + chunk])
    print("--8<--SHEET-END")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    video = args[0] if args else "final_video.mp4"
    out = args[1] if len(args) > 1 else "contact_sheet.jpg"
    jpg, rows, dur = sheet(video, out)
    report(rows, dur, video, jpg)
    if "--b64" in sys.argv:
        dump_b64(jpg)
