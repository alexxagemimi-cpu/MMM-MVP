#!/usr/bin/env python3
"""
brain.py - MMM Factory scriptwriter.

Five stages:
  1. RESEARCH   grounded in Google Search, with a HARD check that real
                sources came back
  2. ANGLE      decide the exact question the film answers, and enforce that
                the answer matches what was actually asked for
  3. DRAFT      write it, schema-enforced
  4. FACT-CHECK a SECOND grounded search pass that re-verifies the claims the
                draft makes. This is the stage the old build was missing: a
                model critiquing its own text without new information cannot
                catch its own hallucination, because the hallucination is the
                only thing it has to check against.
  5. REPAIR     strip or correct everything the fact-check flagged

Writes script.json (title, description, tags, sources, scenes[]).
"""

import os
import re
import sys
import json
import time

from google import genai
from google.genai import types

MODEL          = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
TOPIC          = os.environ.get("TOPIC", "").strip()
TARGET_MINUTES = float(os.environ.get("TARGET_MINUTES", "12"))
STRICT         = os.environ.get("STRICT_FACTS", "1") == "1"
MAX_PASSES     = int(os.environ.get("MAX_PASSES", "4"))
WPM            = 150

TOTAL_WORDS         = int(TARGET_MINUTES * WPM)
SCENE_COUNT         = max(8, min(16, round(TOTAL_WORDS / 165)))
WORDS_PER_SCENE     = round(TOTAL_WORDS / SCENE_COUNT)
MIN_WORDS_PER_SCENE = int(WORDS_PER_SCENE * 0.70)

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


# ===================== QUALITY METRICS (deterministic) =====================
# Measured in Python, not judged by a model. A model asked "is this good?"
# will say yes to its own output. These numbers cannot be flattered.
from statistics import mean, pstdev

AI_TELLS = [
    "delve", "testament to", "it's important to note", "it is important to note",
    "little did they know", "the harsh reality", "buckle up", "let's dive in",
    "game-changer", "game changer", "landscape of", "in the world of",
    "when it comes to", "at the end of the day", "needless to say",
    "the fact of the matter", "tapestry", "unlock the", "navigate the",
    "in conclusion", "firstly", "secondly", "moreover", "furthermore",
    "revolutionize", "profound impact", "stands as a", "serves as a",
    "plays a crucial role", "a testament", "ever-evolving", "paradigm shift",
]
HEDGES = [
    "might", "maybe", "perhaps", "possibly", "arguably", "some say",
    "it seems", "somewhat", "rather", "fairly", "quite possibly",
    "many believe", "it could be argued", "generally speaking",
]
# "and then" connective tissue = episodic. "but/therefore" = causal.
CAUSAL = ["but ", "however", "therefore", "because", "which meant",
          "so that", "as a result", "instead", "yet ", "until "]

def sentences(text):
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]

def measure(scenes):
    text = " ".join(s.get("narration", "") for s in scenes)
    words = text.split()
    n = max(len(words), 1)
    sents = sentences(text)
    lens = [len(s.split()) for s in sents] or [0]
    low = text.lower()

    # numbers and proper nouns = concrete anchors
    nums = len(re.findall(r'\b\d[\d,.]*\b', text))
    props = 0
    for s in sents:
        for w in s.split()[1:]:
            c = w.strip('.,;:!?"\'()')
            if c and c[0].isupper() and c.lower() not in ("i",):
                props += 1

    return {
        "words": len(words),
        "sentences": len(sents),
        "fact_density": round((nums + props) * 100 / n, 2),
        "sent_len_sd": round(pstdev(lens) if len(lens) > 1 else 0, 2),
        "sent_len_mean": round(mean(lens), 1),
        "short_ratio": round(sum(1 for l in lens if l <= 7) / max(len(lens), 1), 3),
        "long_ratio": round(sum(1 for l in lens if l >= 28) / max(len(lens), 1), 3),
        "ai_tells": sum(low.count(t) for t in AI_TELLS),
        "hedge_rate": round(sum(low.count(h) for h in HEDGES) * 100 / n, 2),
        "causal_rate": round(sum(low.count(c) for c in CAUSAL) * 100 / n, 2),
        "questions": text.count("?"),
        "hook_concrete": bool(scenes) and bool(
            re.search(r'\b\d', sentences(scenes[0].get("narration", ""))[0])
            or re.search(r'\b[A-Z][a-z]+',
                         " ".join(sentences(scenes[0].get("narration", ""))[0].split()[1:]))
        ) if scenes and sentences(scenes[0].get("narration", "")) else False,
    }

TARGETS = {
    "fact_density": (5.0,  None, "concrete anchors (numbers, names, places) per 100 words"),
    "sent_len_sd":  (7.0,  None, "sentence-length variation - flat rhythm kills retention"),
    "short_ratio":  (0.10, None, "share of punchy sentences (<=7 words)"),
    "long_ratio":   (None, 0.18, "share of 28+ word sentences - too many is a slog"),
    "ai_tells":     (None, 0,    "banned filler phrases"),
    "hedge_rate":   (None, 1.2,  "hedging per 100 words - vagueness reads as fluff"),
    "causal_rate":  (1.5,  None, "but/therefore connectives per 100 words"),
}

def grade(m):
    fails = []
    for k, (lo, hi, why) in TARGETS.items():
        v = m[k]
        if lo is not None and v < lo:
            fails.append(f"{k}={v} (need >= {lo}) - {why}")
        if hi is not None and v > hi:
            fails.append(f"{k}={v} (need <= {hi}) - {why}")
    if not m.get("hook_concrete"):
        fails.append("hook_concrete=False - scene 1 must open on a number or a name")
    if m["questions"] == 0:
        fails.append("questions=0 - no open loop posed anywhere")
    return fails


SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title":       {"type": "string"},
        "description": {"type": "string"},
        "question":    {"type": "string"},
        "tags":        {"type": "array", "items": {"type": "string"}},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene":          {"type": "integer"},
                    "beat":           {"type": "string"},
                    "narration":      {"type": "string"},
                    "image_keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["scene", "beat", "narration", "image_keywords"],
            },
        },
    },
    "required": ["title", "description", "question", "tags", "scenes"],
}


CRAFT_RULES = """
VOICE AND CRAFT RULES:

1. COLD OPEN on a concrete, verifiable detail. Never "Imagine if", "In today's
   world", "Have you ever wondered".
2. SPECIFICITY OVER ADJECTIVES. Anchor every claim. If you cannot anchor it,
   cut the sentence.
3. NO INVENTED PRECISION. Never state a statistic, date, or figure you are not
   confident is real. A vague true sentence beats a precise false one. This
   rule outranks every other rule here.
4. NO SECOND-PERSON COACHING. No "you need to", "here's what you should do".
5. RHYTHM. Vary sentence length hard. Long, winding, clause-heavy sentences
   that build. Then a short one.
6. NO LIST-SPEAK. Never "Firstly", "In conclusion", "It's important to note",
   "Let's dive in", "buckle up", "delve", "testament to", "game-changer".
7. NO OUTRO CTA. End on an implication.
"""


def strip_fences(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()
    a, b = t.find("{"), t.rfind("}")
    if a != -1 and b > a:
        t = t[a:b + 1]
    return t


def call(prompt, schema=None, grounded=False, retries=3):
    """
    One Gemini call. When grounded=True this also returns the list of source
    URLs the search tool actually used, so the caller can tell the difference
    between 'searched the web' and 'made it up'.
    """
    cfg = {}
    if schema:
        cfg["response_mime_type"] = "application/json"
        cfg["response_schema"] = schema
    if grounded:
        cfg["tools"] = [types.Tool(google_search=types.GoogleSearch())]

    last = None
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=MODEL, contents=prompt,
                config=types.GenerateContentConfig(**cfg))
            text = (resp.text or "").strip()
            if not text:
                raise RuntimeError("empty response")

            sources = []
            if grounded:
                try:
                    gm = resp.candidates[0].grounding_metadata
                    for c in (getattr(gm, "grounding_chunks", None) or []):
                        w = getattr(c, "web", None)
                        if w and getattr(w, "uri", None):
                            sources.append({"title": getattr(w, "title", "") or "",
                                            "uri": w.uri})
                except Exception:
                    sources = []

            if schema:
                return json.loads(strip_fences(text)), sources
            return text, sources

        except Exception as e:
            last = e
            msg = str(e)
            print(f"   ! attempt {attempt+1}/{retries}: {msg[:180]}")
            if schema and "response_schema" in cfg and (
                    "schema" in msg.lower() or "invalid" in msg.lower()):
                print("     -> dropping response_schema, retrying plain JSON")
                cfg.pop("response_schema", None)
            time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"Gemini failed after {retries} attempts: {last}")


# ---------------------------------------------------------------- stage 1 ---
def research():
    if TOPIC:
        ask = (f'Research this subject thoroughly for a documentary: "{TOPIC}".\n'
               f'Answer what was actually asked. If the request names CATEGORIES '
               f'or TYPES of something, research the categories themselves - '
               f'what they are, how they differ, how to tell them apart. Do NOT '
               f'substitute the history of the subject for the subject itself. '
               f'History is background, not the answer.')
    else:
        ask = ("Choose ONE subject for an explainer documentary with a genuine "
               "reversal in it, where the obvious explanation turns out to be "
               "wrong. Avoid the most over-covered subjects. Then research it.")

    prompt = f"""{ask}

Search the web. Base everything on what you find, not on recall.

Produce a research brief containing:
- SUBJECT: one sentence
- QUESTION: the single question this film answers, stated plainly
- FACTS: 12-16 concrete verifiable facts - names, dates, numbers, places.
  Mark any you could not confirm from a source with [UNVERIFIED].
- REVERSAL: what most people believe vs what the sources actually show
- SURPRISES: 3 details that are rarely mentioned

Accuracy outranks interest. [UNVERIFIED] is always better than a confident
guess. Plain text."""

    print("[1/5] grounded research")
    brief, sources = call(prompt, grounded=True)

    print(f"      {len(brief.split())} words, {len(sources)} web sources")
    if STRICT and len(sources) == 0:
        # The old build silently fell back to an ungrounded call here. That is
        # precisely how a script full of confident invented history gets made:
        # every later stage trusts the brief completely, and the brief was
        # never checked against anything.
        raise RuntimeError(
            "Search grounding returned ZERO sources, so this 'research' would "
            "be the model's memory, not the web. Refusing to build a script on "
            "it. Set repository variable STRICT_FACTS=0 to override, but expect "
            "invented facts if you do.")
    if not sources:
        print("      !! WARNING: no sources - facts in this script are unverified")

    for s in sources[:6]:
        print(f"        - {s['title'][:60] or s['uri'][:60]}")
    return brief, sources


# ---------------------------------------------------------------- stage 2 ---
def draft(brief):
    topic_rule = ""
    if TOPIC:
        topic_rule = f"""
TOPIC CONTRACT - this is a hard requirement:
The viewer asked for: "{TOPIC}"
The script must actually deliver that. Set "question" to the precise question
you are answering, and make sure a viewer who wanted "{TOPIC}" gets it.
If the request is about types, kinds, or categories, the body of the film must
identify and explain those categories. Historical background may occupy at most
one or two scenes; it is context, never the substance.
"""

    prompt = f"""You are an elite documentary scriptwriter. Write the narration for
a high-retention explainer in the style of Lemmino, Vox, or Johnny Harris.

RESEARCH BRIEF:
---
{brief}
---
{topic_rule}
HARD REQUIREMENTS:
- EXACTLY {SCENE_COUNT} scenes.
- Every scene's "narration" is {WORDS_PER_SCENE}-{WORDS_PER_SCENE+45} words.
  Under {MIN_WORDS_PER_SCENE} words is a failure. Count them.
- Use ONLY facts present in the brief. Anything marked [UNVERIFIED] must be
  cut or softened to a qualitative statement. Do not add facts from memory.

NARRATIVE ARC - assign each scene a beat, in order:
  HOOK, CONTEXT, INCITING, ESCALATION (2-4), TURN (1-2), FALLOUT (1-2), RESONANCE

{CRAFT_RULES}

FIELDS:
- "narration": exact spoken words, plain prose, no stage directions. This
  string is fed straight to text-to-speech.
- "image_keywords": 5-7 DISTINCT visuals in the order the narration reaches
  them. These are searched against a real stock footage library, so name
  things that genuinely exist on film: real people doing real actions, real
  objects, real places. Vary macro / wide / person / object / environment.
  4-9 words each, naming a concrete photographable SUBJECT.
  Never write "cinematic", "4k", "moody", "dramatic lighting".
  Good: ["hands stitching a wool lapel", "crowded city street commuters",
         "rack of tailored suits in a shop", "close up of fabric weave",
         "empty tailoring workshop at night"]
- "title": under 70 characters, concrete, no clickbait, no ALL CAPS.
- "question": the one question this film answers.
- "description": 2-3 sentences.
- "tags": 8-12 lowercase tags."""

    print(f"[2/5] drafting {SCENE_COUNT} scenes (~{WORDS_PER_SCENE} words each)")
    data, _ = call(prompt, schema=SCRIPT_SCHEMA)
    print(f"      {wordcount(data)} words / {len(data['scenes'])} scenes")
    print(f"      answering: {data.get('question','(none)')[:80]}")
    return data


# ---------------------------------------------------------------- stage 3 ---
def parse_verdicts(report):
    """
    Parse only well-formed verdict lines.

    Substring matching on "WRONG"/"UNSUPPORTED" flagged summary lines like
    "0 WRONG claims found" and VERIFIED lines whose note happened to contain
    the word. Both were false positives that sent clean scripts into repair.
    """
    bad = []
    for line in report.splitlines():
        m = re.match(r'\s*SCENE\s+(\d+)\s*\|(.+)', line, re.I)
        if not m:
            continue
        parts = [p.strip() for p in m.group(2).split("|")]
        if len(parts) < 2:
            continue
        verdict = parts[1].upper()
        if verdict.startswith("WRONG") or verdict.startswith("UNSUPPORTED"):
            bad.append(line.strip())
    return bad


def fact_check(data, chunk=4):
    """
    Independent grounded verification, in chunks.

    One call over 16 scenes had to hold ~50 claims at once and verified them
    shallowly. Four scenes per call keeps each search focused.
    """
    scenes = data["scenes"]
    all_bad, all_report, total_src = [], [], 0

    for i in range(0, len(scenes), chunk):
        group = scenes[i:i + chunk]
        body = "\n\n".join(f'SCENE {s["scene"]}: {s["narration"]}' for s in group)
        prompt = f"""Fact-check this documentary excerpt by SEARCHING THE WEB. Never
rely on memory.

EXCERPT:
---
{body}
---

Extract every specific factual claim: names, dates, numbers, percentages,
attributions, and causal statements ("X caused Y", "X was the first").

Return ONE LINE PER CLAIM, in exactly this format and nothing else:
SCENE <n> | <claim, short> | VERIFIED | 
SCENE <n> | <claim, short> | WRONG | <the correct fact>
SCENE <n> | <claim, short> | UNSUPPORTED | <what you could not confirm>

Be harsh. A confidently stated date or figure you cannot confirm from a source
is UNSUPPORTED, not VERIFIED. No preamble, no summary line."""

        try:
            report, src = call(prompt, grounded=True)
        except Exception as ex:
            raise RuntimeError(
                f"Fact-check failed on scenes {i+1}-{i+len(group)}: {str(ex)[:150]}. "
                f"Refusing to ship an unverified script. Set MAX_PASSES=1 and "
                f"STRICT_FACTS=0 to override.")
        total_src += len(src)
        all_report.append(report)
        all_bad += parse_verdicts(report)

    print(f"      {total_src} sources, {len(all_bad)} problem claim(s)")
    for l in all_bad[:8]:
        print(f"        ! {l[:110]}")
    return "\n".join(all_report), all_bad


# ---------------------------------------------------------------- stage 4 ---
def revise(data, fact_problems, quality_problems, brief, metrics_now):
    """
    One revision pass fixing BOTH accuracy and craft together.

    Separating them wastes iterations: fixing a false claim changes the
    sentence, which changes the rhythm, which the craft pass then has to touch
    again. One pass, both lists, measured after.
    """
    fp = "\n".join("- " + x for x in fact_problems) or "(none)"
    qp = "\n".join("- " + x for x in quality_problems) or "(none)"

    prompt = f"""Rewrite this documentary script. Two lists of problems follow.
Fix every item in both.

=== ACCURACY PROBLEMS (from a web fact-check) ===
{fp}

Rules: every WRONG claim is corrected to the true fact or cut. Every
UNSUPPORTED claim is cut, or softened until it is no longer a specific factual
assertion. Replace anything you cut with real substance from the brief - never
with filler, restatement, or a transition sentence.

=== CRAFT PROBLEMS (measured, not opinion) ===
{qp}

Current measurements: {json.dumps(metrics_now)}

How to move each number, concretely:
- fact_density too low -> the writing is abstract. Replace general statements
  with named people, dated events, specific places, exact quantities from the
  brief. "Clothing changed" becomes "By 1912 the sack suit had displaced the
  frock coat in New York offices."
- sent_len_sd too low -> every sentence is the same length and it drones.
  Build one long clause-heavy sentence, then cut hard to three words.
- short_ratio too low -> not enough punchy sentences. Add short ones after
  long ones. "It failed." "Nobody noticed." "That was the mistake."
- long_ratio too high -> too many 28+ word sentences. Break them.
- ai_tells > 0 -> delete every listed phrase outright, do not paraphrase it.
- hedge_rate too high -> "might", "perhaps", "some say", "arguably" read as
  fluff. Either state it plainly or cut the sentence. If the brief does not
  support a plain statement, cut it entirely.
- causal_rate too low -> scenes are connected by "and then", which is
  episodic and loses viewers. Connect by "but" and "therefore" instead: each
  beat should be a consequence of the last, not a sequel to it.
- hook_concrete False -> scene 1 must open on a number, a name, or a physical
  object. Not a question, not a generalisation.
- questions=0 -> pose the central question early and do not answer it until
  the final third.

=== RETENTION REQUIREMENTS ===
- Every scene ends slightly unresolved. The viewer should feel a reason to
  stay, not a sense of completion.
- Re-hook at least every second scene: a new specific detail that reframes
  what came before.
- Never summarise what you just said. Move.

=== HARD CONSTRAINTS ===
- Keep exactly {SCENE_COUNT} scenes and the same beat order.
- Every narration stays {WORDS_PER_SCENE}-{WORDS_PER_SCENE+45} words.
- Add NO fact that is absent from the brief.
- image_keywords: 5-7 real, photographable subjects per scene.

RESEARCH BRIEF:
---
{brief}
---
SCRIPT:
---
{json.dumps(data, indent=2)}
---
Return the corrected script in the required JSON format."""

    out, _ = call(prompt, schema=SCRIPT_SCHEMA)
    return out


# ---------------------------------------------------------------- checks ----
def wordcount(d):
    return sum(len(s.get("narration", "").split()) for s in d.get("scenes", []))


def validate(d):
    p, scenes = [], d.get("scenes", [])
    if not scenes:
        return ["no scenes"]
    if len(scenes) < 6:
        p.append(f"only {len(scenes)} scenes")
    for i, s in enumerate(scenes, 1):
        w = len(s.get("narration", "").split())
        k = [x for x in (s.get("image_keywords") or []) if x and x.strip()]
        if w < MIN_WORDS_PER_SCENE:
            p.append(f"scene {i}: {w} words (need >= {MIN_WORDS_PER_SCENE})")
        if len(k) < 3:
            p.append(f"scene {i}: {len(k)} image keywords (need >= 3)")
    t = wordcount(d)
    if t < TOTAL_WORDS * 0.65:
        p.append(f"total {t} words, target ~{TOTAL_WORDS}")
    return p


def repair_shape(d, problems, brief):
    prompt = f"""Fix ONLY these structural problems, changing nothing else:

{chr(10).join('- ' + x for x in problems)}

Short scenes get expanded with real substance from the brief below, never
filler. Do not introduce any fact that is not in the brief.

BRIEF:
---
{brief}
---
SCRIPT:
---
{json.dumps(d, indent=2)}
---
Return the corrected script in the required JSON format."""
    print("[5/5] repairing structure")
    out, _ = call(prompt, schema=SCRIPT_SCHEMA)
    return out


def main():
    print("=" * 64)
    print(f"  MMM BRAIN | {MODEL} | {TARGET_MINUTES:.0f}min | {SCENE_COUNT} scenes")
    print(f"  topic      : {TOPIC or '(AI chooses)'}")
    print(f"  max passes : {MAX_PASSES}   strict_facts={STRICT}")
    print("=" * 64)

    brief, sources = research()
    data = draft(brief)

    history = []
    fact_bad, report = [], ""

    # Iterate until the script clears both bars, or the budget runs out.
    # You said slow is fine - this is the part that spends that time.
    for p in range(1, MAX_PASSES + 1):
        m = measure(data["scenes"])
        q_bad = grade(m)

        print(f"\n--- pass {p}/{MAX_PASSES} ---")
        print(f"      facts   : {len(fact_bad)} unresolved")
        print(f"      craft   : {len(q_bad)} below bar")
        print(f"      metrics : density={m['fact_density']} sd={m['sent_len_sd']} "
              f"short={m['short_ratio']} tells={m['ai_tells']} "
              f"hedge={m['hedge_rate']} causal={m['causal_rate']}")
        for x in q_bad[:6]:
            print(f"        - {x}")

        print("      verifying against the web...")
        report, fact_bad = fact_check(data)
        history.append({"pass": p, "metrics": m,
                        "craft_fails": len(q_bad), "fact_fails": len(fact_bad)})

        if not q_bad and not fact_bad:
            print(f"      PASSED both bars on pass {p}")
            break
        if p == MAX_PASSES:
            print(f"      budget exhausted with {len(q_bad)} craft and "
                  f"{len(fact_bad)} fact issues left")
            break

        print("      revising...")
        try:
            cand = revise(data, fact_bad, q_bad, brief, m)
            # only accept a revision that is not worse
            if len(grade(measure(cand["scenes"]))) <= len(q_bad):
                data = cand
            else:
                print("      revision scored worse - keeping previous draft")
        except Exception as ex:
            print(f"      !! revision failed ({str(ex)[:110]})")
            break

    shape = validate(data)
    if shape:
        print(f"\n{len(shape)} structural problem(s)")
        for x in shape[:8]:
            print(f"   - {x}")
        try:
            fixed = repair_shape(data, shape, brief)
            if len(validate(fixed)) < len(shape):
                data, shape = fixed, validate(fixed)
        except Exception as ex:
            print(f"   !! shape repair failed ({str(ex)[:110]})")

    for i, s in enumerate(data["scenes"], 1):
        s["scene"] = i
    final_m = measure(data["scenes"])
    data["sources"] = sources
    data["fact_check"] = fact_bad
    data["quality"] = {"final": final_m, "failing": grade(final_m),
                       "passes": history}

    with open("script.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    w = wordcount(data)
    print("\n" + "=" * 64)
    print(f"script.json written")
    print(f"   title    : {data['title']}")
    print(f"   answers  : {data.get('question','')[:70]}")
    print(f"   scenes   : {len(data['scenes'])}  words: {w} (~{w/WPM:.1f} min)")
    print(f"   sources  : {len(sources)}")
    print(f"   density  : {final_m['fact_density']} anchors/100w "
          f"| rhythm sd {final_m['sent_len_sd']} | tells {final_m['ai_tells']}")
    print(f"   unresolved: {len(grade(final_m))} craft, {len(fact_bad)} fact")
    print("=" * 64)
    for s in data["scenes"]:
        print(f"   {s['scene']:>2}. [{s.get('beat','?')[:10]:<10}] "
              f"{len(s['narration'].split()):>4}w  "
              f"{len(s.get('image_keywords') or []):>2} shots")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFATAL: {e}", file=sys.stderr)
        sys.exit(1)
  
