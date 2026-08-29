#!/usr/bin/env python3
"""
redteam.py — attack the script until it stops bleeding.

WHY A SEPARATE STAGE FROM FACT-CHECKING
---------------------------------------
brain.py already verifies claims against fresh web sources. That catches a
wrong date. It does not catch:

  - an item that is not actually a member of the category (the "runway"
    bug: every individual sentence about runway was TRUE, it simply is not
    a type of business expense, so a fact-checker waves it through)
  - sentences that state nothing and could be deleted with no loss
  - language a fifteen-year-old would have to re-read
  - hedging that turns a claim into noise

Those are the flaws that make a video feel like filler, and none of them is
a factual error.

THE MEMBERSHIP CHECK IS THE IMPORTANT ONE, AND IT IS NEW
--------------------------------------------------------
Until scout.py existed, nothing in this project knew what the RIGHT answer
was, so nothing could tell that runway did not belong. Now topics.py
establishes the verified member list from cross-source agreement BEFORE a
script exists, and this stage holds the script to it. An item in the script
that is not in the verified list is a hard failure, not a warning.

MEASURED FIRST, MODEL SECOND
----------------------------
Everything that can be counted is counted in Python, because a model asked
"is this good?" says yes to its own output. The model is used only for the
adversarial pass, and there it is asked to HUNT rather than to judge - the
difference matters enormously in what comes back.

    python3 redteam.py       # runs the deterministic checks on samples
"""

import re
import json

FLUFF = [
    "it's important to note", "it is important to note", "let's dive in",
    "buckle up", "delve", "testament to", "game-changer", "game changer",
    "in the world of", "when it comes to", "at the end of the day",
    "needless to say", "the fact of the matter", "tapestry", "unlock the",
    "navigate the", "revolutionize", "profound impact", "ever-evolving",
    "paradigm shift", "in today's world", "have you ever wondered",
    "little did they know", "the harsh reality", "plays a crucial role",
    "when you think about it", "more than just", "the truth is",
]
HEDGES = ["might", "maybe", "perhaps", "possibly", "arguably", "some say",
          "it seems", "somewhat", "generally speaking", "many believe",
          "it could be argued", "in a sense", "sort of", "kind of"]

# Words that make narration harder to follow than it needs to be. The brief
# is explicitly "simple, not fluff", and these all have shorter equivalents.
JARGON = {
    "utilize": "use", "leverage": "use", "facilitate": "help",
    "methodology": "method", "subsequently": "then", "aforementioned": "that",
    "commence": "start", "terminate": "end", "endeavour": "try",
    "endeavor": "try", "ascertain": "find out", "requisite": "needed",
    "myriad": "many", "plethora": "plenty", "utilise": "use",
    "optimal": "best", "paradigm": "model", "holistic": "whole",
    "synergy": "working together", "robust": "strong", "granular": "detailed",
}


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def _syllables(word):
    """Approximate. Vowel groups, minus a silent trailing e, floor of one."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups)
    if w.endswith("e") and n > 1 and not w.endswith(("le", "ee", "ye")):
        n -= 1
    return max(n, 1)


def reading_grade(text):
    """
    Flesch-Kincaid US grade level. Roughly: the school year at which a reader
    handles this comfortably. Explainers aimed at everyone want 6-9; above
    about 10 the viewer is decoding instead of listening.
    """
    sents = sentences(text)
    words = re.findall(r"[A-Za-z']+", text or "")
    if not sents or not words:
        return 0.0
    syl = sum(_syllables(w) for w in words)
    return round(0.39 * (len(words) / len(sents))
                 + 11.8 * (syl / len(words)) - 15.59, 1)


def _norm_member(s):
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return " ".join(s.split())


def check(script, verified_members=None, max_grade=9.5):
    """
    Deterministic pass. Returns a list of findings, each with a severity.
    'hard' findings must be fixed; 'soft' ones should be.
    """
    scenes = script.get("scenes", []) if isinstance(script, dict) else []
    text = " ".join(s.get("narration", "") for s in scenes)
    low = text.lower()
    out = []

    # --- membership: the check that would have caught "runway" ------------
    if verified_members:
        allowed = {_norm_member(m) for m in verified_members}
        for i, s in enumerate(scenes, 1):
            term = (s.get("key_term") or "").strip()
            if not term:
                continue
            k = _norm_member(term)
            if not k:
                continue
            hit = any(k == a or k in a or a in k for a in allowed)
            if not hit:
                out.append({
                    "severity": "hard", "scene": i, "kind": "not-a-member",
                    "detail": f"scene {i} explains {term!r}, which is NOT in "
                              f"the verified list for this topic "
                              f"({', '.join(sorted(verified_members))[:120]}). "
                              f"Every sentence about it may be true and it "
                              f"still does not belong - this is exactly the "
                              f"'runway is not an expense type' failure.",
                })
        covered = set()
        for s in scenes:
            k = _norm_member(s.get("key_term") or "")
            for a in allowed:
                if k and (k == a or k in a or a in k):
                    covered.add(a)
        missing = allowed - covered
        if missing and len(missing) > len(allowed) * 0.34:
            out.append({
                "severity": "soft", "scene": None, "kind": "incomplete",
                "detail": f"{len(missing)} of {len(allowed)} verified members "
                          f"are never explained: "
                          f"{', '.join(sorted(missing))[:140]}",
            })

    # --- language -----------------------------------------------------------
    grade = reading_grade(text)
    if grade > max_grade:
        out.append({
            "severity": "hard", "scene": None, "kind": "too-complex",
            "detail": f"reading grade {grade} (target <= {max_grade}). The "
                      f"narration is harder to follow than it needs to be - "
                      f"shorter sentences and plainer words.",
        })

    for j, plain in JARGON.items():
        if re.search(rf"\b{j}\b", low):
            out.append({"severity": "soft", "scene": None, "kind": "jargon",
                        "detail": f"'{j}' -> say '{plain}'"})

    for f in FLUFF:
        if f in low:
            out.append({"severity": "hard", "scene": None, "kind": "fluff",
                        "detail": f"banned filler phrase: '{f}'"})

    n_words = max(len(text.split()), 1)
    hedge_rate = sum(low.count(h) for h in HEDGES) * 100 / n_words
    if hedge_rate > 1.2:
        out.append({"severity": "soft", "scene": None, "kind": "hedging",
                    "detail": f"hedging {hedge_rate:.2f} per 100 words - "
                              f"state it plainly or cut it"})

    # --- repetition: the same sentence opening again and again --------------
    openings = {}
    for s in sentences(text):
        k = " ".join(s.split()[:3]).lower()
        openings[k] = openings.get(k, 0) + 1
    for k, c in openings.items():
        if c >= 3 and len(k) > 6:
            out.append({"severity": "soft", "scene": None, "kind": "repetition",
                        "detail": f"{c} sentences start with '{k}...'"})
    return out


ATTACK_PROMPT = """You are red-teaming a documentary script before it is
published. You are NOT reviewing it and you are NOT looking for things to
praise. Your job is to find everything wrong with it.

You will be judged on how many REAL flaws you find. Finding nothing is a
failure. Inventing a flaw that is not there is also a failure.

{members_block}
=== SOURCE MATERIAL (fetched from the web) ===
{context}
=== END SOURCES ===

=== SCRIPT ===
{body}
=== END SCRIPT ===

Hunt specifically for:
1. NOT A MEMBER - something presented as belonging to the category that does
   not belong to it. Every sentence about it can be true and it is still
   wrong to include. This is the most serious flaw there is.
2. UNSUPPORTED - a specific figure, date, name or "X was first" claim the
   source material above does not support.
3. WRONG - a claim the source material contradicts.
4. EMPTY - a sentence that states nothing and could be deleted with no loss.
5. HARD TO FOLLOW - a sentence a 15-year-old would have to read twice, or a
   word with a shorter everyday equivalent.
6. VAGUE - a claim so hedged it says nothing.

Return ONLY JSON, no prose:
{{"findings":[{{"severity":"hard|soft","kind":"not-a-member|unsupported|wrong|empty|hard-to-follow|vague","scene":<n or null>,"quote":"<the exact words at fault, short>","detail":"<what is wrong and what to do>"}}]}}

Quote the actual words. A finding with no quote will be discarded."""


def attack(script, call, context="", verified_members=None):
    """The adversarial model pass. Findings without a quote are dropped -
    that requirement alone removes most vague, unactionable output."""
    scenes = script.get("scenes", []) if isinstance(script, dict) else []
    body = "\n\n".join(f"SCENE {s.get('scene', i)}: {s.get('narration','')}"
                       for i, s in enumerate(scenes, 1))
    mb = ""
    if verified_members:
        mb = ("=== THE VERIFIED MEMBERS OF THIS CATEGORY ===\n"
              "Independent sources agree these, and ONLY these, belong:\n"
              + "\n".join(f"- {m}" for m in verified_members)
              + "\nAnything else presented as a member is a hard failure.\n\n")
    try:
        data = call(ATTACK_PROMPT.format(members_block=mb, context=context[:14000],
                                         body=body), schema={"type": "object"})
    except Exception as e:
        return [{"severity": "soft", "scene": None, "kind": "attack-failed",
                 "detail": f"adversarial pass did not run: {str(e)[:120]}"}]
    found = data.get("findings", []) if isinstance(data, dict) else []
    return [f for f in found if isinstance(f, dict) and f.get("quote")]


def report(findings):
    hard = [f for f in findings if f.get("severity") == "hard"]
    soft = [f for f in findings if f.get("severity") != "hard"]
    lines = [f"{len(hard)} HARD, {len(soft)} soft"]
    for f in hard + soft:
        where = f" scene {f['scene']}" if f.get("scene") else ""
        q = f' "{f["quote"][:60]}"' if f.get("quote") else ""
        lines.append(f"  [{f.get('severity','?'):<4}] {f.get('kind','?')}"
                     f"{where}:{q} {f.get('detail','')[:150]}")
    return "\n".join(lines), len(hard)


if __name__ == "__main__":
    GOOD = {"scenes": [
        {"scene": 1, "key_term": "fixed costs",
         "narration": "Fixed costs stay the same no matter how much you sell. "
                      "Rent is one. So is insurance. A slow month costs you "
                      "exactly what a busy one does."},
        {"scene": 2, "key_term": "variable costs",
         "narration": "Variable costs move with every sale. Materials, "
                      "packaging, delivery. Sell nothing and you pay almost "
                      "nothing."}]}
    BAD = {"scenes": [
        {"scene": 1, "key_term": "fixed costs",
         "narration": "It is important to note that fixed costs, which might "
                      "possibly be considered somewhat foundational, play a "
                      "crucial role in the ever-evolving landscape of "
                      "commercial operations and could arguably be utilized "
                      "to facilitate an optimal methodology."},
        {"scene": 2, "key_term": "runway",
         "narration": "Runway is how many months of cash you have left at "
                      "your current burn rate."}]}
    members = ["fixed costs", "variable costs", "semi-variable costs"]
    for name, s in (("CLEAN SCRIPT", GOOD), ("BAD SCRIPT", BAD)):
        print(f"\n{name}   reading grade "
              f"{reading_grade(' '.join(x['narration'] for x in s['scenes']))}")
        txt, hard = report(check(s, members))
        print(" ", txt.replace("\n", "\n "))
        print(f"  -> {'PASS' if hard == 0 else 'BLOCKED, ' + str(hard) + ' hard'}")
