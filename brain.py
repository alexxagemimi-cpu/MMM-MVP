#!/usr/bin/env python3
"""
brain.py - MMM Factory scriptwriter.

Five stages:
  1. RESEARCH   real web search (research.py) - we run the queries and read
                the pages ourselves, then hand the model actual source text.
                HARD check that real sources came back.
  2. ANGLE      decide the exact question the film answers, and enforce that
                the answer matches what was actually asked for
  3. DRAFT      write it, schema-enforced
  4. FACT-CHECK a SECOND search pass that re-verifies the claims the draft
                makes against FRESH sources. This is the stage the old build
                was missing: a model critiquing its own text without new
                information cannot catch its own hallucination, because the
                hallucination is the only thing it has to check against.
  5. REPAIR     strip or correct everything the fact-check flagged

Writes script.json (title, description, tags, sources, scenes[]).

PROVIDERS
---------
Research does not use any model's built-in search tool. That used to weld
this whole pipeline to Gemini's search-grounding quota - a bucket metered
separately from normal model calls, and when it emptied every run died at
stage 1 with a 429 while the plain model quota sat untouched. Search is now
research.py's job (Tavily if keyed, else keyless DuckDuckGo), and the writer
model is interchangeable: Gemini -> Cerebras -> Groq, whichever has a key
and isn't rate-limited. One provider's wall no longer ends a run.
"""

import os
import re
import sys
import json
import time
import urllib.request
import urllib.error

from google import genai
from google.genai import types

import research as web
import modes
# Module scope, not inside choose_topic(). red_team() needs MIN_MEMBERS too,
# and a function-local import there would have been a NameError on the one
# path that matters - the run where a verified list actually exists.
import topics

MODEL          = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
TOPIC          = os.environ.get("TOPIC", "").strip()
# story | explainer | guide. Auto-detected from the topic unless forced.
MODE           = (os.environ.get("MODE", "").strip().lower()
                  or modes.detect_mode(os.environ.get("TOPIC", "")))
TARGET_MINUTES = float(os.environ.get("TARGET_MINUTES", "12"))
STRICT         = os.environ.get("STRICT_FACTS", "1") == "1"
MAX_PASSES     = int(os.environ.get("MAX_PASSES", "4"))

# MEASURED, not assumed. The old 150 was a guess; the real voice was timed
# from run #20's own log - 168 words of narration produced 61.65s of audio
# by en-US-GuyNeural, which is 163.5 words per minute. Getting this wrong
# scales every length calculation below it.
WPM            = 163

# How long one scene should run, in seconds. A scene is one idea, and this
# is what decides both how many scenes a video gets and how many words each
# one carries.
#
# Run #23 produced 60-second scenes, which is a long time to hold a single
# idea while the picture cuts every five. 45s is a deliberate step toward
# the reference channels without inventing structure: on a taxonomy the
# number of real categories is fixed by the subject, so padding the scene
# count to hit a runtime would mean inventing categories - the same failure
# modes.py exists to prevent, wearing a different hat.
SCENE_SECONDS  = float(os.environ.get("SCENE_SECONDS", "45"))

# Optional fallback LLMs, tried in this order after Gemini. All free-tier,
# no card; each is simply skipped when its key is unset.
#
# Every model id is an env var on purpose. Which models are free ROTATES -
# DeepSeek R1 was free on OpenRouter and went paid-only in July 2026 - so
# chasing that with code edits is a losing game. Set the *_MODEL variable to
# whatever is currently free and good; no redeploy of logic required.
def _env(name, default):
    """
    Env value or default, treating "" as unset.

    GitHub Actions expands an undefined `vars.X` to an empty string rather
    than omitting the variable, so os.environ.get(name, default) would hand
    back "" and silently request a model named nothing.
    """
    return (os.environ.get(name) or "").strip() or default


# Groq before Cerebras deliberately. Cerebras' no-card 1M-tokens/day tier
# ended in August 2026 - new accounts need a verified payment method - so it
# no longer satisfies this project's hard zero-budget, no-card rule. Groq is
# still genuinely card-free, so it is the first fallback; Cerebras stays
# wired up for anyone who does have an account.
#
# gpt-oss-120b, not llama-3.3-70b: Groq deprecated qwen3-32b (June 2026) and
# kimi-k2 (March 2026) and points both at gpt-oss-120b, a 120B reasoning
# model - a better writer and a much better fact-checker than a 70B chat
# model, at the same price of zero.
GROQ_KEY       = _env("GROQ_API_KEY", "")
GROQ_MODEL     = _env("GROQ_MODEL", "openai/gpt-oss-120b")
CEREBRAS_KEY   = _env("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL = _env("CEREBRAS_MODEL", "gpt-oss-120b")
# OpenRouter is last: its free tier is only ~50 requests/day, which one long
# script with several revision passes can exhaust on its own. Useful as a
# safety net and for reaching a model the others do not carry.
OPENROUTER_KEY   = _env("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = _env("OPENROUTER_MODEL",
                        "meta-llama/llama-3.3-70b-instruct:free")

# Groq sits behind Cloudflare, and Cloudflare blocks urllib's default
# "Python-urllib/3.11" User-Agent outright: the reply is HTTP 403 with a body
# of "error code: 1010", which is Cloudflare's BROWSER SIGNATURE BANNED code,
# not anything Groq's API said. Groq's own community forum carries this exact
# report ("Cloudflare Blocking Urllib.request without User-Agent").
#
# This cost a full run and a wrong diagnosis: 403 was read as "this model is
# blocked for your account", so model auto-discovery was built to work around
# a permissions problem that did not exist - and discovery hit the same wall,
# because /models is behind the same edge. research.py already sends a browser
# UA on every request it makes, which is why web research kept working in the
# same runs where every model call died.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TOTAL_WORDS = int(TARGET_MINUTES * WPM)

# Scene count comes from how long a scene should be, not from a magic
# divisor with a floor of 8 bolted on. That floor was quietly distorting
# every short video: a 6-minute request wanted ~6 scenes, got forced to 8,
# and each one was handed a word budget it then overshot.
SCENE_COUNT     = max(5, min(18, round(TOTAL_WORDS / (WPM * SCENE_SECONDS / 60))))
WORDS_PER_SCENE = round(TOTAL_WORDS / SCENE_COUNT)

# THE RANGE IS SYMMETRIC ON PURPOSE, AND THIS IS THE ACTUAL OVERSHOOT FIX.
#
# It used to be WORDS_PER_SCENE to WORDS_PER_SCENE+45. A model given a range
# writes to the TOP of it, and on a 113-word target +45 is +40% - so run #23
# asked for 6 minutes and delivered 8.3. The old MIN of 0.70x made it worse
# by widening the band further.
#
# Centring the band means writing to the top overshoots by ~12% instead of
# 40%, and the midpoint is now the number we actually want.
MIN_WORDS_PER_SCENE = int(WORDS_PER_SCENE * 0.88)
MAX_WORDS_PER_SCENE = int(WORDS_PER_SCENE * 1.12)

# One distinct visual per ~5s of scene (engine.py's TARGET_SHOT_SEC), so a
# long scene does not have to reuse a keyword. Run #23's 60s scenes got 12
# shots from 8-9 keywords, so three or four shots per scene re-ran a search
# already used in that same scene - visible as the same subject twice.
#
# Derived from the ACTUAL scene length rather than the SCENE_SECONDS target,
# because the scene-count clamp can stretch scenes past it: at 16 minutes the
# 18-scene cap makes each scene 53s, which needs 11 visuals, not the 10 a
# 45s target implies. One repeat crept back in at exactly that setting.
_scene_secs = WORDS_PER_SCENE / WPM * 60
KEYWORDS_PER_SCENE = max(6, min(13, round(_scene_secs / 5) + 2))

# Optional: the fallback providers can carry a whole run on their own, so a
# missing Gemini key is no longer fatal at import time.
client = (genai.Client(api_key=os.environ["GEMINI_API_KEY"])
          if os.environ.get("GEMINI_API_KEY") else None)


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

def _legacy_measure_unused(scenes):
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

def _legacy_grade_unused(m):
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


def measure(scenes):
    """Per-MODE metrics. See modes.py - explainer has no fact_density
    floor on purpose, because raising that number on a taxonomy means
    inventing names and dates."""
    return modes.measure(scenes, MODE)


def grade(m):
    return modes.grade(m, MODE)


SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title":       {"type": "string"},
        "description": {"type": "string"},
        "question":    {"type": "string"},
        "tags":        {"type": "array", "items": {"type": "string"}},
        # Thumbnail text. Deliberately SHORTER than the title: the title is
        # read, the thumbnail is glanced at. thumb_accent is the one phrase
        # drawn in red and must be a literal part of thumb_headline, or the
        # renderer has nothing to colour.
        "thumb_headline": {"type": "string"},
        "thumb_accent":   {"type": "string"},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene":          {"type": "integer"},
                    "beat":           {"type": "string"},
                    "narration":      {"type": "string"},
                    "key_term":       {"type": "string"},
                    "key_fact":       {"type": "string"},
                    "image_keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["scene", "beat", "narration", "key_term",
                             "key_fact", "image_keywords"],
            },
        },
    },
    "required": ["title", "description", "question", "tags",
                 "thumb_headline", "thumb_accent", "scenes"],
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


def _gemini(prompt, schema, drop_schema):
    """Gemini via google-genai. No search tool - grounding is research.py's job."""
    cfg = {}
    if schema and not drop_schema:
        cfg["response_mime_type"] = "application/json"
        cfg["response_schema"] = schema
    elif schema:
        cfg["response_mime_type"] = "application/json"
    resp = client.models.generate_content(
        model=MODEL, contents=prompt,
        config=types.GenerateContentConfig(**cfg))
    return (resp.text or "").strip()


def _openai_compatible(prompt, schema, base_url, key, model, label,
                       drop_schema=False):
    """
    Cerebras / Groq / anything speaking the OpenAI chat-completions shape.

    These exist so one provider's quota cannot kill a run. Both have real
    free tiers with no card (Cerebras ~1M tokens/day, Groq ~30 req/min),
    which is the whole point: the Gemini 429 that blocked every run so far
    should degrade to "slower and on another model", not "dead".

    TWO THINGS HERE ARE NOT COSMETIC.

    `drop_schema` was accepted by _call_sweep, printed about, and never
    passed down. `response_format` went on being sent regardless, so the
    "retrying plain JSON" retry was byte-identical to the request that had
    just failed - the same fault as the 413 in §5.11, on a different line.

    And Groq refuses `json_object` mode unless the literal string "json"
    appears somewhere in the messages:

        HTTP 400: 'messages' must contain the word 'json' in some form,
                  to use 'response_format' of type 'json_object'

    That is a real Groq rule, not a bug on our side. Most prompts here say
    "Reply with JSON", but not all of them do, and the ones that do not
    could never be answered by this provider. Say the word.
    """
    text = prompt
    if schema and not drop_schema and "json" not in text.lower():
        text += "\n\nReply with a single JSON object and nothing else."
    elif schema and drop_schema:
        # No structured-output mode at all: ask in words instead, and let
        # strip_fences() dig the object out of whatever comes back.
        text += ("\n\nReply with a single valid JSON object and nothing "
                 "else. No prose, no explanation, no code fences.")

    body = {"model": model,
            "messages": [{"role": "user", "content": text}],
            "temperature": 0.8}
    if schema and not drop_schema:
        body["response_format"] = {"type": "json_object"}

    # REASONING MODELS SPEND THE ANSWER ON THINKING.
    #
    # gpt-oss-120b is a reasoning model and Groq's default reasoning_effort is
    # "medium". Runs 48, 49 and 50 all ended on the same pair - HTTP 400
    # "Failed to validate JSON", then an empty response once the schema was
    # dropped - on three different prompts at two different stages. Groq's own
    # docs say gpt-oss-120b takes reasoning_effort low/medium/high and puts the
    # thinking in a separate `reasoning` field. Asking for less thinking leaves
    # more of the budget for the JSON we actually want.
    #
    # Only for gpt-oss: reasoning_effort is model-specific and sending it to a
    # model that does not take it is a new 400 in place of the old one.
    if "gpt-oss" in (model or ""):
        body["reasoning_effort"] = REASONING_EFFORT
    req = urllib.request.Request(
        base_url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # The BODY is the useful part and urllib drops it from str(e). A real
        # run logged only "HTTP Error 403: Forbidden", which is unactionable;
        # the body said the model was blocked at the organisation level and
        # named the settings page to unblock it.
        #
        # 400 chars was too short and it cost three runs. Groq's "Failed to
        # validate JSON" body carries a `failed_generation` field holding the
        # text the model actually produced - the one piece of evidence that
        # says WHY it was not valid JSON - and it sits past the 400th
        # character every time, so every log of this fault showed the
        # complaint and hid the cause.
        try:
            detail = e.read().decode("utf-8", "replace")[:2000]
        except Exception:
            detail = ""
        raise RuntimeError(f"HTTP {e.code} from {label} ({model}): {detail}")

    msg = data["choices"][0].get("message", {}) or {}
    out = (msg.get("content") or "").strip()
    if not out:
        # AN ANSWER THAT ARRIVED, READ AS NO ANSWER.
        #
        # Groq returns a gpt-oss model's thinking in `reasoning`, separate
        # from `content`. When the model runs out of room mid-thought,
        # `content` is empty and everything it produced - the JSON included,
        # if it got that far - is sitting in `reasoning`. This function read
        # only `content`, so "empty response" meant "we did not look".
        # strip_fences() can dig an object out of prose; give it the chance
        # rather than throwing the reply away.
        reasoning = (msg.get("reasoning") or "").strip()
        if reasoning:
            print(f"     -> {label} returned no content but "
                  f"{len(reasoning):,} chars of reasoning; reading that "
                  f"instead", flush=True)
            out = reasoning
    fin = data["choices"][0].get("finish_reason")
    if fin and fin != "stop":
        # "length" here means the reply was CUT OFF, which is exactly how
        # valid JSON becomes invalid JSON. Without this the log showed a
        # refusal and never said the generation had been truncated.
        print(f"     -> {label} stopped early: finish_reason={fin}",
              flush=True)
    return out


def _oai_available_models(base_url, key):
    """
    Ask the provider which models this key may actually use.

    Groq blocks most models by default on a new account and answers 403 for
    them, so a hardcoded id is a coin flip. Rather than making the owner
    hunt through project settings, discover it: /models returns exactly what
    is permitted. Returns [] on any problem - discovery is best-effort.
    """
    try:
        url = base_url.replace("/chat/completions", "/models")
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {key}",
                          "User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        # Prefer bigger/instruct chat models; skip audio, guard and vision ones.
        bad = ("whisper", "tts", "guard", "vision", "embed", "rerank")
        chat = [i for i in ids if not any(b in i.lower() for b in bad)]
        chat.sort(key=lambda i: ("120b" in i, "70b" in i, "instruct" in i),
                  reverse=True)
        return chat
    except Exception as e:
        print(f"     .. model discovery failed on {base_url}: {str(e)[:110]}")
        return []


# HOW BIG A PROMPT EACH PROVIDER WILL ACTUALLY ACCEPT, in characters.
#
# This is NOT the model's context window. Groq's free tier meters TOKENS PER
# MINUTE, and gpt-oss-120b's on-demand limit is 8,000 - so a request the model
# could easily hold is refused before it is ever read:
#
#     HTTP 413 ... Limit 8000, Requested 8373, please reduce your message size
#
# That is what killed run 36. Gemini's daily quota had run out, research had
# gathered 34,832 characters of source text, and the fallback writer - the one
# thing standing between a quota wall and a dead run - could not physically
# accept the prompt. It failed, waited 20 seconds, re-sent THE SAME prompt,
# and failed identically. The safety net has never once caught a run.
#
# 8,000 tokens is the whole budget, request plus reply, so the input cannot
# have all of it: ~2,600 tokens are reserved for the answer, leaving ~5,400
# in, and at a deliberately pessimistic 3 characters per token that is 16,000
# characters. Pessimistic on purpose - source text is full of names, figures
# and URLs, which tokenize far worse than prose, and being under the limit is
# worth more than squeezing in two more paragraphs.
PROVIDER_CHAR_CAP = {"groq": 16000}

# HOW MUCH OF THAT REPLY BUDGET THE MODEL IS ALLOWED TO SPEND ON THINKING.
#
# The ~2,600 tokens reserved for the answer above assumed the answer was the
# only thing coming back. It is not: gpt-oss-120b is a reasoning model and
# Groq's default reasoning_effort is "medium", so an unknown share of those
# tokens goes on thinking before a single character of JSON is emitted. Runs
# 48, 49 and 50 all died on the consequence - a JSON object that stopped
# mid-structure ("Failed to validate JSON") and then, with the schema
# dropped, nothing in `content` at all.
#
# "low" is the default here because this pipeline never asks for a hard
# think - it asks for a known shape filled in from source text it was handed.
# An env var because if a future model needs more, that must not be a code
# change.
REASONING_EFFORT = _env("REASONING_EFFORT", "low")

# WHO ACTUALLY WROTE THIS SCRIPT, AND HOW MUCH OF THE RESEARCH THEY SAW.
#
# A fallback-written script is not the same product as a Gemini-written one:
# the cap above means Groq sees ~16,000 of research's ~35,000 characters, so
# roughly half the sources never reach the writer at all. That trade is right
# - half the sources beats a dead run - but it is invisible in the output, and
# an invisible quality drop is the thing this project keeps getting caught by.
#
# So it is recorded and reported. Not as a blocker: the owner decides what
# ships (§1), and the job here is to make sure they decide knowing this.
PROVIDER_USE = {}      # provider name -> times it answered
TRIMMED_CALLS = [0, 0]  # [calls whose prompt was cut, calls total]

# STOP RE-ASKING A PROVIDER THAT IS OUT FOR THE DAY.
#
# Watching run 38: every single call went gemini -> 429 -> wait 20s -> gemini
# -> 429 -> switch to groq. Forty seconds of pure waiting per call, on a
# provider whose DAILY quota was already known to be gone, repeated for every
# call of every pass. The job has a 45-minute cap (4.6), so this is not just
# untidy - it is minutes taken away from actually building the video.
#
# A cooldown, NOT a permanent write-off, and that distinction is the whole
# design. A per-minute limit genuinely clears and is worth waiting out; a
# daily quota is not. We cannot always tell which we hit - Gemini's message
# says only "You exceeded your current quota" - so parking the provider for a
# few minutes is the honest middle: a per-minute limit is long over by then,
# and a daily quota stops costing 40s a call.
PROVIDER_COOLDOWN = {}     # provider -> unix time it may be tried again

# TWO KINDS OF RATE LIMIT, TWO LENGTHS OF REST.
#
# Run 47 died because these were the same number. Groq's 429 is a TOKENS PER
# MINUTE limit that clears in about sixty seconds; Gemini's is a daily quota
# that does not clear until midnight Pacific. Parking both for seven minutes
# treated a one-minute problem as a seven-minute one, and with both parked the
# run had nothing left to try.
#
# The provider says which it is, in words, so read them rather than guessing.
COOLDOWN_PER_MINUTE = 75      # "tokens per minute", "requests per minute"
COOLDOWN_PER_DAY    = 1800    # a daily quota; long, but not the rest of time
COOLDOWN_SECONDS    = 420     # unclear which - the old blanket value

# How long the chain will WAIT for a cooling provider before giving up. It
# must exceed COOLDOWN_PER_MINUTE, or a per-minute limit parks every provider
# and the run dies inside a wait it could have sat through.
WAIT_OUT_MAX = 200

# The workflow kills the job at 45 minutes. Brain stops revising at 24 so the
# engine still gets its ~20 - see the note at the deadline check in main().
_started = time.time()
BRAIN_BUDGET_SEC = float(_env("BRAIN_BUDGET_MIN", "24")) * 60
TIME_CUT = []              # caveats about work skipped to make the deadline


def merge_scene_fixes(data, fixed):
    """
    Fold repaired scenes back into the script BY SCENE NUMBER.

    Only the three text fields a repair is allowed to change are taken.
    `beat`, `image_keywords` and everything else stay exactly as drafted -
    a repair that rewrote those would quietly change the video's structure
    while claiming to fix a sentence.

    Returns (new_data, how_many_applied). Scene numbers the script does not
    have are ignored rather than appended: a model inventing scene 47 must
    not be able to grow the script through the repair door.
    """
    by_n = {}
    for s in fixed or []:
        if isinstance(s, dict) and s.get("scene") is not None:
            try:
                by_n[int(s["scene"])] = s
            except (TypeError, ValueError):
                continue
    if not by_n:
        return data, 0

    out = json.loads(json.dumps(data))       # deep copy, do not mutate caller
    applied = 0
    for sc in out.get("scenes", []):
        got = by_n.get(sc.get("scene"))
        if not got:
            continue
        changed = False
        for field in ("narration", "key_term", "key_fact"):
            new = (got.get(field) or "").strip()
            if new and new != (sc.get(field) or "").strip():
                sc[field] = new
                changed = True
        applied += changed
    return out, applied


def enforceable_members(verified):
    """
    The verified list, but ONLY if it is long enough to be the whole category.

    Run 45 is why this exists. The membership gate produced a list for the
    first time ever - and cross-source consensus on "every fighting style
    worldwide" agreed on exactly one: Muay Thai. There are hundreds and no two
    sources name the same ones, so a short list is the honest outcome.

    That one word was then used to judge eleven scenes. Everything that was
    not Muay Thai became a hard "not-a-member": 15 findings, 7 of 11 scenes
    gagged, and seven of twelve frames came out black. The gate had not caught
    ten lies - it had failed to establish the category, and the failure was
    recorded as a verdict. 4.21's mistake in a new place.

    topics.MIN_MEMBERS already encodes the bar: below three there is nothing
    to explain one by one, so below three there is nothing to hold a script
    against either. Under it the list still reaches the WRITER as guidance -
    draft() uses it - it just stops being something the red team can fire.
    """
    return list(verified) if len(verified or []) >= topics.MIN_MEMBERS else None


def keeps_scenes(old, cand, what):
    """
    True if `cand` has not thrown scenes away. ONE definition, used by every
    stage that can replace the script.

    Run 39 drafted 11 scenes and shipped 2, and the reason was the same in
    three separate places: each stage accepted a candidate on a count of
    PROBLEMS, and deleting a scene removes its problems. A shorter script
    wins every one of those comparisons.

    They each had their own acceptance test, which is how one hole became
    three. This is the rule; call it from all of them.
    """
    before = len(old.get("scenes") or [])
    after = len(cand.get("scenes") or [])
    if after >= before:
        return True
    print(f"      {what} DROPPED {before - after} scene(s) "
          f"({before} -> {after}) - rejected. A shorter script always scores "
          f"better; that is not an improvement.")
    return False


def _snapshot(data, sources, why):
    """
    Write script.json NOW, so a timeout cannot cost the draft as well.

    Run 38 was killed at 45 minutes inside brain with nothing on disk: no
    script, no video, and no way to see how close it had got. Every stage
    after the draft only improves a script that already exists, so a kill
    should cost the improvement, never the script.
    """
    try:
        out = dict(data)
        out["sources"] = sources
        out["incomplete"] = why      # absent from the real save at the end
        # Write, THEN swap. json.dump streams, so a value it cannot serialise
        # leaves a half-written file behind - and that half-written file would
        # be sitting where a perfectly good earlier snapshot used to be. A
        # safety net that can destroy the thing it is protecting is worse than
        # none. Build it beside the target and rename only once it is whole.
        with open("script.json.tmp", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace("script.json.tmp", "script.json")
        print(f"      .. saved a usable script.json ({why})")
    except Exception as ex:
        # Never let the safety net be the thing that fails the run.
        print(f"      .. could not snapshot script.json: {str(ex)[:110]}")
        try:
            os.remove("script.json.tmp")     # don't leave the half-written one
        except OSError:
            pass


def shrink(prompt, cap):
    """
    Cut a prompt to `cap` characters by removing the MIDDLE, never the ends.

    Which end matters is not a guess: every prompt in this file puts the
    instructions first and the required output shape last ("Reply with JSON
    ...", the schema, the word counts). Trimming from the tail would delete
    the part that says what to produce, and the model would answer a question
    nobody asked. The middle is the research context - losing some of it
    costs coverage, which is recoverable; losing the instructions costs the
    whole call.

    The cut is marked, so the model is told text is missing rather than
    reading two unrelated halves as one continuous document.
    """
    if len(prompt) <= cap:
        return prompt
    mark = "\n\n[... source material trimmed to fit this provider's limit ...]\n\n"
    keep = cap - len(mark)
    head = int(keep * 0.62)          # instructions + the start of the sources
    tail = keep - head               # the output shape, which must survive
    a, b = prompt[:head], prompt[-tail:]

    # Cut on a LINE BREAK where one is near, not mid-word. A source ending
    # "...the average inseam for a 32 waist is 3" hands the model a number
    # with its digits amputated, and a fact-checker cannot tell a truncated
    # figure from a wrong one. Only snap when a break is close enough that
    # obeying it costs little - otherwise the trim quietly stops being a trim.
    snap = max(200, keep // 20)
    if (i := a.rfind("\n", max(0, head - snap))) > 0:
        a = a[:i]
    if (j := b.find("\n", 0, snap)) > 0:
        b = b[j:]
    return a + mark + b


def _providers():
    """Available LLM backends, best first. Only ones with a key configured."""
    out = []
    if os.environ.get("GEMINI_API_KEY"):
        out.append(("gemini", None))
    if GROQ_KEY:
        out.append(("groq", ("https://api.groq.com/openai/v1/chat/completions",
                             GROQ_KEY, GROQ_MODEL)))
    if CEREBRAS_KEY:
        out.append(("cerebras", ("https://api.cerebras.ai/v1/chat/completions",
                                 CEREBRAS_KEY, CEREBRAS_MODEL)))
    if OPENROUTER_KEY:
        out.append(("openrouter", ("https://openrouter.ai/api/v1/chat/completions",
                                   OPENROUTER_KEY, OPENROUTER_MODEL)))
    return out


def call(prompt, schema=None, retries=2):
    """
    One LLM call, tried across every configured provider before giving up.

    Returns the parsed dict when `schema` is set, else the raw text. There is
    no `grounded` flag any more: web research is done by research.py and fed
    in as context, so this never depends on a provider's search-grounding
    quota - the exact coupling that made every previous run die at stage 1.
    """
    provs = _providers()
    if not provs:
        raise RuntimeError("No LLM provider configured (need GEMINI_API_KEY, "
                           "CEREBRAS_API_KEY or GROQ_API_KEY)")

    # TWO SWEEPS OF THE WHOLE CHAIN, not one.
    #
    # On the first real AI run every provider failed inside the same minute -
    # Gemini answered 503 "high demand" twice, Groq answered 400 then 429 -
    # and one bad minute cost three of four revision passes. The script
    # shipped with 28 unverified claims because the loop that would have
    # fixed them could not make a single call. Both of those errors clear on
    # their own; nothing about them justified abandoning the run.
    last = None
    for sweep in range(2):
        if sweep:
            print("   .. every provider failed this minute - waiting 45s and "
                  "trying the whole chain once more", flush=True)
            time.sleep(45)
        try:
            return _call_sweep(prompt, schema, retries, provs)
        except RuntimeError as e:
            last = e
    raise RuntimeError(f"All LLM providers failed twice. Last error: {last}")


def _call_sweep(prompt, schema, retries, provs):
    last = None

    # Skip providers still cooling off - but NEVER skip all of them. If every
    # provider is parked the run is over, and a stale cooldown is a far worse
    # reason to die than one more wasted 429. When they are all cold, try them
    # all: this is the last chance, not an optimisation.
    now = time.time()
    live = [(n, c) for n, c in provs if PROVIDER_COOLDOWN.get(n, 0) <= now]
    if not live:
        # WAIT FOR THE COOLDOWN INSTEAD OF DYING INSIDE IT.
        #
        # This used to print "next free in 326s" and then try everything
        # immediately anyway - which failed, because 326 seconds had not
        # passed. Run 47 died 53 seconds into a wait it had just measured,
        # with forty minutes of job time unused. A cooldown that outlives the
        # retry budget is not caution, it is a suicide pact.
        #
        # Bounded, so a stale or absurd cooldown can never hang the job: at
        # most WAIT_OUT_MAX, which is comfortably inside the 45-minute cap
        # even if every stage needed it.
        soonest = min(PROVIDER_COOLDOWN.get(n, 0) for n, _ in provs)
        gap = max(0.0, soonest - now)
        if gap <= WAIT_OUT_MAX:
            print(f"   .. every provider is cooling off - waiting {gap:.0f}s "
                  f"for the soonest one rather than failing now", flush=True)
            time.sleep(gap + 2)
        else:
            print(f"   .. every provider is cooling off and the soonest is "
                  f"{gap:.0f}s away, past the {WAIT_OUT_MAX}s wait budget - "
                  f"trying them all anyway", flush=True)
        live = provs
    elif len(live) < len(provs):
        parked = [n for n, _ in provs if PROVIDER_COOLDOWN.get(n, 0) > now]
        print(f"   .. skipping {', '.join(parked)} - out of quota earlier, "
              f"cooling off")

    for name, conf in live:
        drop_schema = False
        tried_discovery = False
        waited_out_a_limit = False
        model = conf[2] if conf else None

        # Fit the prompt to THIS provider before spending a request finding
        # out it does not fit. Gemini gets the full text; Groq gets as much of
        # it as its per-minute token budget can hold.
        # Reserve room for the JSON instruction _openai_compatible may append
        # AFTER this trim. Without it a prompt cut to exactly the cap comes
        # back over the cap - the trim would be defeated by the fix that
        # follows it.
        cap = PROVIDER_CHAR_CAP.get(name, 10 ** 9)
        body = shrink(prompt, cap - 160 if cap < 10 ** 9 else cap)
        if len(body) < len(prompt):
            print(f"   .. {name} caps requests at ~"
                  f"{PROVIDER_CHAR_CAP[name]:,} chars; trimmed the prompt from "
                  f"{len(prompt):,} to {len(body):,}")

        attempts_left = retries
        spent_schema_drop = False
        attempt = -1
        while attempts_left > 0:
            attempt += 1
            attempts_left -= 1
            try:
                if name == "gemini":
                    text = _gemini(body, schema, drop_schema)
                else:
                    base_url, key, _ = conf
                    text = _openai_compatible(body, schema, base_url, key,
                                              model, name, drop_schema)
                if not text:
                    raise RuntimeError("empty response")
                PROVIDER_USE[name] = PROVIDER_USE.get(name, 0) + 1
                TRIMMED_CALLS[1] += 1
                TRIMMED_CALLS[0] += len(body) < len(prompt)
                return json.loads(strip_fences(text)) if schema else text

            except Exception as e:
                last = e
                msg = str(e)
                print(f"   ! {name} attempt {attempt+1}/"
                      f"{attempt + 1 + attempts_left}: {msg[:200]}")
                # "failed to validate json" is named EXPLICITLY. It matched
                # before only through the word "invalid", which appears in
                # this body once - inside the unrelated type name
                # `invalid_request_error`. A safety branch that fires on an
                # incidental substring of a field it is not reading is one
                # provider wording away from never firing again.
                if schema and not drop_schema and (
                        "schema" in msg.lower() or "invalid" in msg.lower()
                        or "failed to validate json" in msg.lower()):
                    print("     -> dropping response_schema, retrying plain JSON")
                    drop_schema = True
                    # DO NOT SPEND AN ATTEMPT ON THE DIAGNOSIS.
                    #
                    # With retries=2 the 400 arrived on attempt 1, so the
                    # schema-less retry WAS attempt 2 - the last one - and a
                    # single empty reply then abandoned a provider that was up
                    # and answering. Runs 49 and 50 both died exactly there.
                    # Changing the request shape is not an attempt at the
                    # request; it is what makes the next attempt different.
                    # Granted once per provider, so this cannot loop.
                    if not spent_schema_drop:
                        spent_schema_drop = True
                        attempts_left += 1
                    continue

                # A 403 carrying "error code: 1010" is CLOUDFLARE, not the
                # provider: the request was banned at the edge on its browser
                # signature and never reached the API, so the model id is
                # irrelevant and discovery would hit the same wall. Say so
                # plainly - reading this as a permissions problem once already
                # cost a run and a wrong fix.
                if "1010" in msg:
                    print(f"     -> blocked by Cloudflare at the edge (code 1010), "
                          f"not by {name}. The request never reached the API. "
                          f"This means the User-Agent is being refused.")
                    break

                rate_limited = ("429" in msg or "RESOURCE_EXHAUSTED" in msg
                                or "rate limit" in msg.lower()
                                or "quota" in msg.lower())
                too_large = "413" in msg or "too large" in msg.lower()

                # A REFUSED SIZE MUST CHANGE THE SIZE.
                #
                # This branch did not exist: `too_large` was computed and then
                # never acted on. Groq's 413 carries the words "rate limit"
                # and code `rate_limit_exceeded`, so it was read as a
                # rate limit, waited out for 20 seconds, and re-sent BYTE FOR
                # BYTE. A limit on how big a request may be does not clear by
                # waiting - only by sending less - so that retry could not
                # ever have worked, and the run died with a provider that was
                # up, keyed and willing.
                #
                # The provider usually names both numbers ("Limit 8000,
                # Requested 8373"), which is a real measurement of our own
                # tokens-per-character on this exact prompt - far better than
                # the estimate in PROVIDER_CHAR_CAP. Use it when it is there,
                # and halve blindly when it is not.
                if too_large:
                    m = re.search(r"Limit (\d+), Requested (\d+)", msg)
                    if m:
                        limit, want = int(m.group(1)), int(m.group(2))
                        # Aim at 60% of the limit: the reply is charged to the
                        # same budget, and other calls in this minute share it.
                        target = int(len(body) * (limit * 0.60) / want)
                    else:
                        target = len(body) // 2
                    target = max(1500, min(target, len(body) - 500))
                    if target < len(body):
                        print(f"     -> too big for {name}: re-sending at "
                              f"{target:,} chars instead of {len(body):,}")
                        body = shrink(body, target)
                        continue
                    print(f"     -> {name} refuses even a minimal prompt, "
                          f"switching provider")
                    break

                # A genuine 403/404 means THIS MODEL is not permitted on this
                # account. A 429 does NOT - it means we are sending too fast.
                #
                # This used to also fire on any message containing the word
                # "model", and Groq's rate-limit text reads "Rate limit reached
                # for model `openai/gpt-oss-120b`", so a 429 triggered model
                # discovery, which then picked the same model that had just
                # failed and retried straight into the identical wall:
                #     429 ... for model `openai/gpt-oss-120b`
                #     -> refused that model; it offers 9. Retrying with
                #        'openai/gpt-oss-120b'
                # Discovery is now limited to real permission errors, and it
                # must return a DIFFERENT model or it is pointless.
                if (conf and not tried_discovery and not rate_limited
                        and not too_large
                        and ("HTTP 403" in msg or "HTTP 404" in msg)):
                    tried_discovery = True
                    avail = [m for m in _oai_available_models(conf[0], conf[1])
                             if m != model]
                    if avail:
                        model = avail[0]
                        print(f"     -> {name} refused that model; retrying "
                              f"with a DIFFERENT one: {model!r}")
                        continue
                    print(f"     -> {name} offers no alternative model "
                          f"(key invalid, or everything blocked)")

                # 503 / UNAVAILABLE / "high demand" is the provider being
                # busy, not the request being wrong. It was falling through
                # to the generic 4-second backoff and burning the provider's
                # whole budget in twelve seconds.
                overloaded = ("503" in msg or "UNAVAILABLE" in msg
                              or "high demand" in msg.lower()
                              or "overloaded" in msg.lower())

                # A per-minute token limit DOES clear, unlike a daily quota,
                # so it is worth one real wait before abandoning a provider.
                #
                # Keyed on "have we waited yet", NOT on attempt == 0. Groq
                # spent attempt 0 on an unrelated schema-validation 400, so
                # when the 429 arrived on attempt 1 this branch was already
                # unreachable and the provider was dropped without ever
                # waiting the limit out.
                transient = overloaded or (rate_limited
                                           and "per day" not in msg.lower())
                if transient and not waited_out_a_limit:
                    waited_out_a_limit = True
                    wait = 20 if rate_limited else 30
                    print(f"     -> {name} is busy, not broken; waiting "
                          f"{wait}s and trying again")
                    time.sleep(wait)
                    continue
                if rate_limited:
                    # It already had its one real wait above and came back
                    # limited anyway, so this is not a busy minute. Park it -
                    # for as long as the limit it actually hit, not a blanket
                    # seven minutes. Run 47 parked Groq's per-MINUTE limit for
                    # seven minutes and then had no provider left to try.
                    low = msg.lower()
                    if "per day" in low or "daily" in low:
                        rest = COOLDOWN_PER_DAY
                    elif "per minute" in low or "tokens per min" in low \
                            or "requests per min" in low or "tpm" in low:
                        rest = COOLDOWN_PER_MINUTE
                    else:
                        rest = COOLDOWN_SECONDS
                    PROVIDER_COOLDOWN[name] = time.time() + rest
                    print(f"     -> {name} is rate/quota limited, switching "
                          f"provider and resting it for {rest}s")
                    break
                time.sleep(4 * (attempt + 1))
        print(f"   .. {name} exhausted, trying next provider")
    raise RuntimeError(f"All LLM providers failed. Last error: {last}")


# ---------------------------------------------------------------- stage 1 ---
def pick_subject():
    """No TOPIC given -> have the model name one concrete subject to search."""
    out = call("""Name ONE specific subject for an explainer documentary where the
obvious explanation turns out to be wrong. Avoid the most over-covered subjects
(Titanic, Chernobyl, Nokia, Blockbuster, Kodak, Theranos).

Reply with the subject as a single short phrase and NOTHING else. No preamble,
no quotes, no explanation.""").strip().strip('"').split("\n")[0][:120]
    print(f"      subject chosen: {out}")
    return out


def plan_queries(subject):
    """Turn the subject into real search-engine queries."""
    raw = call(f"""Write 5 web search queries that would research this subject for a
documentary: "{subject}"

Rules:
- Answer what was actually asked. If the subject names CATEGORIES or TYPES of
  something, query the categories themselves - what they are, how they differ,
  how to tell them apart. Do NOT substitute the history of the subject for the
  subject itself. History is background, not the answer.
- Write queries a search engine handles well: keywords and names, not
  full sentences, no quotes around the whole query.
- Cover different angles, not 5 rewordings of one.

One query per line, 5 lines, nothing else.""")
    qs = [re.sub(r'^\s*[-*\d.)\]]+\s*', '', ln).strip().strip('"')
          for ln in raw.splitlines()]
    qs = [q for q in qs if len(q) > 3][:5]
    return qs or [subject]


# ---------------------------------------------------------------- stage 1 ---
def research():
    """
    Real web research: we run the searches and read the pages ourselves, then
    hand the model actual source text to write from.

    This used to lean on Gemini's built-in google_search tool, which metered
    against a search-grounding quota separate from the normal model quota.
    When that bucket emptied every run died here with a 429 while the plain
    model quota sat untouched. Owning the search removes that single point of
    failure - and the sources are now objects we hold, so the STRICT check
    below tests something real instead of trusting returned metadata.
    """
    print(f"[1/5] web research | mode={MODE}")
    subject = TOPIC or pick_subject()
    queries = plan_queries(subject)
    for q in queries:
        print(f"        ? {q[:76]}")

    context, sources = web.gather(queries, per_query=5, read_pages=True)
    print(f"      {len(sources)} sources, {len(context):,} chars of source text")

    if STRICT and not sources:
        # The old build silently fell back to an ungrounded call here. That is
        # precisely how a script full of confident invented history gets made:
        # every later stage trusts the brief completely, and the brief was
        # never checked against anything.
        raise RuntimeError(
            "Web search returned ZERO sources, so this 'research' would be the "
            "model's memory, not the web. Refusing to build a script on it. "
            "Set repository variable STRICT_FACTS=0 to override, but expect "
            "invented facts if you do.")
    if not sources:
        print("      !! WARNING: no sources - facts in this script are unverified")

    for s in sources[:6]:
        print(f"        - {(s['title'] or s['uri'])[:70]}")

    prompt = f"""You are researching a documentary about: "{subject}"

Below is text pulled from real web pages, just now. Everything you write must
come from THIS text. You are reading sources, not recalling facts.

=== SOURCE MATERIAL ===
{context}
=== END SOURCE MATERIAL ===

Answer what was actually asked. If the subject names CATEGORIES or TYPES of
something, the brief must identify and explain those categories. Do NOT
substitute the history of the subject for the subject itself.

Produce a research brief containing:
- SUBJECT: one sentence
- QUESTION: the single question this film answers, stated plainly
- FACTS: 12-16 concrete verifiable facts - names, dates, numbers, places.
  After each fact cite the source number it came from, like [SOURCE 3].
  If a fact is not supported by the source material above, mark it
  [UNVERIFIED] - or better, leave it out.
- ANGLE: {modes.MODES[MODE]['research']}
- SURPRISES: 3 details from the sources that are rarely mentioned

Accuracy outranks interest. Plain text."""

    brief = call(prompt)
    print(f"      brief: {len(brief.split())} words")
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

    members_rule = ""
    if VERIFIED_MEMBERS:
        members_rule = f"""
THE ANSWER IS ALREADY ESTABLISHED - DO NOT INVENT A DIFFERENT ONE:

{chr(10).join('  - ' + m for m in VERIFIED_MEMBERS)}

Independent sources were checked against each other and agree on exactly
these. Give each one its own scene, in a sensible order, and use it as that
scene's key_term. Do NOT add a member that is not on this list, however
plausible it sounds and however true the sentences about it would be - being
true is not the same as belonging to the category. Do not silently drop one
either.
"""

    prompt = f"""You are an elite documentary scriptwriter. Write the narration for
a high-retention explainer in the style of Lemmino, Vox, or Johnny Harris.
{members_rule}

RESEARCH BRIEF:
---
{brief}
---
{topic_rule}
HARD REQUIREMENTS:
- EXACTLY {SCENE_COUNT} scenes.
- Every scene's "narration" is {MIN_WORDS_PER_SCENE}-{MAX_WORDS_PER_SCENE}
  words, and should land near {WORDS_PER_SCENE}. Count them. This is a real
  constraint, not a guide: the whole film is spoken aloud, so going over on
  every scene makes the finished video minutes longer than asked for.
- Use ONLY facts present in the brief. Anything marked [UNVERIFIED] must be
  cut or softened to a qualitative statement. Do not add facts from memory.

BEATS - assign each scene a beat, in order:
  {modes.MODES[MODE]['beats']}

{modes.craft_rules(MODE)}

FIELDS:
- "narration": exact spoken words, plain prose, no stage directions. This
  string is fed straight to text-to-speech.
- "image_keywords": EXACTLY {KEYWORDS_PER_SCENE} DISTINCT visuals, in the
  order the narration reaches them. These are searched against a real stock
  footage library, so name things that genuinely exist on film: real people
  doing real actions, real objects, real places. Vary macro / wide / person
  / object / environment. 4-9 words each, naming a concrete photographable
  SUBJECT.
  The count matters. Editing cuts roughly every 5 seconds, so a scene of
  this length needs {KEYWORDS_PER_SCENE} of them; supply fewer and the
  system re-runs an earlier search to fill the gap, and the same subject
  visibly appears twice inside one scene. Every entry must be genuinely
  different from the others - not a rewording of the same shot.
  Never write "cinematic", "4k", "moody", "dramatic lighting".
  Good: ["hands stitching a wool lapel", "crowded city street commuters",
         "rack of tailored suits in a shop", "close up of fabric weave",
         "empty tailoring workshop at night"]
- "key_term": the ONE concept this scene is about, 1-4 words, written EXACTLY
  as it appears in this scene's narration - it is matched against the spoken
  words to place an on-screen card, so it must be a literal substring of the
  narration. Not a sentence, not a description: the name of the thing.
  Good: "compound interest", "fixed costs", "the 4% rule"
  Bad: "understanding how interest builds over time"
- "key_fact": ONE short line under that term, max 60 characters, giving the
  single most useful thing to know about it. No period at the end.
  Good: "Interest earning interest on itself"
- "title": under 70 characters, concrete, no clickbait, no ALL CAPS.
- "question": the one question this film answers.
- "description": 2-3 sentences.
- "tags": 8-12 lowercase tags.
- "thumb_headline": the words on the THUMBNAIL. Not the title - much shorter.
  4 words or fewer, 26 characters or fewer, no punctuation. A title is read;
  a thumbnail is glanced at on a phone, so every extra word shrinks the type.
  Name the subject flatly and let the picture carry the rest.
  Good: "Types of Business Expenses" / "Every Type of Phobia"
  Bad: "The 5 Business Expenses That Are Quietly Killing Your Company"
- "thumb_accent": the ONE phrase inside thumb_headline drawn in red, 1-2
  words. It must appear in thumb_headline EXACTLY as written there - it is
  matched against those words, so anything else leaves the headline all
  black. Choose the words carrying the subject, not the filler.
  For "Types of Business Expenses" -> "Business Expenses" (not "Types")."""

    print(f"[2/5] drafting {SCENE_COUNT} scenes (~{WORDS_PER_SCENE} words each)")
    data = call(prompt, schema=SCRIPT_SCHEMA)
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


def split_verdicts(bad):
    """
    CONTRADICTED and MERELY UNCONFIRMED are not the same finding.

    "WRONG" means the sources say otherwise - that is a factual error and it
    has to be fixed before anything ships. "UNSUPPORTED" means the eighteen
    pages we happened to fetch did not mention it, and on a real run the
    notes read "Not mentioned in the sources", "Drop rise is not mentioned".
    That is absence of evidence, and it is much weaker - especially when four
    of that run's searches errored outright, which makes the silence partly
    ours rather than the world's.

    Collapsing the two made the publish gate fire on every run, and a gate
    that always fires is a gate nobody reads. It is the same confusion the
    topic gate had: a failure to verify reported as a verdict.

    Unsupported claims still matter - the runway lesson is exactly that a
    plausible unsourced claim is the dangerous kind - so they are counted,
    reported and sent to the reviser. They just do not, on their own and in
    small numbers, mean the script is wrong.
    """
    wrong, unsupported = [], []
    for line in bad:
        parts = [p.strip().upper() for p in line.split("|")]
        (wrong if any(p.startswith("WRONG") for p in parts[1:3])
         else unsupported).append(line)
    return wrong, unsupported
    return bad


def fact_check(data, chunk=4):
    """
    Independent verification against FRESH web sources, in chunks.

    Two things make this a real check rather than a model re-reading itself:
      1. the search happens now, against the claims as written, so the
         verifier sees text the drafting stage never saw
      2. it is chunked - one call over 16 scenes had to hold ~50 claims at
         once and verified them shallowly

    Each chunk is a search-then-verify pair: the model names what it would
    look up, we actually look it up, then it rules on the claims against
    what came back.
    """
    scenes = data["scenes"]
    all_bad, all_report, total_src = [], [], 0

    for i in range(0, len(scenes), chunk):
        group = scenes[i:i + chunk]
        body = "\n\n".join(f'SCENE {s["scene"]}: {s["narration"]}' for s in group)

        try:
            raw = call(f"""Read this documentary excerpt and list the checkable factual
claims in it - names, dates, numbers, percentages, attributions, "X was the
first" style causal claims.

EXCERPT:
---
{body}
---

For each claim write ONE web search query that would verify it. Keywords and
names, not sentences. Max 6 queries, one per line, nothing else.""")
            queries = [re.sub(r'^\s*[-*\d.)\]]+\s*', '', ln).strip().strip('"')
                       for ln in raw.splitlines()]
            queries = [q for q in queries if len(q) > 3][:6]

            context, srcs = web.gather(queries, per_query=4, read_pages=True,
                                       max_sources=10) if queries else ("", [])
            total_src += len(srcs)

            report = call(f"""Fact-check this documentary excerpt against the source
material below. Judge ONLY against this material - not memory.

EXCERPT:
---
{body}
---

=== SOURCE MATERIAL (fetched from the web just now) ===
{context if context else "(no sources came back)"}
=== END SOURCE MATERIAL ===

Extract every specific factual claim: names, dates, numbers, percentages,
attributions, and causal statements ("X caused Y", "X was the first").

Return ONE LINE PER CLAIM, in exactly this format and nothing else:
SCENE <n> | <claim, short> | VERIFIED |
SCENE <n> | <claim, short> | WRONG | <the correct fact>
SCENE <n> | <claim, short> | UNSUPPORTED | <what you could not confirm>

Be harsh. A confidently stated date or figure the source material does not
support is UNSUPPORTED, not VERIFIED. No preamble, no summary line.""")
        except Exception as ex:
            raise RuntimeError(
                f"Fact-check failed on scenes {i+1}-{i+len(group)}: {str(ex)[:150]}. "
                f"Refusing to ship an unverified script. Set MAX_PASSES=1 and "
                f"STRICT_FACTS=0 to override.")

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
- Every narration stays {MIN_WORDS_PER_SCENE}-{MAX_WORDS_PER_SCENE} words,
  landing near {WORDS_PER_SCENE}. Do not let a rewrite grow the script.
- Add NO fact that is absent from the brief.
- image_keywords: exactly {KEYWORDS_PER_SCENE} real, photographable subjects
  per scene, all genuinely different from each other.
- Each scene's "key_term" must still appear WORD FOR WORD in that same
  scene's narration. If you reword the sentence containing it, either keep
  the phrase intact or change key_term to match the new wording. It is
  matched against the spoken audio to place a card on screen, so a term the
  narration no longer says loses its card silently.
- Keep "thumb_headline" (max 26 characters) and "thumb_accent", and
  thumb_accent must still appear word for word inside thumb_headline.

RESEARCH BRIEF:
---
{brief}
---
SCRIPT:
---
{json.dumps(data, indent=2)}
---
Return the corrected script in the required JSON format."""

    out = call(prompt, schema=SCRIPT_SCHEMA)
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
        # Only "too short" was ever checked, which is why nothing caught the
        # overshoot: run #23 wrote every scene ~40% over and passed validation
        # cleanly, then delivered 8.3 minutes for a 6-minute request. A budget
        # policed on one side is not a budget.
        elif w > MAX_WORDS_PER_SCENE:
            p.append(f"scene {i}: {w} words (max {MAX_WORDS_PER_SCENE}) - "
                     f"trim it; every scene over budget lengthens the video")
        if len(k) < 3:
            p.append(f"scene {i}: {len(k)} image keywords (need >= 3)")
        # A term the narration never says cannot be timed to the voice, so
        # its on-screen card would either be dropped or land at an arbitrary
        # moment. Checked here, ignoring punctuation and case, because the
        # engine matches the same way.
        term = (s.get("key_term") or "").strip()
        if not term:
            p.append(f"scene {i}: missing key_term")
        else:
            flat = re.sub(r"[^a-z0-9]+", " ", s.get("narration", "").lower())
            want = re.sub(r"[^a-z0-9]+", " ", term.lower()).strip()
            if want and want not in flat:
                p.append(f"scene {i}: key_term {term!r} is never spoken in the "
                         f"narration - it must be a literal phrase from it")
        fact = (s.get("key_fact") or "").strip()
        if len(fact) > 70:
            p.append(f"scene {i}: key_fact is {len(fact)} chars (max ~60)")
    # The red phrase must be literally inside the headline, for exactly the
    # reason key_term must be inside its narration: the renderer matches the
    # words to decide what to colour, and a phrase that is not there fails
    # SILENTLY - the thumbnail just comes out all black, with nothing in the
    # log to say why.
    head = (d.get("thumb_headline") or "").strip()
    acc = (d.get("thumb_accent") or "").strip()
    if not head:
        p.append("missing thumb_headline (the words on the thumbnail)")
    elif len(head) > 34:
        p.append(f"thumb_headline is {len(head)} chars (max ~26) - long "
                 f"headlines set small and are unreadable on a phone")
    if head and acc:
        flat = re.sub(r"[^a-z0-9]+", " ", head.lower())
        want = re.sub(r"[^a-z0-9]+", " ", acc.lower()).strip()
        if want and want not in flat:
            p.append(f"thumb_accent {acc!r} is not inside thumb_headline "
                     f"{head!r} - it must be a literal phrase from it")

    t = wordcount(d)
    if t < TOTAL_WORDS * 0.75:
        p.append(f"total {t} words, target ~{TOTAL_WORDS} "
                 f"(~{t/WPM:.1f} min vs {TARGET_MINUTES:.0f} asked for)")
    elif t > TOTAL_WORDS * 1.15:
        p.append(f"total {t} words, target ~{TOTAL_WORDS} - that is "
                 f"~{t/WPM:.1f} min of speech for a {TARGET_MINUTES:.0f} min "
                 f"video. Cut, do not pad.")
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
    out = call(prompt, schema=SCRIPT_SCHEMA)
    return out


def write_run_summary(data, sources, metrics, fact_bad, history):
    """
    Put the important facts on the run's own page, not only in the log.

    Reason this exists: the log is only retrievable from its END, and the
    writing stage runs FIRST. On a real 8-scene run the engine's per-shot
    output pushed everything brain.py printed - the title, the mode, which
    provider actually answered, how many sources came back - past the point
    the log can be read back at all. The one thing hardest to judge was the
    one thing that had become unreadable.

    GitHub renders $GITHUB_STEP_SUMMARY on the run page and keeps it, so this
    survives regardless of log size. Never fatal: a failure to write a
    summary must not fail a run that produced a good script.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        scenes = data.get("scenes", [])
        rows = "\n".join(
            f"| {s.get('scene','?')} | {s.get('beat','?')} | "
            f"{len(s.get('narration','').split())} | "
            f"`{s.get('key_term','')}` | {s.get('key_fact','')} |"
            for s in scenes)
        srcs = "\n".join(f"- [{(s.get('title') or s.get('uri'))[:90]}]({s.get('uri')})"
                         for s in sources[:14]) or "_none_"
        words = wordcount(data)

        # DID THE QUALITY LOOP CONVERGE, OR DID IT JUST RUN OUT OF PASSES?
        #
        # `history` was accepted by this function and never used. That is a
        # shame, because CLAUDE.md section 9 lists "whether the quality loop
        # converges within MAX_PASSES or always spends the budget" as an OPEN
        # QUESTION - and the answer was being measured every single run and
        # then dropped on the floor. Two columns of arithmetic already in
        # memory close a question that has been open for the life of the
        # project.
        if history:
            hrows = "\n".join(
                f"| {h.get('pass','?')} | {h.get('fact_fails','?')} | "
                f"{h.get('craft_fails','?')} |" for h in history)
            first, last = history[0], history[-1]
            moved = (first.get("fact_fails", 0) - last.get("fact_fails", 0),
                     first.get("craft_fails", 0) - last.get("craft_fails", 0))
            verdict_line = (
                "converged - both bars cleared before the budget ran out"
                if last.get("fact_fails") == 0 and last.get("craft_fails") == 0
                else f"spent all {len(history)} pass(es); "
                     f"fact issues {'fell' if moved[0] > 0 else 'did not fall'} "
                     f"by {moved[0]}, craft by {moved[1]}")
            passes = (f"**Revision passes:** {verdict_line}\n\n"
                      f"| Pass | Fact issues | Craft issues |\n"
                      f"|---|---|---|\n{hrows}")
        else:
            passes = "**Revision passes:** none recorded."
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"""## Script: {data.get('title','(untitled)')}

**{MODE} mode** · {len(scenes)} scenes · {words} words (~{words/WPM:.1f} min read)
· {len(sources)} sources · {len(fact_bad)} unresolved fact issue(s)
· {len(grade(metrics))} craft issue(s)

> {data.get('question','')}

**Thumbnail:** {data.get('thumb_headline','(none)')} — red on
`{data.get('thumb_accent','(none)')}`

| # | Beat | Words | Term card | Fact |
|---|---|---|---|---|
{rows}

{passes}

<details><summary>{len(sources)} sources used</summary>

{srcs}

</details>
""")
    except Exception as e:
        print(f"   (run summary not written: {str(e)[:100]})")


# THE SHAPE THE SCOPED REPAIR MUST RETURN.
#
# Run 44's scoped repair died on "empty response" - not the 429 that killed
# every earlier run, a genuinely new failure. It asked with a bare
# {"type": "object"}, which describes nothing, and the same bare schema is
# exactly what made topics.py's extraction come back as {} on that run. A
# schema with no properties cannot oblige a model to produce anything.
SCENE_FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene": {"type": "integer"},
                    "key_term": {"type": "string"},
                    "key_fact": {"type": "string"},
                    "narration": {"type": "string"},
                },
                "required": ["scene", "narration"],
            },
        },
    },
    "required": ["scenes"],
}

VERIFIED_MEMBERS = []


def _set_length(minutes):
    """
    Re-derive every length constant when the scout sets the runtime from the
    material. These are module-level because everything reads them, so they
    have to be rebound together or the prompts and the validator disagree
    about how long the film is.
    """
    global TARGET_MINUTES, TOTAL_WORDS, SCENE_COUNT, WORDS_PER_SCENE
    global MIN_WORDS_PER_SCENE, MAX_WORDS_PER_SCENE, KEYWORDS_PER_SCENE
    TARGET_MINUTES = float(minutes)
    TOTAL_WORDS = int(TARGET_MINUTES * WPM)
    SCENE_COUNT = max(5, min(18, round(TOTAL_WORDS /
                                       (WPM * SCENE_SECONDS / 60))))
    WORDS_PER_SCENE = round(TOTAL_WORDS / SCENE_COUNT)
    MIN_WORDS_PER_SCENE = int(WORDS_PER_SCENE * 0.88)
    MAX_WORDS_PER_SCENE = int(WORDS_PER_SCENE * 1.12)
    secs = WORDS_PER_SCENE / WPM * 60
    KEYWORDS_PER_SCENE = max(6, min(13, round(secs / 5) + 2))


def choose_topic():
    """
    Stage 0. With no TOPIC given, the scout picks one that has survived a
    truth gate and a demand gate.

    This replaces the least trustworthy path in the whole project: a blank
    topic used to make pick_subject() ask the model to name a subject FROM
    MEMORY - no sources, no evidence, no check of any kind - and that was
    what ran by default.
    """
    global TOPIC, MODE, VERIFIED_MEMBERS
    if TOPIC:
        # A GIVEN TOPIC STILL GETS ITS MEMBERSHIP ESTABLISHED.
        #
        # This branch used to return here, and that is the whole reason the
        # membership check has never once fired in anger. Everything
        # downstream - redteam's not-a-member finding, the writer being told
        # what the real members are, the revision loop refusing to ship -
        # is gated on VERIFIED_MEMBERS being non-empty, and it was only ever
        # filled in on the scout path. Hand a topic in, which is what
        # happens whenever the owner picks one, and the entire apparatus
        # silently did nothing. That is the path the runway video was made on.
        #
        # The topic is NOT rejected if the check fails. The owner chose it,
        # and their choice is theirs; the check exists to find out what the
        # real members ARE, not to veto. A failed check is reported loudly
        # and the run continues with no verified list - exactly as before,
        # but now visibly rather than silently.
        print(f"[0/6] topic given: {TOPIC}")
        MODE = MODE or modes.detect_mode(TOPIC)
        try:
            import topics
            v = topics.assess(TOPIC, call, web.gather)
            if v.get("checked"):
                VERIFIED_MEMBERS = v.get("members") or []
                if VERIFIED_MEMBERS:
                    print(f"      members  : {', '.join(VERIFIED_MEMBERS)}")
                    print(f"      agreement: {v.get('agreement')}")
                if not v.get("build"):
                    print("      !! sources do NOT agree this topic has one "
                          "settled answer:")
                    for r in v.get("reasons", [])[:3]:
                        print(f"         - {r}")
                    print("      building it anyway because it was asked for "
                          "- but the script is more likely to invent a list.")
            else:
                print("      !! could not check membership (not a rejection): "
                      f"{'; '.join(v.get('reasons', []))[:150]}")
        except Exception as e:
            print(f"      !! membership check failed to run - {str(e)[:120]}")
        return

    import scout
    probe = measure_yt = dv = ds = None
    if os.environ.get("YOUTUBE_API_KEY", "").strip():
        import youtube as yt
        probe, measure_yt, dv, ds = yt.probe, yt.measure, yt.verdict, yt.score
    else:
        print("      !! no YOUTUBE_API_KEY - the scout can check whether a "
              "topic is TRUE but not whether anyone wants it")

    niche = _env("NICHE", "money, business and self-improvement")
    print(f"[0/6] no topic given - scouting '{niche}'")
    best, _ = scout.next_topic(niche, call, web.gather, probe=probe,
                               measure=measure_yt, demand_verdict=dv,
                               demand_score=ds)
    if not best:
        raise RuntimeError(
            "The scout found nothing worth making: no candidate had both a "
            "real, agreed answer AND an audience. That is a real result, not "
            "a crash. Re-run to generate a different batch, or set TOPIC "
            "explicitly to override.")
    TOPIC = best["topic"]
    VERIFIED_MEMBERS = best.get("members") or []
    MODE = modes.detect_mode(TOPIC)
    if best.get("minutes"):
        _set_length(best["minutes"])
    print(f"      topic    : {TOPIC}")
    print(f"      members  : {', '.join(VERIFIED_MEMBERS)}")
    print(f"      length   : {TARGET_MINUTES:.0f} min / {SCENE_COUNT} scenes "
          f"(set by the material, not by a target)")


def red_team(data, brief, sources_context):
    """
    Stage 6. Attack the finished script, and refuse to ship it while any HARD
    finding stands.

    Separate from fact-checking on purpose: fact-checking catches a wrong
    date, and cannot catch an item that is not a member of the category,
    because every sentence about it may be true. That is the runway failure,
    and it is caught here by holding the script to the member list the scout
    verified BEFORE the script existed.
    """
    import redteam
    for attempt in range(1, 4):
        # A LIST TOO SHORT TO BE THE ANSWER MUST NOT JUDGE THE SCRIPT.
        #
        # Run 45, on "every type of fighting style worldwide", is the first
        # run where the membership gate ever produced anything - 8 sources
        # read, 6 naming members - and cross-source consensus agreed on
        # exactly ONE: Muay Thai. There are hundreds of fighting styles and no
        # two sources list the same ones, so low agreement is the honest
        # result.
        #
        # That one word was then used to judge eleven scenes. Everything that
        # was not Muay Thai became a hard "not-a-member" finding: 15 findings,
        # 7 of 11 scenes gagged, and with fewer than two ungagged members left
        # there was no checklist to draw either - so seven of twelve frames
        # came out black. The gate did not catch ten lies; it failed to
        # establish the category and that failure was recorded as a verdict.
        # 4.21's mistake exactly, in a new place.
        #
        # topics.MIN_MEMBERS already encodes the bar: below three there is
        # nothing to explain one by one, so below three there is also nothing
        # to hold a script against. Under that, the list still reaches the
        # writer as guidance (draft() uses it) - it simply stops being a
        # weapon the red team can fire.
        gate_members = enforceable_members(VERIFIED_MEMBERS)
        if VERIFIED_MEMBERS and not gate_members:
            print(f"      !! only {len(VERIFIED_MEMBERS)} verified member(s) "
                  f"({', '.join(VERIFIED_MEMBERS)}) - too few to be the whole "
                  f"category, so NOT enforcing membership. This is a failure "
                  f"to establish the taxonomy, not evidence the script is "
                  f"wrong.")
        findings = redteam.check(data, gate_members)
        findings += redteam.attack(data, call, sources_context,
                                   gate_members)
        text, hard = redteam.report(findings)
        print(f"\n[6/6] red team, attempt {attempt}/3")
        print("      " + text.replace("\n", "\n      "))
        if not hard:
            print("      PASSED - no hard findings")
            return data, findings
        if attempt == 3:
            print(f"      !! shipping with {hard} hard finding(s) unresolved")
            return data, findings

        fixes = "\n".join(
            f"- [{f.get('kind')}] "
            f"{('scene ' + str(f['scene']) + ': ') if f.get('scene') else ''}"
            f"{(chr(34) + f['quote'] + chr(34) + ' -- ') if f.get('quote') else ''}"
            f"{f.get('detail','')}"
            for f in findings if f.get("severity") == "hard")
        members_rule = ""
        if VERIFIED_MEMBERS:
            members_rule = (
                "\n\nTHE ONLY VALID MEMBERS OF THIS CATEGORY ARE:\n"
                + "\n".join(f"- {m}" for m in VERIFIED_MEMBERS)
                + "\nIf a scene explains anything else as a member, REPLACE it "
                  "with one of these that the script does not yet cover. Do "
                  "not keep it because its sentences are true - being true is "
                  "not the same as belonging.")
        # REPAIR ONLY THE SCENES THAT ARE BROKEN.
        #
        # This used to send json.dumps(data) - the whole script - plus the
        # brief, and ask for the whole script back, in order to fix five
        # sentences. It is stage 6 of 6, so by the time it runs brain has
        # already spent ~20 model calls on research, drafting, three
        # fact-check passes and shape repair. The single most important call
        # in the pipeline was also the LAST and the LARGEST, and it has
        # therefore never once succeeded: runs 38, 39 and 41 all end with
        # "red-team repair failed ... 429". Every video this project has made
        # shipped with hard findings the system had already identified and
        # could not afford to fix.
        #
        # Worse on the fallback: at ~12,000 characters of script the prompt
        # exceeds Groq's cap, and shrink() cuts the MIDDLE - so the repair
        # would have been handed a script with its middle scenes deleted.
        #
        # A finding names its scene. Send those scenes and nothing else.
        # Typically three scenes out of eleven: a quarter of the tokens, a
        # far better-defined job, and the scene count cannot change because
        # the result is merged back by number rather than replacing the
        # script.
        scoped = sorted({f["scene"] for f in findings
                         if f.get("severity") == "hard" and f.get("scene")})
        wanted = [s for s in data.get("scenes", []) if s.get("scene") in scoped]

        try:
            if wanted:
                slim = [{k: s.get(k) for k in
                         ("scene", "beat", "key_term", "key_fact", "narration")}
                        for s in wanted]
                print(f"      repairing {len(slim)} scene(s) "
                      f"({', '.join(str(n) for n in scoped)}) "
                      f"instead of all {len(data.get('scenes', []))}")
                cand = call(f"""Fix every problem listed in these scenes. Change nothing else.

PROBLEMS FOUND BY AN ADVERSARIAL REVIEW:
{fixes}
{members_rule}

Write in plain language a 15-year-old reads without stopping. Short
sentences. No filler, no hedging, no jargon where a common word exists.
Keep each narration between {MIN_WORDS_PER_SCENE} and {MAX_WORDS_PER_SCENE}
words. Each scene's key_term must still appear word for word in its own
narration. Keep the same scene numbers and beats.

SCENES TO FIX:
---
{json.dumps(slim, indent=2)}
---
Reply with JSON: {{"scenes": [{{"scene": <number>, "key_term": "...",
"key_fact": "...", "narration": "..."}}]}} - only the scenes above.""",
                            schema=SCENE_FIX_SCHEMA)
                merged, applied = merge_scene_fixes(
                    data, cand.get("scenes") if isinstance(cand, dict) else None)
                if applied:
                    print(f"      applied {applied} scene fix(es)")
                    data = merged
                else:
                    print(f"      !! the repair changed nothing - "
                          f"keeping the previous draft")
            else:
                # Nothing scene-scoped: the findings are about the script as a
                # whole (reading level, for instance). That genuinely needs the
                # whole script, and it is also the cheap case - there are no
                # per-scene quotes to carry.
                cand = call(f"""Fix every problem listed. Change nothing else.

PROBLEMS FOUND BY AN ADVERSARIAL REVIEW:
{fixes}
{members_rule}

Write in plain language a 15-year-old reads without stopping. Short
sentences. No filler, no hedging, no jargon where a common word exists.
Keep exactly {SCENE_COUNT} scenes and the same beat order, and keep every
narration between {MIN_WORDS_PER_SCENE} and {MAX_WORDS_PER_SCENE} words.
Each scene's key_term must still appear word for word in its own narration.

BRIEF (the grounded summary this script was written from):
---
{brief}
---
SCRIPT:
---
{json.dumps(data, indent=2)}
---
Return the corrected script in the required JSON format.""",
                            schema=SCRIPT_SCHEMA)
                if cand.get("scenes") and keeps_scenes(data, cand,
                                                       "red-team repair"):
                    data = cand
        except Exception as ex:
            print(f"      !! red-team repair failed ({str(ex)[:110]})")
            return data, findings
    return data, findings


def main():
    choose_topic()
    print("=" * 64)
    print(f"  MMM BRAIN | {MODEL} | {TARGET_MINUTES:.0f}min | {SCENE_COUNT} scenes")
    print(f"  topic      : {TOPIC}")
    print(f"  mode       : {MODE}   max passes: {MAX_PASSES}   "
          f"strict_facts={STRICT}")
    if VERIFIED_MEMBERS:
        print(f"  verified   : {len(VERIFIED_MEMBERS)} members from "
              f"cross-source agreement")
    print("=" * 64)

    brief, sources = research()
    data = draft(brief)

    # SAVE A USABLE SCRIPT THE MOMENT THERE IS ONE.
    #
    # Run 38 spent 45 minutes in this stage on the fallback writer and was
    # killed by the job's timeout with NOTHING on disk - no script, no video,
    # no artifact to look at, and no way to tell how close it had got. Every
    # later stage here is an improvement on a draft that already exists, so
    # there is no reason for a kill to cost the draft as well.
    #
    # Overwritten by the real save at the end. This is a floor, not a result.
    _snapshot(data, sources, "first draft")

    history = []
    fact_bad, report = [], ""

    # BRAIN GETS ITS OWN DEADLINE, INSIDE THE JOB'S.
    #
    # The workflow kills the whole job at 45 minutes. Brain had no budget of
    # its own, so an unusually slow stage did not cost a revision pass - it
    # cost the entire video. The quality loop is optional refinement; the
    # script is the deliverable. Stopping early with a slightly rougher
    # script beats being killed with none.
    #
    # 24 minutes leaves ~20 for the engine, which is what a 6-minute build
    # has actually taken.
    deadline = _started + BRAIN_BUDGET_SEC

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
        if time.time() > deadline:
            left = MAX_PASSES - p
            print(f"      OUT OF TIME after {(time.time()-_started)/60:.1f} min "
                  f"- stopping with {left} pass(es) unused so the video still "
                  f"gets built. {len(q_bad)} craft and {len(fact_bad)} fact "
                  f"issues remain.")
            TIME_CUT.append(f"script stopped early with {left} of "
                            f"{MAX_PASSES} revision passes unused - the run "
                            f"was going to exceed its time budget")
            break

        _snapshot(data, sources, f"after pass {p}")

        print("      revising...")
        try:
            cand = revise(data, fact_bad, q_bad, brief, m)

            # A REVISION MAY NOT DELETE SCENES, AND THIS IS NOT A DETAIL.
            #
            # Run 39 drafted 11 scenes and shipped 2. An 8-minute video came
            # out at 78 seconds, because the only thing this test measured was
            # the NUMBER of failures - and the cheapest way to have fewer
            # failing scenes is to have fewer scenes. Deleting the scene the
            # fact-checker complained about scores perfectly every time.
            #
            # The loop was rewarded for throwing the video away. Each pass
            # took the bait: 11 scenes, then findings on 1-3, then on 1-2,
            # then a 222-word script. Nothing anywhere noticed that the
            # deliverable had lost four fifths of itself.
            #
            # So: a revision fixes scenes, it does not remove them. Losing one
            # is an automatic reject however well the remainder scores.
            if not keeps_scenes(data, cand, "revision"):
                pass
            elif len(grade(measure(cand["scenes"]))) <= len(q_bad):
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
            # Deleting a scene deletes its structural problems too, so this
            # comparison is bait in exactly the way the revision loop's was.
            if len(validate(fixed)) < len(shape) and keeps_scenes(
                    data, fixed, "shape repair"):
                data, shape = fixed, validate(fixed)
        except Exception as ex:
            print(f"   !! shape repair failed ({str(ex)[:110]})")

    # Red team LAST, after every other repair has had its turn. It is the
    # only stage that can see the verified member list, so it is the only one
    # that can catch a scene explaining something that does not belong.
    src_ctx = "\n".join(f"[{s.get('title','')}] {s.get('uri','')}"
                        for s in sources[:14])
    data, rt_findings = red_team(data, brief, src_ctx)

    # VALIDATE AGAIN, because the red team is allowed to rewrite narration.
    #
    # validate() runs at stage 5, BEFORE the red team - so anything the
    # repair breaks was checked before it was broken. The specific risk is
    # 4.3: a repaired narration that no longer contains its own key_term.
    # The engine handles that gracefully (no match, no card) which is exactly
    # why it would be invisible - the video would just quietly lose a term
    # card and nobody would know which stage took it.
    #
    # Reported, not repaired. Another repair pass here would be a third model
    # call on an exhausted quota to fix a cosmetic problem, and the publish
    # gate already carries the important verdicts.
    after = validate(data)
    new_faults = [x for x in after if x not in shape]
    if new_faults:
        print(f"\n   !! the red-team repair introduced "
              f"{len(new_faults)} structural problem(s):")
        for x in new_faults[:6]:
            print(f"      - {x}")
        print("      (the engine degrades safely on these - a term with no "
              "match simply gets no card - but the repair caused them)")

    for i, s in enumerate(data["scenes"], 1):
        s["scene"] = i
    final_m = measure(data["scenes"])
    data["sources"] = sources
    data["verified_members"] = VERIFIED_MEMBERS
    data["red_team"] = rt_findings
    data["fact_check"] = fact_bad
    data["quality"] = {"final": final_m, "failing": grade(final_m),
                       "passes": history}

    with open("script.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    write_run_summary(data, sources, final_m, fact_bad, history)

    w = wordcount(data)
    print("\n" + "=" * 64)
    print(f"script.json written")
    print(f"   title    : {data['title']}")
    print(f"   answers  : {data.get('question','')[:70]}")
    print(f"   scenes   : {len(data['scenes'])}  words: {w} (~{w/WPM:.1f} min)")
    print(f"   sources  : {len(sources)}")
    print(f"   density  : {final_m['fact_density']} anchors/100w "
          f"| rhythm sd {final_m['sent_len_sd']} | tells {final_m['ai_tells']}")
    _w, _u = split_verdicts(fact_bad)
    print(f"   unresolved: {len(grade(final_m))} craft, "
          f"{len(_w)} CONTRADICTED, {len(_u)} unconfirmed")

    # IS THIS SHIPPABLE, and if not, say so where it cannot be missed.
    #
    # The first real run ended with 28 claims the fact-checker could not
    # confirm - including invented-looking precision like "a front rise under
    # eight inches" - and announced it in one grey line between two other
    # numbers. A polished video full of unverified measurements is the
    # runway bug wearing a better suit, and it is worse than no video,
    # because it is the version a viewer believes.
    #
    # The run is NOT failed over it. The owner's own rule is that a human
    # decides what gets published; the job here is to make sure they decide
    # knowing this, not to decide for them.
    blockers = []
    wrong, unconfirmed = split_verdicts(fact_bad)
    if wrong:
        # Sources say otherwise. Never shippable, at any count.
        blockers.append(f"{len(wrong)} claim(s) CONTRADICTED by sources")
    # Absence of evidence, weighed rather than counted. A handful of claims
    # our fetched pages did not happen to mention is normal; most of the
    # script unconfirmed means the research did not really land, and that IS
    # worth stopping for.
    claims = max(1, len(fact_bad))
    if unconfirmed and len(unconfirmed) >= 0.34 * claims and len(unconfirmed) >= 6:
        blockers.append(f"{len(unconfirmed)} claim(s) not found in any source "
                        f"we fetched - not proof they are wrong, but too much "
                        f"of the script is unverified to publish")
    # rt_findings is a LIST of finding dicts, not a dict keyed by severity.
    hard_left = [f for f in (rt_findings or [])
                 if f.get("severity") == "hard"]
    if hard_left:
        blockers.append(f"{len(hard_left)} unfixed HARD red-team finding(s): "
                        + ", ".join(sorted({f.get("kind", "?")
                                            for f in hard_left})))
    if not VERIFIED_MEMBERS:
        blockers.append("no verified member list - nothing checked that the "
                        "categories explained are really members of the topic")

    # Who wrote it, and on how much of the research. NOT a blocker - see the
    # note on PROVIDER_USE. A caveat the owner reads is the whole point; a
    # gate that fires on it would be a gate nobody reads (§4.21).
    caveats = list(TIME_CUT)
    cut, total = TRIMMED_CALLS
    if cut:
        caveats.append(f"{cut} of {total} model calls had their prompt "
                       f"trimmed to fit a provider's limit - that much of the "
                       f"research never reached the writer")
    fallback = {p: n for p, n in PROVIDER_USE.items() if p != "gemini"}
    if fallback:
        caveats.append("written partly by the fallback writer ("
                       + ", ".join(f"{p}: {n} call(s)"
                                   for p, n in sorted(fallback.items()))
                       + ") - less grounded than a full Gemini run")
    data["written_by"] = dict(sorted(PROVIDER_USE.items()))
    data["publishable"] = {"ok": not blockers, "blockers": blockers,
                           "caveats": caveats}
    if caveats:
        print("\n  CAVEATS (not blockers - you decide):")
        for c in caveats:
            print(f"    - {c}")

    print("=" * 64)
    if blockers:
        print("!! NOT READY TO PUBLISH")
        for b in blockers:
            print(f"   - {b}")
        print("   The video will still build so it can be looked at.")
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
  
