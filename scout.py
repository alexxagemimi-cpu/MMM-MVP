#!/usr/bin/env python3
"""
scout.py — choose what to make next, and be willing to choose nothing.

THE DECISION THIS REPLACES
--------------------------
Until now the topic arrived in an environment variable, or - worse - when it
was left blank brain.py asked the model to name a subject FROM MEMORY, with
no sources, no evidence and no check of any kind. That was the least
trustworthy path in the whole system and it was the one that ran by default.

THE THREE GATES, IN THIS ORDER
------------------------------
    1. GENERATE   many candidates, not one. One idea cannot be compared to
                  anything, so it always wins.
    2. IS IT TRUE?    topics.py - do independent sources agree there is a
                  real, closed answer? Kills the "runway in a list of
                  expense types" class of topic before a token is spent on
                  a script.
    3. IS IT WANTED?  youtube.py - do videos on this get watched, is it still
                  alive, and can a small channel break through?

Truth first, deliberately. A wanted topic with no true answer produces a
confident, popular, WRONG video, which is the worst thing this project can
make. Checking demand first would waste the expensive gate on topics that
were never buildable.

WHY IT CAN RETURN NOTHING
-------------------------
A scout that always finds something good is a random number generator with
better manners. Refusing a whole batch is a real outcome and the caller must
handle it - the correct response is to generate different candidates, not to
lower the bar.

MEMORY
------
made.json records every topic ever built, so the channel cannot slowly
repeat itself and so a rejected topic is not re-proposed next week.
"""

import os
import json
import time

import topics as T

MEMORY = os.environ.get("SCOUT_MEMORY", "made.json")

# Demand costs 102 quota units per topic, truth costs a model call and a web
# fetch. Neither is free, so only the topics that pass the truth gate are ever
# measured for demand.
MAX_DEMAND_CHECKS = 8


def _load(path=MEMORY):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"made": [], "rejected": []}


def _save(state, path=MEMORY):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def seen(state):
    return {t.lower().strip() for t in
            [x["topic"] for x in state.get("made", [])] +
            [x["topic"] for x in state.get("rejected", [])]}


def brainstorm(niche, call, n=15, avoid=()):
    """
    Candidates shaped to PASS the truth gate, because the gate's criteria are
    known in advance and there is no reason to generate topics that will
    obviously fail it.
    """
    avoid_txt = ("\n\nAlready covered or already rejected - do NOT repeat "
                 "these or near-duplicates:\n" +
                 "\n".join(f"- {a}" for a in sorted(avoid)[:60])) if avoid else ""
    raw = call(f"""Propose {n} explainer video topics in this niche: "{niche}"

Every topic will be checked automatically against two hard gates, so propose
only topics that can survive them:

GATE 1 - the topic must have a REAL, CLOSED, AGREED set of members.
  Independent sources, asked the same question, must come back with the SAME
  list. Good: "every blood type", "the planets", "types of coffee roast",
  "every operating system". Bad: "types of business expenses" - accountants
  classify differently, so no single list exists and any confident answer is
  partly invented. Avoid anything where experts disagree, anything ranked by
  opinion ("best X"), and anything open-ended.

GATE 2 - people must actually search for and watch it.
  Prefer subjects a curious person would look up unprompted.

Aim for 4 to 12 members - enough for a real video, few enough that each one
gets time worth watching.{avoid_txt}

One topic per line, phrased as a searchable subject, nothing else. No
numbering, no explanation.""")
    out = []
    for ln in (raw or "").splitlines():
        t = ln.strip().strip('"').lstrip("-*0123456789.) ").strip()
        if 8 < len(t) < 90 and t.lower() not in avoid:
            out.append(t)
    return out[:n]


def run(niche, call, gather, probe=None, measure=None, demand_verdict=None,
        demand_score=None, n=15, memory=MEMORY, verbose=True):
    """
    Full scout. The YouTube functions are injected so this is testable with
    fixtures and degrades cleanly when no key is configured - in which case
    it still runs the truth gate and simply cannot rank by demand.
    """
    state = _load(memory)
    avoid = seen(state)
    cands = brainstorm(niche, call, n=n, avoid=avoid)
    if verbose:
        print(f"[scout] {len(cands)} candidates in '{niche}' "
              f"({len(avoid)} already seen)")

    # ---- gate 1: is there a real answer? --------------------------------
    survived = []
    for c in cands:
        try:
            r = T.assess(c, call, gather, verbose=verbose)
        except Exception as e:
            if verbose:
                print(f"  [ERROR ] {c}: {str(e)[:90]}")
            continue
        (survived if r["build"] else state["rejected"]).append(
            r if r["build"] else {"topic": c, "why": r["reasons"][:1],
                                  "at": time.strftime("%Y-%m-%d")})
    if verbose:
        print(f"[scout] {len(survived)}/{len(cands)} have a real answer")
    if not survived:
        _save(state, memory)
        return None, state

    # ---- gate 2: does anyone want it? -----------------------------------
    if probe and measure:
        ranked = []
        for r in survived[:MAX_DEMAND_CHECKS]:
            try:
                m = measure(probe(r["topic"]))
                ok, why = demand_verdict(m)
                r["demand"] = m
                r["demand_ok"] = ok
                r["demand_why"] = why
                r["score"] = demand_score(m)
                if verbose:
                    print(f"  [{'WANTED' if ok else 'UNWANTED'}] "
                          f"{r['topic']}  (score {r['score']})")
                    for w in why:
                        print(f"        - {w}")
            except Exception as e:
                # A quota wall must not silently downgrade every topic to
                # zero and make an arbitrary pick look like a measured one.
                if verbose:
                    print(f"  [demand ] {r['topic']}: {str(e)[:90]}")
                r["score"] = None
            ranked.append(r)
        measured = [r for r in ranked if r.get("score") is not None]
        if not measured:
            if verbose:
                print("[scout] demand could not be measured for ANY topic - "
                      "refusing to pick blind")
            _save(state, memory)
            return None, state
        measured.sort(key=lambda r: -r["score"])
        best = measured[0]
        if best["score"] <= 0:
            if verbose:
                print("[scout] every topic that is TRUE is also unwanted - "
                      "no pick. Generate different candidates; do not lower "
                      "the bar.")
            _save(state, memory)
            return None, state
    else:
        if verbose:
            print("[scout] no YouTube key - ranking on agreement alone, "
                  "which says nothing about whether anyone wants it")
        survived.sort(key=lambda r: -r["agreement"])
        best = survived[0]

    state["made"].append({"topic": best["topic"],
                          "members": best.get("members", []),
                          "at": time.strftime("%Y-%m-%d")})
    _save(state, memory)
    if verbose:
        print(f"\n[scout] CHOSE: {best['topic']}")
        print(f"        members  : {', '.join(best.get('members', []))}")
        print(f"        agreement: {best.get('agreement')}")
        if best.get("demand"):
            d = best["demand"]
            print(f"        demand   : breakout {d['breakout']}, median "
                  f"{d['median_views']:,} views, {int(d['fresh_2y']*100)}% recent")
    return best, state


if __name__ == "__main__":
    print(__doc__.strip()[:1200])
    print("\nRun from brain.py, or import and inject call/gather/probe.")
