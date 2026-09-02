#!/usr/bin/env python3
"""
test_wiring.py - the bug this project keeps shipping, caught by a machine.

THE PATTERN
-----------
Seven times now, a value has been computed, logged about, and then never
actually used. Every one of them looked correct in review and green in the
log, and every one was found by a human noticing something odd in an
artifact days later:

    5.6   modes.py written, tested, never imported by brain.py
    5.8   overview_clip, point_card and stat_card - built, never called
    5.11  `too_large` computed in _call_sweep, never acted on, so a 413
          re-sent identical bytes forever
    5.12  `drop_schema` set and printed about, never passed down, so
          "retrying plain JSON" retried the exact same request
    -     `title_eyebrow` read in the asset phase, defined in the render
          phase; the try/except swallowed it and the build stayed green
    -     engine.py kept private copies of MEMBER_BEATS/CLOSING_BEATS while
          CLAUDE.md claimed the rules had been centralised in modes.py

That is not seven unrelated mistakes. It is one mistake with seven faces,
and reviewing harder has visibly not fixed it. So it gets a test.

WHAT THIS CANNOT DO
-------------------
It cannot prove a value is used *correctly* - only that it is used at all.
`drop_schema` would have passed a "is it referenced?" check on the day it
was broken, because it was referenced in a print(). So the orphan checks
below are a floor, not a ceiling, and the ALLOW lists are the honest record
of what a static pass cannot decide.

    python3 test_wiring.py
"""

import ast
import os
import re
import sys

PY = sorted(f for f in os.listdir(".") if f.endswith(".py"))
SRC = {f: open(f, encoding="utf-8").read() for f in PY}
TREES = {f: ast.parse(s) for f, s in SRC.items()}
BLOB = "\n".join(SRC.values())


# Parameters a static pass flags that are genuinely fine. Each one needs a
# reason - an allowlist without reasons becomes a place to hide real bugs.
ALLOW_PARAMS = {
    # test doubles must match the signature of the thing they replace
    ("test_engine_local.py", "fake_video"),
    ("test_engine_local.py", "fake_synth"),
    ("test_providers.py", "__call__"),
    ("test_providers.py", "fake"),
    ("test_providers.py", "dead_gemini"),
    ("test_providers.py", "live_groq"),
    ("test_providers.py", "picky"),
    # verdict() takes the topic for symmetry with score(); the arithmetic
    # genuinely does not need it
    ("topics.py", "verdict"),
}

# Functions reached by something a regex cannot see.
ALLOW_ORPHANS = {
    # dispatched from a dict: {"stat": graphics.stat_clip, ...}
    "stat_clip", "point_clip", "compare_clip",
    # passed as a reference, never called with parens at the call site
    "fetch_page",          # pool.map(fetch_page, ...)
    "fetch_shot_asset",    # asyncio.to_thread(fetch_shot_asset, ...)
    "gemini_ask",          # injected as verify.watch(..., ask=gemini_ask)
    # sound kit: built by name from a table in sfx.build()
    "whoosh", "tick", "thud", "riser", "pop",
    # entry points called from the workflow, not from Python
    "from_script", "one",
}


def unused_params():
    print("PARAMETERS ACCEPTED AND NEVER USED")
    print("  (drop_schema's exact shape - 5.12)")
    bad = []
    for f, tree in TREES.items():
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (f, fn.name) in ALLOW_PARAMS:
                continue
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            names |= {n.attr for n in ast.walk(fn)
                      if isinstance(n, ast.Attribute)}
            for a in fn.args.args + fn.args.kwonlyargs:
                if a.arg in ("self", "cls") or a.arg.startswith("_"):
                    continue
                if a.arg not in names:
                    bad.append(f"{f}:{fn.lineno} {fn.name}({a.arg})")
    for b in bad:
        print(f"  FAIL  {b}")
    if not bad:
        print("  ok    every parameter is read by its own function")
    return len(bad)


def orphan_functions():
    print("\nFUNCTIONS DEFINED AND NEVER CALLED")
    print("  (modes.py, overview_clip, point_card, stat_card - 5.6 / 5.8)")
    bad = []
    for f, tree in TREES.items():
        if f.startswith("test_"):
            continue                      # test bodies are collected by name
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            n = fn.name
            if n.startswith("_") or n == "main" or n in ALLOW_ORPHANS:
                continue
            # a call looks like `name(` or `module.name(`
            calls = len(re.findall(rf"(?<!def )\b{re.escape(n)}\s*\(", BLOB))
            if calls == 0:
                bad.append(f"{f}:{fn.lineno} {n}()")
    for b in bad:
        print(f"  FAIL  {b}  -- built and never wired in")
    if not bad:
        print("  ok    every module-level function has a caller")
    return len(bad)


def beat_rules_are_centralised():
    """
    THE SPECIFIC DRIFT THAT WAS LIVE UNTIL TODAY.

    CLAUDE.md section 11 says the beat rules "moved to modes.py so the
    engine, the red team and brain cannot drift apart". redteam.py really
    did call modes.is_member(). engine.py kept its own copies and compared
    strings itself, so the claim was half false and nothing would have
    noticed until someone added a beat and the checklist quietly disagreed
    with the red team.
    """
    print("\nBEAT RULES COME FROM modes.py, NOWHERE ELSE")
    bad = 0
    literal = re.compile(r"\{\s*[\"'](?:CATEGORY|STEP|CLOSE|RESONANCE)[\"']")
    for f, s in SRC.items():
        if f in ("modes.py", "test_wiring.py"):
            continue
        # strip comments so the explanation of the bug is not the bug
        code = "\n".join(l.split("#")[0] for l in s.split("\n"))
        if literal.search(code):
            print(f"  FAIL  {f} declares its own beat set - "
                  f"call modes.is_member()/is_closing() instead")
            bad += 1
    for f in ("engine.py", "redteam.py"):
        uses = "modes.is_member" in SRC[f] or "modes.is_closing" in SRC[f]
        print(f"  {'ok  ' if uses else 'FAIL'}  {f} asks modes.py")
        bad += not uses
    if not bad:
        print("  ok    one definition, every consumer reads it")
    return bad


def graphics_are_reachable():
    """Every card renderer must be called from engine.py - 5.8 four times."""
    print("\nEVERY CARD RENDERER IS CALLED BY THE ENGINE")
    bad = 0
    eng = SRC["engine.py"]
    for fn in ast.walk(TREES["graphics.py"]):
        if not isinstance(fn, ast.FunctionDef):
            continue
        n = fn.name
        if n.startswith("_") or not (n.endswith("_card") or n.endswith("_clip")):
            continue
        used = f"graphics.{n}" in eng
        print(f"  {'ok  ' if used else 'FAIL'}  graphics.{n}")
        bad += not used
    return bad


def main():
    bad = (unused_params() + orphan_functions()
           + beat_rules_are_centralised() + graphics_are_reachable())
    print()
    if bad:
        print(f"{bad} WIRING FAULT(S) - something is built and not connected.")
        print("This is the project's most-repeated bug. Wire it or delete it.")
    else:
        print("all wired - nothing is computed, logged, and then ignored.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
