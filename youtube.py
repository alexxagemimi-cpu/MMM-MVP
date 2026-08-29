#!/usr/bin/env python3
"""
youtube.py — measure whether anyone actually wants a topic.

WHY
---
topics.py answers "does this topic have a real answer?" That is the accuracy
gate, and it is necessary but nowhere near sufficient. "Types of doorknob
hinge" has a perfectly real closed answer and nobody on earth wants the
video. Truth and demand are different questions and the system needs both.

This is the demand half, and it is measured rather than reasoned about,
because a model asked "is this a good topic?" will say yes to anything.

QUOTA IS THE DESIGN CONSTRAINT
------------------------------
The YouTube Data API is free with no card, metered at 10,000 units a day,
resetting midnight Pacific. The costs are wildly uneven and that shapes
everything here:

    search.list    100 units      videos.list    1 unit      channels.list  1

So one search costs as much as a hundred detail lookups. The rule followed
throughout: search ONCE per topic, then batch every id into single
videos.list and channels.list calls. That is 102 units per topic assessed,
about 98 topics a day - far more than needed, and it stays that way only
because nothing here ever searches in a loop.

THE SIGNAL THAT ACTUALLY MATTERS
--------------------------------
Raw view counts mostly measure how big the channels are, not how good the
topic is. For a channel starting from zero the useful question is different:

    on this topic, do videos get far more views than their channel has
    subscribers?

A high ratio means the topic travels on search and suggestion rather than on
an existing audience - which is the only way a new channel ever gets seen. A
topic where every top result comes from a million-subscriber channel and gets
proportionally modest views is a wall, not an opening. That ratio is the
headline number here, not total views.

HONEST LIMIT
------------
These are proxies. Views measure what already succeeded, which is survivorship
bias with a nice interface. This makes bad choices rarer; it does not make
good ones certain, and nothing in this file should be read as a prediction.
"""

import os
import re
import json
import statistics
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://www.googleapis.com/youtube/v3/"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

SEARCH_COST, LIST_COST = 100, 1

# Shorts skew everything - a 40-second clip and a 12-minute explainer are not
# competing for the same viewer, and Shorts view counts are an order of
# magnitude larger. Anything under this is excluded from the statistics.
MIN_SECONDS = 120


class QuotaError(RuntimeError):
    pass


def _get(endpoint, params, key):
    params = dict(params, key=key)
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        # 403 here is nearly always quota exhausted or the API not enabled on
        # the project, and those need opposite fixes - so say which.
        if e.code == 403 and "quota" in body.lower():
            raise QuotaError(f"YouTube daily quota exhausted: {body[:160]}")
        raise RuntimeError(f"YouTube HTTP {e.code}: {body[:220]}")


def _iso_seconds(s):
    """PT12M34S -> 754. Returns 0 for anything unparseable."""
    m = re.match(r"^P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", s or "")
    if not m:
        return 0
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


def probe(topic, key=None, n=25):
    """
    One search plus two batched lookups -> the raw rows for a topic.
    Costs exactly SEARCH_COST + 2 * LIST_COST units. Never loops.
    """
    key = key or os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY is not set")

    res = _get("search", {
        "part": "snippet", "q": topic, "type": "video",
        "maxResults": min(n, 50), "relevanceLanguage": "en",
        "order": "relevance",
    }, key)
    items = res.get("items", [])
    vids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]
    if not vids:
        return []

    stats = _get("videos", {"part": "statistics,contentDetails,snippet",
                            "id": ",".join(vids)}, key).get("items", [])
    ch_ids = sorted({v["snippet"]["channelId"] for v in stats
                     if v.get("snippet", {}).get("channelId")})
    subs = {}
    if ch_ids:
        for c in _get("channels", {"part": "statistics",
                                   "id": ",".join(ch_ids[:50])}, key).get("items", []):
            subs[c["id"]] = int(c.get("statistics", {})
                                 .get("subscriberCount", 0) or 0)

    rows = []
    for v in stats:
        sn, st = v.get("snippet", {}), v.get("statistics", {})
        secs = _iso_seconds(v.get("contentDetails", {}).get("duration", ""))
        if secs < MIN_SECONDS:
            continue
        rows.append({
            "title": sn.get("title", ""),
            "channel": sn.get("channelTitle", ""),
            "published": sn.get("publishedAt", ""),
            "seconds": secs,
            "views": int(st.get("viewCount", 0) or 0),
            "subs": subs.get(sn.get("channelId"), 0),
        })
    return rows


def measure(rows, now=None):
    """
    Rows -> the numbers. Deterministic; no model involved.

    breakout is the headline: median of views divided by the channel's
    subscriber count. Above ~1 means videos on this topic routinely outrun
    the audience that already existed, which is what a channel starting from
    zero needs. Channels under 1000 subs are floored to 1000 so a brand-new
    channel with one lucky video cannot produce a meaningless huge ratio.
    """
    if not rows:
        return {"results": 0}
    now = now or datetime.now(timezone.utc)

    views = [r["views"] for r in rows]
    ratios = [r["views"] / max(r["subs"], 1000) for r in rows]

    ages = []
    for r in rows:
        try:
            d = datetime.fromisoformat(r["published"].replace("Z", "+00:00"))
            ages.append((now - d).days / 365.25)
        except Exception:
            pass
    fresh = (sum(1 for a in ages if a <= 2) / len(ages)) if ages else 0.0

    return {
        "results": len(rows),
        "median_views": int(statistics.median(views)),
        "top_views": max(views),
        "breakout": round(statistics.median(ratios), 2),
        "fresh_2y": round(fresh, 2),
        "median_len_min": round(statistics.median(
            [r["seconds"] for r in rows]) / 60, 1),
        "big_channel_share": round(
            sum(1 for r in rows if r["subs"] > 1_000_000) / len(rows), 2),
    }


def verdict(m):
    """
    Absolute kill rules only, plus the numbers. Ranking between candidates is
    left to the caller, because comparing several real topics is far more
    defensible than asserting a threshold none of us has calibrated yet.
    """
    if not m or m.get("results", 0) < 5:
        return False, ["fewer than 5 real videos exist on this - too obscure "
                       "to have an audience, or phrased in a way nobody "
                       "searches"]
    bad = []
    if m["median_views"] < 5000:
        bad.append(f"median {m['median_views']:,} views - the videos that "
                   f"exist are not being watched")
    if m["breakout"] < 0.35:
        bad.append(f"breakout {m['breakout']} - videos here get far fewer "
                   f"views than their channels have subscribers, so this "
                   f"travels on existing audience, not on search. A new "
                   f"channel would be invisible")
    if m["fresh_2y"] < 0.20:
        bad.append(f"only {int(m['fresh_2y']*100)}% of top results are from "
                   f"the last 2 years - interest has moved on")
    return (not bad), (bad or [
        f"{m['results']} real videos, median {m['median_views']:,} views, "
        f"breakout {m['breakout']}, {int(m['fresh_2y']*100)}% recent"])


def score(m):
    """
    Single comparable number for ranking candidates against each other.
    Breakout dominates deliberately - it is the only one of these that says
    anything about whether WE could be seen.

    Gated on verdict() rather than trusting the caller to check first. A
    topic whose top results are all seven years old scored 0.82 here, above
    a live-but-walled topic at 0.44, purely because its breakout was high;
    ranking on that number alone would have picked a dead topic as the best
    of the batch. Anything the kill rules reject scores zero.
    """
    if not m or m.get("results", 0) < 5:
        return 0.0
    ok, _ = verdict(m)
    if not ok:
        return 0.0
    import math
    reach = min(1.0, math.log10(max(m["median_views"], 1)) / 6.0)
    breakout = min(1.0, m["breakout"] / 3.0)
    return round(0.55 * breakout + 0.28 * reach + 0.17 * m["fresh_2y"], 3)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        rows = probe(" ".join(sys.argv[1:]))
        m = measure(rows)
        ok, why = verdict(m)
        print(json.dumps(m, indent=2))
        print("VERDICT:", "PURSUE" if ok else "SKIP")
        for w in why:
            print(" -", w)
        print("score:", score(m))
    else:
        print("usage: python3 youtube.py <topic>   (needs YOUTUBE_API_KEY)")
