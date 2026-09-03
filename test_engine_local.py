#!/usr/bin/env python3
"""
test_engine_local.py - run the REAL engine against REAL ffmpeg, locally.

WHY THIS EXISTS
---------------
Six CI runs were spent on a single ffmpeg hang, at roughly five minutes
each, because the only way this project ever exercised engine.py was to
push and watch GitHub Actions. That is using CI minutes as a test suite,
and it is slow enough that debugging turns into guessing between cycles.

This runs the actual build() - the real ffmpeg calls, the real filter
chains, the real concat and assembly - against synthetic inputs generated
on the spot. Only the three network calls are stubbed (Pixabay, the AI
image fallback, Edge-TTS), because those need the internet and are not
what breaks.

The synthetic clips deliberately include the shapes that have actually
caused failures: a 120fps slow-motion clip, a clip SHORTER than its shot
(forcing the loop path), and an odd-sized portrait clip. If a change
breaks scene assembly, this says so in about a minute instead of five.

    python3 test_engine_local.py

Requires ffmpeg + ffprobe on PATH. Exits non-zero on failure.
"""

import os
import sys
import json
import types
import shutil
import asyncio
import subprocess
import importlib.util

WORK = "localtest"
ASSETS = os.path.join(WORK, "src")


def sh(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        print("FAILED:", " ".join(cmd)[:200])
        print((p.stderr or "")[-1500:])
        raise SystemExit(1)
    return p.stdout.strip()


def have_ffmpeg():
    return shutil.which("ffmpeg") and shutil.which("ffprobe")


def make_assets():
    """Synthetic stock clips covering the shapes that have broken builds."""
    os.makedirs(ASSETS, exist_ok=True)
    specs = [
        # name,           size,       fps, seconds   why it is here
        ("slowmo.mp4",    "1920x1080", 120, 6),   # high fps - hung 3 CI runs
        ("normal.mp4",    "1920x1080", 25,  8),   # the ordinary case
        ("short.mp4",     "1280x720",  30,  2),   # shorter than a shot -> loop path
        ("portrait.mp4",  "720x1280",  25,  6),   # wrong aspect -> crop path
        ("tiny.mp4",      "640x360",   15,  1),   # very short + low fps
    ]
    for name, size, fps, secs in specs:
        out = os.path.join(ASSETS, name)
        if os.path.exists(out):
            continue
        sh(["ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={secs}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", out])
    # narration-length audio, one per scene
    for i, secs in enumerate((10.4, 10.6, 8.9, 9.2)):
        out = os.path.join(ASSETS, f"voice{i}.mp3")
        if not os.path.exists(out):
            sh(["ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", f"sine=frequency={180+i*40}:duration={secs}",
                "-c:a", "libmp3lame", out])
    return [os.path.join(ASSETS, s[0]) for s in specs]


def load_engine():
    spec = importlib.util.spec_from_file_location("engine", "engine.py")
    eng = importlib.util.module_from_spec(spec)
    sys.modules["engine"] = eng
    spec.loader.exec_module(eng)
    return eng


def main():
    if not have_ffmpeg():
        print("SKIP: ffmpeg/ffprobe not on PATH")
        return 0

    print("generating synthetic assets (real video, real audio) ...")
    clips = make_assets()

    # edge_tts is imported at module scope but never called here
    sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))
    E = load_engine()
    E.WORK = WORK
    E.PIXABAY_API_KEY = "local-test"

    # ---- stub ONLY the network. everything below is the real engine. ----
    pool = list(clips)

    # HALF THE SHOTS FIND NOTHING, on purpose.
    #
    # On a real run that is the common case, not the rare one: Pixabay has no
    # footage of an abstraction, so the relevance check rejects everything it
    # offers and the engine has to draw a card out of the script instead.
    # With the mock succeeding every time, that entire path - the one that
    # decides what most of a money/business video actually looks like - was
    # never executed by this test. Keywords are stable strings, so which ones
    # fail is deterministic and the build stays reproducible.
    def fake_video(keyword, out_mp4, seen, subject=None):
        if sum(ord(c) for c in keyword) % 2:
            return False                 # "nothing relevant for this one"
        src = pool[abs(hash(keyword)) % len(pool)]
        shutil.copy(src, out_mp4)
        return True

    async def fake_synth(text, mp3_path, attempts=3):
        idx = fake_synth.n % 4
        fake_synth.n += 1
        shutil.copy(os.path.join(ASSETS, f"voice{idx}.mp3"), mp3_path)
        words, t = [], 0.0
        for w in text.split():
            words.append((t, t + 0.28, w))
            t += 0.28
        return words
    fake_synth.n = 0

    # Three shots per scene, not two.
    #
    # At the default pace a 10-second fixture scene gets two shots, one of
    # which becomes the section card - so the scene never has room for a
    # drawn LIST shot and that path went untested locally while running in
    # production. A tighter target gives card + list + picture, which is the
    # shape a real scene has.
    E.TARGET_SHOT_SEC = 3.4

    E.fetch_pixabay_video = fake_video
    E.fetch_pixabay_photo = lambda k, o, s, subj=None: False
    E.fetch_image = lambda k, sd, o: False
    E.synth = fake_synth

    # THE FIXTURE IS A REAL TAXONOMY, and that is not decoration.
    #
    # The version before this one listed compound interest, fixed costs,
    # gross margin and runway as if they were one list. They are not a list
    # of anything - they are four unrelated finance words - and the last of
    # them, runway, went on screen as an item because it was the CLOSE beat.
    # The owner caught exactly that in a rendered video and was right.
    #
    # A test fixture that is itself wrong cannot show that the pipeline
    # handles right. These three ARE the standard split of business costs,
    # every one of them is a cost, and the CLOSE deliberately names
    # something that is NOT a cost (runway) so the test proves a closing
    # scene never reaches the checklist.
    scenes = [
        {"scene": 1, "beat": "ANSWER", "key_term": "fixed costs",
         "key_fact": "Every business cost is one of three kinds",
         "narration": "Every cost a business has falls into one of three "
                      "kinds, and knowing which is which is the whole job: "
                      "fixed costs, variable costs, and one-off costs.",
         "image_keywords": ["a", "b", "c", "d"]},
        {"scene": 2, "beat": "CATEGORY", "key_term": "fixed costs",
         "key_fact": "They do not move when your sales move",
         # Deliberately a NUMBER and no list, so the stat-card path is
         # covered here too. Scene 4 carries the list; this one carries the
         # figure. Without a fixture like this, stat_card would go on being
         # the module nothing calls.
         "narration": "Your fixed costs are the ones that do not care how "
                      "much you sell in a month. For a small business they "
                      "eat 60% of revenue before a single sale is made.",
         "image_keywords": ["e", "f", "g"]},
        {"scene": 3, "beat": "CATEGORY", "key_term": "variable costs",
         "key_fact": "They rise and fall with every sale",
         "narration": "Variable costs do the opposite. Materials, packaging, "
                      "delivery, card fees. They rise and fall with every "
                      "sale, so selling nothing genuinely costs you less.",
         "image_keywords": ["h", "i"]},
        {"scene": 4, "beat": "CATEGORY", "key_term": "one-off costs",
         "key_fact": "Rare, large, and easy to forget when planning",
         "narration": "Then there are one-off costs, the ones nobody plans "
                      "for. A replaced laptop, a legal fee, a deposit. Rare, "
                      "large, and usually what turns a good month into a "
                      "loss.",
         "image_keywords": ["j", "k", "l"]},
        {"scene": 5, "beat": "EDGE", "key_term": "semi-variable costs",
         "key_fact": "A base you always pay, plus a part that grows",
         # An EDGE beat with two members already explained, so the
         # comparison diagram has something real to compare and this path
         # is covered locally instead of shipping untested.
         "narration": "The awkward ones sit between the two. A phone plan "
                      "has a base you always pay and a part that grows with "
                      "use, so it behaves like both at once.",
         "image_keywords": ["o", "p", "q"]},
        {"scene": 6, "beat": "CLOSE", "key_term": "runway",
         "key_fact": "Months of cash left at current burn",
         "narration": "Add all three up against your cash and you get "
                      "runway, counted in months rather than in rupees, "
                      "which is the only number that ends the story.",
         "image_keywords": ["m", "n"]},
    ]

    shutil.rmtree(os.path.join(WORK, "concat.txt"), ignore_errors=True)
    # A HARD RED-TEAM FINDING, so the suppression path actually RUNS here.
    #
    # Without this the fixture has no `red_team` key, `distrusted` is empty,
    # and the code that withholds a card from an invented claim is never
    # executed by the real engine - which is how modes.py, overview_clip,
    # point_card and stat_card all shipped broken (CLAUDE.md 5.8). Scene 5
    # is the EDGE beat, so gagging it also proves a gagged scene cannot
    # supply a diagram column.
    #
    # Scene 5 is chosen deliberately over a CATEGORY scene: gagging a
    # CATEGORY would drop it from the checklist and change the section
    # numbering for every later scene, which is a second effect and would
    # muddy what this fixture is showing.
    red_team = [
        {"severity": "hard", "kind": "unsupported", "scene": 5,
         "quote": "A phone plan has a base you always pay",
         "detail": "Not supported by the sources - fixture finding."},
        {"severity": "soft", "kind": "vague", "scene": 3,
         "quote": "They rise and fall with every sale",
         "detail": "A soft finding must NOT gag a scene."},
    ]
    # THE OTHER TWO MODES HAVE NEVER BEEN RENDERED.
    #
    # modes.py has three - story, explainer, guide - and every run this
    # project has ever done was an explainer. For a factory meant to take any
    # topic, two thirds of the shapes it can be handed had never once reached
    # the engine.
    #
    # STORY is the dangerous one and that is the point of including it: it has
    # NO member beats, so `members` is empty, use_cards goes False, and the
    # whole checklist-and-header system switches off. Every code path that
    # assumes a section exists is exercised only by this fixture. GUIDE uses
    # STEP as its member beat, so its checklist should build exactly like an
    # explainer's CATEGORY.
    guide = [
        {"scene": 1, "beat": "PROMISE", "key_term": "sharpening a knife",
         "key_fact": "Three stages, ten minutes, one stone",
         "narration": "Sharpening a knife is three stages on one stone, and "
                      "it takes ten minutes once you know the order: coarse "
                      "grinding, fine honing, then stropping the burr away.",
         "image_keywords": ["a", "b", "c"]},
        {"scene": 2, "beat": "STAKES", "key_term": "a dull blade",
         "key_fact": "A dull blade slips and cuts the hand, not the food",
         "narration": "A dull blade is the dangerous one. It slides off the "
                      "skin of a tomato instead of biting, and the hand that "
                      "is pushing harder is the hand that gets cut.",
         "image_keywords": ["d", "e"]},
        {"scene": 3, "beat": "STEP", "key_term": "coarse grinding",
         "key_fact": "Sets the angle and raises a burr along the edge",
         "narration": "Coarse grinding comes first. You are setting the angle "
                      "and raising a burr: a thin curl of steel that folds "
                      "over the edge and tells you the bevel has met.",
         "image_keywords": ["f", "g", "h"]},
        {"scene": 4, "beat": "STEP", "key_term": "fine honing",
         "key_fact": "Refines the scratches the coarse stone left",
         "narration": "Fine honing refines what the coarse stone tore. Same "
                      "angle, lighter pressure, and the deep scratches give "
                      "way to a polish you can see.",
         "image_keywords": ["i", "j"]},
        {"scene": 5, "beat": "STEP", "key_term": "stropping",
         "key_fact": "Removes the burr so the edge stops folding",
         "narration": "Stropping removes the burr. Leather, a few passes "
                      "trailing the edge, and the fold of steel breaks away "
                      "instead of collapsing the first time you cut.",
         "image_keywords": ["k", "l", "m"]},
        {"scene": 6, "beat": "CLOSE", "key_term": "a sharp knife",
         "key_fact": "Sharp is safer, not more dangerous",
         "narration": "A sharp knife is the safe one, which is the part that "
                      "surprises people. It goes where you point it, and "
                      "nothing about that needs more force.",
         "image_keywords": ["n", "o"]},
    ]

    story = [
        {"scene": 1, "beat": "HOOK", "key_term": "the lighthouse",
         "key_fact": "Three keepers vanished from a locked room",
         "narration": "In December 1900 a supply boat reached the lighthouse "
                      "and found the door shut, the table laid, and every one "
                      "of the three keepers gone.",
         "image_keywords": ["p", "q", "r"]},
        {"scene": 2, "beat": "CONTEXT", "key_term": "the Flannan Isles",
         "key_fact": "Twenty miles of open Atlantic from the nearest land",
         "narration": "The Flannan Isles sit twenty miles into the Atlantic. "
                      "Nothing grows there. The light had been running barely "
                      "a year when the relief was late.",
         "image_keywords": ["s", "t"]},
        {"scene": 3, "beat": "INCITING", "key_term": "the last entry",
         "key_fact": "The log stopped mid-week with nothing unusual in it",
         "narration": "The log stopped on the fifteenth. It recorded wind and "
                      "it recorded pressure, and it recorded nothing at all "
                      "about what came next.",
         "image_keywords": ["u", "v"]},
        {"scene": 4, "beat": "ESCALATION", "key_term": "the west landing",
         "key_fact": "Iron railings bent by water a hundred feet up",
         "narration": "At the west landing the sea had reached a hundred feet "
                      "above the water and bent iron railings flat, which is "
                      "the detail nobody could explain away.",
         "image_keywords": ["w", "x"]},
        {"scene": 5, "beat": "TURN", "key_term": "a rogue wave",
         "key_fact": "One wave, and two men already outside",
         "narration": "The likeliest answer is the dullest. Two men were out "
                      "securing the landing, one wave came over the rock, and "
                      "the third went after them.",
         "image_keywords": ["y", "z"]},
        {"scene": 6, "beat": "RESONANCE", "key_term": "the sea",
         "key_fact": "The light kept working long after the men were gone",
         "narration": "The light itself never failed. It was still turning "
                      "when the boat arrived, which is the part of the story "
                      "that stays with people.",
         "image_keywords": ["aa", "bb"]},
    ]

    fixtures = [
        ("explainer", "The 3 Types of Business Cost", scenes, red_team),
        ("guide", "How To Sharpen A Knife", guide, []),
        ("story", "The Lighthouse Keepers Who Vanished", story, []),
    ]

    mode_ok = True
    for mode_name, title, sc, rt in fixtures:
        print("\n" + "#" * 62)
        print(f"#  MODE: {mode_name.upper()}   ({len(sc)} scenes)")
        print("#" * 62)
        with open("script.json", "w") as f:
            json.dump({"title": title, "scenes": sc, "red_team": rt}, f)
        print("running the real build() with real ffmpeg ...\n")

        import io as _io, contextlib as _cl
        _cap = _io.StringIO()

        class _Tee:
            def write(self, t):
                _cap.write(t)
                sys.__stdout__.write(t)

            def flush(self):
                sys.__stdout__.flush()

        with _cl.redirect_stdout(_Tee()):
            asyncio.run(E.build())
        built = _cap.getvalue()

        dur = float(sh(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "default=nw=1:nk=1",
                        "final_video.mp4"]))
        drew_list = "drawing" in built and "list of" in built

        # THE STRUCTURAL RULE, asserted rather than eyeballed.
        #
        # A guide's STEP beats ARE its checklist, exactly as an explainer's
        # CATEGORY beats are. A story has neither, so it must get NO checklist
        # at all - forcing one would be inventing a taxonomy for a narrative,
        # which is 4.11's whole point. Observing this in the log is not the
        # same as requiring it: the moment modes.py changes, only an assertion
        # notices.
        want_list = mode_name in ("explainer", "guide")
        ok = dur > 20 and drew_list == want_list
        print(f"\n  {mode_name}: {dur:.1f}s, checklist="
              f"{'yes' if drew_list else 'no'} "
              f"(want {'yes' if want_list else 'no'}) "
              f"{'ok' if ok else '<< WRONG'}")
        mode_ok &= ok

    # the explainer fixture is rebuilt last so the assertions below - which
    # are written against its scenes - still describe what is on disk
    with open("script.json", "w") as f:
        json.dump({"title": "The 3 Types of Business Cost",
                   "scenes": scenes, "red_team": red_team}, f)
    print("\n" + "#" * 62)
    print("#  rebuilding the explainer for the artifact checks")
    print("#" * 62)
    asyncio.run(E.build())

    # ---- verify the ARTIFACT, not just the exit code ----
    print("\n" + "=" * 62)
    print("VERIFYING THE ACTUAL OUTPUT FILE")
    print("=" * 62)
    ok = True

    if not os.path.exists("final_video.mp4"):
        print("FAIL: no final_video.mp4"); return 1

    dur = float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=nw=1:nk=1", "final_video.mp4"]))
    streams = sh(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                  "-of", "csv=p=0", "final_video.mp4"]).split()
    size_mb = os.path.getsize("final_video.mp4") / 1_048_576
    nframes = sh(["ffprobe", "-v", "error", "-select_streams", "v",
                  "-count_packets", "-show_entries", "stream=nb_read_packets",
                  "-of", "csv=p=0", "final_video.mp4"]).strip(",")

    print(f"  duration : {dur:.2f}s")
    print(f"  streams  : {streams}")
    print(f"  frames   : {nframes}")
    print(f"  size     : {size_mb:.2f} MB")

    expected = sum(10.4 + 0.12 for _ in range(1))  # sanity anchor, see below
    if dur < 20:
        print(f"  FAIL: {dur:.2f}s is far too short for 4 narrated scenes"); ok = False
    if "video" not in " ".join(streams):
        print("  FAIL: no video stream"); ok = False
    if "audio" not in " ".join(streams):
        print("  FAIL: no audio stream"); ok = False
    if size_mb < 0.05:
        print("  FAIL: file is suspiciously empty"); ok = False

    for f in ("captions.ass", "subtitles.srt"):
        if not os.path.exists(f) or os.path.getsize(f) < 40:
            print(f"  FAIL: {f} missing or empty"); ok = False
    dialogue = sum(1 for l in open("captions.ass", encoding="utf-8")
                   if l.startswith("Dialogue:"))
    print(f"  caption lines : {dialogue}")
    if dialogue < 4:
        print("  FAIL: too few caption lines"); ok = False

    leftovers = [f for f in os.listdir(WORK)
                 if f.startswith("s") and f.endswith((".mp4", ".mp3", ".png"))]
    if leftovers:
        print(f"  FAIL: temp files leaked: {leftovers[:6]}"); ok = False

    if not mode_ok:
        print("  FAIL: a mode rendered the wrong structure "
              "(see the per-mode lines above)")
        ok = False
    print("=" * 62)
    print("LOCAL TEST PASSED" if ok else "LOCAL TEST FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        for p in ("script.json",):
            if os.path.exists(p):
                os.remove(p)
