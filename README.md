# MMM-MVP

Automated documentary-style YouTube video pipeline. Runs entirely on GitHub
Actions (free tier) — no local compute, no paid APIs, ₹0 budget.

**Pipeline:** pick a topic → `brain.py` researches it and writes a script
(Gemini, grounded web search, fact-checked) → `engine.py` turns that script
into a finished video (voice, real stock footage or AI visuals, subtitles,
music) → you review and upload manually.

## Running it

GitHub → **Actions** tab → **MMM Offline Factory** → **Run workflow**. Fill
in a topic (or leave blank to let it choose one) and a target length, then
run. When it finishes, download `finished-720p-video` from the run's
artifacts.

## Secrets (Settings → Secrets and variables → Actions)

| Name | Required? | What it's for |
|---|---|---|
| `GEMINI_API_KEY` | **Required** | Script generation. Free tier at [aistudio.google.com](https://aistudio.google.com). |
| `PIXABAY_API_KEY` | Optional | Real stock video/photos instead of AI-generated images — makes the video look like it was actually shot and edited, not a slideshow. Free, no card, sign up at [pixabay.com/api/docs](https://pixabay.com/api/docs/). Without it, the engine falls back to AI images automatically. |

No other keys are needed. Everything else (voice, images-as-fallback, music)
is free and keyless.
