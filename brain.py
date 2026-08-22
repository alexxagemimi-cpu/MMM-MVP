#!/usr/bin/env python3
"""
brain.py — MMM Factory scriptwriter.

Three-stage generation:
  1. Grounded research  (Google Search tool, plain text out)
  2. Draft script       (JSON schema, enforced scene count + word budget)
  3. Ruthless rewrite   (self-critique pass, JSON schema)

Writes: script.json  (title, description, tags, scenes[])
"""

import os
import sys
import json
import math
import time

from google import genai
from google.genai import types

# ----------------------------------------------------------------------------
# CONFIG (all overridable from the workflow)
# ----------------------------------------------------------------------------
MODEL          = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
TOPIC          = os.environ.get("TOPIC", "").strip()
TARGET_MINUTES = float(os.environ.get("TARGET_MINUTES", "12"))
WPM            = 150                      # Edge-TTS GuyNeural ~150 wpm

TOTAL_WORDS    = int(TARGET_MINUTES * WPM)
SCENE_COUNT    = max(8, min(16, round(TOTAL_WORDS / 165)))
WORDS_PER_SCENE = round(TOTAL_WORDS / SCENE_COUNT)
MIN_WORDS_PER_SCENE = int(WORDS_PER_SCENE * 0.70)

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


# ----------------------------------------------------------------------------
# SCHEMA — guarantees the engine never crashes on malformed output
# ----------------------------------------------------------------------------
SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title":       {"type": "string"},
        "description": {"type": "string"},
        "tags":        {"type": "array", "items": {"type": "string"}},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene":         {"type": "integer"},
                    "beat":          {"type": "string"},
                    "narration":     {"type": "string"},
                    "image_keyword": {"type": "string"},
                },
                "required": ["scene", "beat", "narration", "image_keyword"],
            },
        },
    },
    "required": ["title", "description", "tags", "scenes"],
}


# ----------------------------------------------------------------------------
# STYLE RULES — this is what separates "AI slop" from a real documentary script
# ----------------------------------------------------------------------------
CRAFT_RULES = """
VOICE AND CRAFT RULES (non-negotiable):

1. COLD OPEN. Scene 1 opens on a concrete, specific, verifiable detail — a
   number, a date, a name, a physical object. NEVER open with "Imagine if",
   "In today's world", "Have you ever wondered", or any rhetorical question.

2. SPECIFICITY OVER ADJECTIVES. "A 4.2-second delay" beats "a shocking delay".
   Every claim carries a concrete anchor. If you cannot anchor it, cut it.

3. NO FAKE PRECISION. Do not invent statistics, dates, dollar figures, or study
   results. If you are not confident a number is real, write the qualitative
   claim instead ("most", "a small minority"). A vague true sentence beats a
   precise false one. This rule outranks every other rule here.

4. NO SECOND-PERSON MOTIVATION. No "you need to", "here's what you should do",
   "the truth is". You are narrating, not coaching.

5. RHYTHM. Vary sentence length hard. Long, winding, clause-heavy sentences that
   build. Then a short one. That contrast is the entire engine of retention.

6. DELAYED PAYOFF. Pose the central question early. Do not answer it until the
   final third. Each scene should end on something slightly unresolved.

7. NO OUTRO CTA. No "like and subscribe", no "let me know in the comments".
   End on an implication that lingers.

8. NO LIST-SPEAK. Never write "Firstly", "Secondly", "In conclusion",
   "It's important to note", "Let's dive in", "buckle up".
"""

BEAT_STRUCTURE = """
NARRATIVE ARC — assign each scene one beat, in this order:
  HOOK        — the arresting concrete detail (1 scene)
  CONTEXT     — the world before, what was normal (1-2 scenes)
  INCITING    — the thing that broke the normal (1 scene)
  ESCALATION  — consequences compounding, tension building (2-4 scenes)
  TURN        — the reveal, the reframe, the thing you were set up to miss (1-2)
  FALLOUT     — what it meant, who paid, what changed (1-2 scenes)
  RESONANCE   — the wider implication, ending unresolved (1 scene)
"""


def call(prompt, schema=None, grounded=False, retries=3):
    """One Gemini call with retry + optional schema + optional search grounding."""
    cfg_kwargs = {}
    if schema:
        cfg_kwargs["response_mime_type"] = "application/json"
        cfg_kwargs["response_schema"] = schema
    if grounded:
        cfg_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]

    last = None
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            text = (resp.text or "").strip()
            if not text:
                raise RuntimeError("empty response")
            if schema:
                return json.loads(text)
            return text
        except Exception as e:
            last = e
            print(f"   ! attempt {attempt + 1}/{retries} failed: {e}")
            time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"Gemini call failed after {retries} attempts: {last}")


# ----------------------------------------------------------------------------
# STAGE 1 — grounded research
# ----------------------------------------------------------------------------
def research():
    if TOPIC:
        ask = f'Research this subject for a documentary: "{TOPIC}".'
    else:
        ask = (
            "Choose ONE subject for an investigative documentary: a technology "
            "story, an engineering failure, a corporate collapse, an unsolved "
            "case, or a historical decision with consequences that are still "
            "unfolding. Pick something with a genuine reversal in it — where "
            "the obvious explanation turns out to be wrong. Avoid the most "
            "over-covered subjects. Then research it."
        )

    prompt = f"""{ask}

Produce a research brief containing:
- The subject, stated in one sentence
- The central question the documentary will answer
- 10-14 concrete, verifiable facts: names, dates, numbers, places, quotes.
  Mark any fact you are NOT confident about with [UNVERIFIED].
- The reversal: what most people believe vs what actually happened
- 3 details that are surprising and rarely mentioned

Prioritise accuracy over interest. An [UNVERIFIED] tag is better than a
confident guess. Plain text, no markdown headers."""

    print("🔎 Stage 1/3 — grounded research...")
    try:
        brief = call(prompt, grounded=True)
    except Exception as e:
        print(f"   ! grounding unavailable ({e}) — falling back to ungrounded")
        brief = call(prompt)
    print(f"   ✓ brief: {len(brief.split())} words")
    return brief


# ----------------------------------------------------------------------------
# STAGE 2 — draft
# ----------------------------------------------------------------------------
def draft(brief):
    prompt = f"""You are an elite documentary scriptwriter. Write the full narration
script for a video in the style of Lemmino, Vox, or MagnatesMedia.

RESEARCH BRIEF:
---
{brief}
---

HARD REQUIREMENTS:
- EXACTLY {SCENE_COUNT} scenes.
- EVERY scene's "narration" must be {WORDS_PER_SCENE}-{WORDS_PER_SCENE + 45} words.
  This is the most commonly failed requirement. Count your words. A scene under
  {MIN_WORDS_PER_SCENE} words is a failure.
- Total narration ≈ {TOTAL_WORDS} words (~{TARGET_MINUTES:.0f} minutes spoken).
- Drop or soften anything marked [UNVERIFIED] in the brief. Never state an
  unverified claim as fact.

{BEAT_STRUCTURE}

{CRAFT_RULES}

FIELD SPEC:
- "narration": the exact spoken words. No stage directions, no speaker labels,
  no scene headings, no markdown. Plain prose only — this string is fed
  directly to a text-to-speech engine.
- "image_keyword": the SUBJECT of the visual only, 4-9 words. Concrete and
  photographable. Cinematic styling is applied automatically downstream, so do
  NOT include words like "cinematic", "4k", "moody", "dramatic lighting".
  Good: "abandoned server room, rows of dark racks"
  Bad:  "cinematic 8k shot of technology, dramatic"
- "beat": one of HOOK, CONTEXT, INCITING, ESCALATION, TURN, FALLOUT, RESONANCE.
- "title": under 70 characters. Specific and concrete. No clickbait, no
  ALL CAPS, no "SHOCKING", no "You Won't Believe".
- "description": 2-3 sentences for the YouTube description box.
- "tags": 8-12 lowercase search tags."""

    print(f"✍️  Stage 2/3 — drafting {SCENE_COUNT} scenes "
          f"(~{WORDS_PER_SCENE} words each)...")
    data = call(prompt, schema=SCRIPT_SCHEMA)
    print(f"   ✓ draft: {wordcount(data)} words across {len(data['scenes'])} scenes")
    return data


# ----------------------------------------------------------------------------
# STAGE 3 — critique and rewrite
# ----------------------------------------------------------------------------
def polish(data, brief):
    prompt = f"""Below is a draft documentary script. Critique it ruthlessly, then
return the REWRITTEN version with every flaw fixed.

RESEARCH BRIEF (for fact-checking the draft):
---
{brief}
---

DRAFT:
---
{json.dumps(data, indent=2)}
---

Hunt specifically for these failures and fix every instance:

1. INVENTED PRECISION — any statistic, date, percentage, or dollar figure that
   is not supported by the brief. Replace with a qualitative claim or cut it.
   This is the highest-priority fix.
2. FALSE DICHOTOMY — "either X or Y" framings where other explanations exist.
3. AI TELLS — "delve", "testament to", "it's important to note", "in the world
   of", "little did they know", "the harsh reality", "buckle up", "let's dive
   in", "game-changer", "landscape of". Purge all of them.
4. FLAT RHYTHM — consecutive sentences of similar length. Break them up.
5. WEAK OPENER — if scene 1 does not open on a concrete detail, rewrite it.
6. UNDERLENGTH — any scene under {MIN_WORDS_PER_SCENE} words must be expanded
   with real substance from the brief, not padding.
7. CTA / SECOND PERSON — remove entirely.
8. VAGUE IMAGE KEYWORDS — each must name a photographable subject.

Keep the same scene count ({SCENE_COUNT}) and the same beat structure.
Return only the corrected script in the required JSON format."""

    print("🔬 Stage 3/3 — critique and rewrite...")
    out = call(prompt, schema=SCRIPT_SCHEMA)
    print(f"   ✓ final: {wordcount(out)} words across {len(out['scenes'])} scenes")
    return out


# ----------------------------------------------------------------------------
# VALIDATION
# ----------------------------------------------------------------------------
def wordcount(data):
    return sum(len(s.get("narration", "").split()) for s in data.get("scenes", []))


def validate(data):
    problems = []
    scenes = data.get("scenes", [])

    if not scenes:
        problems.append("no scenes at all")
        return problems

    if len(scenes) < 6:
        problems.append(f"only {len(scenes)} scenes (need >= 6)")

    for i, s in enumerate(scenes, 1):
        n = s.get("narration", "").strip()
        k = s.get("image_keyword", "").strip()
        w = len(n.split())
        if w < MIN_WORDS_PER_SCENE:
            problems.append(f"scene {i}: {w} words (need >= {MIN_WORDS_PER_SCENE})")
        if not k:
            problems.append(f"scene {i}: empty image_keyword")

    total = wordcount(data)
    if total < TOTAL_WORDS * 0.65:
        problems.append(f"total {total} words, target ~{TOTAL_WORDS}")

    return problems


def repair(data, problems, brief):
    prompt = f"""This script failed validation. Fix ONLY these problems, changing
nothing else:

{chr(10).join('- ' + p for p in problems)}

Short scenes must be expanded with real substance drawn from the research brief
below — additional facts, consequences, or context. Do NOT pad with filler
sentences, restatements, or transitional fluff.

RESEARCH BRIEF:
---
{brief}
---

SCRIPT:
---
{json.dumps(data, indent=2)}
---

Return the corrected script in the required JSON format."""

    print("🔧 Repairing validation failures...")
    return call(prompt, schema=SCRIPT_SCHEMA)


# ----------------------------------------------------------------------------
def main():
    print("=" * 62)
    print(f"  MMM BRAIN | model={MODEL}")
    print(f"  target={TARGET_MINUTES:.0f}min  scenes={SCENE_COUNT}  "
          f"words/scene≈{WORDS_PER_SCENE}")
    print(f"  topic={TOPIC or '(AI chooses)'}")
    print("=" * 62)

    brief = research()
    data = draft(brief)
    data = polish(data, brief)

    problems = validate(data)
    if problems:
        print(f"⚠️  {len(problems)} validation problem(s):")
        for p in problems[:12]:
            print(f"     - {p}")
        try:
            fixed = repair(data, problems, brief)
            still = validate(fixed)
            if len(still) < len(problems):
                data = fixed
                problems = still
        except Exception as e:
            print(f"   ! repair failed ({e}) — keeping previous version")

    if problems:
        print(f"⚠️  Proceeding with {len(problems)} remaining issue(s). "
              f"Review script.json before publishing.")

    # renumber scenes defensively
    for i, s in enumerate(data["scenes"], 1):
        s["scene"] = i

    with open("script.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    words = wordcount(data)
    print("-" * 62)
    print(f"✅ script.json written")
    print(f"   title    : {data['title']}")
    print(f"   scenes   : {len(data['scenes'])}")
    print(f"   words    : {words}  (~{words / WPM:.1f} min spoken)")
    print("-" * 62)
    for s in data["scenes"]:
        beat = s.get("beat", "?")[:10].ljust(10)
        print(f"   {s['scene']:>2}. [{beat}] {len(s['narration'].split()):>4}w  "
              f"| {s['image_keyword'][:44]}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ FATAL: {e}", file=sys.stderr)
        sys.exit(1)

