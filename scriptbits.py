#!/usr/bin/env python3
"""
scriptbits.py — pull the listable content back out of narration.

WHY
---
The money/business niche has nothing to photograph. CLAUDE.md says so in its
first section: stock footage of an office carries no information, so the words
on screen are the content. A real run proved it - Pixabay answered "materials,
packaging, payment processing, delivery" with a crop sprayer in a field, and
"fixed costs, variable costs and one-off costs" with a golden retriever
standing on a patio.

The answer is not a better search. It is to stop asking a stock library for a
picture of an abstraction and draw the thing being said instead. Explainer
narration is FULL of drawable content, and it is sitting in plain sight:

    "Rent, salaries, insurance, software."
    "Materials, packaging, payment processing, delivery."
    "A replaced laptop, a legal fee, a deposit, a repair."

Those are the four bullets that belong on screen at that moment. They are
already written, already fact-checked, already spoken aloud - so a card built
from them cannot be off-topic, cannot be wrong, and needs no quota.

WHAT IT DOES NOT DO
-------------------
This finds lists that the writer already wrote. It does not invent them, does
not summarise, and returns nothing at all when the sentence is prose - in
which case the caller falls back to the term and its definition. A wrong
bullet is worse than no bullet, so the rule is strict and silent failure is
the intended behaviour.

    python3 scriptbits.py      # runs against known-good and known-bad samples
"""

import re

MIN_ITEMS = 3          # two commas is a pair, not a list
MAX_ITEMS = 6
MAX_WORDS_PER_ITEM = 3
LEAD = re.compile(r"^(a|an|the|your|our|their|its|his|her|and|or)\s+", re.I)


def _clean(seg):
    seg = seg.strip().strip(".;:!?").strip()
    seg = LEAD.sub("", seg)
    return seg.strip()


def list_items(text, min_items=MIN_ITEMS, max_items=MAX_ITEMS):
    """
    The longest run of short comma-separated phrases in `text`.

    Returns [] when there isn't one. Sentence by sentence, because a list
    lives inside a single sentence - spanning the full stop would happily
    join the tail of one sentence to the head of the next and produce
    something the writer never wrote.
    """
    best = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        if "," not in sentence:
            continue
        # "a, b, and c" and "a, b and c" both split cleanly once the
        # conjunction before the final item is turned into a comma
        s = re.sub(r",?\s+(and|or)\s+", ", ", sentence, flags=re.I)
        segs = [_clean(p) for p in s.split(",")]
        run = []
        for seg in segs:
            words = seg.split()
            if seg and 1 <= len(words) <= MAX_WORDS_PER_ITEM \
                    and not seg.lower().startswith(("which", "that", "so ",
                                                    "because", "but ")):
                run.append(seg)
            else:
                if len(run) > len(best):
                    best = run
                run = []
        if len(run) > len(best):
            best = run
    if len(best) < min_items:
        return []
    return [b[0].upper() + b[1:] for b in best[:max_items]]


# ---------------------------------------------------------------------------
# validation - known-good AND known-bad, per the working agreement
# ---------------------------------------------------------------------------
GOOD = [
    ("Fixed costs are the ones that quietly kill companies. Rent, salaries, "
     "insurance, software. They do not move when your sales move.",
     ["Rent", "Salaries", "Insurance", "Software"]),
    ("The second group is variable costs. Materials, packaging, payment "
     "processing, delivery. They rise and fall with every sale you make.",
     ["Materials", "Packaging", "Payment processing", "Delivery"]),
    ("Then there are one-off costs, the ones almost nobody plans for. A "
     "replaced laptop, a legal fee, a deposit, a repair. They are rare.",
     ["Replaced laptop", "Legal fee", "Deposit", "Repair"]),
    ("Every expense falls into one of three kinds. Fixed costs, variable "
     "costs, and one-off costs. Get these three straight.",
     ["Fixed costs", "Variable costs", "One-off costs"]),
]

# Prose with commas in it. Every one of these must return NOTHING - a card
# built from a subordinate clause is worse than no card.
BAD = [
    "They do not move when your sales move, which means a slow month costs "
    "exactly the same as a good one.",
    "Add all three together against the cash in your account and you get "
    "runway: months of cash left at your current burn.",
    "Runway is the number that decides whether the business is still here "
    "next year, and it is counted in months.",
    "Not revenue, not profit.",
    "In nineteen eighty-one, when the company was still small, the founders "
    "made a decision that would take twenty years to matter.",
]

if __name__ == "__main__":
    ok = True
    print("KNOWN-GOOD (a list is really there)\n")
    for text, want in GOOD:
        got = list_items(text)
        good = got == want
        ok &= good
        print(f"  {'ok ' if good else 'FAIL'}  {got}")
        if not good:
            print(f"        wanted {want}")

    print("\nKNOWN-BAD (prose with commas - must find nothing)\n")
    for text in BAD:
        got = list_items(text)
        good = not got
        ok &= good
        print(f"  {'ok ' if good else 'FAIL'}  {got or '(nothing)'}   "
              f"<- {text[:52]}...")

    print(f"\n{'ALL PASS' if ok else 'FAILURES ABOVE'}")
    raise SystemExit(0 if ok else 1)
