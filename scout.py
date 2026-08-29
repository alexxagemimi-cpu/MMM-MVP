#!/usr/bin/env python3
"""
scout.py — choose what to make next, keep the runners-up, never repeat.

THE PIPELINE THIS IMPLEMENTS
----------------------------
    1. GENERATE   20 candidates in the niche, from a model that has just been
                  told exactly what the gates will test.
    2. RED-TEAM   topics.py: do independent sources agree there is a real,
                  closed answer? Kills the "runway in a list of expense
                  types" class of topic before a token is spent on a script.
    3. RED-TEAM   youtube.py: does anyone watch this, is it still alive, and
                  can a small channel break through?
    4. KEEP THE REST. Every topic that survives both gates goes to a BACKLOG.
    5. BLOCK.     Anything made, and anything rejected, can never be proposed
                  again.

WHY THE BACKLOG IS THE IMPORTANT PART
-------------------------------------
The previous version gated twenty topics, picked one, and threw the other
survivors away - so the next run paid the whole research and quota cost over
again to rediscover topics it had already proved were good. That is not a
small inefficiency: each survivor cost a web fetch, a model call and 102
YouTube quota units to verify.

Now the survivors are saved, and a later run takes the best from the backlog
without spending anything at all. Generation only happens when the backlog
runs dry. The backlog is also visible on its own - the "suggested but not yet
made" list - so a human can read what the system believes is worth building
and disagree with it.

BLOCKING IS PERMANENT AND IN BOTH DIRECTIONS
--------------------------------------------
Made topics are blocked so the channel cannot slowly repeat itself. Rejected
topics are blocked too, which matters more than it looks: without it the
generator proposes the same plausible-sounding bad idea every single week and
the expensive gates re-prove the same rejection forever.

    python3 scout.py status      # show the backlog and what is blocked
"""

import os
import json
import time

import topics as T

MEMORY = os.environ.get("SCOUT_MEMORY", "made.json")

# Demand costs 102 quota units per topic; truth costs a model call and a web
# fetch. Only topics that survive the truth gate are ever measured for demand.
MAX_DEMAND_CHECKS = 10
CANDIDATES = 20


def _blank():
    return {"made": [], "backlog": [], "rejected": []}


def load(path=MEMORY):
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        for k in ("made", "backlog", "rejected"):
            s.setdefault(k, [])
        return s
    except Exception:
        return _blank()


def save(state, path=MEMORY):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _key(t):
    return " ".join((t or "").lower().split())


def blocked(state):
    """Made AND rejected. Both must block regeneration - see module docstring."""
    return ({_key(x["topic"]) for x in state["made"]} |
            {_key(x["topic"]) for x in state["rejected"]})


def brainstorm(niche, call, n=CANDIDATES, avoid=()):
    """
    Candidates shaped to survive the gates, because the gates' criteria are
    known in advance and there is no point generating topics that will
    obviously fail them.
    """
    avoid_txt = ""
    if avoid:
        avoid_txt = ("\n\nALREADY MADE OR ALREADY REJECTED - do not propose "
                     "these or anything close to them:\n" +
                     "\n".join(f"- {a}" for a in sorted(avoid)[:80]))
    raw = call(f"""Propose {n} explainer video topics in this niche: "{niche}"

Each will be checked automatically against two hard gates. Propose only
topics that can survive both.

GATE 1 - THE TOPIC MUST HAVE A REAL, CLOSED, AGREED SET OF MEMBERS.
  Independent sources, asked the same question, must return the SAME list.
  Good: "every blood type", "the planets", "types of coffee roast", "every
  operating system" - each has an answer you can look up and verify.
  Bad: "types of business expenses" - accountants classify by behaviour, by
  function and by tax treatment, so no single list exists and any confident
  answer is partly invented. Also bad: anything ranked by opinion ("best
  X", "top 10"), anything open-ended, anything where experts disagree.

GATE 2 - PEOPLE MUST ACTUALLY SEARCH FOR AND WATCH IT.
  Prefer subjects a curious person looks up unprompted. Avoid subjects only
  a specialist would ever type.

Aim for 4 to 12 members - enough for a real video, few enough that each gets
time worth watching.{avoid_txt}

One topic per line, phrased as a searchable subject. No numbering, no
explanation, no preamble.""")

    out, seen_here = [], set()
    for ln in (raw or "").splitlines():
        t = ln.strip().strip('"').lstrip("-*0123456789.)â€¢ ").strip()
        k = _key(t)
        if 8 < len(t) < 90 and k not in avoid and k not in seen_here:
            seen_here.add(k)
            out.append(t)
    return out[:n]


def refill(niche, call, gather, probe=None, measure=None,
           demand_verdict=None, demand_score=None, n=CANDIDATES,
           memory=MEMORY, verbose=True):
    """Generate and gate a fresh batch, adding every survivor to the backlog."""
    state = load(memory)
    avoid = blocked(state) | {_key(b["topic"]) for b in state["backlog"]}
    cands = brainstorm(niche, call, n=n, avoid=avoid)
    if verbose:
        print(f"[scout] {len(cands)} candidates in '{niche}' "
              f"({len(avoid)} blocked or already shortlisted)")

    survived = []
    for c in cands:
        try:
            r = T.assess(c, call, gather, verbose=verbose)
        except Exception as e:
            if verbose:
                print(f"  [ERROR ] {c}: {str(e)[:90]}")
            continue
        if r["build"]:
            survived.append(r)
        else:
            state["rejected"].append({
                "topic": c, "why": (r["reasons"] or ["rejected"])[0][:180],
                "agreement": r.get("agreement"),
                "at": time.strftime("%Y-%m-%d")})
    if verbose:
        print(f"[scout] {len(survived)}/{len(cands)} have a real answer")

    for r in survived[:MAX_DEMAND_CHECKS]:
        if not (probe and measure):
            r["score"] = None
            continue
        try:
            m = measure(probe(r["topic"]))
            ok, why = demand_verdict(m)
            r["demand"], r["score"] = m, demand_score(m)
            if verbose:
                print(f"  [{'WANTED' if ok else 'UNWANTED'}] {r['topic']} "
                      f"(score {r['score']})")
                for w in why:
                    print(f"        - {w}")
            if not ok:
                state["rejected"].append({
                    "topic": r["topic"], "why": why[0][:180],
                    "at": time.strftime("%Y-%m-%d")})
                r["score"] = -1
        except Exception as e:
            # A quota wall must never look like a measurement of zero.
            if verbose:
                print(f"  [demand ] {r['topic']}: {str(e)[:100]}")
            r["score"] = None

    added = 0
    for r in survived:
        if r.get("score") is not None and r["score"] < 0:
            continue                       # failed the demand gate outright
        state["backlog"].append({
            "topic": r["topic"], "members": r.get("members", []),
            "agreement": r.get("agreement"), "score": r.get("score"),
            "minutes": r.get("minutes"),
            "sources": r.get("sources"), "at": time.strftime("%Y-%m-%d")})
        added += 1

    # best first, and an unmeasured topic ranks below every measured one
    state["backlog"].sort(key=lambda b: -(b.get("score") if b.get("score")
                                          is not None else -1))
    save(state, memory)
    if verbose:
        print(f"[scout] backlog: +{added} -> {len(state['backlog'])} waiting")
    return state


def take(memory=MEMORY, verbose=True):
    """
    Pop the best topic off the backlog and mark it made. Costs nothing - the
    verification was paid for when it entered the backlog.
    """
    state = load(memory)
    if not state["backlog"]:
        return None, state
    best = state["backlog"].pop(0)
    state["made"].append({"topic": best["topic"],
                          "members": best.get("members", []),
                          "agreement": best.get("agreement"),
                          "score": best.get("score"),
                          "at": time.strftime("%Y-%m-%d")})
    save(state, memory)
    if verbose:
        print(f"[scout] CHOSE: {best['topic']}")
        print(f"        members  : {', '.join(best.get('members') or [])}")
        print(f"        agreement: {best.get('agreement')}  "
              f"demand score: {best.get('score')}")
        print(f"        {len(state['backlog'])} topics still waiting")
    return best, state


def next_topic(niche, call, gather, probe=None, measure=None,
               demand_verdict=None, demand_score=None, memory=MEMORY,
               verbose=True):
    """
    The entry point. Uses the backlog first and only generates when it is
    empty, so a normal run spends no research budget and no YouTube quota at
    all. Returns None when nothing survives - which is a real answer, not a
    failure, and the caller must not paper over it.
    """
    state = load(memory)
    if not state["backlog"]:
        state = refill(niche, call, gather, probe, measure, demand_verdict,
                       demand_score, memory=memory, verbose=verbose)
    elif verbose:
        print(f"[scout] {len(state['backlog'])} verified topics already "
              f"waiting - no generation needed")
    if not state["backlog"]:
        if verbose:
            print("[scout] nothing survived both gates. Generate different "
                  "candidates; do not lower the bar.")
        return None, state
    return take(memory, verbose)


def status(memory=MEMORY):
    """The 'suggested but not yet made' view, plus what is permanently blocked."""
    s = load(memory)
    print(f"\nBACKLOG - verified, waiting to be made ({len(s['backlog'])})")
    print("-" * 72)
    for b in s["backlog"]:
        print(f"  {b.get('score') if b.get('score') is not None else '  ?':>5}  "
              f"{b['topic'][:52]:<52} {len(b.get('members') or [])} members")
    print(f"\nMADE - blocked from ever being proposed again ({len(s['made'])})")
    print("-" * 72)
    for m in s["made"]:
        print(f"  {m.get('at','')}  {m['topic'][:60]}")
    print(f"\nREJECTED - also blocked ({len(s['rejected'])})")
    print("-" * 72)
    for r in s["rejected"][:20]:
        print(f"  {r['topic'][:44]:<44} {str(r.get('why',''))[:60]}")
    if len(s["rejected"]) > 20:
        print(f"  ... and {len(s['rejected']) - 20} more")
    return s


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        status()
    else:
        print(__doc__.strip())
