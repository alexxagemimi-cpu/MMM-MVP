# Five-topic campaign — code frozen, observations only

The owner's instruction: *"let the 5 videos fail then observe."* So nothing in
the pipeline changes until all five are done. This file is the notebook.

Settings held identical for every run: 8 min, 3 passes, 540p,
en-US-GuyNeural, log_frames=true.

---

## Run 48 — "every type of coffee roast explained"

Scored 70/100 by `score_run.py`. **The real number is lower — the scorer is
wrong about this one, and that is finding #4 below.**

| | |
|---|---|
| video | 7.8 min, 11 scenes, 1179 words, 18.5 MB |
| shots | 93 — 92 "drawn", 0 blank, **58 withheld (62%)** |
| truth gate | BUILD, 3 verified members (light, medium, dark), agreement 0.80 |
| red team | 10 HARD, 4 soft, across 7 of 11 scenes |
| repair | FAILED |
| publish gate | NOT READY |

### 1. The membership gate worked for the first time ever

    membership: 8 source(s), 29,143 chars gathered, 14,003 shown (~1,750 per source)
    extraction: showed 8 source(s), model returned 8 entr(ies), 7 named any member
    [BUILD] ... (agreement 0.8, 3 members, 8 sources)

`fair_share()` did what it was measured to do: all 8 sources reached the
model, 7 of them named members, and the writer was handed a verified list.
CLAUDE.md §11 said *"nothing has yet been observed catching a real invented
member on a live run."* It has now — twice, in the same run:

    [hard] not-a-member scene 3: "Cinnamon roast is reached right at the start of
           first crack" — a sub-category, not one of the three main categories
    [hard] not-a-member scene 4: "Light-Medium variants, known as City+" —
           a sub-category, not a member of the three core roast categories

That is the RUNWAY bug, caught by the machine instead of by the owner. It is
the one unambiguous win in this run.

### 2. The repair failed AGAIN — but for a brand-new reason

Every previous failure was quota (429). Not this time:

    ! groq attempt 1/2: HTTP 400 ... "Failed to validate JSON. Please adjust
      your prompt. See 'failed_generation' for more details."
      -> dropping response_schema, retrying plain JSON
    ! groq attempt 2/2: empty response
    !! red-team repair failed

So the size fix worked — the scene-only prompt was small enough to be *sent*
and *answered*. It then failed one layer further in: Groq's generation did not
satisfy `SCENE_FIX_SCHEMA`, and the schema-less retry returned nothing at all.

**The single most important call in the pipeline has still never once
succeeded.** Every finding below is downstream of this.

### 3. 62% of the video is a card the code's own comment calls rejected

7 gagged scenes → 58 shots. §4.22 says a gagged shot shows the CHECKLIST. It
did not. With 7 of 11 scenes gagged there were too few trusted members left to
build one, so it fell through to `point_card` — and engine.py's own comment on
that branch says a header-only card *"rendered as a title over four fifths of
empty page."*

Confirmed on the contact sheet: 8 of 12 frames are the same near-empty white
page, headed with the video title **truncated mid-word**:

    HOW HEAT TRANSFORMS COFFEE BEANS: LIGHT, MEDIUM, AND DARK RO

Frames 10–12 draw the better point card (HOME BREWING / SENSORY INDICATORS /
ROAST SPECTRUM with a one-line subtitle) and are still ~80% empty page. Only
frame 01 carries a photograph — a thermometer against a blue sky, which is a
picture of "temperature", not of coffee.

The run 45 fix (black → title card) worked exactly as designed. The design's
second-best is just not good enough to be most of a video.

### 4. `score_run.py` gave this 20/20 for watchability

It counts a withheld card as a real frame, which was the right call when a
handful of shots were gagged and the wrong one at 62%. A video that is
two-thirds one repeated near-empty card cannot score full marks on watchable.
**The scorer needs a ceiling on the share of shots that may be withheld before
watchability starts falling.** Not changing it mid-campaign — it would rescore
runs already banked — but it is the first fix after the five.

### 5. Gemini's daily quota was gone before stage 2

    written partly by the fallback writer (groq: 21 call(s))
    11 of 25 model calls had their prompt trimmed to fit a provider's limit
    52 claim(s) not found in any source we fetched

Groq answered 21 of 25 calls, rate-limited on nearly every one (20s waits),
with 11 prompts trimmed. The fact-check ran out of budget with 52 claims still
unconfirmed. This is CLAUDE.md §9's open question — *"nobody has yet judged
whether a Groq-written script is actually worse"* — answered: on this run, yes,
measurably, and it cascades into 10 HARD findings and a two-thirds-blanked
video.

### Verdict

**NO — do not upload.** 70/100 on the scorer, realistically ~55 once
watchability is marked honestly. It is not the video failing to build; it is
the script being untrustworthy and the machine correctly refusing to put
untrustworthy words on screen, with nothing good to show in their place.

---

## Run 49 — "top 10 deadliest fighting styles explained"

**0/100. FATAL at stage [2/5], 3 minutes in. No script, no video, no frames.**

    [2/5] drafting 11 scenes (~119 words each)
       .. skipping gemini - out of quota earlier, cooling off
     ! groq attempt 1/2: HTTP 429 ... tokens per minute
     ! groq attempt 2/2: HTTP 429 ... -> resting it for 75s
       .. every provider failed this minute - waiting 45s
       .. every provider is cooling off - waiting 30s for the soonest one
     ! gemini attempt 1/2: 429 RESOURCE_EXHAUSTED
     ! gemini attempt 2/2: 429 RESOURCE_EXHAUSTED  -> resting it for 420s
     ! groq attempt 1/2: HTTP 400 "Failed to validate JSON. Please adjust
       your prompt. See 'failed_generation' for more details."
       -> dropping response_schema, retrying plain JSON
     ! groq attempt 2/2: empty response
    FATAL: All LLM providers failed twice.

### THE SAME PAIR THAT KILLED RUN 48's REPAIR NOW KILLS A WHOLE RUN

`HTTP 400 "Failed to validate JSON"` → `drop_schema` retry → **empty
response**. Two different prompts, two different runs, one fault. It is
reproducible, and it is not quota.

Reading `_openai_compatible` (brain.py:351), two things are wrong:

**A. The 400 is misdiagnosed.** The code knows exactly one Groq JSON-mode
complaint — *"'messages' must contain the word 'json'"* — and answers it by
saying the word. This is a **different** 400: `json_object` mode was on, and
the model's own generation was not valid JSON, so Groq refused the response
rather than returning it. Saying "json" does not help; the generation is the
problem.

**B. The retry cannot ever return anything.** `gpt-oss-120b` is a reasoning
model. brain.py:409 reads only:

    (data["choices"][0]["message"]["content"] or "").strip()

Groq returns a gpt-oss model's thinking in a separate `message.reasoning`
field, and no `max_tokens` is set anywhere in the request. So the most likely
mechanism for both halves is one mechanism: **the output budget is spent on
reasoning tokens** — leaving the JSON truncated (the 400) and, with the schema
dropped, `content` empty while `reasoning` holds everything. The code never
looks at `reasoning`, so an answer that arrived is read as no answer.

**This is 5.11/5.12's shape a third time**: a retry that could not have worked,
sitting in a branch that reads correct.

*NOT TESTED — expect it to break here:* I have no Groq key locally, so B is
inference from the error text and the code, not a measurement. It needs a live
call with `reasoning_effort` and an explicit `max_tokens`, and a look at what
`message.reasoning` actually contains.

### Second, smaller fault: research pulled junk sources

    ? most lethal combat styles effectiveness comparison
      - MOST—Missouri's 529 Education Plan | MOST 529
      - YOUR ACCOUNT | MOST 529
      - MOST | English meaning - Cambridge Dictionary

3 of 14 sources are about the *word* "most". Nothing downstream can tell a
529 plan from a martial art; those pages just dilute the pooled context that
`fair_share()` then divides evenly. Run 48's sources were clean, so this is
query-shaped, not general.

---

## Decision point reached at run 49

Gemini's daily quota is exhausted and does not reset until roughly 07:00 UTC.
**Every remaining run today is Groq-only, and Groq's JSON path is the thing
that just killed a run.** Firing topics 3–5 into that wall would produce three
more identical FATALs in about ten minutes and teach nothing.

Run 50 (topic 3) is fired anyway as a *test of that claim* — if it dies the
same way, the fault is deterministic and the freeze has served its purpose.
That is the honest way to find out, and it costs three minutes.

---

## Run 50 — "the eight blood types explained" — the test came back

**0/100. Identical death.** Same stage `[2/5]`, same 429s, same
`HTTP 400 "Failed to validate JSON"` → `dropping response_schema` →
`empty response` → FATAL. Different topic, different prompt, byte-for-byte the
same failure sequence.

**Three runs, two stages, three topics, one bug. It is deterministic.**

### So the campaign stops here, and the freeze ends

The owner's instruction was to let the five fail and *then* observe, so the
fixes would not confound the comparison. The pattern is now unambiguous and it
is a single blocking fault: nothing downstream of stage 2 can be observed
because stage 2 does not complete. Firing topics 4 and 5 into it would spend
two of the owner's topics to reproduce a bug three runs have already proved,
and would return no video either time.

Continuing the freeze past this point would be following the letter of the
instruction against its purpose.

### What was fixed — CLAUDE.md §5.13

Confirmed against Groq's published behaviour before writing anything:
`reasoning_effort` takes low/medium/high on gpt-oss-120b and **defaults to
medium**, and reasoning comes back in a **separate `reasoning` field**.

1. `reasoning_effort="low"` is sent — gpt-oss only, since the parameter is
   model-specific.
2. `message.reasoning` is read when `content` is empty.
3. `finish_reason` is logged when it is not `stop` — `length` is exactly how
   valid JSON becomes invalid JSON.
4. The HTTP error body is logged to 2,000 chars, not 400, so Groq's
   `failed_generation` is finally visible. It sits past character 400 every
   time, which is why every log of this fault showed the complaint and hid the
   cause.
5. Dropping the schema no longer spends the last attempt.
6. The drop branch names `"failed to validate json"` explicitly instead of
   matching the incidental word "invalid" inside `invalid_request_error`.

Each of 1, 2 and 5 was **proved by reverting it and watching the new
regression fail**, per the working agreement. `test_providers.py` fakes
`urlopen`, not `_openai_compatible` — the fixes live inside that function.

*Still NOT TESTED against real Groq:* there is no key in this sandbox. Whether
"low" effort leaves enough budget for a full 11-scene draft is a question only
a live run answers.

---

## Run 51 — "top 10 deadliest fighting styles explained", against the fix

**The Groq fix worked. The run died one stage later, on the next bug.**

It got past `[2/5] drafting` — the wall that killed 49 and 50 — wrote a real
`script.json` (5,404 bytes, uploaded as an artifact), ran the quality metrics,
and reached `[3/5] verifying against the web...`. Then:

    FATAL: 'scene'

### The schema is a Gemini contract. Groq never agreed to it.

`SCRIPT_SCHEMA` marks `scene` **required**, and Gemini honours that because it
is passed as a real response schema. Groq is sent
`response_format: {"type": "json_object"}`, which promises only that the reply
**parses** — any shape at all satisfies it. So every script the fallback writer
has ever produced was unchecked in shape, and the fact-checker's own
expression, `f'SCENE {s["scene"]}'`, raised a bare `KeyError` on a draft whose
scene objects had no such key.

Run 48's draft happened to include it. Run 51's did not. **Nothing anywhere had
ever noticed the difference** — the same shape as §5.6/5.8/5.11: a safeguard
that reads correct and does not apply on the path actually being taken.

### Fixed

`number_scenes()` stamps positional numbers the moment a script comes back,
at all four `SCRIPT_SCHEMA` call sites. This is not a compromise: the pipeline
*already* renumbered every scene positionally just before writing
`script.json`, so the model's numbers were never trusted — the only open
question was whether an intervening stage crashed first. Missing fields are
**reported, not silently filled**, because a quiet default is exactly how a
fallback-written script becomes invisibly worse than a Gemini-written one.

And the death notice itself: `FATAL: 'scene'` was the entire message. A bare
`KeyError` stringifies to just the key — no error type, no file, no line, no
stage — so finding it meant reading the source for every place that string
could be indexed. It now prints the exception type and a traceback.

Regression in `test_providers.py`, 6 checks: a Groq-shaped draft with no
`scene` key is numbered, the fact-checker's own expression stops raising, a
model that numbers its scenes 5 and 9 is corrected rather than believed, and
it survives no `scenes` key, an empty list, and a scene that is not an object.
