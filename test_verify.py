#!/usr/bin/env python3
"""
test_verify.py — the video verifier's logic, without a network.

verify.py's only real risk is not the model: it is what happens to a reply
that is malformed, empty, wrapped in prose, or quietly wrong. A check that
crashes on a bad reply costs a run; one that invents findings from garbage
is worse, because it sends the owner hunting for problems that are not
there. Both are testable with no key and no upload, which is why watch()
takes its caller as an argument.

    python3 test_verify.py
"""

import json
import verify

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


MIXED = json.dumps({"findings": [
    {"at": 40.0, "severity": "soft", "kind": "repeat shot",
     "detail": "same sewing machine twice", "fix": "vary the keyword"},
    {"at": 12.5, "severity": "hard", "kind": "text cut off",
     "detail": "heading clipped mid-word", "fix": "fit to one line"},
    {"at": 3.0, "severity": "hard", "kind": "no header",
     "detail": "section header missing", "fix": "draw the overlay"},
]})


@case
def sorts_hard_first_then_by_time():
    got = [f["kind"] for f in verify._parse(MIXED)]
    return got == ["no header", "text cut off", "repeat shot"], got


@case
def json_wrapped_in_prose_still_parses():
    raw = ('Sure! Here you go:\n```json\n{"findings":[{"at":1,'
           '"severity":"hard","kind":"k","detail":"d"}]}\n```')
    return len(verify._parse(raw)) == 1, verify._parse(raw)


@case
def a_clean_video_stays_clean():
    # The prompt tells it not to invent problems; this proves an empty list
    # survives the parser rather than being turned into something.
    return verify._parse('{"findings": []}') == [], "not empty"


@case
def garbage_never_becomes_a_finding():
    bad = ["the video looks fine to me", None, "", "{oops",
           '{"findings": "lots"}', '{"nope": []}']
    got = [verify._parse(b) for b in bad]
    return all(g == [] for g in got), got


@case
def a_finding_with_no_detail_is_dropped():
    raw = '{"findings":[{"at":5,"severity":"hard","kind":"x","detail":"  "}]}'
    return verify._parse(raw) == [], verify._parse(raw)


@case
def an_unparseable_timestamp_does_not_crash():
    raw = '{"findings":[{"at":"halfway","severity":"hard","kind":"x","detail":"d"}]}'
    f = verify._parse(raw)
    return len(f) == 1 and f[0]["at"] is None, f


@case
def unknown_severity_defaults_to_soft():
    # Never upgrade to hard on a word we do not recognise: a hard finding is
    # the one that makes a human go and look.
    raw = '{"findings":[{"at":1,"severity":"catastrophic","kind":"x","detail":"d"}]}'
    return verify._parse(raw)[0]["severity"] == "soft", verify._parse(raw)


@case
def the_script_digest_reaches_the_prompt():
    seen = {}

    def ask(prompt, video):
        seen.update(prompt=prompt, video=video)
        return '{"findings":[]}'

    verify.watch("v.mp4", {"scenes": [
        {"scene": 1, "beat": "CATEGORY", "key_term": "slim fit",
         "narration": "Slim fit jeans taper below the knee."}]}, ask)
    return ("slim fit" in seen["prompt"] and seen["video"] == "v.mp4",
            seen.get("video"))


@case
def hard_findings_are_counted():
    _, hard = verify.report(verify._parse(MIXED))
    return hard == 2, hard


if __name__ == "__main__":
    bad = 0
    for fn in CASES:
        ok, detail = fn()
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {fn.__name__}"
              f"{'' if ok else f'  -> {detail}'}")
    print(f"\n{len(CASES) - bad}/{len(CASES)} "
          f"{'ALL PASS' if not bad else 'FAILURES ABOVE'}")
    raise SystemExit(1 if bad else 0)
