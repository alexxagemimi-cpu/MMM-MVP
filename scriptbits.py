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

# ---------------------------------------------------------------------------
# the one number worth putting on screen
# ---------------------------------------------------------------------------
# A figure spoken aloud is gone in a second. On screen, alone and large, it
# is the most "edited" thing a money video can show - and graphics.stat_card
# has been sitting in this repo unused the whole time.
#
# DIGITS ONLY, and deliberately. "three kinds" is not a statistic, it is a
# sentence, and a card reading "3" over narration about categories would be
# noise. A year is not a statistic either - "in 2019 the company changed
# hands" is context - so bare four-digit years are excluded unless money is
# attached to them.
_MONEY = r"[$£€₹]\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|m|bn|billion|million|crore|lakh))?"
_PCT   = r"\d[\d,]*(?:\.\d+)?\s?(?:%|per ?cent)"
_MULT  = r"\d[\d,]*(?:\.\d+)?\s?(?:x|times)\b"
_SPAN  = r"\d[\d,]*\s?(?:months?|years?|weeks?|days?|hours?)\b"
_BIG   = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?"

_NUMBER = re.compile(f"({_MONEY}|{_PCT}|{_MULT}|{_SPAN}|{_BIG})", re.I)
_YEAR = re.compile(r"^(19|20)\d\d$")
_DANGLE = {"a", "an", "the", "of", "in", "on", "at", "to", "and", "or",
           "for", "from", "with", "by", "you", "your", "have", "has",
           "is", "are", "was", "were", "that", "this", "it", "its"}


def headline_number(text, max_label_words=7):
    """
    The most striking figure in `text`, with a short label, or None.

    Returns (value, label). The label is the words around the number in its
    own sentence, trimmed - so the card says what the number IS rather than
    leaving a bare figure on screen with no referent.

    Ordered by how much a viewer cares: money, then a percentage, then a
    multiple, then a span of time. Returns None far more often than not, on
    purpose - the same rule the list extractor follows, because a card built
    from a number that was not really a statistic is worse than no card.
    """
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        for m in _NUMBER.finditer(sentence):
            value = m.group(1).strip()
            if _YEAR.match(value.replace(",", "")):
                continue
            # The label is what comes AFTER the number, because that is what
            # describes it: "60% OF REVENUE", "18 MONTHS OF RUNWAY". Taking
            # words from both sides spliced the sentence back together with
            # its own number cut out of the middle and produced captions like
            # "insurance can eat of revenue before you".
            after = re.split(r"[,;:]", sentence[m.end():])[0].split()
            label = " ".join(after[:max_label_words])
            if not label:
                label = " ".join(sentence[:m.start()].split()[-max_label_words:])
            label = re.sub(r"[^A-Za-z0-9 %'-]", " ", label)
            words = label.split()
            # Don't end a caption on a dangling word. Cutting at a fixed
            # length left "of revenue before you have sold a" hanging on its
            # article, which reads like the text was clipped - because it was.
            while words and words[-1].lower() in _DANGLE:
                words.pop()
            label = " ".join(words).strip()
            if not label:
                continue
            return value.replace(" per cent", "%").replace(" percent", "%"), label
    return None


NUM_GOOD = [
    ("Rent, salaries and insurance can eat 60% of revenue before you have "
     "sold a single thing.", "60%"),
    ("Most companies that fail do it with 18 months of runway still on the "
     "books.", "18 months"),
    ("That is $1,200 a month you cannot avoid paying.", "$1,200"),
    ("A bad month can cost you 3x what a good one does.", "3x"),
]

NUM_BAD = [
    "Every cost a business has falls into one of three kinds.",
    "Fixed costs, variable costs, and one-off costs.",
    "In 2019 the company changed hands and nothing else changed.",
    "They do not move when your sales move.",
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

    print("\nHEADLINE NUMBER (a real statistic is there)\n")
    for text, want in NUM_GOOD:
        got = headline_number(text)
        val = got[0] if got else None
        good = val == want
        ok &= good
        lab = got[1] if got else ""
        print(f"  {'ok ' if good else 'FAIL'}  {str(val):<12} label: {lab}")
        if not good:
            print(f"        wanted {want!r}")

    print("\nHEADLINE NUMBER (no statistic - must find nothing)\n")
    for text in NUM_BAD:
        got = headline_number(text)
        good = got is None
        ok &= good
        print(f"  {'ok ' if good else 'FAIL'}  {got or '(nothing)'}   "
              f"<- {text[:48]}...")

    print(f"\n{'ALL PASS' if ok else 'FAILURES ABOVE'}")
    raise SystemExit(0 if ok else 1)
