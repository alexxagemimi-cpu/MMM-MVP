# MMM Factory — project memory

Read this before changing anything. It is the accurate state of the repo,
not the plan. Where an earlier handoff document described files like
`modes.py`, `run_local.py` or a shot-level engine rewrite, those had never
reached this repository — they were written in a session whose work was
never pushed. Some have since been rebuilt here; this file says which.

---

## 1. What this is

**MMM = "Money Making Machine."** A private, zero-budget automation project.
MVP #1 is an automated YouTube explainer-video pipeline.

Owner: vibecodes — directs the work and judges the output, does not write the
code. Explain in plain English, not jargon.

**Budget is ₹0 and no card on file. This is a hard constraint, not a
preference.** Any dependency that needs a payment method is disqualified,
however good it is.

Compute is **GitHub Actions**. A human QA gate before publishing is the
owner's own decision — never automate the upload.

**First channel niche: money / business / self-improvement.** This matters
for visuals: unlike a hardware channel, there is nothing to photograph. Stock
footage of an office carries no information, so the **words on screen are the
content**. That is why term cards exist (§4).

---

## 2. Files

| File | Purpose | State |
|---|---|---|
| `brain.py` | Script writing: research → draft → fact-check → revise → validate | Runs green end-to-end on CI |
| `research.py` | Web search + page reading, independent of any LLM | Verified working on CI |
| `modes.py` | story / explainer / guide: beats, craft rules, per-mode metrics | Wired into brain.py; detector 12/12 |
| `engine.py` | Video assembly: TTS, visuals, Ken Burns, captions, term cards, mix | Runs green; produces real videos |
| `test_engine_local.py` | Runs the REAL engine against REAL ffmpeg locally, ~40s | Passing |
| `.github/workflows/factory.yml` | The workflow. Has a no-AI engine-only test mode | Stable |

---

## 3. How the pipeline actually works

**Script** (`brain.py`): detect mode from topic → pick search queries → run
them via `research.py` → read the result pages → write a brief from that
source text → draft → fact-check against *fresh* searches → revise → validate.

**Video** (`engine.py`): Edge-TTS per scene with real word timings → per shot,
Pixabay video → Pixabay photo → AI image → slate → render each shot → assemble
each scene in ONE ffmpeg pass → concat → burn karaoke captions + term cards,
mix ducked music, normalise loudness once.

**Research is NOT done by the model's built-in search tool.** That is
deliberate and load-bearing — see §5.1.

---

## 4. Design decisions that are load-bearing (do not "simplify" these)

**4.1 Explainer mode has NO `fact_density` floor.** Measured on hand-written
samples of the real failure case:

    invented-history sample : fact_density 7.21
    true-taxonomy    sample : fact_density 0.00

The fabricated script scores *higher*. A floor on that metric pushes the
writer to invent names and dates — optimising directly into the disease it
was meant to catch. Do not add one.

**4.2 Three modes, not one.** One narrative arc forced onto every topic, with
research told to find a "reversal", is what produced confident fake history
for "types of men's fashion": asked for a shape the material does not contain,
the model manufactures the shape. The fake-history bug and the one-size-arc
bug are the same bug.

**4.3 `key_term` must be a literal phrase from its own narration.**
`validate()` rejects it otherwise. A term the narration never says cannot be
timed to the voice, so its card would land arbitrarily. **No match means no
card** — a mistimed card is worse than none.

**4.4 Term cards use ASS `BorderStyle=3` (opaque box).** The renderer measures
the glyphs and sizes the background itself. A previous attempt hardcoded a
560px box and long definitions ran off the edge of their own background. This
removes the failure mode rather than fixing its arithmetic.

**4.5 `loudnorm` runs ONCE over the finished programme, never per scene.**
Per-scene normalisation re-levels every scene against itself, flattening the
loud/quiet contrast between scenes that makes narration sound edited. It also
hung (§5.2). `I=-14` is YouTube's own target; quieter and the platform leaves
the file alone and it just plays quieter than everything beside it.

**4.6 Every ffmpeg call is time-bounded, and budgets are sized against the
45-minute job cap.** A flat 240s per shot multiplies to 48 minutes across 12
shots — past the cap, so a bad run would still die by timeout. `shot_timeout()`
scales with output length instead.

**4.7 No unbounded ffmpeg inputs, ever.** No `-stream_loop -1`, no bare
`apad`. Loop counts and pad durations are computed. Two separate multi-hour
hangs came from this pattern.

**4.8 Durations are computed, never inferred.** Scene length comes from our
own frame budget (`frames / FPS`), not from probing a file and not from
`-shortest`. Every hang in this file traces to asking ffmpeg to work out a
duration itself.

---

## 5. Real bugs found here (do not reintroduce)

**5.1 Grounding welded to one provider's quota.** Research used Gemini's
built-in `google_search`. That quota (~5k/month) is metered *separately* from
normal model calls, so when it emptied every run died at stage 1 with a 429
while the plain model quota sat untouched. A fresh key from a new AI Studio
project failed identically — **the limit is account-wide, not per-project.**
Fixed by owning the search (`research.py`).

**5.2 The multi-run hang.** Cost six CI runs. Four wrong theories in order:
`-shortest`, a missing `-t`, timestamp inheritance, stream copying. The actual
cause was **`loudnorm` stalling on one particular narration clip** — 73s and
killed, versus 1.5s for the same scene without it. It was found not by a fifth
theory but by making the audio chain *degrade through tiers and log which tier
worked*.

**5.3 `image_keyword` vs `image_keywords`.** engine.py read the singular key,
which never existed. Every image silently fell back to a generic placeholder.
No error — the worst kind of bug.

**5.4 Fetched asset and rendered output shared a filename.** ffmpeg would read
and write the same file for every stock-footage shot. Caught by a mocked
end-to-end run before it ever shipped.

**5.5 Sentence boundaries mistaken for word boundaries.** Edge-TTS emits both
in the same shape, so a whole sentence arrived as one timed "word" — 4 caption
cues for a 4-scene script, whole sentences dumped on screen, nothing for the
karaoke highlight to step through. `split_multiword()` splits them back.

**5.6 A module built and never wired in.** `modes.py` was written, tested and
left orphaned; brain.py kept grading every topic on story rules. Caught only
by reading a real run's log and noticing an explainer being asked for
`fact_density >= 5.0`. **Check that new modules are actually imported.**

**5.7 The output looked like an advert.** The visual style string literally
asked for "low-key moody lighting, volumetric haze, muted desaturated teal and
amber" — a description of a commercial. `STYLE=explainer` is now the default.

**The pattern under most of these:** they shipped because they were reasoned
about and never *run*, with CI used as the test suite at ~5 minutes a cycle.
That is what `test_engine_local.py` exists to end.

---

## 6. Working agreement

- **Correct → Verified → Right layer → Fast.** Speed is last.
- Hand nothing over without either real command output showing it working, or
  the label `NOT TESTED — expect it to break here: <where>`. "Should work" is
  banned.
- **Look at the artifact, not the logs.** Probe the file, extract frames, read
  the actual script. The advert-look problem and the see-through term card
  were both found by looking at a rendered frame.
- Test failure paths, not just the happy path. Nearly every bug above was one.
- After any change, ask what that change itself could have broken.
- Never invent an unvalidated proxy for "good" and optimise toward it —
  `fact_density` is the cautionary example. Validate a new metric against
  hand-written known-good and known-bad samples and show both scores.
- Say the gap in the same message as the handover, not when asked.
- Urgency does not lower the bar. State what would be skipped, then do it
  their way.
- The owner's stated premise gets checked, not absorbed.

---

## 7. Keys and setup

GitHub → Settings → Secrets and variables → Actions.

| Secret | Needed? | Notes |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Free tier. Daily quota is real and *does* run out mid-project. |
| `PIXABAY_API_KEY` | Recommended | Free, no card. Without it, visuals fall back to AI images. |
| `GROQ_API_KEY` | **Recommended** | Free, no card. The fallback writer. Without it, a Gemini quota wall kills the run outright. |
| `TAVILY_API_KEY` | Optional | Better search. Not required — keyless DuckDuckGo is confirmed working on the runner. |
| `OPENROUTER_API_KEY` | Optional | Last resort; only ~50 requests/day free. |

Repo *variables*: `STYLE` (explainer/cinematic), `GEMINI_MODEL`, `GROQ_MODEL`,
`STRICT_FACTS`, `MAX_PASSES`.

**Do not use Cerebras as the primary fallback**: its no-card free tier ended
in August 2026 and now needs a verified payment method, which breaks the ₹0
rule. Which models are free *rotates* (DeepSeek R1 was free on OpenRouter
until July 2026), which is why every model id is an env var.

---

## 8. Testing

    python3 test_engine_local.py     # real engine, real ffmpeg, ~40s
    python3 modes.py                 # mode detector against its test set

Workflow test mode: run with `skip_brain_test_fixture=true` to render a
hand-written script with **no AI quota at all**. Use it for any engine or
visual change.

---

## 9. Known-untested / open

- Term cards over real stock footage at full video length (verified on
  rendered frames and on a 4-scene fixture only).
- Whether the quality loop converges within `MAX_PASSES` or always spends the
  budget.
- Pixabay behaviour across a long video's worth of requests (~45 shots).
- Nothing yet judged by the owner as publishable. That is the real bar.
