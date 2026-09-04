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

import io
import os
import json
import sys
import urllib.error
import time
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

# Tests below monkeypatch this to fake a provider. Keep the REAL one: the
# json-mode test must exercise the actual request-building code, not a stand-in
# for it - an earlier version of that test called the leftover fake and failed
# against a fix that was working.
REAL_OAI = B._openai_compatible


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

    # Cutting mid-number is worse than cutting less. A source ending
    # "...the average inseam is 3" gives the fact-checker a figure whose
    # digits were amputated, and it cannot tell that from a wrong one.
    lines = "".join(f"SOURCE {i}: the measured inseam is {i}2 inches.\n"
                    for i in range(400))
    cut = B.shrink(head + lines + tail, 4000)
    body = cut.split("\n\n[... source material")[0]
    clean = body.rstrip().endswith("inches.")
    print(f"  {'ok  ' if clean else 'FAIL'}  cuts on a line break, not "
          f"mid-figure (ends {body.rstrip()[-24:]!r})")
    bad += not clean

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

    def __call__(self, prompt, schema, base_url, key, model, name,
                 drop_schema=False):
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


def capture_requests():
    """
    Fake the HTTP layer, NOT _openai_compatible.

    The first version of this test replaced _openai_compatible with a fake -
    and the fix under test lives INSIDE that function, so the fake meant the
    fixed code never ran and the test failed against a working fix. Testing
    one layer above the change proves nothing about the change. This stubs
    urlopen instead, so the real request-building code runs and every body it
    produces is captured exactly as Groq would receive it.
    """
    import urllib.request as U
    sent = []
    real = U.urlopen

    class Reply:
        def __init__(self, text):
            self._t = json.dumps(
                {"choices": [{"message": {"content": text}}]}).encode()

        def read(self):
            return self._t

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        body = json.loads(req.data.decode())
        sent.append(body)
        content = body["messages"][0]["content"]
        # Groq's real rule, verbatim from run 37's log.
        if (body.get("response_format", {}).get("type") == "json_object"
                and "json" not in content.lower()):
            raise RuntimeError(
                "HTTP 400: 'messages' must contain the word 'json' in some "
                "form, to use 'response_format' of type 'json_object'.")
        return Reply('{"ok": true}')

    U.urlopen = fake
    return sent, (lambda: setattr(U, "urlopen", real))


def test_json_mode_400():
    line("Groq's 'must contain the word json' 400 (run 37's actual failure)")
    bad = 0
    sent, restore = capture_requests()
    B._openai_compatible = REAL_OAI
    try:
        # A prompt that never says "json" - exactly what stage 2 sends.
        out = REAL_OAI(
            "Write eight scenes about denim. Keep each under 130 words.",
            {"x": 1}, "https://api.groq.com/openai/v1/chat/completions",
            "k", "openai/gpt-oss-120b", "groq")
        content = sent[0]["messages"][0]["content"]
        checks = [
            ("the request was accepted", json.loads(out) == {"ok": True}),
            ("the word 'json' reached the provider", "json" in content.lower()),
            ("json_object mode was still used",
             sent[0].get("response_format", {}).get("type") == "json_object"),
            ("it took ONE request, not a failed round trip", len(sent) == 1),
        ]
        for what, ok in checks:
            print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
            bad += not ok

        # THE REGRESSION. drop_schema was accepted by _call_sweep, printed
        # about, and never passed down, so "retrying plain JSON" re-sent
        # identical bytes and the provider refused identically.
        line("dropping the schema actually changes the request")
        sent.clear()
        REAL_OAI(
            "Write eight scenes about denim.", {"x": 1},
            "https://api.groq.com/openai/v1/chat/completions", "k",
            "openai/gpt-oss-120b", "groq", drop_schema=True)
        body = sent[0]
        text = body["messages"][0]["content"]
        checks2 = [
            ("response_format is genuinely gone",
             "response_format" not in body),
            ("the prompt still asks for JSON, in words",
             "json" in text.lower()),
            ("it is not the bare original prompt",
             text != "Write eight scenes about denim."),
        ]
        for what, ok in checks2:
            print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
            bad += not ok
    finally:
        restore()
    return bad


def scripted_provider(replies):
    """
    Fake urlopen with a SCRIPTED sequence of replies, one per request.

    capture_requests() above always answers `{"ok": true}`, which cannot
    express the fault that killed runs 48, 49 and 50: a 400 followed by a
    reply that is empty in the one field the code reads. Each entry here is
    either an Exception to raise or a dict to return as the message object,
    so the exact shape Groq sends can be reproduced.
    """
    import urllib.request as U
    sent = []
    real = U.urlopen
    seq = list(replies)

    class Reply:
        def __init__(self, message, finish="stop"):
            self._t = json.dumps(
                {"choices": [{"message": message, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 3775, "completion_tokens": 2458,
                           "completion_tokens_details":
                               {"reasoning_tokens": 900}}}).encode()

        def read(self):
            return self._t

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        sent.append(json.loads(req.data.decode()))
        nxt = seq.pop(0) if seq else {"content": '{"ok": true}'}
        if isinstance(nxt, Exception):
            raise nxt
        msg = dict(nxt) if isinstance(nxt, dict) else {"content": nxt}
        # "_finish" lets a case say the reply was CUT OFF, which is a
        # different thing from a reply that is merely malformed - and the
        # difference is the whole point of the run-53 fix. The first version
        # of that test forgot to set it, so the truncation branch never ran
        # and the check that the retry shrinks correctly failed.
        return Reply(msg, msg.pop("_finish", "stop"))

    U.urlopen = fake
    return sent, (lambda: setattr(U, "urlopen", real))


def test_reasoning_model_answers():
    """
    RUNS 48, 49 AND 50 - one fault, three runs, two stages.

        ! groq HTTP 400 "Failed to validate JSON"
          -> dropping response_schema, retrying plain JSON
        ! groq attempt 2/2: empty response
        FATAL

    gpt-oss-120b is a reasoning model. Groq's default reasoning_effort is
    "medium" and the thinking comes back in a SEPARATE `reasoning` field.
    So the model spent the reply budget thinking, the JSON was cut off
    mid-structure (the 400), and once the schema was dropped `content` was
    empty while `reasoning` held everything it had produced. brain.py read
    only `content`, so an answer that arrived was reported as no answer.

    Faked at urlopen, not at _openai_compatible: the fix lives inside that
    function, and an earlier test in this file faked the function itself and
    so never ran the code it claimed to be testing.
    """
    line("a reasoning model's reply is not thrown away (runs 48/49/50)")
    bad = 0
    B._openai_compatible = REAL_OAI

    sent, restore = scripted_provider([{"content": '{"ok": true}'}])
    try:
        REAL_OAI("Write eight scenes. Reply with json.", {"x": 1},
                 "https://api.groq.com/openai/v1/chat/completions", "k",
                 "openai/gpt-oss-120b", "groq")
        checks = [
            ("reasoning_effort is sent to gpt-oss",
             "reasoning_effort" in sent[0]),
            ("and it asks for LESS thinking, not the medium default",
             sent[0].get("reasoning_effort") == "low"),
        ]
        for what, ok in checks:
            print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
            bad += not ok
    finally:
        restore()

    # A model that does not take the parameter must not be sent it - that
    # would replace one 400 with another.
    sent, restore = scripted_provider([{"content": '{"ok": true}'}])
    try:
        REAL_OAI("Reply with json.", {"x": 1}, "https://x/v1", "k",
                 "llama-3.3-70b-versatile", "groq")
        ok = "reasoning_effort" not in sent[0]
        print(f"  {'ok  ' if ok else 'FAIL'}  "
              f"a non-reasoning model is NOT sent reasoning_effort")
        bad += not ok
    finally:
        restore()

    # THE REGRESSION ITSELF: content empty, reasoning full.
    sent, restore = scripted_provider([
        {"content": "", "reasoning": 'thinking... {"ok": true}'}])
    try:
        out = REAL_OAI("Reply with json.", {"x": 1}, "https://x/v1", "k",
                       "openai/gpt-oss-120b", "groq", drop_schema=True)
        # Parsed defensively ON PURPOSE. Without the fix `out` is "", and
        # json.loads("") raises - which killed the whole suite with a
        # traceback instead of printing FAIL against the check that found
        # the bug. A regression that explodes says less than one that
        # reports.
        try:
            parsed = json.loads(B.strip_fences(out))
        except Exception:
            parsed = None
        checks = [
            ("an empty content field is not the end of the story",
             bool(out)),
            ("the JSON is dug out of the reasoning field",
             parsed == {"ok": True}),
        ]
        for what, ok in checks:
            print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
            bad += not ok
    finally:
        restore()

    # And when BOTH are empty it must still report empty - the fallback must
    # not invent an answer out of nothing.
    sent, restore = scripted_provider([{"content": "", "reasoning": ""}])
    try:
        out = REAL_OAI("Reply with json.", {"x": 1}, "https://x/v1", "k",
                       "openai/gpt-oss-120b", "groq", drop_schema=True)
        ok = out == ""
        print(f"  {'ok  ' if ok else 'FAIL'}  "
              f"a genuinely empty reply is still empty")
        bad += not ok
    finally:
        restore()
    return bad


def test_schema_drop_gets_its_own_attempt():
    """
    The schema drop used to eat the last attempt.

    With retries=2 the 400 arrived on attempt 1, so the schema-less retry WAS
    attempt 2 - and one empty reply then abandoned a provider that was up and
    answering. That is precisely how runs 49 and 50 reached FATAL. Changing
    the shape of a request is not an attempt at it.
    """
    line("dropping the schema does not spend the last attempt (49/50)")
    bad = 0
    B._openai_compatible = REAL_OAI
    def four_hundred(message, code):
        return urllib.error.HTTPError(
            "https://x", 400, "Bad Request", {},
            io.BytesIO(json.dumps({"error": {
                "message": message,
                # NOTE: deliberately NOT "invalid_request_error". That type
                # name is the only reason the old branch matched at all, and
                # a test that leaves it in is testing the accident, not the
                # fix.
                "type": "bad_request_error",
                "code": code,
                "failed_generation": '{"scenes": [{"narration": "Green coffee'
                }}).encode()))

    err = four_hundred("Failed to validate JSON. Please adjust your prompt. "
                       "See 'failed_generation' for more details.",
                       "json_validate_failed")
    # 400 -> schema dropped -> empty reply -> and there must STILL be a try
    # left, which succeeds.
    sent, restore = scripted_provider([
        err,
        {"content": "", "reasoning": ""},
        {"content": '{"ok": true}'},
    ])
    try:
        B.PROVIDER_COOLDOWN.clear()
        out = B._call_sweep("Reply with json.", {"x": 1},
                            provs=[("groq", ("https://x/v1", "k",
                                             "openai/gpt-oss-120b"))],
                            retries=2)
        checks = [
            ("it got an answer instead of dying", out == {"ok": True}),
            ("that took three requests, not two", len(sent) == 3),
            ("the schema was gone from the retry",
             "response_format" not in sent[1]),
        ]
        for what, ok in checks:
            print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
            bad += not ok
    except Exception as e:
        print(f"  FAIL  it still died: {str(e)[:120]}")
        bad += 1
    finally:
        restore()

    # RUN 52: A SECOND WORDING FOR THE SAME FAULT.
    #
    # The commit that named "failed to VALIDATE json" shipped, and the very
    # next run came back with "Failed to GENERATE JSON" - which the new
    # explicit string missed, and which the incidental "invalid" caught
    # again. Both wordings, and the machine-readable codes under them, are
    # now pinned here so the next wording is a test failure and not a
    # silently dead safety branch.
    for message, code in (
            ("Failed to validate JSON. Please adjust your prompt.",
             "json_validate_failed"),
            ("Failed to generate JSON. Please adjust your prompt.",
             "json_generate_failed")):
        sent, restore = scripted_provider(
            [four_hundred(message, code), {"content": '{"ok": true}'}])
        try:
            B.PROVIDER_COOLDOWN.clear()
            out = B._call_sweep("Reply with json.", {"x": 1},
                                provs=[("groq", ("https://x/v1", "k",
                                                 "openai/gpt-oss-120b"))],
                                retries=2)
            ok = out == {"ok": True} and "response_format" not in sent[1]
        except Exception:
            ok = False
        print(f"  {'ok  ' if ok else 'FAIL'}  {code} drops the schema "
              f"(no help from the word 'invalid')")
        bad += not ok
    return bad


def test_truncated_and_wrong_shape():
    """
    RUN 53 - two faults, one run, and the traceback added that morning found
    both in a single log.

        ! groq HTTP 400 "Failed to generate JSON"
          -> dropping response_schema, retrying plain JSON
        ! groq attempt 2/3: HTTP 429 -> waiting 20s
          -> groq stopped early: finish_reason=length
        ! groq attempt 3/3: Expecting ',' delimiter: line 78 column 6 (char 10242)
        FATAL: KeyError: 'scenes'
          File "brain.py", line 1275, in draft
            print(f"... {len(data['scenes'])} scenes")

    ONE: the reply was CUT OFF. finish_reason=length, an object that stopped
    mid-structure at ten thousand characters. Groq's per-minute budget covers
    the request and the reply together, so an 11-scene script does not fit
    behind a 16,000-character prompt. Waiting cannot help; only sending less
    can, which is exactly what the 413 path already does.

    TWO: something eventually came back that PARSED and had no "scenes" - most
    likely a real object dug out of the model's own thinking by the reasoning
    fallback added that same morning. json_object mode promises the reply
    parses and nothing about what is in it, so `call()` returned it and the
    caller found out by subscripting it, four stack frames from any mention of
    a provider.
    """
    line("a cut-off or wrong-shaped reply is not an answer (run 53)")
    bad = 0
    B._openai_compatible = REAL_OAI
    B.PROVIDER_CHAR_CAP = {}
    schema = {"type": "object", "properties": {}, "required": ["scenes"]}

    # ONE. finish_reason=length must not be handed on as a half-object, and
    # the request that follows must be SMALLER.
    sent, restore = scripted_provider([
        {"content": '{"scenes": [{"narration": "cut off here',
         "_finish": "length"},
        {"content": '{"scenes": [1]}'},
    ])
    try:
        B.PROVIDER_COOLDOWN.clear()
        out = B._call_sweep("SOURCE. " * 4000 + "\nReply with json.", schema,
                            provs=[("groq", ("https://x/v1", "k",
                                             "openai/gpt-oss-120b"))],
                            retries=3)
        sizes = [len(b["messages"][0]["content"]) for b in sent]
        checks = [
            ("it recovered instead of dying", out == {"scenes": [1]}),
            ("the truncated object was NOT parsed and returned", len(sent) > 1),
            (f"the retry was smaller ({sizes})",
             len(sizes) > 1 and sizes[1] < sizes[0]),
        ]
    except Exception as e:
        checks = [(f"it recovered instead of dying (raised {e})", False)]
    finally:
        restore()
    for what, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
        bad += not ok

    # TWO. A reply that parses but has no "scenes" is a failed attempt, not a
    # result - so the NEXT attempt runs and the caller never sees the wrong
    # object.
    sent, restore = scripted_provider([
        {"content": '{"title": "Blood Types", "question": "what?"}'},
        {"content": '{"scenes": [1, 2]}'},
    ])
    try:
        B.PROVIDER_COOLDOWN.clear()
        out = B._call_sweep("Reply with json.", schema,
                            provs=[("groq", ("https://x/v1", "k",
                                             "openai/gpt-oss-120b"))],
                            retries=2)
        ok_shape = out == {"scenes": [1, 2]}
    except Exception:
        ok_shape = False
    finally:
        restore()
    print(f"  {'ok  ' if ok_shape else 'FAIL'}  a parsed object with no "
          f"'scenes' is retried, not returned")
    bad += not ok_shape

    # And when it never arrives, the error must name the provider - not
    # surface as a KeyError inside draft().
    sent, restore = scripted_provider([
        {"content": '{"title": "x"}'}, {"content": '{"title": "x"}'},
        {"content": '{"title": "x"}'}, {"content": '{"title": "x"}'}])
    try:
        B.PROVIDER_COOLDOWN.clear()
        B._call_sweep("Reply with json.", schema,
                      provs=[("groq", ("https://x/v1", "k",
                                       "openai/gpt-oss-120b"))], retries=2)
        why = "returned a bad object instead of raising"
        ok = False
    except KeyError as e:
        why, ok = f"raised KeyError({e}) - the run-53 death", False
    except Exception as e:
        why, ok = f"raised {type(e).__name__}: {str(e)[:60]}", "missing required" in str(e)
    finally:
        restore()
    print(f"  {'ok  ' if ok else 'FAIL'}  a shape that never arrives says so "
          f"-- {why}")
    bad += not ok
    return bad


def test_unschemad_draft_survives():
    """
    RUN 51 - the fix above worked, and the run died one stage later.

        [2/5] drafting ... ok
        [3/5] ... verifying against the web...
        FATAL: 'scene'

    SCRIPT_SCHEMA marks "scene" required and Gemini honours it, because it is
    sent as a real response schema. Groq is sent
    `response_format: {"type": "json_object"}`, which promises only that the
    reply PARSES - any shape satisfies it. So a fallback-written script is
    unchecked in shape, and the fact-checker's f'SCENE {s["scene"]}' raised a
    bare KeyError on a draft whose scenes had no such key. Run 48's happened
    to have one; nothing had ever noticed the difference.
    """
    line("a fallback draft with no 'scene' key does not kill the run (51)")
    bad = 0
    groqish = {"title": "T", "scenes": [
        {"beat": "ANSWER", "narration": "One.", "key_term": "One",
         "key_fact": "f", "image_keywords": ["a"]},
        {"beat": "CATEGORY", "narration": "Two.", "key_term": "Two",
         "key_fact": "f", "image_keywords": ["b"]},
    ]}
    out = B.number_scenes(groqish, "draft")
    checks = [
        ("every scene is numbered",
         [s.get("scene") for s in out["scenes"]] == [1, 2]),
        ("the fact-checker's own expression no longer raises",
         "\n".join(f'SCENE {s["scene"]}' for s in out["scenes"])
         == "SCENE 1\nSCENE 2"),
    ]

    # Numbering is POSITIONAL, not the model's opinion - brain.py already
    # renumbers this way before writing script.json, so a model that numbers
    # its scenes 5, 9 was never being believed anyway.
    lied = {"scenes": [{"scene": 5, "narration": "a", "beat": "b",
                        "key_term": "k", "key_fact": "f",
                        "image_keywords": []},
                       {"scene": 9, "narration": "b", "beat": "b",
                        "key_term": "k", "key_fact": "f",
                        "image_keywords": []}]}
    checks.append(("bad numbering from the model is corrected, not trusted",
                   [s["scene"] for s in
                    B.number_scenes(lied, "draft")["scenes"]] == [1, 2]))
    # Must not explode on the shapes a loose json_object reply can really be.
    for name, arg in (("no scenes key", {}),
                      ("scenes is empty", {"scenes": []}),
                      ("a scene is not an object", {"scenes": ["oops"]})):
        try:
            B.number_scenes(arg, "draft")
            ok = True
        except Exception as e:
            ok = False
            print(f"        raised {type(e).__name__}: {e}")
        checks.append((f"survives {name}", ok))

    for what, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
        bad += not ok
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

    # A quality drop the owner cannot see is the fault this project keeps
    # being caught by. A trimmed call must leave a trace in the output.
    cut, total = B.TRIMMED_CALLS
    checks2 = [
        ("the trim was counted, not silent", cut >= 1),
        ("the fallback writer was recorded", B.PROVIDER_USE.get("groq", 0) >= 1),
    ]
    for what, ok in checks2:
        print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
        bad += not ok
    print(f"  recorded           : {cut} of {total} calls trimmed, "
          f"providers {dict(B.PROVIDER_USE)}")
    return bad


def test_cooldown():
    line("a provider that is out for the day is not asked again every call")
    bad = 0
    B.PROVIDER_COOLDOWN.clear()
    B.PROVIDER_CHAR_CAP = {}
    tried = []

    def dead_gemini(prompt, schema, drop_schema):
        tried.append("gemini")
        raise RuntimeError("429 RESOURCE_EXHAUSTED. You exceeded your "
                           "current quota")

    def live_groq(prompt, schema, base_url, key, model, name,
                  drop_schema=False):
        tried.append("groq")
        return '{"ok": true}'

    B._gemini = dead_gemini
    B._openai_compatible = live_groq
    B._providers = lambda: [("gemini", None),
                            ("groq", ("u", "k", "openai/gpt-oss-120b"))]

    # First call pays the wait and discovers gemini is out.
    B.COOLDOWN_SECONDS = 420
    real_sleep, B.time.sleep = B.time.sleep, lambda s: None
    try:
        B.call("write something", schema={"x": 1}, retries=2)
        first = list(tried)
        tried.clear()
        # Three more calls. Gemini must not be asked again.
        for _ in range(3):
            B.call("write something", schema={"x": 1}, retries=2)
    finally:
        B.time.sleep = real_sleep

    checks = [
        ("the first call did try gemini", "gemini" in first),
        ("it fell through to groq", "groq" in first),
        ("gemini was parked after that", "gemini" not in tried),
        ("groq answered all three later calls",
         tried.count("groq") == 3),
    ]
    for what, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
        bad += not ok
    print(f"  first call: {first}   next three: {tried}")

    # THE DANGEROUS CASE. If every provider is parked, skipping them all
    # means the run dies of its own cooldown - a far worse failure than one
    # more wasted 429. It must try anyway.
    line("when EVERY provider is cooling off, it still tries")
    tried.clear()
    B.PROVIDER_COOLDOWN["gemini"] = time.time() + 999
    B.PROVIDER_COOLDOWN["groq"] = time.time() + 999
    got = B.call("write something", schema={"x": 1}, retries=2)
    ok = got == {"ok": True} and "groq" in tried
    print(f"  {'ok  ' if ok else 'FAIL'}  did not die of its own cooldown "
          f"(tried {tried})")
    bad += not ok
    B.PROVIDER_COOLDOWN.clear()
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



def test_no_scene_loss():
    """
    A revision may not delete scenes. Run 39's actual failure.

    It drafted 11 scenes and shipped 2 - an 8-minute video that came out at
    78 seconds - because three separate stages accepted a candidate on a
    COUNT OF PROBLEMS, and deleting a scene deletes its problems. A shorter
    script wins every one of those comparisons, so the loop was rewarded for
    throwing the video away.
    """
    line("a revision may not delete scenes (run 39's actual failure)")
    bad = 0
    eleven = {"scenes": [{"scene": i + 1, "narration": f"n{i}"}
                         for i in range(11)]}
    checks = [
        ("11 scenes -> 2 is rejected",
         not B.keeps_scenes(eleven, {"scenes": eleven["scenes"][:2]}, "x")),
        ("losing a single scene is rejected",
         not B.keeps_scenes(eleven, {"scenes": eleven["scenes"][:10]}, "x")),
        ("the same count is fine",
         B.keeps_scenes(eleven, {"scenes": eleven["scenes"]}, "x")),
        ("adding a scene is fine",
         B.keeps_scenes(eleven,
                        {"scenes": eleven["scenes"] + [{"scene": 12}]}, "x")),
        ("an empty candidate is rejected",
         not B.keeps_scenes(eleven, {"scenes": []}, "x")),
        ("a candidate with no scenes key is rejected",
         not B.keeps_scenes(eleven, {}, "x")),
    ]
    for what, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
        bad += not ok

    # All three stages must use it - one hole became three because each had
    # its own acceptance test.
    # Matched with a regex over collapsed whitespace: the call sites wrap
    # across lines and indent differently, and a literal-substring check that
    # breaks when a line is re-wrapped is a test that fails for the wrong
    # reason - which trains people to ignore it.
    import re as _re
    src = _re.sub(r"\s+", " ", open("brain.py").read())
    for name in ("revision", "red-team repair", "shape repair"):
        ok = bool(_re.search(
            rf'keeps_scenes\( *\w+, *\w+, *"{_re.escape(name)}"', src))
        print(f"  {'ok  ' if ok else 'FAIL'}  the {name} stage calls it")
        bad += not ok
    return bad



def test_targeted_repair():
    """
    The red-team repair must be SMALL, and it must not be able to damage the
    script it is repairing.

    It is stage 6 of 6, so it runs when the quota is most depleted - and it
    used to send the whole script plus the brief and ask for the whole script
    back, to fix a handful of sentences. Runs 38, 39 and 41 all end the same
    way: "red-team repair failed ... 429". The most important call in the
    pipeline was the last and the largest, so it never once succeeded, and
    every video shipped with findings the system had already identified.
    """
    line("the red-team repair is small, and cannot damage the script")
    bad = 0
    scenes = [{"scene": i + 1, "beat": "CATEGORY", "key_term": f"term {i+1}",
               "key_fact": "a fact about this cut",
               "narration": "word " * 116,
               "image_keywords": [f"kw{j}" for j in range(9)]}
              for i in range(11)]
    data = {"title": "Every Type of Men's Jeans Explained",
            "description": "d" * 300, "question": "q" * 160,
            "tags": ["a"] * 12, "scenes": scenes}
    scoped = [2, 5, 8]
    slim = [{k: s.get(k) for k in
             ("scene", "beat", "key_term", "key_fact", "narration")}
            for s in scenes if s["scene"] in scoped]
    whole, part = len(json.dumps(data, indent=2)), len(json.dumps(slim, indent=2))
    print(f"  whole script : {whole:,} chars")
    print(f"  3 scenes     : {part:,} chars  ({100 - 100*part//whole}% smaller)")

    fixed = [{"scene": 2, "narration": "repaired two", "key_term": "term 2"},
             {"scene": 47, "narration": "A SCENE THAT DOES NOT EXIST"},
             {"scene": 8, "narration": "repaired eight", "key_term": "term 8"}]
    out, applied = B.merge_scene_fixes(data, fixed)

    checks = [
        ("the targeted prompt is at least half the size", part < whole / 2),
        ("it fits the fallback writer's cap", part < B.PROVIDER_CHAR_CAP.get("groq", 16000)),
        ("both real fixes were applied", applied == 2),
        ("the scene count is unchanged", len(out["scenes"]) == len(scenes)),
        ("scene 2 was repaired", out["scenes"][1]["narration"] == "repaired two"),
        ("scene 3 was left alone",
         out["scenes"][2]["narration"] == scenes[2]["narration"]),
        ("an invented scene number is ignored",
         all(s["scene"] != 47 for s in out["scenes"])),
        ("image_keywords survive the repair",
         out["scenes"][1]["image_keywords"] == scenes[1]["image_keywords"]),
        ("the caller's script is not mutated",
         data["scenes"][1]["narration"].startswith("word")),
        ("an empty reply changes nothing",
         B.merge_scene_fixes(data, [])[1] == 0),
        ("a malformed reply changes nothing",
         B.merge_scene_fixes(data, ["nonsense", {"no_scene": 1}])[1] == 0),
    ]
    for what, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
        bad += not ok
    return bad



def test_membership_gate_threshold():
    """
    A verified list too short to BE the category must not judge the script.

    Run 45: the gate produced its first ever list - one member, "Muay Thai" -
    and that single word made 15 hard not-a-member findings against an
    eleven-scene script. Seven scenes were gagged and seven of twelve frames
    came out black. The category had not been established; the failure to
    establish it was recorded as a verdict.
    """
    line("a one-member 'verified list' must not gate an 11-scene script")
    bad = 0
    checks = [
        ("no list at all does not gate", B.enforceable_members([]) is None),
        ("None does not gate", B.enforceable_members(None) is None),
        ("ONE member does not gate (run 45's exact case)",
         B.enforceable_members(["Muay Thai"]) is None),
        ("two members still do not gate",
         B.enforceable_members(["Muay Thai", "Judo"]) is None),
        ("three members DO gate - topics.MIN_MEMBERS is the bar",
         B.enforceable_members(["Muay Thai", "Judo", "Boxing"])
         == ["Muay Thai", "Judo", "Boxing"]),
        ("a full list gates",
         len(B.enforceable_members(["a", "b", "c", "d", "e"]) or []) == 5),
        ("the bar is topics' own constant, not a second copy",
         B.topics.MIN_MEMBERS == 3),
    ]
    for what, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
        bad += not ok
    return bad



def test_cooldown_sized_and_waited():
    """
    Run 47's real death, reproduced.

        15:48:49  gemini rate limited -> resting it for 7 min
        15:49:38  groq rate limited   -> resting it for 7 min
        15:50:23  every provider is cooling off (next free in 326s)
                  - trying them all anyway
        15:51:16  FATAL

    It died 53 seconds into a wait it had just measured at 326, with forty
    minutes of job time unused. Two faults in one: Groq's 429 is a PER-MINUTE
    token limit that clears in about a minute and was parked for seven, and
    the chain refused to sit through a cooldown it could easily have waited
    out. A cooldown that outlives the retry budget is a suicide pact.
    """
    line("a per-minute limit rests for a minute, not seven (run 47)")
    bad = 0

    GROQ_TPM = ('HTTP 429 from groq (openai/gpt-oss-120b): {"error":{"message":'
                '"Rate limit reached for model `openai/gpt-oss-120b` in '
                'organization `org_x` service tier `on_demand` on tokens per '
                'minute (TPM): Limit 8000, Used 7800","code":'
                '"rate_limit_exceeded"}}')
    GEMINI_DAY = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': "
                  "'You exceeded your current quota ... limit: "
                  "generate_content_free_tier_requests, per day'}}")

    seen = []

    def gem(prompt, schema, drop_schema):
        seen.append(("gemini", time.time()))
        raise RuntimeError(GEMINI_DAY)

    groq_state = {"fail_until": time.time() + 60}

    def groq(prompt, schema, base_url, key, model, name, drop_schema=False):
        seen.append(("groq", time.time()))
        if time.time() < groq_state["fail_until"]:
            raise RuntimeError(GROQ_TPM)
        return '{"ok": true}'

    B._gemini, B._openai_compatible = gem, groq
    B._providers = lambda: [("gemini", None),
                            ("groq", ("u", "k", "openai/gpt-oss-120b"))]
    B.PROVIDER_COOLDOWN.clear()
    B.PROVIDER_CHAR_CAP = {}

    # Virtual clock: prove the LOGIC waits, without waiting in real life.
    clock = {"t": time.time()}
    real_time, real_sleep = time.time, time.sleep
    B.time.time = lambda: clock["t"]
    B.time.sleep = lambda s: clock.__setitem__("t", clock["t"] + s)
    groq_state["fail_until"] = clock["t"] + 60
    try:
        started = clock["t"]
        got = B.call("write something", schema={"x": 1}, retries=2)
        waited = clock["t"] - started
    except Exception as e:
        got, waited = f"FAILED: {str(e)[:60]}", clock["t"] - started
    finally:
        B.time.time, B.time.sleep = real_time, real_sleep

    gem_rest = B.PROVIDER_COOLDOWN.get("gemini", 0) - clock["t"]
    checks = [
        ("the call SUCCEEDED instead of dying in the cooldown",
         got == {"ok": True}),
        ("it waited past groq's 60s per-minute limit", waited >= 60),
        ("gemini's PER DAY limit got the long rest",
         gem_rest > B.COOLDOWN_PER_MINUTE * 2),
        ("groq was retried after resting, not abandoned",
         [p for p, _ in seen].count("groq") >= 2),
        ("the wait budget exceeds a per-minute cooldown",
         B.WAIT_OUT_MAX > B.COOLDOWN_PER_MINUTE),
    ]
    for what, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
        bad += not ok
    print(f"  providers tried : {[p for p, _ in seen]}")
    print(f"  virtual time    : {waited:.0f}s waited, result {got}")
    B.PROVIDER_COOLDOWN.clear()
    return bad


def main():
    # PROVIDER_CHAR_CAP is monkeypatched by the tests above; keep the real one.
    real_cap = dict(B.PROVIDER_CHAR_CAP)
    bad = test_shrink()
    bad += test_413_shrinks_and_succeeds()
    bad += test_json_mode_400()
    bad += test_reasoning_model_answers()
    bad += test_schema_drop_gets_its_own_attempt()
    bad += test_unschemad_draft_survives()
    bad += test_truncated_and_wrong_shape()
    bad += test_precap_avoids_the_round_trip()
    bad += test_cooldown()
    bad += test_no_scene_loss()
    bad += test_targeted_repair()
    bad += test_membership_gate_threshold()
    bad += test_cooldown_sized_and_waited()
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
