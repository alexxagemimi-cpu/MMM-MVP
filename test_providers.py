#!/usr/bin/env python3
"""
test_providers.py - the fallback writer, and whether it can actually answer.

WHY THIS EXISTS
---------------
Run 36 died like this:

    gemini  -> 429 RESOURCE_EXHAUSTED     (daily quota gone)
    groq    -> HTTP 413 Request too large (Limit 8000, Requested 8373)
    groq    -> HTTP 413 Request too large (identical, 20 seconds later)
    FATAL: All LLM providers failed twice.

Groq was up, keyed, and willing. It refused because the prompt was 373
tokens over a per-minute budget, and the retry sent exactly the same bytes,
because a 413 was being read as a rate limit - it says "rate limit" in the
body - and rate limits are waited out. A size limit is not waited out.

So the fallback provider, the whole reason a Gemini quota wall is survivable,
had never once worked on a real prompt. That is not a tuning problem; it is
a path that was reasoned about and never run. This runs it.

The provider is faked, deliberately. What is under test is OUR logic - how
the prompt is cut and whether the retry differs from the request that just
failed - and a fake provider can be made to refuse on a known threshold,
which the real one cannot.

    python3 test_providers.py
"""

import os
import sys
import types

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("GROQ_API_KEY", "test-key")

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))
# brain imports the real genai client at module scope; it is never called here.
try:
    import google.genai  # noqa: F401
except Exception:
    g = types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    genai.Client = object
    gt = types.ModuleType("google.genai.types")
    genai.types = gt
    g.genai = genai
    sys.modules.setdefault("google", g)
    sys.modules.setdefault("google.genai", genai)
    sys.modules.setdefault("google.genai.types", gt)

import brain as B  # noqa: E402


def line(t):
    print(f"\n{t}\n" + "-" * len(t))


def test_shrink():
    line("shrink() keeps both ends and cuts the middle")
    bad = 0
    head = "INSTRUCTIONS: write six scenes about denim.\n"
    mid = "SOURCE. " * 4000
    tail = "\nReply with JSON only: {\"scenes\": [...]}"
    prompt = head + mid + tail

    out = B.shrink(prompt, 4000)
    checks = [
        ("fits the cap", len(out) <= 4000),
        ("keeps the instructions", out.startswith("INSTRUCTIONS:")),
        ("keeps the output shape", out.endswith('{"scenes": [...]}')),
        ("says text was removed", "trimmed" in out),
        ("actually removed some", len(out) < len(prompt)),
    ]
    for what, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
        bad += not ok

    short = "already small"
    same = B.shrink(short, 4000) == short
    print(f"  {'ok  ' if same else 'FAIL'}  a prompt under the cap is untouched")
    bad += not same

    # A cap smaller than the marker must not produce a negative slice.
    tiny = B.shrink(prompt, 80)
    ok = len(tiny) <= 80 or len(tiny) < len(prompt)
    print(f"  {'ok  ' if ok else 'FAIL'}  a brutal cap does not explode "
          f"(got {len(tiny)} chars)")
    bad += not ok
    return bad


class FakeProvider:
    """Refuses anything over `accepts` characters, the way Groq does."""

    def __init__(self, accepts):
        self.accepts = accepts
        self.sizes = []

    def __call__(self, prompt, schema, base_url, key, model, name):
        self.sizes.append(len(prompt))
        # Groq's real 413 body: it says "rate limit" and carries both numbers.
        if len(prompt) > self.accepts:
            want = max(1, len(prompt) // 4)
            limit = max(1, self.accepts // 4)
            raise RuntimeError(
                f'HTTP 413 from groq ({model}): {{"error":{{"message":"Request '
                f'too large for model `{model}` on tokens per minute (TPM): '
                f'Limit {limit}, Requested {want}, please reduce your message '
                f'size and try again.","type":"tokens",'
                f'"code":"rate_limit_exceeded"}}}}')
        return '{"ok": true}'


def test_413_shrinks_and_succeeds():
    line("a 413 makes the NEXT request smaller (run 36's actual failure)")
    bad = 0
    fake = FakeProvider(accepts=9000)
    B._openai_compatible = fake
    B.PROVIDER_CHAR_CAP = {}          # no pre-trim: force the 413 path itself

    prompt = "INSTRUCTIONS.\n" + ("SOURCE. " * 5000) + "\nReply with JSON."
    got = B.call(prompt, schema={"x": 1}, retries=4)

    print(f"  request sizes sent : {fake.sizes}")
    checks = [
        ("the call eventually succeeded", got == {"ok": True}),
        ("more than one size was tried", len(set(fake.sizes)) > 1),
        ("every retry was SMALLER than the one that failed",
         all(b < a for a, b in zip(fake.sizes, fake.sizes[1:]))),
        ("the last request fitted", fake.sizes[-1] <= fake.accepts),
    ]
    for what, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
        bad += not ok

    # THE REGRESSION. Before the fix every entry in fake.sizes was identical.
    repeated = len(fake.sizes) > 1 and len(set(fake.sizes)) == 1
    print(f"  {'FAIL' if repeated else 'ok  '}  it did not re-send the same "
          f"bytes after a refusal")
    bad += repeated
    return bad


def test_precap_avoids_the_round_trip():
    line("Groq is pre-trimmed, so the 413 does not happen at all")
    bad = 0
    fake = FakeProvider(accepts=16000)
    B._openai_compatible = fake
    B.PROVIDER_CHAR_CAP = {"groq": 16000}

    prompt = "INSTRUCTIONS.\n" + ("SOURCE. " * 5000) + "\nReply with JSON."
    got = B.call(prompt, schema={"x": 1}, retries=3)

    checks = [
        ("succeeded", got == {"ok": True}),
        ("first try already fitted", fake.sizes[0] <= 16000),
        ("only one request was needed", len(fake.sizes) == 1),
    ]
    for what, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
        bad += not ok
    print(f"  request sizes sent : {fake.sizes} "
          f"(prompt was {len(prompt):,})")
    return bad


def test_real_cap_against_run36():
    line("the configured cap vs the size that actually got refused")
    # Run 36: 8,373 tokens requested against a 8,000 limit.
    tokens_per_char = 8373 / 34832
    cap = B.PROVIDER_CHAR_CAP.get("groq", 16000) if hasattr(
        B, "PROVIDER_CHAR_CAP") else 16000
    est = cap * tokens_per_char
    print(f"  run 36 measured     : 34,832 chars -> 8,373 tokens "
          f"({tokens_per_char:.3f} tok/char)")
    print(f"  our cap             : {cap:,} chars -> ~{est:,.0f} tokens")
    print(f"  groq's limit        : 8,000 tokens (request + reply)")
    ok = est < 8000 * 0.75
    print(f"  {'ok  ' if ok else 'FAIL'}  leaves room for the reply "
          f"(~{8000 - est:,.0f} tokens)")
    return 0 if ok else 1


def main():
    # PROVIDER_CHAR_CAP is monkeypatched by the tests above; keep the real one.
    real_cap = dict(B.PROVIDER_CHAR_CAP)
    bad = test_shrink()
    bad += test_413_shrinks_and_succeeds()
    bad += test_precap_avoids_the_round_trip()
    B.PROVIDER_CHAR_CAP = real_cap
    bad += test_real_cap_against_run36()

    print()
    if bad:
        print(f"{bad} FAILED - the fallback writer still cannot answer when "
              f"Gemini's quota runs out.")
    else:
        print("all passed - a quota wall no longer kills the run.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
