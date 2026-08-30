#!/usr/bin/env python3
"""
verify.py — a model WATCHES the finished video and reports what is wrong.

THE GAP THIS CLOSES
-------------------
Every quality gate in this project reads text. brain.py checks claims against
sources, redteam.py attacks the script, validate() checks its shape. Once
pixels exist, nothing looks at them - and this session alone shipped three
faults that were invisible in a green log and obvious in a frame:

  - the persistent section header vanished from an entire 8-scene video
    behind a caught exception
  - a resolution change made every card animation composite at the wrong
    coordinates, printing rows over each other
  - a card sat motionless for five seconds, twice, from unrelated causes

contact.py made those findable by a human. This makes them findable without
one, which matters because the human is the bottleneck the whole project
exists to remove.

WHY GEMINI AND NOT GROQ
-----------------------
Groq's vision models take IMAGES only - there is no video input. Gemini's
API takes real video on the free tier (inline to 100MB, Files API to 2GB),
and a 540p six-minute cut is a few tens of megabytes. It can hear the
narration and see WHEN something happens, which is most of the job: "the
card is still up eight seconds after the sentence ended" is not a judgement
you can make from stills.

Groq still has a role. It can read contact.py's sheet - twelve frames on one
JPEG, well under the 20MB image limit - so when Gemini's quota is gone there
is still an eye on the output. Fewer findings, but not zero.

WHAT IT JUDGES, AND WHAT IT MUST NOT
------------------------------------
The EDIT, not the facts. Whether a claim is true was settled upstream
against real sources; asking a model to re-judge it here would be asking it
to mark its own homework with no new information, which is the exact mistake
note 4 in brain.py's header warns about. This stage asks only: watching
this, what looks wrong?

IT REPORTS. IT DOES NOT EDIT, AND IT NEVER PUBLISHES.
A model that can both judge and fix satisfies findings by deleting - this
project has already watched that happen, when a membership check flagged a
legitimate closing scene and the only way to satisfy it was to cut a good
ending. And the owner's own rule is that a human decides what publishes.

    python3 verify.py final_video.mp4 script.json
"""

import json
import os
import re
import sys

MODEL = os.environ.get("VERIFY_MODEL", "gemini-3.6-flash")
MAX_INLINE_MB = 90          # Gemini takes 100MB inline; leave headroom


PROMPT = """You are reviewing a finished explainer video before a human sees
it. You can see the picture and hear the narration.

Report ONLY what is wrong with the EDIT - things visible or audible in this
file. Do NOT judge whether the facts are true; that was checked separately
against sources, and you have no sources here.

Look hard for:
- text that is cut off, overlapping something else, or too small to read
- a card or graphic that stays up long after its sentence ended, or flashes
  past before it can be read
- a shot that has nothing to do with what is being said at that moment
- the same shot or the same kind of shot appearing twice close together
- stretches where nothing changes on screen
- audio: narration clipped or cut off, music drowning the voice, a sound
  effect landing on nothing
- anything that makes it look automated rather than edited

For each problem give:
  at       - when it happens, in seconds from the start
  severity - "hard" if a viewer would notice and think the video is broken,
             "soft" if it is a polish issue
  kind     - a two or three word label, lowercase, e.g. "text cut off"
  detail   - what you actually see or hear, specifically
  fix      - one concrete change that would remove it

Be specific and be honest. If the video is genuinely fine, return an empty
list - do not invent problems to look useful. If something is only slightly
off, mark it soft rather than inflating it.

Return JSON: {"findings": [{"at": 12.5, "severity": "hard", "kind": "...",
"detail": "...", "fix": "..."}]}

The narration script, for reference on what SHOULD be on screen when:
%s
"""


def _scene_digest(script, limit=40):
    """A compact map of what the video is supposed to be showing when."""
    scenes = (script.get("scenes") or []) if isinstance(script, dict) else []
    out = []
    for s in scenes[:limit]:
        out.append(f"scene {s.get('scene')} [{s.get('beat', '?')}] "
                   f"key term: {s.get('key_term', '-')!r} :: "
                   f"{(s.get('narration') or '')[:160]}")
    return "\n".join(out)


def _parse(raw):
    """
    Findings out of whatever the model returned.

    Tolerant on purpose: this stage is advisory, so a reply that is valid
    JSON wrapped in prose should still be usable rather than costing the
    whole check.
    """
    if isinstance(raw, dict):
        data = raw
    else:
        text = (raw or "").strip()
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return []
    items = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []

    out = []
    for f in items:
        if not isinstance(f, dict):
            continue
        detail = str(f.get("detail") or "").strip()
        if not detail:
            continue                     # a finding with no substance is noise
        try:
            at = float(f.get("at"))
        except (TypeError, ValueError):
            at = None
        sev = str(f.get("severity") or "soft").lower()
        out.append({
            "at": at,
            "severity": "hard" if sev.startswith("hard") else "soft",
            "kind": (str(f.get("kind") or "issue").strip().lower())[:40],
            "detail": detail[:400],
            "fix": str(f.get("fix") or "").strip()[:300],
        })
    out.sort(key=lambda f: (f["severity"] != "hard",
                            f["at"] if f["at"] is not None else 1e9))
    return out


def watch(video, script, ask):
    """
    Hand the video to `ask` and parse what comes back.

    `ask(prompt, video_path)` is injected so the decision logic here can be
    tested without a key, a network or a 60MB upload - the same reason
    topics.py takes its model call as an argument.
    """
    prompt = PROMPT % _scene_digest(script)
    return _parse(ask(prompt, video))


# ---------------------------------------------------------------------------
# the real caller
# ---------------------------------------------------------------------------
def gemini_ask(prompt, video):
    """
    Upload the video and ask. Raises on failure - the caller decides whether
    a failed check is worth failing a run over (it is not).
    """
    from google import genai
    from google.genai import types

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("no GEMINI_API_KEY - cannot watch the video")
    client = genai.Client(api_key=key)

    size_mb = os.path.getsize(video) / 1048576
    if size_mb > MAX_INLINE_MB:
        # Files API, then wait for it to finish processing. A video is not
        # readable the instant the upload returns.
        import time
        f = client.files.upload(file=video)
        for _ in range(60):
            f = client.files.get(name=f.name)
            state = getattr(f.state, "name", str(f.state))
            if state == "ACTIVE":
                break
            if state == "FAILED":
                raise RuntimeError("Gemini could not process the video")
            time.sleep(5)
        parts = [f, prompt]
    else:
        with open(video, "rb") as fh:
            parts = [types.Part.from_bytes(data=fh.read(),
                                           mime_type="video/mp4"), prompt]

    resp = client.models.generate_content(
        model=MODEL, contents=parts,
        config=types.GenerateContentConfig(response_mime_type="application/json"))
    return (resp.text or "").strip()


def report(findings):
    if not findings:
        return "the model watched the video and found nothing to flag", 0
    hard = sum(1 for f in findings if f["severity"] == "hard")
    lines = [f"{hard} HARD, {len(findings) - hard} soft"]
    for f in findings:
        at = f"{f['at']:.1f}s" if f["at"] is not None else "  --  "
        lines.append(f"  [{f['severity']}] {at} {f['kind']}: {f['detail']}")
        if f["fix"]:
            lines.append(f"        fix: {f['fix']}")
    return "\n".join(lines), hard


def main():
    video = sys.argv[1] if len(sys.argv) > 1 else "final_video.mp4"
    spath = sys.argv[2] if len(sys.argv) > 2 else "script.json"
    if not os.path.exists(video):
        print(f"no {video} to watch")
        return 0
    script = {}
    if os.path.exists(spath):
        with open(spath, encoding="utf-8") as f:
            script = json.load(f)

    try:
        findings = watch(video, script, gemini_ask)
    except Exception as e:
        # NOT a failure of the video. Same rule as the topic gate: a check
        # that could not run is not a verdict, and must never read like one.
        print(f"COULD NOT WATCH (not a verdict on the video): {str(e)[:200]}")
        return 0

    text, hard = report(findings)
    print("\n" + "=" * 62)
    print("  WHAT A MODEL SEES WATCHING THE FINISHED VIDEO")
    print("=" * 62)
    print(text)
    print("=" * 62)
    if hard:
        print("These are edit problems, not fact problems. Nothing here has "
              "been changed automatically.")

    with open("verify.json", "w", encoding="utf-8") as f:
        json.dump({"findings": findings}, f, indent=2, ensure_ascii=False)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"\n## What a model sees watching the video\n\n")
            f.write("Nothing flagged.\n" if not findings else
                    "".join(f"- **{x['severity']}** "
                            f"{('%.1fs' % x['at']) if x['at'] is not None else ''} "
                            f"*{x['kind']}* - {x['detail']}\n"
                            for x in findings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
