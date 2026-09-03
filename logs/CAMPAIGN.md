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
