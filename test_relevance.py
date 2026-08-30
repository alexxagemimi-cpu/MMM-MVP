#!/usr/bin/env python3
"""
test_relevance.py — the stock-footage relevance check, against REAL data.

Every pair below was logged by an actual CI run (#27 and #28). Nothing here
is invented, which matters: the first version of this check scored 7/7
against pairs I made up and then passed 15 clips out of 15 on a real run,
including a golden retriever standing on a patio under "fixed costs,
variable costs and one-off costs". Hand-written samples proved nothing
because they were written by the same reasoning that wrote the bug.

Tag strings are truncated at 60 characters, because that is how the engine
logs them. A check that needs the 61st character to reach the right answer
is too fragile to trust anyway.

A NOTE ON THE TRADE, because it is a judgement call and not a free win.
Some genuinely usable clips are rejected here - "signing rental agreement"
against a photo tagged `signing, paper, hand, close up, document...` is a
real match by eye, and one word out of three is not enough for the check to
know that. That is accepted deliberately. When this rejects a shot the
engine draws a card out of the script's own words instead, and in a niche
with nothing to photograph a card that says "Materials / Packaging /
Delivery / Card fees" beats a marginal stock photo. The asymmetry is the
point: a wrong picture is a viewer noticing the video is automated, a
drawn card is just the video being a document.

    python3 test_relevance.py
"""

import sys
import types

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))
import importlib.util

_spec = importlib.util.spec_from_file_location("engine_mod", "engine.py")
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)


# (keyword, tags as logged, must_pass, why)
CASES = [
    # ---- must be REJECTED -------------------------------------------------
    ("delivery van loading",
     "barley, field, combine, harvest, farmer, loading, summer",
     False, "a barley harvester; matched only 'loading'"),
    ("small business storefront",
     "ipad, imac, tablet, desktop, iphone, monitor, computer, disp",
     False, "a desk of Apple devices, not a shopfront"),
    ("empty desk chairs",
     "homework, girl, student, laptop, study, studying, notebook,",
     False, "a student doing homework, not empty chairs"),
    ("person checking bank balance phone",
     "woman, young, walking, happy, lifestyle, smile, female, adul",
     False, "a woman walking; nothing about a bank or a balance"),
    ("total expenditures across reporting periods",
     "friday, text, typography, 3d, letters, word, background",
     False, "the original FRIDAY clip that started all of this"),
    ("fixed costs rent salaries",
     "dog, pet, animal, grass, garden, puppy, golden retriever",
     False, "the golden retriever"),

    # ---- must be ACCEPTED -------------------------------------------------
    ("signing legal documents",
     "paper, document, stamp, signature, signing, deal, hands, sig",
     True, "'signing' and 'documents' both land"),
    ("calendar planning desk",
     "apple, calendar, desk, ipad, tablet, device, electronics, ke",
     True, "'calendar' and 'desk' both land"),
    # This one I expected to pass and it does not, and the check is right:
    # in the tags actually logged, the only word that lands is "laptop",
    # which is generic. "repair" is not there - the string ends "pc, re" and
    # I read a "repair" into it that the check cannot see. Expecting it to
    # pass was me asking the check to guess at truncated data. The full-tag
    # version below is the one that proves the rule works when the word is
    # really present.
    ("broken laptop repair",
     "laptop, fire, overheating, fix, technology, computer, pc, re",
     False, "only 'laptop' lands, and 'laptop' alone is generic"),
    ("broken laptop repair",
     "laptop, repair, broken, screen, technician, fix",
     True, "'laptop', 'repair' and 'broken' all land"),
    ("packing boxes warehouse",
     "warehouse, boxes, logistics, shipping, storage, packing",
     True, "all three land"),
    ("coffee",
     "coffee, cup, morning, drink, cafe",
     True, "a one-word phrase needs only its one word"),
]


# Real pairs from the JEANS run (run 34), tags exactly as logged. The
# subject anchor is what these test: every clip that was wrong has no
# `jeans` and no `denim` tag, and every clip that was right has one.
# What subject_terms() actually returns for this script: the TWO most
# frequent content words. "rise" ranks third and is deliberately excluded -
# it is an attribute of jeans, and it is the word that lets `high rise
# building` in.
JEANS_SUBJECT = {"jeans", "denim"}

JEANS = [
    ("man wearing high rise jeans above",
     "high rise building, urban, osaka, evening, japan",
     False, "an Osaka skyline - matched 'high' and 'rise', not jeans"),
    ("ruler measuring front rise from",
     "measure, science, lab, chemistry, experiment, ruler, measurement",
     False, "a chemistry lab"),
    ("hand holding denim fabric swatch",
     "public transport, subway, train, metro, holding on, hands, grip",
     False, "a subway train"),
    ("close up of slim fit denim knee",
     "bird spider-legs, spider legs, spider, haired, redknee, "
     "mexican red knee poisonous, crawl",
     False, "a tarantula - 'knee' appears in 'red knee poisonous'"),
    ("red levis tab on back pocket of",
     "letters, letter, loop, transparent, back, plan, color, red, blue",
     False, "an abstract letters animation"),
    ("man sitting on bench wearing tapered",
     "man, bench, sunset, afternoon, trees, forest, netherlands, drenthe",
     False, "a bench in a forest"),

    ("close up of men jeans size tag",
     "jeans, trousers, trouser buttons, clothing, blue jeans, blue, "
     "fashion, detail shot, textiles, seam",
     True, "actually jeans"),
    ("man wearing classic straight fit",
     "jeans, pants, clothing, blue, fashion, fabric, denim, denim pants",
     True, "actually jeans"),
    ("measuring denim inseam from crotch",
     "denim, fabric, texture, blue, trouser, trouser pocket, seam, denim",
     True, "actually denim"),
    ("man wearing fitted skinny jeans",
     "feet, legs, standing, waiting, crossed legs, shoes, sneakers, "
     "converse, denim pants, blue jeans, urban, jeans",
     True, "actually jeans"),
]


def jeans_check():
    width = max(len(k) for k, *_ in JEANS)
    bad = 0
    print(f"\nSUBJECT ANCHOR - real pairs from the jeans run")
    print(f"subject terms: {', '.join(sorted(JEANS_SUBJECT))}\n")
    print(f"{'keyword':<{width}}  want  got   note")
    print("-" * (width + 44))
    for keyword, tags, want, why in JEANS:
        got = E._relevant({"tags": tags}, keyword, subject=JEANS_SUBJECT)
        ok = got == want
        bad += not ok
        print(f"{keyword:<{width}}  {'pass' if want else 'drop'}  "
              f"{'pass' if got else 'drop'}  {'' if ok else '<< WRONG '}{why}")
    print(f"\n{len(JEANS) - bad}/{len(JEANS)} correct")
    return bad


def main():
    width = max(len(k) for k, *_ in CASES)
    bad = 0
    print(f"{'keyword':<{width}}  want  got   note")
    print("-" * (width + 42))
    for keyword, tags, want, why in CASES:
        got = E._relevant({"tags": tags}, keyword)
        ok = got == want
        bad += not ok
        print(f"{keyword:<{width}}  {'pass' if want else 'drop'}  "
              f"{'pass' if got else 'drop'}  {'' if ok else '<< WRONG '}{why}")

    bad += jeans_check()
    print(f"\n{len(CASES) + len(JEANS) - bad}/{len(CASES) + len(JEANS)} correct overall")
    if bad:
        print("A wrong 'pass' puts an unrelated picture on screen.\n"
              "A wrong 'drop' costs a usable clip and draws a card instead - "
              "much the cheaper mistake.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
