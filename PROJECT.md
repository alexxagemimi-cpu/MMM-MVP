# MMM Factory — complete project record

Everything established about this project: what it is, what every file does,
every bug found and how, every number measured, every research finding, and
what is still broken. Written so someone with no history here can pick it up.

`CLAUDE.md` is the short operational memory. **This file is the full record.**

Last updated: 2026-08-29

---

## 1. What this is

**MMM = "Money Making Machine."** A private, zero-budget automated YouTube
explainer-video pipeline running on GitHub Actions.

**Owner:** vibecodes. Directs the work and judges the output; does not write
code. Explanations must be plain English, no jargon.

**Hard constraints, not preferences:**

| Constraint | Detail |
|---|---|
| **Budget ₹0** | No card on file anywhere. Any dependency needing a payment method is disqualified however good it is. |
| **Compute** | GitHub Actions, 45-minute job cap |
| **Human QA gate** | The owner watches before publishing. **Never automate the upload.** This was his own decision and it is the project's main defence against YouTube's inauthentic-content policy. |
| **First niche** | Money / business / self-improvement — though the live demand data now questions this (§8.3) |

---

## 2. Files

| File | Purpose | State |
|---|---|---|
| `scout.py` | Chooses **what to make**: generate → triage → demand → truth → backlog | Built, fixture-tested; ran live once |
| `topics.py` | Truth gate — do sources agree there's a real, closed answer? | Built, 6/6 on test cases |
| `youtube.py` | Demand gate — does anyone watch this, can a small channel break in? | Built, 5/5 fixtures; live API confirmed working |
| `redteam.py` | Attacks the finished script; blocks on hard findings | Built, tested |
| `brain.py` | Script writing: scout → research → draft → fact-check → revise → red-team | Runs green; scout stage failed once (correctly) |
| `research.py` | Web search + page reading, independent of any LLM | Verified on CI: 14 sources, 44,650 chars |
| `modes.py` | story / explainer / guide — beats, craft rules, per-mode metrics | Detector 12/12 |
| `engine.py` | Video assembly: TTS, visuals, Ken Burns, captions, term cards, mix | Runs green, produces real videos |
| `graphics.py` | Drawn information cards + animation | Built, not wired in |
| `thumbnail.py` | Grid-style thumbnail from `script.json` | Built and wired; ships in artifact |
| `sfx.py` | Synthesised sound kit | Built, not wired in |
| `test_engine_local.py` | Real engine, real ffmpeg, ~40s | Passing |
| `.github/workflows/factory.yml` | The workflow, incl. no-AI engine test mode | Stable |

---

## 3. The pipeline

```
0. SCOUT      20 candidates → triage → demand → truth → pick   (6 model calls)
1. RESEARCH   real web search, pages read by us
2. DRAFT      against the VERIFIED member list
3. FACT-CHECK second search pass against fresh sources
4. REVISE     fix accuracy + craft together
5. REPAIR     structural fixes
6. RED TEAM   attack; refuse to ship while a HARD finding stands
──────────────
   ENGINE     TTS → shots → scenes → concat → captions + music
   THUMBNAIL  drawn from script.json
   HUMAN      the owner watches, then uploads manually
```

---

## 4. Measured numbers

Everything here was measured, not estimated.

### 4.1 Speed and cost

| Thing | Measurement |
|---|---|
| Narration speed | **163.5 wpm** (168 words → 61.65s, en-US-GuyNeural). Was assumed 150. |
| Final composite | 0.52× real time → a 12-min video ≈ 6 min to finish |
| Whole engine, 8-scene video | 7 min 44 s |
| Whole brain, 8 scenes, 2 passes | 6 min 5 s |
| Full run #23 | **14 min 32 s** against a 45-min cap |
| Thumbnail render | 0.44 s |
| 4 static graphics cards | ~1 s |
| 5 s animated card | 4.8 s |
| Scout model calls | **6** (was 21) |
| YouTube quota per topic | 102 units of 10,000/day ≈ 98 topics/day |

### 4.2 Output quality

| Thing | Measurement |
|---|---|
| Triple re-encode loss | **SSIM 0.9966** — negligible. Do not restructure for this. |
| Real footage hit rate | 95/96 shots (run #23), 13/13 (run #20) |
| Shot length achieved | 4.84–5.80 s against a 5 s target |
| Reading grade, clean script | 4.6 |
| Reading grade, bad script | 15.7 |
| SFX peaks | pop −34.7, tick −35.8, whoosh −26.3, thud −31.2, riser −19.0 dB |

### 4.3 The topic gate, on known cases

| Topic | Agreement | Verdict |
|---|---|---|
| blood types | 1.00 | BUILD |
| planets in the solar system | 0.942 | BUILD (Pluto correctly contested) |
| types of operating systems | 0.893 | BUILD |
| types of coffee roast | 0.84 | BUILD |
| best programming language | 0.28 | REJECT — opinion |
| **types of business expenses** | **0.112** | **REJECT — "runway" is contested** |

### 4.4 First live YouTube demand data

| Topic | Median views | Breakout | Verdict |
|---|---|---|---|
| four types of business ownership structures | 3,112 | — | UNWANTED (19% fresh) |
| five components of SMART goals | 1,531 | 0.06 | UNWANTED |

---

## 5. Research findings

### 5.1 What the reference explainer actually does

Frame-by-frame analysis of *"Every Operating System Explained in 8 Minutes"*
plus the owner's screen recording of our own run.

1. **It opens on the WHOLE LIST.** Frame one is a grid of all eight operating
   systems, logo and name. Ours opened on a soft-focus coffee cup.
2. **A section header never leaves the screen.** Icon top-left, section name
   top-centre. It works *because it does not move.* Ours had no orientation.
3. **Nothing cuts.** Scene detection finds **zero hard cuts in 43 seconds**.
   The MS-DOS logo holds while screenshots appear beside it and swap.
   Elements **accumulate on a stable canvas.** We cut between unrelated
   full-frame stock clips every 5 s. This is the biggest structural
   difference and it is not a tuning problem.
4. **White background, black text.** Reads as a document.
5. **Real artifacts of the subject** — actual MS-DOS boot output, the real
   Windows 1.01 splash, real logos. Zero stock footage.

**Our failure by comparison:** a giant 3D "FRIDAY" clip appeared under
narration about "total expenditures across reporting periods"; in 47 seconds
the video passed through **seven unrelated visual worlds**.

### 5.2 Retention and editing

- **The 5-second rule** — if nothing changes for 5 s (no cut, zoom, text or
  sound), that is a hole viewers leak out of.
- **Pattern interrupt every 15–30 s.**
- **Sound on the cut** is the first thing editors name as separating cheap
  from professional. We have none.
- **Progressive disclosure** — reveal elements one at a time, in the order
  you want them thought about, to keep working memory low.
- Explainer cut rate ≈ 4–6 s per visual; b-roll held 3–7 s.
- A concrete value claim inside the first 15 s → ~52% vs ~44% retention.
- YouTube's loudness target is **−14 LUFS**.

### 5.3 Free resources confirmed

| Resource | Terms |
|---|---|
| YouTube Data API v3 | Free, no card, 10,000 units/day. search=100, videos.list=1, channels.list=1 |
| Pixabay | Free API, images + video + 130k sound effects |
| Freesound | Free API key, CC0 filtering available |
| Anton font | SIL Open Font License — vendored in repo |
| DuckDuckGo (`ddgs`) | Keyless, no quota — confirmed working on the runner |
| Edge-TTS | Free, broadcast-quality, gives real word timings |

---

## 6. Load-bearing design decisions

Do not "simplify" these.

**6.1 Explainer mode has NO `fact_density` floor.** Measured on hand-written
samples: the *invented-history* sample scored **7.21**, the *true taxonomy*
scored **0.00**. The fabrication scores higher, so a floor rewards inventing
names and dates — optimising into the disease it was meant to catch.

**6.2 Three modes, not one.** One narrative arc forced on every topic, with
research told to find a "reversal", is what produced confident fake history.
A model asked for a shape the material lacks will manufacture that shape.

**6.3 `key_term` must be a literal phrase from its own narration.** A term
the narration never says cannot be timed to the voice. **No match means no
card** — a mistimed card is worse than none.

**6.4 Term cards use ASS `BorderStyle=3`** so the renderer measures the
glyphs and sizes the box. A hardcoded 560px box let long text run off its own
background.

**6.5 `loudnorm` runs ONCE over the finished programme.** Per-scene
normalisation flattens the contrast between scenes that makes narration sound
edited — and it hung a render (§7.3).

**6.6 Every ffmpeg call is time-bounded, sized against the 45-min cap.** A
flat 240 s × 12 shots = 48 min, past the cap. `shot_timeout()` scales with
output length.

**6.7 No unbounded ffmpeg inputs.** No `-stream_loop -1`, no bare `apad`.
Two multi-hour hangs came from this pattern.

**6.8 Durations are computed, never inferred.** Scene length comes from our
own frame budget (`frames / FPS`), never from probing or `-shortest`.

**6.9 Cheap filters first in the scout.** A model call is scarce and
rate-limited; a YouTube lookup is 102 of 10,000 daily units. Spend the scarce
one last, on a handful.

**6.10 Video length is derived from the material,** not requested up front.
Asking 12 minutes of a 4-item topic forces padding, and padding a taxonomy
means inventing members.

**6.11 A failed check is not a rejection.** See §7.9 — the most dangerous
confusion in the system.

---

## 7. Every bug found, and how

### 7.1 `image_keyword` vs `image_keywords`
engine.py read the singular key, which never existed. **Every image silently
fell back to a generic placeholder.** No error — the worst kind of bug.

### 7.2 Fetched asset and rendered output shared a filename
ffmpeg would read and write the same file for every stock-footage shot.
Caught by a mocked end-to-end run before it shipped.

### 7.3 The multi-run hang — cost six CI runs
Four wrong theories in order: `-shortest`, a missing `-t`, timestamp
inheritance, stream copying. The actual cause was **`loudnorm` stalling on
one particular narration clip** — 73 s and killed, versus 1.5 s without it.
Found not by a fifth theory but by making the audio chain **degrade through
tiers and log which tier worked.**

### 7.4 Sentence boundaries mistaken for word boundaries
Edge-TTS emits both in the same shape, so a whole sentence arrived as one
timed "word" — 4 caption cues for a 4-scene script. `split_multiword()` fixes
it.

### 7.5 Grounding welded to one provider's quota
Research used Gemini's built-in `google_search`. That quota is metered
separately and is **account-wide, not per-project** — a fresh key from a new
project failed identically. Fixed by owning search (`research.py`).

### 7.6 A module built and never wired in
`modes.py` was written, tested and left orphaned; brain.py kept grading every
topic on story rules. **Check that new modules are actually imported.**

### 7.7 The 403 was Cloudflare, not Groq
`error code: 1010` is Cloudflare's *browser signature banned*, not a Groq
error. Python's default `User-Agent: Python-urllib/3.11` is refused, so the
request never reached the API. The tell was that `research.py` — which sends
a browser UA — kept working in the same runs where every model call died.

### 7.8 Model discovery fired on a rate limit and retried the same model
The trigger included `"model" in msg`, and Groq's 429 text reads *"Rate limit
reached for model ..."*. So a 429 was read as a permissions problem,
discovery ran, and picked the model that had just failed.

### 7.9 A failed check was recorded as a rejection — **most serious**
```
[REJECT] The five factors that make up a FICO credit score
       - too few usable sources to judge this topic at all
```
Eight sources were present. The line appeared immediately after both model
providers hit their limits: the extraction call failed, so nothing was named,
and **the gate read its own blindness as a verdict** — then wrote the topic to
the permanent blocklist. Fixed with an explicit `checked` flag.

### 7.10 Length overshoot: asked 6 min, got 8.3
Scenes were specified as `N` to `N+45` words. **A model given a range writes
to the top of it**, and on a 113-word target +45 is +40%. Compounded by `WPM`
being a guessed 150 against a measured 163.5, and by `validate()` only ever
checking the *lower* bound — so every scene ran 40% over and passed clean.

### 7.11 Repeated shots inside one scene
12 shots drawn from 8–9 keywords, so `plan_shots()` cycled and the same
subject appeared twice in one scene.

### 7.12 Pixabay photos stretched
`resize((1920,1080))` on any non-16:9 photo. A 3:2 camera photo is stretched
**18% wider than reality**. Only affects the photo path, which is why run #20
never showed it.

### 7.13 Blood types rejected by the topic gate
One source wrote "A positive" where others wrote "A+", so literal comparison
saw sixteen members instead of eight. **Found only by testing a case in the
middle** rather than one obvious pass and one obvious fail.

### 7.14 Ranking ignored the kill rules
A topic whose top results were all seven years old scored **0.82** — above a
live-but-walled topic at 0.44 — purely on high breakout. Ranking on that
alone would have picked a corpse as best of the batch.

### 7.15 A quota failure looked like a measurement of zero
Which would have made an arbitrary pick look like a measured one.

### 7.16 Smaller ones
- Term card alpha `E0` — nearly see-through. Caught by extracting a frame.
- The visual style string literally described a commercial: *"low-key moody
  lighting, volumetric haze, muted desaturated teal and amber."*
- Thumbnail labels overlapping; circles colliding with the headline.
- Graphics: rail detail printed over the last row; stat card's `%` descender
  ran through its own label.
- `sfx.py` measurement used `-v error`, which silences `volumedetect` — every
  peak printed `?`, reading as a broken sound rather than a broken ruler.
- A separate `engine-test.yml` was undispatchable — GitHub only registers
  `workflow_dispatch` from the default branch.
- **Workflow logs can only be read from the END.** The writer runs first, so
  its output is pushed out of reach by the engine's per-shot logging. Fixed by
  writing a summary to `$GITHUB_STEP_SUMMARY`.

**The pattern under most of these:** they shipped because they were reasoned
about and never *run*, with CI used as the test suite at ~5 minutes a cycle.
That is what `test_engine_local.py` exists to end.

---

## 8. The content problem — the most serious issue

### 8.1 "Runway" was never a research bug. It was a topic bug.

Runway is **not** a type of business expense — it is months of cash remaining
at the current burn rate. It appeared in a list of expense types in both the
AI-written script *and* the hand-written test fixture.

"Types of business expenses" **has no single agreed answer.** Accountants
classify by behaviour (fixed/variable), by function (COGS/operating), and by
tax treatment (capital/revenue). Demand one clean list from a fuzzy category
and a model must invent one. **Better prompting cannot fix a broken question.**

### 8.2 What works instead has the opposite property

*Every Operating System*, *Every Blood Type*, *Every Type of Phobia* — all
have a **real, closed, verifiable set.** That is the precondition that lets a
video be accurate and satisfying at once, and it is what `topics.py` now tests
by measuring cross-source agreement.

### 8.3 The niche may be the problem

First live demand data: in money/business/self-improvement, the topics that
passed the **truth** gate were ones nobody watches (median 1,531 and 3,112
views). That is a finding about the niche, not a bug — and it is unresolved.

---

## 9. Keys and setup

GitHub → Settings → Secrets and variables → Actions.

| Secret | Needed? | Notes |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Free tier. Daily quota is real and runs out mid-project. |
| `GROQ_API_KEY` | **Recommended** | Free, no card. Has a per-minute token limit as well as a daily one. |
| `PIXABAY_API_KEY` | Recommended | Free, no card. Without it, visuals fall back to AI images. |
| `YOUTUBE_API_KEY` | Recommended | Free, no card, 10,000 units/day. Enables the demand gate. |
| `TAVILY_API_KEY` | Optional | Keyless DuckDuckGo works without it. |
| `OPENROUTER_API_KEY` | Optional | Last resort; ~50 requests/day free. |

Repo *variables*: `STYLE`, `GEMINI_MODEL`, `GROQ_MODEL`, `STRICT_FACTS`,
`MAX_PASSES`, `NICHE`, `SCENE_SECONDS`.

**Do not use Cerebras as primary fallback** — its no-card tier ended August
2026. Which models are free *rotates*, which is why every model id is an env
var.

---

## 10. Testing

```bash
python3 test_engine_local.py   # real engine, real ffmpeg, ~40s
python3 modes.py               # mode detector against its test set
python3 topics.py              # truth gate against known good/bad taxonomies
python3 redteam.py             # script attack on clean vs bad samples
python3 graphics.py            # render the card set
python3 sfx.py                 # build and measure the sound kit
python3 scout.py status        # backlog + blocklist
```

Workflow test mode: run with `skip_brain_test_fixture=true` to render a
hand-written script with **no AI quota at all**.

---

## 11. Working agreement

- **Correct → Verified → Right layer → Fast.** Speed is last.
- Hand nothing over without real command output, or the label
  `NOT TESTED — expect it to break here: <where>`. "Should work" is banned.
- **Look at the artifact, not the logs.** Probe the file, extract frames.
- Test the **middle**, not just the obvious pass and obvious fail — §7.13.
- Test failure paths. Nearly every bug above was one.
- After any change, ask what that change could have broken.
- Never invent an unvalidated proxy for "good" and optimise toward it —
  `fact_density` is the cautionary example.
- Say the gap in the same message as the handover, not when asked.
- The owner's stated premise gets checked, not absorbed.

---

## 12. What is still broken or unproven

| Item | Status |
|---|---|
| **Scout memory does not survive a run** | The runner is destroyed each job, so the backlog and blocklist are lost. `made.json` ships as an artifact only. **The blocklist is the point, and it is currently defeated.** |
| **graphics.py not wired into engine.py** | The drawn cards exist but no video uses them |
| **sfx.py not wired in** | Still zero sound effects in the output |
| Nothing cuts vs everything cuts | The reference accumulates on a stable canvas; we still hard-cut between clips |
| Pixabay relevance | Nothing checks a clip is relevant — this is how "FRIDAY" got in |
| Thumbnail circles | Never visually confirmed whether Pixabay illustrations look right |
| Music | Same synthesised drone in every video, tonally wrong for business |
| Captions | Small (2.8% of frame height) and sit where the player controls sit |
| No pauses | Narration is wall-to-wall |
| 720p / 25 fps | Measured as affordable to raise to 1080p30; not done |
| Scout rate limits | 6 calls is much better than 21, but Gemini 503s and Groq's per-minute cap are still live risks |
| **Nothing judged publishable by the owner** | That is the real bar |
