"""
Offline simulation of the whole podcast_daily pipeline.

No network, no Gemini, no SMTP: feeds, audio downloads, Gemini and email are
all faked. Exercises feed resolution + caching, episode selection vs lookback
and seen-state, per-show and global caps, speaker extraction, both email
variants (digest and quiet note), failure deferral, seen pruning, and the
dry-run path.

Run from the repo root:  python3 tools/simulate.py
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.chdir(REPO)
sys.path.insert(0, str(REPO))

os.environ.setdefault("GEMINI_API_KEY", "fake-key-for-simulation")
os.environ["SMTP_USER"] = "fake@example.com"
os.environ["SMTP_PASSWORD"] = "abcd efgh ijkl mnop"  # spaces on purpose
os.environ["EMAIL_TO"] = "fake@example.com"

CHECKS = {"passed": 0, "failed": 0}


def check(label, cond):
    mark = "OK  " if cond else "FAIL"
    print(f"  {mark} {label}")
    CHECKS["passed" if cond else "failed"] += 1


def load_module():
    spec = importlib.util.spec_from_file_location("pd", REPO / "podcast_daily.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iso(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def make_rss(show_title, episodes):
    items = []
    for e in episodes:
        items.append(f"""
    <item>
      <title>{e['title']}</title>
      <guid>{e['guid']}</guid>
      <link>https://example.com/{e['guid']}</link>
      <pubDate>{iso(e['published'])}</pubDate>
      <itunes:duration>{e.get('duration', '45:00')}</itunes:duration>
      <enclosure url="https://cdn.example.com/{e['guid']}.mp3"
                 type="audio/mpeg" length="1000"/>
    </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel><title>{show_title}</title>{''.join(items)}</channel></rss>"""


def main():
    pd = load_module()
    now = datetime.now(timezone.utc)

    # ---------- unit checks ----------
    print("\n== Unit checks ==")
    check("parse_duration 1:04:05", pd.parse_duration("1:04:05") == 3845)
    check("parse_duration 23:45", pd.parse_duration("23:45") == 1425)
    check("parse_duration '3825'", pd.parse_duration("3825") == 3825)
    check("format_duration 6000 -> 1h 40m", pd.format_duration(6000) == "1h 40m")
    check("ago yesterday", pd.ago(now - timedelta(days=1)) == "yesterday")
    sp, body = pd.extract_speaker("SPEAKER: Jensen Huang\n\n## Summary\nBody")
    check("extract_speaker name", sp == "Jensen Huang")
    check("extract_speaker strips line", body.startswith("## Summary"))
    sp2, _ = pd.extract_speaker("## Summary\nNo speaker line")
    check("extract_speaker missing -> Unknown", sp2 == "Unknown")

    shows = pd.read_podcasts()
    check("podcasts.csv reads 5 shows", len(shows) == 5)
    check("csv names populated", shows[0]["name"] == "Eye on the Market")

    # resolve_feed_url: raw RSS passes through; apple URL uses cache on failure
    url, pid = pd.resolve_feed_url("https://feeds.example.com/x.rss", {})
    check("raw RSS passes through", url == "https://feeds.example.com/x.rss" and pid is None)

    def fail_get(*a, **k):
        raise RuntimeError("lookup down")
    pd.requests = types.SimpleNamespace(get=fail_get)
    url2, pid2 = pd.resolve_feed_url(
        "https://podcasts.apple.com/gb/podcast/x/id111", {"111": "https://cached/feed"})
    check("apple URL falls back to cache", url2 == "https://cached/feed" and pid2 == "111")
    try:
        pd.resolve_feed_url("https://podcasts.apple.com/gb/podcast/x/id222", {})
        check("apple URL no-cache raises", False)
    except Exception:
        check("apple URL no-cache raises", True)

    # ---------- build the fake world ----------
    print("\n== Pipeline run 1: three new episodes, one failure ==")
    feeds = {
        "https://feeds.a.com/rss": make_rss("Show A", [
            {"title": "A: brand new", "guid": "a-new",
             "published": now - timedelta(hours=5)},
            {"title": "A: old", "guid": "a-old",
             "published": now - timedelta(days=9)},
        ]),
        "https://feeds.b.com/rss": make_rss("Show B", [
            {"title": "B: new 1", "guid": "b-new-1",
             "published": now - timedelta(hours=3), "duration": "1:40:00"},
            {"title": "B: already seen", "guid": "b-seen",
             "published": now - timedelta(hours=10)},
        ]),
        "https://feeds.c.com/rss": make_rss("Show C", [
            {"title": "C: will fail", "guid": "c-fail",
             "published": now - timedelta(hours=2)},
        ]),
        "https://feeds.quiet.com/rss": make_rss("Quiet Show", [
            {"title": "Quiet: old ep", "guid": "q-old",
             "published": now - timedelta(days=4)},
        ]),
    }

    class FakeResp:
        def __init__(self, content=b"", headers=None):
            self.content = content
            self.headers = headers or {}
        def raise_for_status(self): pass
        def iter_content(self, chunk_size):
            yield b"x" * 1024
        def json(self): return {}

    def fake_get(url, **kw):
        if url in feeds:
            return FakeResp(feeds[url].encode())
        if "cdn.example.com" in url or "cdn." in url:
            return FakeResp(headers={"Content-Type": "audio/mpeg"})
        raise RuntimeError(f"unexpected URL {url}")

    pd.requests = types.SimpleNamespace(get=fake_get)

    def fake_summarize(audio_path, model, show, title):
        if "fail" in title:
            raise RuntimeError("simulated Gemini outage")
        return (f"SPEAKER: Guest Of {show}\n\n## Summary\nMain claim of {title}.\n\n"
                f"## Why It Matters\nContext.\n\n## Key Points\n- Point with number 42\n"
                f"- Second point\n\n## Worth Quoting\n- \"A memorable quote.\"")
    pd.summarize_audio = fake_summarize

    sent = []
    pd.send_email = lambda html, text, subject: sent.append(
        {"html": html, "text": text, "subject": subject})

    # Redirect state into a sandbox and shrink the csv to the fake shows
    sandbox = Path(tempfile.mkdtemp())
    pd.SEEN_PATH = sandbox / "seen.json"
    pd.CACHE_PATH = sandbox / "cache.json"
    pd.OUTPUT_DIR = sandbox / "out"
    pd.read_podcasts = lambda *a, **k: [
        {"source": "https://feeds.a.com/rss", "name": ""},
        {"source": "https://feeds.b.com/rss", "name": "Show B override"},
        {"source": "https://feeds.c.com/rss", "name": "Show C"},
        {"source": "https://feeds.quiet.com/rss", "name": "Quiet Show"},
    ]
    pd.save_json(pd.SEEN_PATH, {"b-seen": {
        "show": "Show B", "title": "B: already seen",
        "published": (now - timedelta(hours=10)).isoformat(),
        "emailed": (now - timedelta(hours=9)).isoformat()}})

    pd.main()

    seen = json.loads(pd.SEEN_PATH.read_text())
    check("one email sent", len(sent) == 1)
    check("digest subject lists shows",
          "2 new" in sent[0]["subject"] and "Show A" in sent[0]["subject"])
    check("already-seen episode not re-sent", "B: already seen" not in sent[0]["html"])
    check("old episode excluded", "A: old" not in sent[0]["html"])
    check("failed episode flagged in footer", "will retry tomorrow" in sent[0]["html"])
    check("failed episode NOT marked seen", "c-fail" not in seen)
    check("new episodes marked seen", "a-new" in seen and "b-new-1" in seen)
    check("speaker chip in html", "Guest Of Show A" in sent[0]["html"])
    check("show name override used", "Show B override" in sent[0]["html"])
    check("duration rendered", "1h 40m" in sent[0]["html"])
    check("digest html written", any(pd.OUTPUT_DIR.glob("digest_*.html")))

    # ---------- run 2: nothing new -> quiet note ----------
    print("\n== Pipeline run 2: nothing new ==")
    sent.clear()
    pd.main()
    check("quiet note sent", len(sent) == 1 and "nothing new" in sent[0]["subject"])
    check("quiet note lists latest per show", "Quiet Show" in sent[0]["html"]
          and "days ago" in sent[0]["html"])
    check("no duplicate summaries", "Main claim" not in sent[0]["html"])

    # ---------- run 3: feed outage -> flagged, others fine ----------
    print("\n== Pipeline run 3: one feed down ==")
    sent.clear()
    broken = dict(feeds)
    del broken["https://feeds.c.com/rss"]
    def fake_get3(url, **kw):
        if url == "https://feeds.c.com/rss":
            raise RuntimeError("HTTP 503 from host")
        return fake_get(url, **kw)
    pd.requests = types.SimpleNamespace(get=fake_get3)
    pd.main()
    check("feed outage flagged in subject", "feed problem" in sent[0]["subject"])
    check("feed outage named in body", "Show C" in sent[0]["html"])

    # ---------- run 4: backlog caps ----------
    print("\n== Pipeline run 4: backlog caps ==")
    sent.clear()
    pd.requests = types.SimpleNamespace(get=fake_get)
    feeds["https://feeds.a.com/rss"] = make_rss("Show A", [
        {"title": f"A: burst {i}", "guid": f"a-burst-{i}",
         "published": now - timedelta(hours=i)} for i in range(1, 7)])
    pd.main()
    # Each episode title appears once in the plain-text body (the HTML also
    # repeats it in the contents box, so count in text).
    n_a = sent[0]["text"].count("Show A — A: burst")
    check("per-show cap holds (3 of 6)", n_a == 3)
    seen = json.loads(pd.SEEN_PATH.read_text())
    check("capped episodes not marked seen",
          sum(1 for g in seen if g.startswith("a-burst")) == 3)

    # ---------- run 5: dry run ----------
    print("\n== Pipeline run 5: dry run ==")
    sent.clear()
    pd.DRY_RUN = True
    pd.main()
    seen_before = json.loads(pd.SEEN_PATH.read_text())
    check("dry run sends no email", len(sent) == 0)
    check("dry run marks nothing seen",
          sum(1 for g in seen_before if g.startswith("a-burst")) == 3)
    pd.DRY_RUN = False

    # ---------- run 6: seen pruning ----------
    print("\n== Seen-state pruning ==")
    old = {"ancient": {"show": "X", "title": "old", "published": "2024-01-01T00:00:00+00:00",
                       "emailed": "2024-01-02T00:00:00+00:00"}}
    old.update(json.loads(pd.SEEN_PATH.read_text()))
    pd.save_json(pd.SEEN_PATH, old)
    sent.clear()
    pd.main()  # processes remaining burst episodes; prunes on save
    seen = json.loads(pd.SEEN_PATH.read_text())
    check("ancient entry pruned", "ancient" not in seen)

    print(f"\n{'=' * 50}\n{CHECKS['passed']} passed, {CHECKS['failed']} failed")
    if CHECKS["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
