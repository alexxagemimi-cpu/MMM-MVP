#!/usr/bin/env python3
"""
scout.py — choose what to make next, keep the runners-up, never repeat.

THE PIPELINE THIS IMPLEMENTS
----------------------------
    1. GENERATE   20 candidates .................... 1 model call
    2. TRIAGE     judge all 20 on wording alone .... 1 model call
    3. DEMAND     youtube.py on the survivors ...... 0 model calls
    4. TRUTH      topics.py on the best few ........ ~4 model calls
    5. KEEP THE REST in a BACKLOG, so later runs cost nothing.
    6. BLOCK      anything made or rejected, forever.

CHEAP FILTERS FIRST - THIS ORDER IS THE WHOLE POINT
---------------------------------------------------
The first live run ran the expensive truth gate on all twenty candidates,
one model call each. Twenty-one calls exhausted Gemini's daily quota and
tripped Groq's per-minute token limit before a single video existed.

The two resources are not equally scarce:

    a model call     scarce, rate limited, daily cap is real and was hit
    a YouTube lookup 102 units of 10,000/day - about 98 available daily

So the scarce one is spent last, on a handful. Measured on twenty
candidates: 6 model calls where the old order used 21, and the expensive
check now runs only on topics already known to have an audience.

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


def triage(cands, call, verbose=True):
    """
    Judge ALL candidates in ONE call, on shape alone - no web research.

    Most bad candidates need no research to reject. "Best programming
    language" is an opinion; "the seven habits of highly effective people" is
    one author's framework, not an agreed taxonomy. Both can be thrown out by
    reading the phrasing, and the first live run spent a full web fetch and a
    model call proving each of them separately.

    Returns the survivors in the model's own order of confidence.
    """
    listed = "\n".join(f"{i+1}. {c}" for i, c in enumerate(cands))
    try:
        data = call(f"""Here are {len(cands)} candidate explainer topics.

{listed}

For each, judge ON THE WORDING ALONE whether it names a REAL, CLOSED,
AGREED set of members - a list independent sources would all give the same
way. You are not researching, only reading the phrasing.

REJECT: opinions ("best X", "top 10", "most important"), one author's
framework rather than an agreed classification, anything open-ended, and
anything where experts are known to classify differently.
KEEP: named, countable, checkable sets a reference work would agree on.

Return ONLY JSON, best candidates first:
{{"keep":[{{"n":<number>,"why":"<6 words>"}}],"drop":[{{"n":<number>,"why":"<6 words>"}}]}}""",
                    schema={"type": "object"})
    except Exception as e:
        # Triage is an optimisation, never a gate. If it cannot run, every
        # candidate goes forward to the real check rather than being lost.
        if verbose:
            print(f"[scout] triage unavailable ({str(e)[:70]}) - checking all")
        return list(cands), []

    keep_n = {k.get("n") for k in data.get("keep", []) if isinstance(k, dict)}
    drops = [(cands[d["n"] - 1], d.get("why", ""))
             for d in data.get("drop", [])
             if isinstance(d, dict) and isinstance(d.get("n"), int)
             and 1 <= d["n"] <= len(cands)]
    kept = [cands[k - 1] for k in keep_n
            if isinstance(k, int) and 1 <= k <= len(cands)]
    if verbose:
        print(f"[scout] triage (1 call): kept {len(kept)}, "
              f"dropped {len(drops)} on wording alone")
        for t, why in drops[:8]:
            print(f"        x {t[:56]:<56} {why[:40]}")
    return (kept or list(cands)), drops


def refill(niche, call, gather, probe=None, measure=None,
           demand_verdict=None, demand_score=None, n=CANDIDATES,
           memory=MEMORY, verbose=True, deep=4):
    """
    Generate and gate a fresh batch, adding every survivor to the backlog.

    THE ORDER HERE IS THE WHOLE POINT, and the first live run got it wrong.
    It ran the expensive truth gate on all twenty candidates - one model call
    each, twenty-one calls in total - which exhausted Gemini's daily quota and
    tripped Groq's per-minute token limit before a single video existed.

    Two resources, wildly different scarcity:
        a model call        scarce, rate limited, and the daily cap is real
        a YouTube lookup    102 units of a 10,000/day budget - about 98 a day

    So the cheap filters run first and the scarce one runs last, on a handful:

        1. brainstorm ............ 1 model call
        2. triage all 20 at once .. 1 model call, no research
        3. demand on survivors .... 0 model calls, ~102 quota units each
        4. truth on the best few .. `deep` model calls

    About 6 model calls where there were 21, and the expensive check now runs
    only on topics already known to have an audience.
    """
    state = load(memory)
    avoid = blocked(state) | {_key(b["topic"]) for b in state["backlog"]}
    cands = brainstorm(niche, call, n=n, avoid=avoid)
    if verbose:
        print(f"[scout] {len(cands)} candidates in '{niche}' "
              f"({len(avoid)} blocked or already shortlisted)")

    # ---- 2. shape triage: one call for the whole list --------------------
    cands, dropped = triage(cands, call, verbose=verbose)
    for t, why in dropped:
        state["rejected"].append({"topic": t, "why": f"wording: {why}"[:180],
                                  "at": time.strftime("%Y-%m-%d")})

    # ---- 3. demand, on everything left, before any research -------------
    if probe and measure:
        scored = []
        for c in cands:
            try:
                m = measure(probe(c))
                ok, why = demand_verdict(m)
                sc = demand_score(m)
                if verbose:
                    print(f"  [{'WANTED' if ok else 'UNWANTED'}] {c[:56]:<56} "
                          f"{sc}")
                    if not ok:
                        print(f"        - {why[0][:100]}")
                if ok:
                    scored.append((sc, c, m))
                else:
                    state["rejected"].append({
                        "topic": c, "why": why[0][:180],
                        "at": time.strftime("%Y-%m-%d")})
            except Exception as e:
                # A quota wall is not a measurement. Keep the topic in play
                # rather than silently ranking it last.
                if verbose:
                    print(f"  [demand ] {c}: {str(e)[:90]}")
                scored.append((None, c, None))
        scored.sort(key=lambda x: -(x[0] if x[0] is not None else -1))
        ordered = [(c, m) for _, c, m in scored][:deep]
        if verbose:
            print(f"[scout] {len(scored)} wanted; researching the top "
                  f"{len(ordered)} in depth")
    else:
        ordered = [(c, None) for c in cands[:deep]]
        if verbose:
            print("[scout] no YouTube key - cannot rank by demand, so the "
                  "deep check runs on an arbitrary few")

    # ---- 4. the expensive truth gate, on a handful ----------------------
    survived, unchecked = [], []
    for c, dem in ordered:
        try:
            r = T.assess(c, call, gather, verbose=verbose)
            r["demand"] = dem
            r["score"] = demand_score(dem) if (dem and demand_score) else None
        except Exception as e:
            if verbose:
                print(f"  [ERROR ] {c}: {str(e)[:90]}")
            continue
        if r["build"]:
            survived.append(r)
        elif r.get("checked"):
            state["rejected"].append({
                "topic": c, "why": (r["reasons"] or ["rejected"])[0][:180],
                "agreement": r.get("agreement"),
                "at": time.strftime("%Y-%m-%d")})
        else:
            # The gate could not run - a model outage, a rate limit, a dead
            # search. Blocking the topic here would bury a possibly good idea
            # forever because of a transient failure, so it is simply skipped
            # and remains available to a later run.
            unchecked.append(c)
    if verbose:
        print(f"[scout] {len(survived)}/{len(cands)} have a real answer"
              + (f"  ({len(unchecked)} COULD NOT BE CHECKED and are left "
                 f"unblocked)" if unchecked else ""))
        for u in unchecked:
            print(f"        ? {u}")

    added = 0
    for r in survived:
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
