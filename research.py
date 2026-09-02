#!/usr/bin/env python3
"""
research.py — web search + page reading, independent of any LLM provider.

WHY THIS EXISTS
---------------
brain.py used to research via Gemini's built-in google_search tool. That
welded "can this project do web research at all" to one provider's search
grounding quota - a bucket (~5k/month on the free tier) that is metered
SEPARATELY from normal generateContent calls. When it ran out, every run
died at stage 1 with a 429 even though the plain-text model quota was
completely untouched.

Doing the search ourselves fixes that at the architecture level:
  - research works on ANY LLM, including providers with no search tool
  - source URLs are real objects we hold, not metadata we hope came back,
    so brain.py's STRICT_FACTS check ("did real sources come back?")
    becomes a fact rather than an inference
  - the search provider can fail over independently of the writer model

PROVIDER CHAIN (first one that returns results wins):
  1. Tavily      - needs TAVILY_API_KEY. Free tier: 1000 credits/month,
                   no card. Returns cleaned page content, not just snippets.
  2. DuckDuckGo  - needs NOTHING. No key, no signup, no quota to exhaust.
                   Snippets only, so we optionally fetch the pages ourselves.

The DuckDuckGo leg is why this module never hard-fails on a missing key:
the zero-budget default path still does real grounded research.
"""

import os
import re
import html
import json
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PAGE_TIMEOUT   = 10      # per-page read; short on purpose, a slow source is skipped
PAGE_CHARS     = 3500    # per-page cap fed to the model
MAX_PAGE_BYTES = 2_000_000
FETCH_WORKERS  = 8


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------
def _tavily(query, max_results):
    """Tavily search. Returns [] (never raises) so the chain can fall through."""
    if not TAVILY_API_KEY:
        return []
    try:
        body = json.dumps({
            "api_key": TAVILY_API_KEY,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
        }).encode()
        req = urllib.request.Request(
            "https://api.tavily.com/search", data=body,
            headers={"Content-Type": "application/json", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read())
        out = []
        for h in data.get("results", []):
            if h.get("url"):
                out.append({"title": (h.get("title") or "").strip(),
                            "url": h["url"],
                            "content": (h.get("content") or "").strip()})
        return out
    except Exception as e:
        print(f"      [research] tavily failed: {str(e)[:120]}", flush=True)
        return []


def _duckduckgo(query, max_results):
    """Keyless DuckDuckGo search. Snippets only - pages fetched separately."""
    try:
        from ddgs import DDGS
    except ImportError:
        print("      [research] ddgs not installed", flush=True)
        return []
    try:
        hits = DDGS().text(query, max_results=max_results, region="us-en")
        out = []
        for h in hits or []:
            url = h.get("href") or h.get("url") or h.get("link")
            if url:
                out.append({"title": (h.get("title") or "").strip(),
                            "url": url,
                            "content": (h.get("body") or "").strip()})
        return out
    except Exception as e:
        print(f"      [research] duckduckgo failed: {str(e)[:120]}", flush=True)
        return []


def _wikipedia(query, max_results):
    """
    Keyless Wikipedia search. Free, no signup, no quota, and unusually
    reliable next to the alternatives.

    Added because a real run's fact-check came back with 27 of 27 claims
    "UNSUPPORTED - not mentioned in the sources", while four of that run's
    searches had errored outright (DuckDuckGo's backend returning
    ConnectError against grokipedia and google.com). Absence of evidence
    reported as a fact problem, when the absence was ours.

    It is also the right shape for what keeps failing. The unconfirmed
    claims are definitional - what "leg" means in denim, what selvedge is,
    which rises exist - and an encyclopedia is precisely the source that
    settles those, where a shopping result is not.
    """
    try:
        base = "https://en.wikipedia.org/w/api.php"
        q = urllib.parse.urlencode({
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": max_results, "format": "json", "srprop": "snippet"})
        req = urllib.request.Request(f"{base}?{q}",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        out = []
        for h in data.get("query", {}).get("search", []):
            title = h.get("title") or ""
            snippet = _TAGS.sub("", h.get("snippet") or "")
            out.append({
                "title": title,
                "url": "https://en.wikipedia.org/wiki/"
                       + urllib.parse.quote(title.replace(" ", "_")),
                "content": snippet.strip()})
        return out
    except Exception as e:
        print(f"      [research] wikipedia failed: {str(e)[:120]}", flush=True)
        return []


def search(query, max_results=6):
    """
    One query -> [{title, url, content}]. Never raises, may return [].

    Wikipedia is tried when the others come back empty rather than first:
    a general search reaches the whole web and an encyclopedia does not, so
    it is the safety net for a failed search, not a replacement for one.
    """
    for provider in (_tavily, _duckduckgo, _wikipedia):
        hits = provider(query, max_results)
        if hits:
            return hits
    return []


# ---------------------------------------------------------------------------
# page reading (depth the snippets alone don't give)
# ---------------------------------------------------------------------------
_TAG_junk = re.compile(
    r"<(script|style|noscript|svg|nav|header|footer|form)[^>]*>.*?</\1>",
    re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


def strip_html(raw):
    """Crude tag strip. Good enough to feed a model; no bs4 dependency."""
    txt = _TAG_junk.sub(" ", raw)
    txt = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", txt, flags=re.I)
    txt = _TAGS.sub(" ", txt)
    txt = html.unescape(txt)
    txt = _WS.sub(" ", txt)
    txt = _NL.sub("\n\n", txt)
    return txt.strip()


def fetch_page(url):
    """Read one page as plain text. Returns '' on any problem - never raises."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=PAGE_TIMEOUT) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and "text" not in ctype:
                return ""
            raw = r.read(MAX_PAGE_BYTES).decode(
                r.headers.get_content_charset() or "utf-8", errors="replace")
        return strip_html(raw)[:PAGE_CHARS]
    except Exception:
        return ""


def _dedupe(hits):
    seen, out = set(), []
    for h in hits:
        key = h["url"].split("#")[0].rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


def gather(queries, per_query=5, read_pages=True, max_sources=14):
    """
    Run several queries, dedupe by URL, optionally read the pages, and return
    (context_text, sources) where sources is [{title, uri}] - the same shape
    brain.py already writes into script.json's "sources".

    read_pages matters for the DuckDuckGo path, whose snippets are ~200 chars
    and too thin to write a documentary from. Pages are read in parallel with
    a short timeout, so a slow source costs the run a few seconds, not minutes.
    """
    hits = []
    for q in queries:
        hits += search(q, max_results=per_query)
    hits = _dedupe(hits)[:max_sources]

    if not hits:
        return "", []

    if read_pages:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            pages = list(pool.map(fetch_page, [h["url"] for h in hits]))
        read = 0
        for h, page in zip(hits, pages):
            # keep the snippet when the page read came back empty or thinner
            if len(page) > len(h["content"]):
                h["content"] = page
                read += 1

        # SAY HOW MANY PAGES ACTUALLY OPENED.
        #
        # fetch_page() returns "" on any exception, so a blocked site, a
        # timeout and a page with genuinely no text are the same value. When
        # a read fails the snippet stays - about 200 characters - and the run
        # carries on quietly with a fortieth of the material it thinks it
        # has. Nothing downstream can tell the difference: the writer is
        # simply asked for 1,200 words from almost nothing, and fills the gap
        # the way models do.
        #
        # That degradation must never be silent again. It is not fatal - some
        # sites always refuse - so this reports rather than raises, and only
        # shouts when most of them failed.
        if read < len(hits):
            note = (" - MOST SOURCES ARE SEARCH SNIPPETS, not pages; expect "
                    "thin, poorly grounded output"
                    if read < len(hits) / 2 else "")
            print(f"      pages opened: {read}/{len(hits)} "
                  f"({sum(len(h['content']) for h in hits):,} chars){note}",
                  flush=True)

    blocks = []
    for i, h in enumerate(hits, 1):
        blocks.append(
            f"[SOURCE {i}] {h['title']}\nURL: {h['url']}\n{h['content']}\n")

    sources = [{"title": h["title"], "uri": h["url"]} for h in hits]
    return "\n".join(blocks), sources


if __name__ == "__main__":
    import sys
    q = sys.argv[1:] or ["how is coffee made from bean to cup"]
    ctx, srcs = gather(q, per_query=4)
    print(f"{len(srcs)} sources, {len(ctx)} chars of context\n")
    for s in srcs:
        print(" -", s["title"][:70], "|", s["uri"][:70])
    print("\n--- first 1200 chars ---\n")
    print(ctx[:1200])
