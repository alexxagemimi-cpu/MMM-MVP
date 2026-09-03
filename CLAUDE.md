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
| `research.py` | Web search + page reading, independent of any LLM | Tavily → DDG → Wikipedia |
| `modes.py` | story / explainer / guide: beats, craft rules, per-mode metrics | Wired into brain.py; detector 12/12 |
| `engine.py` | Video assembly: TTS, visuals, Ken Burns, captions, term cards, mix | Runs green; produces real videos |
| `graphics.py` | The on-screen system: white cards, persistent header, animated list | Wired in; verified on frames |
| `sfx.py` | Synthesised sound kit — whoosh/pop/tick/thud/riser, no key, no quota | Wired in; levels measured |
| `thumbnail.py` | Grid-style thumbnail drawn from script.json | Wired in |
| `scout.py` | Chooses the topic: 20 candidates → triage → demand → truth gate | 6 model calls, not 21 |
| `topics.py` | Truth gate: do independent sources agree the list is real? | Tested on known good/bad |
| `youtube.py` | Demand gate: does anyone actually search for this? | Free API, gated on kill rules |
| `redteam.py` | Attacks the finished script; "not-a-member" is a hard finding | 3/3 runway regression |
| `scriptbits.py` | Pulls real lists and figures back out of narration | 17/17 on good + bad samples |
| `test_relevance.py` | Stock-relevance check vs tags from REAL runs | 67/68, incl. the retriever and the Nikon |
| `verify.py` | A model WATCHES the finished video and reports edit faults | 9/9 offline; refusal path proven live |
| `contact.py` | 12 frames of the finished video on one JPEG, plus measurements | **How faults get found** |
| `test_providers.py` | The fallback writer, against a provider that refuses on size | 19/19 |
| `test_distrust.py` | A hard-flagged claim must never reach a card | 10/10 |
| `test_wiring.py` | Nothing is computed, logged, and then ignored | 4 checks, each proved by reintroducing its bug |
| `test_engine_local.py` | Runs the REAL engine against REAL ffmpeg locally, ~50s | Passing |
| `.github/workflows/factory.yml` | The workflow. Has a no-AI engine-only test mode | Stable |

---

## 3. How the pipeline actually works

**Script** (`brain.py`): detect mode from topic → pick search queries → run
them via `research.py` → read the result pages → write a brief from that
source text → draft → fact-check against *fresh* searches → revise → validate.

**Video** (`engine.py`): Edge-TTS per scene with real word timings → per shot,
a drawn list card where the script has one, else Pixabay video → Pixabay photo
→ drawn card → slate → render each shot → assemble
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

**4.9 The section card is a BEAT, with a fixed dwell — never a share of the
scene.** On an equal split with two shots it took half the scene: measured on
a finished 40s build sampled every 0.5s, **51% of the video was one card that
never changed a pixel.** It is the one shot with no inherent motion, so it is
the one that must not be long. Fixed at 2.4s (3.6s opening) → re-measured 29%.

**4.10 Only what changed moves.** The opening card animates its rows arriving;
every card after it animates *only* the tick landing and the box moving. The
reference video's single structural lesson is that elements accumulate on a
stable canvas — replaying the build animation at every section contradicts it.

**4.11 Only CATEGORY (explainer) and STEP (guide) beats are list items.**
The checklist used to be built from every scene's `key_term`, which is exactly
how RUNWAY reached the screen as a type of business expense — it was the CLOSE
beat. A story has neither beat and correctly gets no checklist at all. A CLOSE
also inherits no section header, or the wrap-up gets labelled "3 OF 3".

**4.15 The cards are a designed system, not four greys and a grid.** Owner's
words: "organised and good but not so much well designed and elegant like best
channels." Pure #FFF on #111 is what a first draft looks like — print has never
used pure white or pure black. PAPER is warmed, INK pulled off black. Every
list row is a *surface* with a hairline edge and a state tint (green done, red
live with a solid keyline, flat tint to come); before, only the live row had a
box so the others read as absences. Short lists are ONE column of full-width
rows, centred in the usable band — a 2-column grid left a three-item list with
a quarter of the frame empty and the items floating in it.

**4.16 `_layout()` is the single definition of where rows go.** The still card
and both animations measure from it. They each had their own copy, and the
moment the card's layout changed the animations went on cropping the old
rectangles and rendered rows chopped off half-way across the frame.

**4.13 When the stock library has nothing relevant, DRAW the shot.** Not an
AI image, not a slate. Measured on a real run: with the old check, 15 of 15
clips passed and the video showed a golden retriever under "fixed costs,
variable costs and one-off costs" and a crop sprayer under "materials,
packaging, payment processing". §1 already said why — there is nothing to
photograph in this niche. `scriptbits.list_items()` pulls the list the writer
already wrote ("Rent, salaries, insurance, software") and it goes on screen as
bullets: already written, already fact-checked, already spoken aloud, so it
cannot be off-topic. It finds nothing in prose, on purpose — a wrong bullet is
worse than no bullet.

**4.21 CONTRADICTED and UNCONFIRMED are different findings.** "WRONG" means
the sources say otherwise — never shippable, at any count. "UNSUPPORTED" means
the pages we happened to fetch didn't mention it, which on a real run read
*"Not mentioned in the sources"* while four of that run's searches had errored
outright. Collapsing them made the publish gate fire on every single run, and
**a gate that always fires is a gate nobody reads.** Same confusion as §5's
worst bug: a failure to verify reported as a verdict.

**4.20 The comparison card is the first DIAGRAM.** Everything else here is a
list — it shows *what* the things are, never how they relate. A taxonomy is
mostly about the difference between neighbours, and a difference is a picture.
Fires on the EDGE/APPLY beat (where `modes.py` puts "how these differ") with
the two most recently explained members, and both columns are the writer's own
`key_term` + `key_fact`, already fact-checked. **A made-up comparison is a
made-up fact carrying a diagram's authority** — so it never invents a
relationship, it only draws one the script already stated.

**4.19 Numbers get their own card.** `scriptbits.headline_number()` finds a
real figure (money, %, multiple, span) in a scene's narration and
`graphics.stat_clip` puts it on screen alone and large. Digits only, and years
excluded — "three kinds" is a sentence, not a statistic, and a card reading
"3" would be noise. A list beats a number where a scene has both.

**4.18 Every drawn card MOVES.** The section card animates its tick; the
content cards animate their bullets arriving, one at a time, over the first
70% of the shot. A card held still for five seconds is the same hole as §4.9
and it reopened once already on a different code path. `contact.py` now runs
ffmpeg `freezedetect` on every build and prints any motionless stretch ≥3.5s,
so the third time is caught by a machine instead of by eye.

**4.17 A scene with a real list SPENDS a shot showing it.** Not as a fallback
— as the plan. Two runs in a row drew zero cards: the video filter rejected
clip after clip and the photo step then found something every single time, so
the drawn card never once reached the screen while narration listed the four
things that mattered. Drawing only when Pixabay comes back empty treats the
card as a failure state; in this niche it is the better answer. Shot index 1
(right after the section card, where the list is spoken) carries it. **Guarded
at ≥3 shots** — with two, the scene would be section card + list card, a still
held ~7s, which is §4.9 again.

**4.14 One thing says a thing once.** The term card is suppressed where a
drawn card or the checklist already names the term, and clipped to the moment
a drawn card takes the screen. Three separate rendered frames showed the same
words printed twice on one screen, once literally on top of themselves.

**4.25 The red-team repair fixes SCENES, not the script.** It is stage 6 of 6,
so it runs when the quota is most depleted — and it used to send the whole
script plus the brief and ask for the whole script back, to fix a handful of
sentences. Runs 38, 39 and 41 all end `red-team repair failed … 429`. **The
single most important call in the pipeline was also the last and the largest,
so it has never once succeeded**, and every video this project has made
shipped with hard findings the system had already identified. Worse on the
fallback: at ~11,000 characters the prompt exceeds Groq's cap and `shrink()`
cuts the MIDDLE, so the repair would receive a script with its middle scenes
deleted. A finding names its scene, so only those scenes are sent — measured
at **80% smaller** (10,733 → 2,177 chars on run 41's shape) — and the reply is
merged back **by scene number**, which makes deleting or adding a scene
structurally impossible rather than forbidden by instruction.

**4.22 A claim the red team flagged HARD never gets a card.** Run 38 wrote
"top block" as one of three structural measurements of jeans. Not a standard
term; in none of the run's 14 sources. The red team caught it HARD *twice*
and the publish gate named it — then the repair could not run (every provider
out of quota) and the engine, which never read the findings, printed TOP
BLOCK in the largest type in the video with a made-up definition under it.
**§11's failure in a new topic.** Rewriting narration needs a model and there
may not be one; *not amplifying* needs nothing. A scene with an unfixed HARD
finding now gets no term card, checklist row, diagram column, list card or
stat card. **Soft findings gag nothing** — they are style notes, not "this is
invented", and gagging on them would strip cards off a sound script. The
narration still says the sentence: that is a script problem and it stays
visible in the publish gate.

**What a gagged shot shows instead is the CHECKLIST, not black.** The first
version drew a flat slate and run 41 measured the cost: 38 of 129 shots blank,
three of twelve contact-sheet frames pure black, scenes 5 and 8 reduced to
long stretches of nothing. Trading an invented term for a void is not a fix —
§4.9 already says the worst frame in this video is one carrying nothing, and
black carries less than a still card. The checklist is built only from
ungagged scenes, so every word on it is trusted; it fills the frame and tells
the viewer where they are, which is the one honest thing to say during a scene
whose own claims are in doubt. A header-only card was tried in between and
rejected on sight: a title over four fifths of empty page.

**4.23 One comparison per pair.** EDGE and APPLY both follow the last
CATEGORY, so both took the same "last two members" and drew the identical
diagram twice, 28s apart in run 38. A diagram earns its place by saying
something new.

**4.24 The anchor asks WHERE the subject sits in the tags, not whether it
is there at all.** Pixabay orders tags by relevance, so position measures how
central the subject is — for free. Someone wearing jeans is in a great many
photographs, which is why run 38 put a man holding a Nikon (`nikon, man,
casio, jeans, nikon…`) on screen under narration about wide-leg jeans. The
subject must appear in the **first three** tags. Three is measured, not
taste: at four the camera photo comes back. Validated 39/40 on that run's own
logged tags, with `subject={jeans}` because that is the anchor the engine
really produced — an earlier version tested `{jeans, denim}`, scored better,
and was testing easier input than the code gets.

**4.12 The header plate is fully opaque.** At alpha 232 it looked like a
reasonable "barely there" choice; over saturated footage the colour read
straight through and the supposedly stable white band changed with every shot.

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

**5.8 Modules built, tested, and never imported — FOUR times now.**
`modes.py` (5.6), then `overview_clip` (an animated card existed the whole
time while `build()` rendered a still PNG), then `point_card`, then
`stat_card` — which had its own bug fixes in it and had never once been
called. The owner found the last one by asking whether the templates were
actually being used. **After writing a module, grep for its name and confirm
something calls it.**

**5.9 The term card printed straight through the section card's own list.**
It slid in and put "GROSS MARGIN" on top of the checklist's GROSS MARGIN row.
The three-band layout assumes the middle of the frame is a *picture*; on a
section card the middle of the frame is the *list*. It now waits until the
card is done. Found on a rendered frame; invisible in every log.

**5.10 Captions were half the size YouTube's own default burns in.** Size 20
on a 720-high frame — 2.8% of frame height, unreadable on a phone, which is
where this audience watches. Now 40 (~5.5%).

**5.11 The fallback writer could never accept a real prompt.** Run 36:
Gemini's *daily* quota was gone, Groq answered `HTTP 413 ... Limit 8000,
Requested 8373`, the code waited 20s and re-sent **the same bytes**, and the
run died with a provider that was up, keyed and willing. Groq's free tier
meters tokens *per minute* (8,000 for gpt-oss-120b) — nothing to do with the
model's context window — and research's 34,832 chars of source text put us 373
tokens over. `too_large` was computed in `_call_sweep` and **never once acted
on**, so the 413 fell through to the rate-limit branch: Groq's 413 body says
"rate limit" and carries `code: rate_limit_exceeded`. **A size limit does not
clear by waiting.** That retry could not ever have worked, so the entire
reason a quota wall is meant to be survivable had never caught a single run.
Now `PROVIDER_CHAR_CAP` trims per provider *before* sending, and a 413 reads
"Limit N, Requested M" out of the refusal and re-sends at 60% of the limit.
`shrink()` cuts the **middle** — instructions are at the top, the required
output shape at the bottom; trimming the tail deletes the part saying what to
produce — and snaps to a line break so a figure never loses its digits.
**Same shape as 5.6/5.8: a safety mechanism that was written, looked right,
and was never run.** The regression asserts the retry is SMALLER than the
request that failed; before the fix every request in the sequence was
byte-identical.

**5.12 "Dropping the schema" never dropped the schema — 5.11's twin, found
the same afternoon.** With the size fix in, run 37 got further than any run
ever had on the fallback (`brief: 765 words`, written by Groq) and then died
one stage later on `HTTP 400: 'messages' must contain the word 'json' ... to
use 'response_format' of type 'json_object'`. The log said *"dropping
response_schema, retrying plain JSON"* — and the retry was byte-identical,
because `drop_schema` was set in `_call_sweep`, printed about, and **never
passed to `_openai_compatible`**, which set `response_format` whenever
`schema` was truthy. Underneath was a real Groq rule nobody knew: JSON mode
is refused unless the literal word "json" appears in the messages. Most
prompts here say "Reply with JSON"; the *drafting* prompt relies on the
schema and never says it — so **stage 2 could never have been answered by
this provider, on any run.** Both now fixed, and the trim reserves 160 chars
for the appended instruction so the fix that follows the trim cannot defeat
it. **Look for this shape elsewhere: a flag that is set, logged, and not
plumbed through is indistinguishable in the log from one that works.**

**A note on how 5.12's test was got wrong first.** The first version of the
regression faked `_openai_compatible` — the function the fix lives *inside* —
so the fixed code never ran and the test reported FAIL against a working fix.
It now fakes `urlopen`, so the real request-building code runs and the exact
bytes Groq would receive are asserted on. **Testing one layer above the
change proves nothing about the change.**

**The pattern under most of these:** they shipped because they were reasoned
about and never *run*, with CI used as the test suite at ~5 minutes a cycle.
That is what `test_engine_local.py` exists to end.

**And the pattern under 5.7, 5.9 and 5.10:** they were all invisible in a
green log and obvious in a frame. That is what `contact.py` exists to end —
one 60KB JPEG of twelve frames, uploaded by every run.

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
| `GROQ_API_KEY` | **Recommended** | Free, no card. The fallback writer. Without it, a Gemini quota wall kills the run outright. **Free tier is 8,000 tokens per MINUTE** — see §5.11; prompts are capped to fit it. |
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

    python3 test_engine_local.py     # real engine, real ffmpeg, ~50s
    python3 modes.py                 # mode detector against its test set
    python3 graphics.py              # renders one of each card to graphics_demo/
    python3 sfx.py                   # builds the kit and measures every level
    python3 scriptbits.py            # list extractor vs known-good/known-bad
    python3 test_relevance.py        # stock relevance vs real logged tags
    python3 redteam.py               # runway regression, both directions
    python3 test_verify.py           # video-verifier logic, no network
    python3 test_providers.py        # the fallback writer when Gemini is out
    python3 test_wiring.py           # is anything built and not connected?
    python3 research.py --selftest   # a blocked site costs its slot, not the run
    python3 contact.py final_video.mp4   # LOOK at what was just built

Workflow test mode: run with `skip_brain_test_fixture=true` to render a
hand-written script with **no AI quota at all**. Use it for any engine or
visual change.

**Always finish by looking.** `contact.py` puts twelve frames of the finished
video on one ~60KB JPEG with per-frame measurements, and CI uploads it as
`contact_sheet.jpg` on every run. Run the workflow with `log_frames=true` to
have the sheet printed into the log as base64 as well — needed when the
artifact host is unreachable, which it is from some sandboxes.

---

## 9. Known-untested / open

- **A script written by the fallback is less grounded than one written by
  Gemini.** Groq's cap (§5.11) means it sees ~16,000 of research's ~35,000
  characters — under half the sources. Right trade against a dead run, but it
  was invisible. *Now reported:* `script.json` carries `written_by`, and
  `publishable.caveats` says how many calls were trimmed and which fallback
  answered. Deliberately **not** a blocker — the owner decides, and a gate
  that fires on every fallback run is the §4.21 mistake. **What is still
  open: nobody has yet judged whether a Groq-written script is actually
  worse, or by how much.** The caveat says "less grounded"; that is reasoning,
  not a measurement.
- Whether the quality loop converges within `MAX_PASSES` or always spends the
  budget.
- Pixabay behaviour across a long video's worth of requests (~45 shots).
- The animated cards and the three-band layout at **full video length** —
  verified on a 5-scene fixture and on rendered frames, not on 12 minutes.
- Music still does not fit or vary; one bed for the whole video.
- Diagrams: the comparison card exists (§4.20). A *spectrum* — items placed
  along an ordered axis — does not, and cannot be built safely until the
  ordering itself is verified: inventing an order is inventing a fact.
- `verify.py` has never run against a real video - it needs a live run with
  Gemini quota. Its parsing is tested; its usefulness is not.
- **Run 38 (jeans, 8 scenes, 338s, 540p, 59.4MB) is the first complete video
  from a fully-fallback-written script.** Pictures: `subject anchor: jeans`
  held, section headers persisted, checklist correct, `freezedetect` found no
  motionless stretch ≥3.5s, 28/68 shots drawn. **Content: not genuine.** 9
  unfixed HARD findings — "top block" invented, two rise thresholds
  contradicting the sources, a fabricated GQ quote, a fabricated product
  (Orslow 107 Ivy Fit). The gate correctly refused it. §4.22 stops the video
  amplifying them; **nothing yet fixes the narration when the repair cannot
  run**, and that is the open problem.
- **The subject anchor asked "is there denim in this?", not "is this ABOUT
  denim?"** Run 38: a man holding a Nikon, a shirtless portrait, a banjo
  player, a toddler in dungarees, a laundry line, a beach — all containing
  jeans, none about jeans. *Fixed* (§4.24), 39/40 on run 38's own logged
  tags. **Still open:** the anchor is one word from the title, so a clip
  whose tags LEAD with `denim` is dropped when the anchor is only `jeans`.
  Widening it from the writer's `image_keywords` is the obvious next move
  and is NOT done — on run 35's keywords a naive frequency threshold
  readmits "straight" and "rise", which is the bug the anchor exists to end.
- The comparison card can pair things of different KINDS — run 38 drew
  "Wrangler Classic Cowboy Cut vs Mid-rise jean", a leg cut against a rise
  height. It checks that both are members, not that they are commensurable.
- `verify.py` has now RUN live and correctly reported `COULD NOT WATCH (not a
  verdict on the video)` on a 429. Its refusal path is proven; its actual
  judgement is still unproven.
- Nothing yet judged by the owner as publishable. That is the real bar.

---

## 10. What the reference explainers actually do (studied, not guessed)

Frame-by-frame analysis of "Every Operating System Explained in 8 Minutes",
alongside the owner's screen recording of our own run #23. Every point below
is from looking at frames, not from reasoning about what ought to work.

**10.1 It opens on the WHOLE LIST.** Frame one is a grid of all eight
operating systems, logo and name. The viewer sees everything they will learn
before a word is spoken. Ours opened on a soft-focus coffee cup.

**10.2 A section header never leaves the screen.** Icon top-left, section
name top-centre in large type — "WINDOWS", then "LINUX" — persisting across
every shot of that section. This is the orientation device, and it works
*because it does not move.* Ours had no orientation at all.

**10.3 Nothing cuts.** Scene detection finds ZERO hard cuts in 43 seconds.
Sampling one frame per second shows the MS-DOS logo holding on the left while
screenshots appear beside it and are swapped. Elements **accumulate on a
stable canvas.** We cut between unrelated full-frame stock clips every 5s.
This is the single biggest structural difference and it is not a tuning
problem.

**10.4 White background, black text.** Reads as a document. Not cinematic,
not dark. The dark card design tried before this was rejected by the owner
on sight, correctly.

**10.5 Real artifacts of the subject.** Actual MS-DOS boot output, the actual
Windows 1.01 splash, the real Windows 10 desktop, real logos. Not one frame
of stock footage. Where no real picture exists, a flat icon or stick-figure
doodle stands in — never a photograph of an unrelated office.

**10.6 The failure this exposes in our output.** A giant 3D "FRIDAY" clip
appeared under narration about "total expenditures across reporting periods".
Pixabay returned it; nothing ever asked whether it was relevant. In 47
seconds our video passed through seven unrelated visual worlds.

## 11. THE TAXONOMY IS NOT CHECKED — open, and the worst content bug

The owner caught this and it is more serious than any visual problem:

> "runway etc you're explaining is incorrect ... there wasn't proper research
> done for what to explain"

He is right. **Runway is not a type of business expense** — it is months of
cash remaining at the current burn rate. It appeared as an item in a list of
expense types in both the AI-written script AND the hand-written test fixture
in factory.yml (which this project's own author wrote).

Nothing anywhere validates that the listed items are genuinely members of the
category the topic asked about. `validate()` checks word counts, that
`key_term` is spoken aloud, that `thumb_accent` is inside `thumb_headline` —
and never once asks whether the list is *true*.

This defeats the entire point. A viewer who looks it up finds the video is
wrong, which is worse than a video that looks cheap.

The fix is a research stage that establishes the real membership of the
category from sources FIRST, and a check that every `key_term` survives it.

**Partly closed, and it is important to be exact about which part.**

*Done:* `topics.py` is the truth gate — it asks whether independent sources
agree the category has a real, closed membership, before a script is written
at all. `redteam.py` treats "not a member" as a hard finding against the
finished script. And the reason RUNWAY specifically reached the screen turned
out to be narrower than it looked: the on-screen checklist was built from
*every* scene's `key_term`, so the CLOSE beat became an item. Only CATEGORY
and STEP beats are list items now (§4.11), and both test fixtures — which had
the same fault, written by this project's own author — are corrected, with
runway deliberately left on the CLOSE as the regression test.

*Still open, and the description here was WRONG until now.* `VERIFIED_MEMBERS`
was described as "still an empty list". It is not — `choose_topic()` fills it
from `topics.assess()`, `redteam.check()` holds every scene to it, and a
not-a-member finding is HARD and blocks the revision loop. All of that was
built and wired.

The real hole was narrower and worse: **`choose_topic()` returned early when a
topic was supplied**, so `VERIFIED_MEMBERS` stayed empty and the entire
apparatus silently did nothing — on exactly the path the owner uses when they
pick a topic themselves, which is the path the runway video was made on. A
given topic now gets its membership established too. It is **not** rejected if
the sources disagree — the owner's choice is theirs — but the run says so
loudly instead of quietly skipping.

Second fault, found while fixing the first: the check flagged **every** scene's
`key_term`, so a CLOSE beat naming "runway" produced a HARD finding the writer
could only satisfy by deleting a legitimate ending. Only member beats are
checked now (§4.11), with the beat rules moved to `modes.py` so the engine, the
red team and brain cannot drift apart. If a script carries no beats at all the
check falls back to testing every scene — a missing field must never silently
disable a safety check.

`python3 redteam.py` is the regression, in both directions: runway as a CLOSE
must pass, runway as a CATEGORY must be blocked, and a script with no beats
must still be checked. 3/3.

*Why it never once produced a list — found on run 41's investigation, and it
was not quota.* `assess()` truncated the pooled source text with
`context[:11000]`. `research.gather()` concatenates whole pages as
`[SOURCE 1] … [SOURCE 8]` and one page is easily 5,000 characters, so the model
was shown the first two or three sources and then reported honestly on those.
`score()` saw fewer than three lists and the gate printed *"fewer than 3
sources named any members"* — true, and completely misleading. Measured on a
context sized like a real gather (8 pages, 43,000 chars): the old slice left
**3 of 8** sources still naming their members; `fair_share()` leaves **8 of 8**
in 14,004 chars. The gate was starved, not broken, and that is why
`VERIFIED_MEMBERS` was empty on every jeans run — so "top block" walked
straight through the one check built to stop it.

The budget deliberately fits inside `PROVIDER_CHAR_CAP`: `shrink()` cuts the
MIDDLE, so a second trim on this prompt would delete the middle sources and
recreate the same starvation in a new form.

*What is genuinely left:* **nothing has yet been observed catching a real
invented member on a live run.** The fix is measured offline; the gate has
still never handed the writer a verified list on CI.

---

**Full project record — every measurement, bug, research finding and open
problem — is in [PROJECT.md](PROJECT.md).** This file is the short
operational memory; that one is the complete history.
