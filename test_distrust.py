#!/usr/bin/env python3
"""
test_distrust.py - a claim the red team called invented must not get a card.

WHY THIS EXISTS
---------------
Run 38 wrote "top block" as one of three structural measurements of a pair
of jeans. It is not a standard term and appears in none of the 14 sources
the run fetched. The red team caught it - HARD, twice:

    [hard] unsupported scene 1: "A pair of denim jeans is defined by just
           three structural me" Sources list more than three measurements
           and do not define 'top block' as a standard term.
    [hard] unsupported scene 2: "First is the top block, which refers
           strictly to the crotch " 'Top block' is not defined in any of
           the listed sources.

The publish gate said NOT READY TO PUBLISH and named all of it. Then the
repair could not run - every provider was out of quota - and the engine,
which had never read the findings, printed TOP BLOCK across the screen in
the largest type in the video with a definition underneath.

This is CLAUDE.md 11 exactly: the RUNWAY failure in a different topic. A
made-up term given a card's authority.

Rewriting the narration needs a model, and there may not be one. NOT
amplifying it needs nothing at all. That is what this tests.

The findings below are copied from run 38's log, verbatim. Hand-written
samples would be written by the same reasoning that wrote the bug.

    python3 test_distrust.py
"""

import sys
import types
import importlib.util

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))
_spec = importlib.util.spec_from_file_location("engine_mod", "engine.py")
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)


# Verbatim from run 38.
RUN38 = [
    {"severity": "hard", "kind": "unsupported", "scene": 1,
     "quote": "A pair of denim jeans is defined by just three structural me",
     "detail": "Sources do not define 'top block' as a standard term."},
    {"severity": "hard", "kind": "unsupported", "scene": 2,
     "quote": "First is the top block, which refers strictly to the crotch ",
     "detail": "'Top block' is not defined in any of the listed sources."},
    {"severity": "hard", "kind": "wrong", "scene": 2,
     "quote": "A rise under nine inches is classified as low-rise.",
     "detail": "Most guides define low-rise as under about 8 inches."},
    {"severity": "hard", "kind": "unsupported", "scene": 3,
     "quote": "GQ famously likens straight jeans to Baby Bear's porridge",
     "detail": "No GQ article contains this analogy."},
    {"severity": "hard", "kind": "unsupported", "scene": 6,
     "quote": "Mid-rise is widely considered the single most classic rise",
     "detail": "Not backed by any of the listed guides."},
    # Soft findings must NOT gag a scene: they are style notes, not
    # 'this is invented'. Gagging on them would strip cards off a sound
    # script and quietly make the video worse.
    {"severity": "soft", "kind": "vague", "scene": 7,
     "quote": "Low-rise jeans actually accommodate a broader range",
     "detail": "Vague."},
    {"severity": "soft", "kind": "unsupported", "scene": 8,
     "quote": "Tall men measuring six feet or taller often favor high-rise",
     "detail": "No source backs this."},
    # A whole-script finding names no scene and cannot single one out.
    {"severity": "hard", "kind": "too-complex", "scene": None,
     "quote": "", "detail": "Reading level too high."},
]

SCENES = [{"scene": i + 1, "narration": f"n{i}", "key_term": f"t{i}"}
          for i in range(8)]


def case(name, script, want):
    got = E._distrusted_scenes(script)
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"          wanted {sorted(want)}, got {sorted(got)}")
    return 0 if ok else 1


def main():
    bad = 0
    print("which scenes get gagged (0-based indices)\n"
          "-----------------------------------------")

    # scenes 1,2,3,6 (1-based) -> 0,1,2,5
    bad += case("run 38's real findings gag exactly the hard-flagged scenes",
                {"scenes": SCENES, "red_team": RUN38}, {0, 1, 2, 5})

    bad += case("a soft-only finding gags nothing",
                {"scenes": SCENES,
                 "red_team": [f for f in RUN38
                              if f["severity"] != "hard"]}, set())

    bad += case("a whole-script hard finding gags nothing (names no scene)",
                {"scenes": SCENES,
                 "red_team": [{"severity": "hard", "kind": "too-complex",
                               "scene": None}]}, set())

    print("\nit must not fall over on a malformed file\n"
          "-----------------------------------------")
    bad += case("no red_team key at all", {"scenes": SCENES}, set())
    bad += case("red_team is a dict, not a list",
                {"scenes": SCENES, "red_team": {"hard": []}}, set())
    bad += case("a finding is a string",
                {"scenes": SCENES, "red_team": ["broken"]}, set())
    bad += case("scene number is out of range",
                {"scenes": SCENES,
                 "red_team": [{"severity": "hard", "scene": 99}]}, set())
    bad += case("scene number is not a number",
                {"scenes": SCENES,
                 "red_team": [{"severity": "hard", "scene": "two"}]}, set())
    bad += case("severity is capitalised",
                {"scenes": SCENES,
                 "red_team": [{"severity": "HARD", "scene": 4}]}, {3})

    # The engine drops scenes with empty narration BEFORE indexing them, so a
    # finding's scene number and the engine's list can disagree. Getting this
    # wrong gags an innocent scene and leaves the invented one on screen -
    # worse than doing nothing, because it looks like it worked.
    print("\nindex alignment when a scene is dropped\n"
          "---------------------------------------")
    raw = [dict(s) for s in SCENES]
    raw[1]["narration"] = ""            # scene 2 will be filtered out
    kept = [s for s in raw if (s.get("narration") or "").strip()]
    flagged = E._distrusted_scenes({"scenes": raw,
                                    "red_team": [{"severity": "hard",
                                                  "scene": 6}]})
    bad_ids = {id(raw[k]) for k in flagged}
    mapped = {i for i, s in enumerate(kept) if id(s) in bad_ids}
    # scene 6 is raw index 5; with raw[1] dropped it is kept index 4
    ok = mapped == {4}
    print(f"  {'ok  ' if ok else 'FAIL'}  a dropped scene shifts the index "
          f"and the RIGHT scene is still gagged (got {sorted(mapped)}, "
          f"want [4])")
    bad += not ok
    named = kept[4]["key_term"] if mapped == {4} else "?"
    print(f"          gagged term is {named!r} - scene 6's own term, "
          f"not its neighbour's")

    print()
    if bad:
        print(f"{bad} FAILED - an invented term can still reach a card.")
    else:
        print("all passed - a hard-flagged claim gets no card, no checklist "
              "row, no diagram column.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
