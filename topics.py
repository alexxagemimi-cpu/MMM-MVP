#!/usr/bin/env python3
"""
topics.py — decide WHAT to make, before making anything.

THE GAP THIS FILLS
------------------
Every other file in this project is downstream of a decision nobody makes.
brain.py takes whatever topic arrives in an environment variable and grinds;
engine.py renders whatever brain.py produced. Nothing anywhere asks whether
the idea was worth building. A perfect factory pointed at a worthless topic
produces a beautiful worthless video, and we would not find out until a human
watched it.

The owner put it better: the same materials make a trash can or a drone, and
which one you get is decided before any of the machinery runs.

THE INSIGHT THAT MAKES THIS TESTABLE
------------------------------------
The worst content bug this project has produced was "runway" appearing as an
item in a list of types of business expense. Runway is not an expense - it is
months of cash remaining at the current burn rate. Everyone treated that as a
research failure. It was not. It was a TOPIC failure.

"Types of business expenses" has no single agreed answer. Accountants
classify expenses differently depending on the framework - by behaviour
(fixed/variable), by function (COGS/operating), by tax treatment (capital/
revenue). Asked for one clean list, a model must invent one, and something
like runway falls in. No amount of better prompting or more sources fixes
that, because the flaw is in the question.

Now look at what demonstrably works:

    Every Operating System Explained   Windows, macOS, Linux, ChromeOS,
                                       Android, iOS, UNIX, BSD
    Every Blood Type Explained         exactly eight, by definition
    Every Type of Phobia Explained     named, documented, checkable

Those topics have a REAL, CLOSED, VERIFIABLE SET. That is not a coincidence -
it is the precondition that lets the video be accurate and satisfying at the
same time.

So the gate is: do independent sources, asked the same question, come back
with the SAME list? That is measurable, it is cheap, and it is deterministic
once the extraction is done. High agreement means a real taxonomy. Low
agreement means the category is fuzzy and any confident list we publish will
be partly invented.

    python3 topics.py "types of operating systems"
"""

import os
import re
import json
from statistics import mean


# TWO SEPARATE JUDGEMENTS, and conflating them was a bug.
#
# "Is this topic REAL?" is an accuracy question - if sources disagree, any
# confident list we publish is partly invented, and that is fatal.
# "Is it BIG enough?" is only a question about how long the video should be.
#
# The first version refused coffee roasts, which scored 0.84 agreement - a
# plainly real taxonomy - purely for having three agreed members where four
# were demanded. A true three-item taxonomy should produce a SHORT video, not
# a refusal. Only below three is there genuinely nothing to explain.
MIN_MEMBERS, MAX_MEMBERS = 3, 12

# Fraction of sources that must name an item for it to count as consensus.
# Two thirds is deliberately strict: the whole point is to refuse topics
# where sources disagree, and a simple majority still lets a contested list
# through as though it were settled.
CONSENSUS = 0.60

# A topic is only worth building if enough of its members are agreed on AND
# the sources broadly agree on how many there are.
MIN_AGREEMENT = 0.55


_GENERIC = {"the", "a", "an", "of", "and", "or", "type", "types", "kind",
            "kinds", "category", "categories", "class", "classes", "form",
            "forms", "variety", "varieties", "group", "groups"}


def _norm(s, topic=""):
    """
    Compare members by their meaningful words.

    Two things this has to survive, both found by testing a case in the
    MIDDLE rather than only the obvious pass and the obvious fail:

    1. NOTATION. Sources write "A+" where others write "A positive". Compared
       literally those are different members, and agreement on the most
       perfectly closed set that exists - the eight blood types - collapsed
       to a REJECT. A trailing sign after a short code is spelled out.
       Deliberately narrow: it must not turn "medium-dark" into
       "medium negative dark".

    2. THE CATEGORY NOUN. Sources write "light roast" where others write
       "light", "fixed costs" where others write "fixed". Which word is
       filler depends entirely on the topic, so the filler list is DERIVED
       FROM THE TOPIC rather than guessed in advance - for "types of coffee
       roast", "roast" carries no distinguishing information at all.
    """
    s = (s or "").lower().strip()
    s = re.sub(r"^([a-z]{1,3})\s*\+$", r"\1 positive", s)
    s = re.sub(r"^([a-z]{1,3})\s*[-−]$", r"\1 negative", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)

    drop = set(_GENERIC)
    for w in re.sub(r"[^a-z0-9 ]", " ", (topic or "").lower()).split():
        if len(w) > 2:
            drop.add(w)
            if w.endswith("s"):
                drop.add(w[:-1])
            else:
                drop.add(w + "s")

    words = [w for w in s.split() if w not in drop]
    # never normalise a member down to nothing - if every word was filler,
    # keep the original, or two unrelated items would silently become one
    return " ".join(words) if words else " ".join(s.split())


def extract_prompt(topic, context):
    """
    Ask ONE model call to read every source and report, per source, which
    members that source actually names.

    Per-source rather than pooled, deliberately. Pooling lets the model merge
    everything it read into one tidy list and hand back a consensus that was
    never there - which is exactly the failure being tested for. Keeping the
    lists separate means agreement is computed in Python from what each
    source independently said.
    """
    return f"""Below is text from several web pages about: "{topic}"

=== SOURCES ===
{context}
=== END SOURCES ===

For EACH source separately, list the members it names for this category.

Rules:
- Report ONLY what that specific source actually names. Do not merge sources.
- Do not add members you know of that the source does not mention.
- If a source names none, give it an empty list.
- Use the source's own short name for each member, 1-4 words.

Return ONLY JSON, no prose:
{{"sources": [{{"n": 1, "members": ["...", "..."]}}, {{"n": 2, "members": []}}]}}"""


def score(per_source, topic=""):
    """
    Turn per-source member lists into a verdict. Pure arithmetic - no model
    opinion, because a model asked "is this a good topic?" says yes.

    Returns agreement (0-1), the consensus members, and the contested ones.
    """
    lists = [[_norm(m, topic) for m in s.get("members", []) if _norm(m, topic)]
             for s in per_source]
    lists = [l for l in lists if l]
    if len(lists) < 2:
        return 0.0, [], [], {}

    # display name for each normalised key, from the first source that used it
    display = {}
    for s in per_source:
        for m in s.get("members", []):
            k = _norm(m, topic)
            if k and k not in display:
                display[k] = m.strip()

    counts = {}
    for l in lists:
        for k in set(l):
            counts[k] = counts.get(k, 0) + 1

    n = len(lists)
    consensus = [k for k, c in counts.items() if c / n >= CONSENSUS]
    contested = [k for k, c in counts.items() if c / n < CONSENSUS]

    # How much of a typical source's list is made of agreed members? A real
    # taxonomy scores high because sources mostly name the same things; a
    # fuzzy category scores low because each source has its own idea.
    covered = mean([
        sum(1 for k in set(l) if k in consensus) / max(len(set(l)), 1)
        for l in lists
    ])
    # And do sources agree on HOW MANY there are? Wildly different lengths
    # mean there is no settled answer even where names overlap.
    sizes = [len(set(l)) for l in lists]
    spread = 1.0 - min(1.0, (max(sizes) - min(sizes)) / max(max(sizes), 1))

    agreement = round(0.72 * covered + 0.28 * spread, 3)
    return (agreement,
            [display.get(k, k) for k in sorted(consensus,
                                               key=lambda k: -counts[k])],
            [display.get(k, k) for k in contested],
            {"sources": n, "covered": round(covered, 3),
             "size_spread": round(spread, 3), "sizes": sizes})


def verdict(topic, agreement, consensus, contested, detail):
    """Build or refuse, and say why in words a human can act on."""
    n = len(consensus)
    reasons, ok = [], True

    if detail.get("sources", 0) < 3:
        return False, ["too few usable sources to judge this topic at all"], {}

    if agreement < MIN_AGREEMENT:
        ok = False
        reasons.append(
            f"sources do not agree on the answer (agreement {agreement}, "
            f"need {MIN_AGREEMENT}). {len(contested)} of "
            f"{n + len(contested)} candidate items appear in a minority of "
            f"sources. A confident list here would be partly invented - this "
            f"is the failure that put 'runway' in a list of expense types.")
    if n < MIN_MEMBERS:
        ok = False
        reasons.append(f"only {n} agreed members - there is nothing here to "
                       f"explain one by one")

    members = consensus[:MAX_MEMBERS]
    if n > MAX_MEMBERS:
        reasons.append(f"{n} agreed members, trimmed to the {MAX_MEMBERS} "
                       f"most widely cited so each still gets real time")

    if ok:
        # Members drive length, not the other way round. Asking for twelve
        # minutes from a four-item topic is what forces padding, and padding
        # a taxonomy means inventing members.
        mins = max(3, min(14, round(len(members) * 1.1 + 1)))
        reasons.append(f"{n} members agreed across {detail['sources']} "
                       f"independent sources at {agreement} agreement")
        reasons.append(f"suggested length ~{mins} min, set by the material "
                       f"rather than by a target")
        return True, reasons, {"members": members, "minutes": mins}
    return False, reasons, {"members": members}


def assess(topic, call, gather, per_query=5, verbose=True):
    """
    Full check for one topic. `call` and `gather` are injected (brain.call and
    research.gather) so this module stays testable without a network or a key.
    """
    queries = [topic,
               f"{topic} list",
               f"what are the main {topic}",
               f"{topic} explained categories"]
    context, sources = gather(queries, per_query=per_query, read_pages=True,
                              max_sources=8)
    if not sources:
        return {"topic": topic, "build": False,
                "reasons": ["no sources came back"], "agreement": 0.0,
                "members": [], "sources": 0}

    labelled = context
    try:
        data = call(extract_prompt(topic, labelled), schema={"type": "object"})
        per_source = data.get("sources", []) if isinstance(data, dict) else []
    except Exception as e:
        return {"topic": topic, "build": False,
                "reasons": [f"member extraction failed: {str(e)[:90]}"],
                "agreement": 0.0, "members": [], "sources": len(sources)}

    agreement, consensus, contested, detail = score(per_source, topic)
    build, reasons, extra = verdict(topic, agreement, consensus, contested,
                                    detail)
    out = {"topic": topic, "build": build, "agreement": agreement,
           "members": extra.get("members", consensus[:MAX_MEMBERS]),
           "contested": contested[:8], "sources": len(sources),
           "reasons": reasons, "detail": detail}
    if verbose:
        mark = "BUILD" if build else "REJECT"
        print(f"  [{mark}] {topic}  (agreement {agreement}, "
              f"{len(out['members'])} members, {len(sources)} sources)")
        for r in reasons:
            print(f"        - {r}")
    return out


if __name__ == "__main__":
    # Offline demonstration of the arithmetic, using hand-written per-source
    # lists of the exact shape the extraction returns. This is the load-
    # bearing claim - that the score separates a real taxonomy from a fuzzy
    # one - so it is checked without needing a key or a network.
    REAL = [                      # what sources on operating systems look like
        {"n": 1, "members": ["Windows", "macOS", "Linux", "Android", "iOS"]},
        {"n": 2, "members": ["Windows", "macOS", "Linux", "Android", "iOS", "ChromeOS"]},
        {"n": 3, "members": ["Windows", "Linux", "macOS", "Android", "iOS"]},
        {"n": 4, "members": ["Windows", "macOS", "Linux", "Android", "iOS", "UNIX"]},
    ]
    FUZZY = [                     # what "types of business expenses" looks like
        {"n": 1, "members": ["fixed costs", "variable costs", "operating expenses"]},
        {"n": 2, "members": ["capital expenditure", "operating expenditure"]},
        {"n": 3, "members": ["direct costs", "indirect costs", "overheads", "COGS"]},
        {"n": 4, "members": ["fixed", "variable", "semi-variable", "runway", "burn rate"]},
    ]
    for name, data in (("real taxonomy (operating systems)", REAL),
                       ("fuzzy category (business expenses)", FUZZY)):
        a, cons, cont, det = score(data, name)
        ok, why, _ = verdict(name, a, cons, cont, det)
        print(f"\n{name}")
        print(f"  agreement : {a}   {det}")
        print(f"  consensus : {cons}")
        print(f"  contested : {cont}")
        print(f"  VERDICT   : {'BUILD' if ok else 'REJECT'}")
        for r in why:
            print(f"     - {r}")
