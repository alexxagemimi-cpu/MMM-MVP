#!/usr/bin/env python3
"""
score_run.py — mark a finished run out of 100, from its own log.

WHY THIS EXISTS
---------------
The owner asked for ten topics, ten videos, and an honest score for each.
Ten videos judged by eye, one after another, is ten different standards:
by the sixth you are grading against the fifth rather than against good.
Worse, the person marking is the person who wrote the code, which is the
oldest way there is to get a flattering answer.

So the parts that CAN be measured are measured, from the run's own log,
with the thresholds written down before the runs happen. What is left for
judgement is small and explicit, and it is stated as judgement rather than
smuggled in as a number.

WHAT THIS CANNOT DO
-------------------
It cannot tell you the video is *interesting*, and it cannot tell you the
narration is TRUE - only whether the system's own checks said so. A script
the red team never questioned still might be wrong. Treat a high score as
"nothing known is broken", never as "this is correct".

    python3 score_run.py run44.log            # one run
    python3 score_run.py logs/*.log           # the whole campaign
"""

import re
import sys
import os


# Thresholds, fixed in advance. Moving one of these after seeing a result is
# how a test becomes a formality.
LENGTH_TOLERANCE = 0.25       # within 25% of target counts as on-length
MAX_BLANK_RATIO  = 0.10       # over a tenth of shots blank is a bad video
MIN_DRAWN_RATIO  = 0.05       # a money/explainer video needs SOME cards


def _f(pattern, text, cast=float, default=None):
    m = re.search(pattern, text)
    if not m:
        return default
    try:
        return cast(m.group(1).replace(",", ""))
    except (ValueError, TypeError):
        return default


def read(path):
    txt = open(path, encoding="utf-8", errors="replace").read()
    d = {"file": os.path.basename(path)}

    d["topic"]    = _f(r"TOPIC:\s*(.+)", txt, str)
    d["title"]    = _f(r"title\s*:\s*(.+)", txt, str)
    d["target"]   = _f(r"TARGET_MINUTES:\s*(\d+)", txt, float)
    d["scenes"]   = _f(r"scenes\s*:\s*(\d+)", txt, int)
    d["words"]    = _f(r"words:\s*(\d+)", txt, int)
    d["duration"] = _f(r"duration\s*:\s*([\d.]+)\s*min", txt, float)
    d["size_mb"]  = _f(r"size\s*:\s*([\d.]+)\s*MB", txt, float)

    drawn = re.search(r"drawn\s*:\s*(\d+)/(\d+)", txt)
    d["drawn"], d["shots"] = (int(drawn.group(1)), int(drawn.group(2))) \
        if drawn else (None, None)

    # COUNT SHOTS, NOT LOG LINES.
    #
    # A gagged shot prints twice - once as the withheld notice, once as the
    # ordinary shot tag - so counting "[SLATE]" occurrences gave 130 blanks
    # out of 106 shots on run 45. A percentage over 100 is the measurement
    # telling you it is measuring the wrong thing.
    #
    # Key on (scene, shot) so each shot is counted once however often it is
    # mentioned. A withheld CARD is not blank: it is a checklist or a title,
    # which is a real frame - so those are tallied separately, not as holes.
    def shots_tagged(tag):
        return {(m.group(1), m.group(2)) for m in re.finditer(
            rf"shot scene (\d+) #(\d+) \[{tag}\]", txt)}

    blank = shots_tagged("SLATE")
    withheld = shots_tagged("HEADER")
    # a shot that got a real card is not blank, even if it was ALSO logged
    # as a slate before the card was drawn
    d["slates"] = len(blank - withheld)
    d["headers"] = len(withheld)

    d["hard"]     = _f(r"(\d+) unfixed HARD red-team finding", txt, int, 0)
    d["publish"]  = "NOT READY TO PUBLISH" not in txt
    d["members"]  = _f(r"verified\s*:\s*(\d+) members", txt, int, 0)
    d["frozen"]   = "no motionless stretch" not in txt
    d["repaired"] = bool(re.search(r"applied \d+ scene fix", txt))
    d["fatal"]    = "FATAL:" in txt
    d["gate"]     = ("REJECT" if "[REJECT]" in txt
                     else "COULD NOT CHECK" if "[COULD NOT CHECK]" in txt
                     else "BUILD" if "[BUILD]" in txt else "-")
    return d


def score(d):
    """
    Out of 100, five equal parts. Each returns (points, note).

    Equal weighting is deliberate. A beautiful video about the wrong topic
    and a correct video that is all black are both unusable, and a scheme
    that lets one dimension carry the total would rank one of them highly.
    """
    out = []

    # 1. DID IT RUN AT ALL
    if d["fatal"] or not d["duration"]:
        out.append((0, "run died before producing a video"))
    else:
        out.append((20, "produced a video"))

    # 2. IS IT ABOUT THE TOPIC ASKED FOR
    topic, title = (d["topic"] or "").lower(), (d["title"] or "").lower()
    if not topic or not title:
        out.append((0, "no topic/title recorded"))
    else:
        stop = {"every", "type", "types", "the", "of", "a", "an", "explained",
                "top", "10", "and", "in", "how", "to", "worldwide"}
        want = {w for w in re.findall(r"[a-z]+", topic) if w not in stop}
        got = set(re.findall(r"[a-z]+", title))
        hit = len(want & got) / max(1, len(want))
        out.append((int(20 * hit),
                    f"title matches {hit:.0%} of the topic's key words"))

    # 3. LENGTH
    if not d["duration"] or not d["target"]:
        out.append((0, "no length recorded"))
    else:
        off = abs(d["duration"] - d["target"]) / d["target"]
        pts = 20 if off <= LENGTH_TOLERANCE else max(0, int(20 * (1 - off)))
        out.append((pts, f"{d['duration']:.1f} min vs {d['target']:.0f} "
                         f"asked ({off:+.0%})"))

    # 4. TRUTH - what the system's OWN checks concluded
    t = 20
    notes = []
    if d["hard"]:
        t -= min(20, d["hard"] * 4)
        notes.append(f"{d['hard']} unfixed hard finding(s)")
    if not d["publish"]:
        t -= 5
        notes.append("publish gate says NOT READY")
    if d["gate"] == "REJECT":
        notes.append("topic itself refused by the truth gate")
    out.append((max(0, t), "; ".join(notes) or "no unresolved findings"))

    # 5. WATCHABILITY
    v = 20
    notes = []
    if d["shots"]:
        blank = d["slates"] / d["shots"]
        if blank > MAX_BLANK_RATIO:
            v -= min(15, int(blank * 40))
            notes.append(f"{blank:.0%} of shots blank")
        if d["drawn"] is not None and d["drawn"] / d["shots"] < MIN_DRAWN_RATIO:
            v -= 3
            notes.append("almost no drawn cards")
    if d["frozen"]:
        v -= 5
        notes.append("has a motionless stretch")
    out.append((max(0, v), "; ".join(notes) or "no blank or frozen stretches"))

    return out


def verdict(total, d):
    """The only question that matters, answered conservatively."""
    if d["fatal"] or not d["duration"]:
        return "NO - no video"
    if d["hard"]:
        return "NO - ships claims its own red team called wrong"
    if not d["publish"]:
        return "NO - the publish gate refused it"
    if total < 80:
        return "NO - too many faults"
    return "MAYBE - nothing known is broken; a human must still watch it"


LABELS = ["ran", "on topic", "length", "truth", "watchable"]


def main(paths):
    rows = []
    for p in paths:
        d = read(p)
        parts = score(d)
        total = sum(pt for pt, _ in parts)
        rows.append((d, parts, total))

        print(f"\n{'=' * 68}\n{d['file']}  |  {d.get('topic') or '(no topic)'}")
        print("=" * 68)
        print(f"  title    : {d.get('title') or '-'}")
        print(f"  video    : {d.get('duration') or '-'} min, "
              f"{d.get('scenes') or '-'} scenes, {d.get('words') or '-'} words, "
              f"{d.get('size_mb') or '-'} MB")
        print(f"  shots    : {d.get('shots') or '-'} "
              f"({d.get('drawn') or 0} drawn, {d['slates']} blank, "
              f"{d['headers']} withheld-card)")
        print(f"  gate     : {d['gate']}, {d['members']} verified member(s)")
        print()
        for (pts, note), label in zip(parts, LABELS):
            bar = "#" * pts + "." * (20 - pts)
            print(f"  {label:<10} {pts:>3}/20  {bar}  {note}")
        print(f"  {'TOTAL':<10} {total:>3}/100")
        print(f"\n  UPLOAD TO YOUTUBE?  {verdict(total, d)}")

    if len(rows) > 1:
        print(f"\n{'=' * 68}\nCAMPAIGN\n{'=' * 68}")
        print(f"  {'topic':<44} {'score':>6}  verdict")
        for d, _, total in rows:
            print(f"  {(d.get('topic') or d['file'])[:44]:<44} "
                  f"{total:>3}/100  {verdict(total, d).split(' - ')[0]}")
        good = sum(1 for d, _, t in rows if verdict(t, d).startswith("MAYBE"))
        print(f"\n  {good}/{len(rows)} runs have nothing known broken.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1:]))
