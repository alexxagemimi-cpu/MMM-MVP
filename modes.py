#!/usr/bin/env python3
"""
modes.py — three script shapes, because one shape caused invented facts.

THE BUG THIS EXISTS TO FIX
--------------------------
The script system had ONE mode: a narrative story arc
(HOOK -> CONTEXT -> INCITING -> ESCALATION -> TURN -> FALLOUT -> RESONANCE)
forced onto every topic, with the research stage explicitly told to find a
"reversal" - something everyone believes that turns out wrong.

Ask that system for "types of men's fashion" and there is no inciting
incident and no reversal, because the question is a taxonomy, not a story.
So the model invented one: confident fake dates, a manufactured turning
point, history substituted for the actual answer.

The fake-history bug and the one-size-story-arc bug are the same bug. A
model asked for a shape the material does not contain will manufacture the
shape. The fix is not a sterner prompt - it is not demanding the shape.

AND A SECOND, SUBTLER ONE
-------------------------
`fact_density` (names + dates + numbers per 100 words) was a quality metric
applied to every mode. For a taxonomy script the cheapest way to raise that
number is to invent a name or a date - so the metric actively rewarded the
exact disease it was meant to catch. Metrics are per-mode here, and
explainer mode has NO fact_density floor. That is deliberate. Do not add one.
"""

import re
from statistics import mean, pstdev


# ---------------------------------------------------------------------------
# mode detection
# ---------------------------------------------------------------------------
_GUIDE = re.compile(
    r'\b(how to|how do i|how can i|step by step|guide to|tutorial|'
    r'beginner\'?s guide|walkthrough|instructions for)\b', re.I)

_EXPLAINER = re.compile(
    r'\b(types? of|kinds? of|sorts? of|categor(y|ies) of|varieties of|'
    r'what is|what are|what\'?s the difference|difference between|'
    r'vs\.?|versus|compared to|explained|meaning of|classification)\b', re.I)

_STORY = re.compile(
    r'\b(why did|why does|how did .* (fail|collapse|die|fall|end)|'
    r'rise and fall|downfall|collapse of|the death of|what happened to|'
    r'the story of|scandal|disaster|mystery|untold)\b', re.I)


def detect_mode(topic):
    """
    Topic string -> "story" | "explainer" | "guide".

    Order matters: "how to tell the difference between X and Y" is a guide
    by phrasing but an explainer by substance, so EXPLAINER is tested before
    GUIDE only when both match. Empty topic (AI picks its own subject)
    defaults to story - that is the mode whose research stage is allowed to
    go looking for a reversal.
    """
    t = (topic or "").strip()
    if not t:
        return "story"
    exp, gd, st = bool(_EXPLAINER.search(t)), bool(_GUIDE.search(t)), bool(_STORY.search(t))
    if st and not exp:
        return "story"
    if exp:
        return "explainer"
    if gd:
        return "guide"
    return "story"


# ---------------------------------------------------------------------------
# shared vocabulary
# ---------------------------------------------------------------------------
AI_TELLS = [
    "delve", "testament to", "it's important to note", "it is important to note",
    "little did they know", "the harsh reality", "buckle up", "let's dive in",
    "game-changer", "game changer", "landscape of", "in the world of",
    "when it comes to", "at the end of the day", "needless to say",
    "the fact of the matter", "tapestry", "unlock the", "navigate the",
    "revolutionize", "profound impact", "stands as a", "serves as a",
    "plays a crucial role", "a testament", "ever-evolving", "paradigm shift",
]
# Signposting is filler in a story and a service in a taxonomy or a manual,
# so it is banned per-mode (see MODES[...]["extra_tells"]), not globally.
SIGNPOSTS = ["in conclusion", "firstly", "secondly", "moreover", "furthermore"]

HEDGES = [
    "might", "maybe", "perhaps", "possibly", "arguably", "some say",
    "it seems", "somewhat", "rather", "fairly", "quite possibly",
    "many believe", "it could be argued", "generally speaking",
]
CAUSAL = ["but ", "however", "therefore", "because", "which meant",
          "so that", "as a result", "instead", "yet ", "until "]

DEFINES = ["is a ", "are a ", "is the ", "are the ", "means ", "refers to",
           "known as", "called ", "defined as", "consists of", "made up of",
           "is when ", "is where "]
EXAMPLES = ["for example", "for instance", "such as", "like the ", "e.g.",
            "say, ", "consider "]

# Signals that scene 1 actually DELIVERED the answer rather than teasing it.
# The first version of this matched the bare word "first", which happily
# matched historical prose ("cut the first lounge jacket") and scored pure
# invented history as if it had answered the question.
_ANSWER_SIGNALS = re.compile(
    r'\b(there are (two|three|four|five|six|seven|eight|nine|ten|\d+)|'
    r'(two|three|four|five|six|seven|eight|nine|ten|\d+) (main |broad |basic |key |common )?'
    r'(types|kinds|categories|sorts|varieties|families|groups|steps|stages|ways)|'
    r'falls? into|divide[sd]? into|break[s]? down into|split into|'
    r'is defined as|are defined as|the difference is|differ in|'
    r'comes down to|boils down to|the answer is|the short answer)\b', re.I)

_IMPERATIVE = re.compile(
    r'^(start|stop|take|make|use|pick|choose|check|avoid|keep|put|set|add|'
    r'remove|open|close|write|read|run|try|do|don\'?t|never|always|first,|'
    r'next,|then,|finally,|begin|find|get|give|hold|leave|look|move|place|'
    r'pull|push|save|send|show|turn|watch|work)\b', re.I)


def sentences(text):
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]


# ---------------------------------------------------------------------------
# measurement (deterministic - a model asked "is this good?" says yes)
# ---------------------------------------------------------------------------
def measure(scenes, mode="story"):
    text = " ".join(s.get("narration", "") for s in scenes)
    words = text.split()
    n = max(len(words), 1)
    sents = sentences(text)
    lens = [len(s.split()) for s in sents] or [0]
    low = text.lower()

    nums = len(re.findall(r'\b\d[\d,.]*\b', text))
    props = 0
    for s in sents:
        for w in s.split()[1:]:
            c = w.strip('.,;:!?"\'()')
            if c and c[0].isupper() and c.lower() != "i":
                props += 1

    tells = list(AI_TELLS) + (SIGNPOSTS if MODES[mode]["ban_signposts"] else [])

    scene1 = scenes[0].get("narration", "") if scenes else ""
    s1_sents = sentences(scene1)

    imperatives = sum(1 for s in sents if _IMPERATIVE.match(s))

    return {
        "words": len(words),
        "sentences": len(sents),
        "fact_density": round((nums + props) * 100 / n, 2),
        "sent_len_sd": round(pstdev(lens) if len(lens) > 1 else 0, 2),
        "sent_len_mean": round(mean(lens), 1),
        "short_ratio": round(sum(1 for l in lens if l <= 7) / max(len(lens), 1), 3),
        "long_ratio": round(sum(1 for l in lens if l >= 28) / max(len(lens), 1), 3),
        "ai_tells": sum(low.count(t) for t in tells),
        "hedge_rate": round(sum(low.count(h) for h in HEDGES) * 100 / n, 2),
        "causal_rate": round(sum(low.count(c) for c in CAUSAL) * 100 / n, 2),
        "define_rate": round(sum(low.count(d) for d in DEFINES) * 100 / n, 2),
        "example_rate": round(sum(low.count(e) for e in EXAMPLES) * 100 / n, 2),
        "action_rate": round(imperatives / max(len(sents), 1), 3),
        "questions": text.count("?"),
        "answers_early": bool(_ANSWER_SIGNALS.search(scene1)),
        "hook_concrete": bool(
            s1_sents and (re.search(r'\b\d', s1_sents[0])
                          or re.search(r'\b[A-Z][a-z]+',
                                       " ".join(s1_sents[0].split()[1:])))),
    }


# ---------------------------------------------------------------------------
# the three modes
# ---------------------------------------------------------------------------
_SHARED_TARGETS = {
    "sent_len_sd": (7.0, None, "sentence-length variation - flat rhythm kills retention"),
    "short_ratio": (0.10, None, "share of punchy sentences (<=7 words)"),
    "long_ratio":  (None, 0.18, "share of 28+ word sentences - too many is a slog"),
    "ai_tells":    (None, 0, "banned filler phrases"),
    "hedge_rate":  (None, 1.2, "hedging per 100 words - vagueness reads as fluff"),
}

MODES = {
    # -------------------------------------------------------------- story --
    "story": {
        "label": "story",
        "ban_signposts": True,
        "beats": "HOOK, CONTEXT, INCITING, ESCALATION (2-4), TURN (1-2), "
                 "FALLOUT (1-2), RESONANCE",
        "research": (
            "Look for a REVERSAL: what most people believe versus what the "
            "sources actually show. If the sources do not support a genuine "
            "reversal, say so plainly - an invented reversal is far worse "
            "than no reversal."),
        "shape": (
            "Delay the answer. The central question is posed early and not "
            "resolved until the final third. Each scene is a consequence of "
            "the last, not a sequel to it."),
        "person": "No second person. Never 'you need to', 'here's what you should do'.",
        "targets": {**_SHARED_TARGETS,
                    "fact_density": (5.0, None, "concrete anchors (numbers, names, places) per 100 words"),
                    "causal_rate": (1.5, None, "but/therefore connectives per 100 words")},
        "require": ["hook_concrete", "questions"],
    },
    # ---------------------------------------------------------- explainer --
    "explainer": {
        "label": "explainer",
        "ban_signposts": False,   # signposting HELPS a taxonomy
        "beats": "ANSWER (scene 1), FRAME, CATEGORY (one scene per type), "
                 "EDGE, APPLY, CLOSE",
        "research": (
            "Research the CATEGORIES THEMSELVES - what they are, how they "
            "differ, how to tell them apart. Do NOT hunt for a plot twist "
            "and do NOT substitute the history of the subject for the "
            "subject itself. History is background, never the answer. An "
            "invented reversal is worse than no reversal."),
        "shape": (
            "ANSWER THE QUESTION IN SCENE 1. No exceptions, no teasing. "
            "Name the categories up front, then give each its own scene. "
            "The viewer who wanted the list gets the list immediately."),
        "person": "No second person coaching.",
        # NO fact_density floor. See the module docstring: on a taxonomy the
        # cheapest way to raise it is to invent a name or a date.
        "targets": {**_SHARED_TARGETS,
                    "define_rate": (1.2, None, "definitional phrasing per 100 words - a taxonomy must define"),
                    "example_rate": (0.5, None, "concrete examples per 100 words")},
        "require": ["answers_early"],
    },
    # -------------------------------------------------------------- guide --
    "guide": {
        "label": "guide",
        "ban_signposts": False,   # step signposting is the point
        "beats": "PROMISE, STAKES, STEP (one scene per step), MISTAKE, "
                 "CHECK, CLOSE",
        "research": (
            "Research the actual procedure: the real steps in order, what "
            "each one requires, and the specific mistakes people make. Do "
            "not hunt for a twist. Do not pad with history."),
        "shape": (
            "State the promise in scene 1 - what the viewer will be able to "
            "do by the end. Then the steps, in order, one per scene."),
        "person": "Second person is ALLOWED and expected here.",
        "targets": {**_SHARED_TARGETS,
                    "action_rate": (0.15, None, "share of sentences opening on an imperative verb")},
        "require": ["answers_early"],
    },
}

_REQUIRE_WHY = {
    "hook_concrete": "hook_concrete=False - scene 1 must open on a number, a name, or a physical object",
    "questions": "questions=0 - no open loop posed anywhere",
    "answers_early": "answers_early=False - scene 1 must DELIVER the answer (name the categories/steps), not tease it",
}


def grade(m, mode="story"):
    """Measured failures against this mode's bar. Never a model's opinion."""
    cfg = MODES[mode]
    fails = []
    for k, (lo, hi, why) in cfg["targets"].items():
        v = m[k]
        if lo is not None and v < lo:
            fails.append(f"{k}={v} (need >= {lo}) - {why}")
        if hi is not None and v > hi:
            fails.append(f"{k}={v} (need <= {hi}) - {why}")
    for req in cfg["require"]:
        if not m.get(req):
            fails.append(_REQUIRE_WHY[req])
    if "questions" not in cfg["require"] and m["questions"] == 0 and mode == "story":
        fails.append(_REQUIRE_WHY["questions"])
    return fails


def craft_rules(mode):
    """The per-mode block injected into the drafting and revision prompts."""
    cfg = MODES[mode]
    banned = "'Firstly', 'In conclusion', " if cfg["ban_signposts"] else ""
    return f"""
VOICE AND CRAFT RULES ({cfg['label'].upper()} MODE):

1. COLD OPEN on a concrete, verifiable detail. Never "Imagine if", "In today's
   world", "Have you ever wondered".
2. SPECIFICITY OVER ADJECTIVES. Anchor every claim. If you cannot anchor it,
   cut the sentence.
3. NO INVENTED PRECISION. Never state a statistic, date, or figure you are not
   confident is real. A vague true sentence beats a precise false one. This
   rule outranks every other rule here.
4. {cfg['person']}
5. RHYTHM. Vary sentence length hard. Long, winding, clause-heavy sentences
   that build. Then a short one.
6. NO FILLER. Never {banned}"It's important to note", "Let's dive in",
   "buckle up", "delve", "testament to", "game-changer".
7. NO OUTRO CTA. End on an implication.

SHAPE: {cfg['shape']}
"""


if __name__ == "__main__":
    tests = [
        ("types of men's fashion", "explainer"),
        ("what is quantitative easing", "explainer"),
        ("difference between weather and climate", "explainer"),
        ("bitcoin vs ethereum", "explainer"),
        ("kinds of coffee roast", "explainer"),
        ("how to start a podcast", "guide"),
        ("beginner's guide to sourdough", "guide"),
        ("step by step home espresso setup", "guide"),
        ("why did Nokia collapse", "story"),
        ("the rise and fall of Blockbuster", "story"),
        ("what happened to the Aral Sea", "story"),
        ("", "story"),
    ]
    ok = 0
    for topic, want in tests:
        got = detect_mode(topic)
        flag = "ok " if got == want else "FAIL"
        ok += got == want
        print(f"  {flag} {topic!r:45} -> {got} (want {want})")
    print(f"\n{ok}/{len(tests)} correct")
