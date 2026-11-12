"""
Podcast daily digest.

Checks a list of podcasts (podcasts.csv) every day, finds episodes published
in the last LOOKBACK_HOURS that haven't been summarised before, has Gemini
listen to the audio and produce a transcript-grade summary of each one, and
emails a single HTML digest. On days with no new episodes it sends a short
"nothing new" note instead, so a silent failure is always distinguishable
from a quiet day.

Built from podcast_year.py (the year-in-review tool in You-Tube-Year-Digest):
same feed resolution via the iTunes Lookup API, same Gemini File API
transcribe-and-summarise call, same episode-card styling.

STATE (committed back to the repo by the workflow)
    data/seen_episodes.json   episodes already summarised and emailed
    data/feed_cache.json      iTunes ID -> RSS feed URL (fallback when the
                              lookup API is down)

REQUIRED ENV
    GEMINI_API_KEY
    SMTP_USER, SMTP_PASSWORD (or SMTP_PASS), EMAIL_TO

OPTIONAL ENV
    SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587), EMAIL_FROM
    LOOKBACK_HOURS (default 48)
    LIMIT          cap on episodes this run (smoke test: 1)
    DRY_RUN        "true"/"1": no email, nothing marked seen
"""

import csv
import html as html_lib
import json
import os
import re
import smtplib
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import google.generativeai as genai
import markdown
import requests


# --- Required config ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()

# gemini-2.5-flash supports audio input and has generous free-tier limits.
# Don't use flash-lite here — its audio support has historically been spotty.
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_DELAY_SECONDS = 6      # stay well under flash's free-tier RPM
GEMINI_RETRY_WAIT = 45        # one retry after transient errors / rate limits
AUDIO_PROCESS_TIMEOUT = 600   # max seconds to wait for Gemini to process audio

# --- Behaviour knobs (env-overridable) ---
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS") or "48")
LIMIT = int(os.environ.get("LIMIT") or "0")             # 0 = no cap
DRY_RUN = (os.environ.get("DRY_RUN") or "").strip().lower() in ("1", "true", "yes")

MAX_PER_SHOW_PER_RUN = int(os.environ.get("MAX_PER_SHOW") or "3")   # backlog spread over days
MAX_EPISODES_PER_RUN = int(os.environ.get("MAX_EPISODES") or "10")  # global cap; rest defer
MAX_AUDIO_MB = 400            # skip absurdly large files
SEEN_RETENTION_DAYS = 120     # prune seen-state entries older than this

PODCASTS_CSV = Path("podcasts.csv")
DATA_DIR = Path("data")
SEEN_PATH = DATA_DIR / "seen_episodes.json"
CACHE_PATH = DATA_DIR / "feed_cache.json"
OUTPUT_DIR = Path("output")

HTTP_HEADERS = {"User-Agent": "podcast_daily.py/1.0 (+personal daily digest)"}


SUMMARY_PROMPT = """You are summarizing a podcast episode for someone who follows the topic closely and has limited time. They don't need explained what's already obvious — they need the substance: specific claims, numbers, named people and companies, contrarian takes, and direct quotes worth seeing.

Listen to the audio I'm providing. Begin your response with EXACTLY this format on the first line, before anything else:
SPEAKER: <name>

Where <name> is the primary speaker or guest in the episode. Rules:
- For an interview, use the guest's full name (e.g., "Donald Hoffman"). The host conducting the interview is NOT the speaker.
- If two people share the spotlight roughly equally, list both joined by " & ". Never list more than two.
- If it's a solo monologue by the host with no guest, write "Host".
- If you genuinely cannot tell from the audio, write "Unknown".
- Do not include titles (Dr., Prof., Mr., etc.).

Then leave one blank line, then the summary in this exact structure with these exact headings:

## Summary
One or two sentences capturing the single most important takeaway. Not a description of what the episode is about — the actual conclusion or claim. If the reader reads nothing else, this is what they should walk away with.

## Why It Matters
One short paragraph (2-4 sentences) explaining the implications, stakes, or context. Why should they care about this specific episode right now?

## Key Points
4-7 bullets, each capturing one distinct claim, finding, or argument. Each bullet must include something specific — a number, a name, a date, a comparison, a mechanism. Lead with the most consequential point. No generic statements.

## Worth Quoting
2-4 direct quotes (15-30 words each) that capture the speaker's actual voice, surprising claims, or memorable framings. Use quotation marks. If the audio is dry and has no quote-worthy moments, write "Nothing notable enough to quote." rather than padding.

Critical rules:
- DO NOT pad with generic business-speak or filler ("It's important to note that...", "Many experts agree...").
- DO NOT summarize sponsor reads, advertising breaks, or sign-offs.
- DO NOT extrapolate beyond what's in the audio or invent details.
- DO use the speaker's own framing and terminology, not corporate generic language.
- DO note contradictions, hedges, or moments where the speaker pulled back from a claim.
- DO be willing to say an episode is unremarkable if it is.
"""


# ---------- Small helpers ----------

def esc(s):
    """HTML-escape untrusted text (titles, names) before it goes in the email."""
    return html_lib.escape(str(s or ""), quote=True)


def now_utc():
    return datetime.now(timezone.utc)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, sort_keys=True)
    tmp.replace(path)


def parse_duration(s):
    """Parse iTunes duration: '1:23:45', '23:45', or '3825' (seconds)."""
    if not s:
        return None
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    parts = s.split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 3:
        h, m, sec = parts
        return h * 3600 + m * 60 + sec
    if len(parts) == 2:
        m, sec = parts
        return m * 60 + sec
    return None


def format_duration(seconds):
    """Render duration compactly: 3825 -> '1h 4m'; 600 -> '10m'."""
    if not seconds:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{m}m"


def ago(dt):
    """'today', 'yesterday', '5 days ago' — for the quiet-day email."""
    if not dt:
        return "no dated episodes"
    days = (now_utc().date() - dt.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


# ---------- Podcast list ----------

def read_podcasts(csv_path=PODCASTS_CSV):
    """Read podcasts.csv -> [{'source':..., 'name':...}].

    Columns: source (Apple Podcasts URL, iTunes ID, or RSS URL), name
    (optional display name; blank = use the feed's own title). Lines starting
    with # and blank lines are ignored.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} not found — the list of shows lives there.")
    shows = []
    with open(csv_path, encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f)
                if r and (r[0] or "").strip() and not r[0].lstrip().startswith("#")]
    if not rows:
        raise ValueError(f"{csv_path} has no podcasts in it.")
    header = [c.strip().lower() for c in rows[0]]
    start = 1 if header and header[0] in ("source", "feed", "url") else 0
    for r in rows[start:]:
        source = (r[0] or "").strip()
        name = (r[1] or "").strip() if len(r) > 1 else ""
        if source:
            shows.append({"source": source, "name": name})
    return shows


# ---------- Feed URL resolution (Apple URL / iTunes ID -> RSS) ----------

def resolve_feed_url(source, cache):
    """Return (feed_url, itunes_id_or_None). Caches lookups in data/feed_cache.json.

    The iTunes Lookup API is tried fresh each run (feeds do occasionally
    move); the cache is the fallback when the API is down.
    """
    source = source.strip()
    is_apple = "podcasts.apple.com" in source or "itunes.apple.com" in source
    if source.startswith(("http://", "https://")) and not is_apple:
        return source, None

    m = re.search(r"id(\d+)", source)
    if m:
        podcast_id = m.group(1)
    elif source.isdigit():
        podcast_id = source
    else:
        raise ValueError(
            f"Couldn't parse a podcast ID from {source!r}. "
            "Use an Apple Podcasts URL, a numeric ID, or an RSS feed URL."
        )

    try:
        resp = requests.get(
            f"https://itunes.apple.com/lookup?id={podcast_id}",
            timeout=20, headers=HTTP_HEADERS,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        feed_url = results[0].get("feedUrl") if results else None
        if feed_url:
            cache[podcast_id] = feed_url
            return feed_url, podcast_id
        raise ValueError(f"iTunes ID {podcast_id}: no public RSS feed in lookup result")
    except Exception as e:
        cached = cache.get(podcast_id)
        if cached:
            print(f"  lookup failed ({e}); using cached feed URL", file=sys.stderr)
            return cached, podcast_id
        raise


# ---------- Episode listing ----------

def fetch_feed(feed_url):
    """Fetch the feed over HTTP ourselves, then hand the bytes to feedparser.

    feedparser.parse(url) swallows HTTP errors silently (a blocked feed looks
    identical to an empty one — a lesson from the YouTube digest), so we fetch
    with requests first and let a bad status raise loudly.
    """
    resp = requests.get(feed_url, timeout=30, headers=HTTP_HEADERS)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"Feed parse failed: {feed.bozo_exception}")
    return feed


def list_recent_episodes(show_name, feed, lookback_hours):
    """Return (episodes_in_window_newest_first, latest_published_datetime)."""
    cutoff = now_utc() - timedelta(hours=lookback_hours)
    episodes, latest = [], None
    for entry in feed.entries:
        audio_url = None
        for enc in entry.get("enclosures", []) or []:
            etype = (enc.get("type") or "").lower()
            href = enc.get("href")
            if href and (etype.startswith("audio/") or etype.startswith("video/")):
                audio_url = href
                break
        if not audio_url:
            continue

        pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if not pub_struct:
            continue
        try:
            published = datetime(*pub_struct[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue

        if latest is None or published > latest:
            latest = published
        if published < cutoff:
            continue

        episodes.append({
            "show": show_name,
            "title": (entry.get("title") or "Untitled").strip(),
            "audio_url": audio_url,
            "published": published,
            "duration_seconds": parse_duration(entry.get("itunes_duration") or ""),
            "episode_url": entry.get("link") or "",
            "guid": str(entry.get("id") or audio_url),
        })
    episodes.sort(key=lambda e: e["published"], reverse=True)
    return episodes, latest


# ---------- Audio download ----------

def download_episode(audio_url, dest_dir):
    """Stream a podcast audio file to disk. Returns the local Path."""
    print("  downloading audio...")
    resp = requests.get(audio_url, stream=True, timeout=120,
                        allow_redirects=True, headers=HTTP_HEADERS)
    resp.raise_for_status()

    ct = (resp.headers.get("Content-Type") or "").lower()
    if "mp4" in ct or "m4a" in ct or "aac" in ct:
        ext = ".m4a"
    elif "ogg" in ct or "opus" in ct:
        ext = ".ogg"
    elif "wav" in ct:
        ext = ".wav"
    elif "mpeg" in ct or "mp3" in ct:
        ext = ".mp3"
    else:
        path_lower = urlparse(audio_url).path.lower()
        if path_lower.endswith(".m4a"):
            ext = ".m4a"
        elif path_lower.endswith((".ogg", ".opus")):
            ext = ".ogg"
        elif path_lower.endswith(".wav"):
            ext = ".wav"
        else:
            ext = ".mp3"

    dest_path = Path(dest_dir) / f"episode{ext}"
    total = 0
    limit_bytes = MAX_AUDIO_MB * 1024 * 1024
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)
                total += len(chunk)
                if total > limit_bytes:
                    raise RuntimeError(f"Audio larger than {MAX_AUDIO_MB} MB — skipping")
    print(f"  downloaded {total / 1024 / 1024:.1f} MB to {ext}")
    return dest_path


# ---------- Gemini audio summarization ----------

def summarize_audio(audio_path, model, show_name, episode_title):
    """Upload audio to Gemini's File API, generate a summary, clean up."""
    print("  uploading to Gemini File API...")
    audio_file = genai.upload_file(path=str(audio_path))

    waited = 0
    while audio_file.state.name == "PROCESSING":
        if waited >= AUDIO_PROCESS_TIMEOUT:
            _safe_delete(audio_file)
            raise RuntimeError(f"Audio processing timeout after {AUDIO_PROCESS_TIMEOUT}s")
        time.sleep(3)
        waited += 3
        audio_file = genai.get_file(audio_file.name)

    if audio_file.state.name != "ACTIVE":
        state = audio_file.state.name
        _safe_delete(audio_file)
        raise RuntimeError(f"Audio file in unexpected state: {state}")

    prompt = (
        f"Episode context: this is from the podcast \"{show_name}\", "
        f"episode title \"{episode_title}\".\n\n{SUMMARY_PROMPT}"
    )
    print("  generating summary...")
    try:
        try:
            resp = model.generate_content([prompt, audio_file])
            return resp.text.strip()
        except Exception as e:
            # One retry covers transient 429/500s without hammering the API.
            print(f"  first attempt failed ({e}); retrying in {GEMINI_RETRY_WAIT}s...",
                  file=sys.stderr)
            time.sleep(GEMINI_RETRY_WAIT)
            resp = model.generate_content([prompt, audio_file])
            return resp.text.strip()
    finally:
        _safe_delete(audio_file)
        time.sleep(GEMINI_DELAY_SECONDS)


def _safe_delete(audio_file):
    try:
        genai.delete_file(audio_file.name)
    except Exception as e:
        print(f"  warning: failed to delete uploaded file: {e}", file=sys.stderr)


# ---------- Speaker extraction ----------

SPEAKER_RE = re.compile(r"^\s*SPEAKER:\s*(.+?)\s*$", re.MULTILINE)


def extract_speaker(summary_text):
    """Pull the SPEAKER: line off the top. Returns (speaker, cleaned_summary)."""
    if not summary_text:
        return "Unknown", summary_text
    head = summary_text[:300]
    m = SPEAKER_RE.search(head)
    if not m:
        return "Unknown", summary_text
    speaker = m.group(1).strip()
    if not speaker or speaker.lower() in ("n/a", "none", ""):
        speaker = "Unknown"
    cleaned = SPEAKER_RE.sub("", summary_text, count=1).lstrip()
    return speaker, cleaned


# ---------- HTML styling (same look as the year-in-review tool) ----------

def style_summary_html(html):
    html = html.replace(
        "<h2>",
        "<h2 style='font-size:13px;font-weight:700;text-transform:uppercase;"
        "letter-spacing:0.8px;color:#0066cc;margin:24px 0 8px;"
        "padding-bottom:5px;border-bottom:2px solid #0066cc;'>"
    )
    html = html.replace(
        "<h3>",
        "<h3 style='font-size:15px;font-weight:600;color:#222;margin:18px 0 5px;'>"
    )
    html = html.replace("<ul>", "<ul style='margin:8px 0 14px;padding-left:22px;'>")
    html = html.replace("<ol>", "<ol style='margin:8px 0 14px;padding-left:22px;'>")
    html = html.replace("<li>", "<li style='margin-bottom:6px;'>")
    html = html.replace("<p>", "<p style='margin:8px 0;'>")
    html = html.replace("<strong>", "<strong style='color:#000;'>")
    html = html.replace(
        "<blockquote>",
        "<blockquote style='border-left:3px solid #0066cc;margin:10px 0;"
        "padding:6px 14px;color:#444;font-style:italic;background:#f5f9ff;'>"
    )
    return _stylize_quotes_section(html)


def _stylize_quotes_section(html):
    pattern = re.compile(
        r"(<h2[^>]*>[^<]*worth quoting[^<]*</h2>)(.*?)(<ul[^>]*>)(.*?)(</ul>)",
        re.IGNORECASE | re.DOTALL,
    )

    def replace_section(match):
        heading = match.group(1)
        between = match.group(2)
        ul_inner = match.group(4)
        items = re.findall(r"<li[^>]*>(.*?)</li>", ul_inner, re.DOTALL)
        if not items:
            return match.group(0)
        quote_blocks = []
        for item in items:
            quote_blocks.append(
                f"<div style='border-left:4px solid #0066cc;background:#f5f9ff;"
                f"padding:12px 18px;margin:10px 0;font-style:italic;color:#222;"
                f"font-size:15px;line-height:1.5;border-radius:0 4px 4px 0;'>"
                f"{item.strip()}"
                f"</div>"
            )
        return heading + between + "".join(quote_blocks)

    return pattern.sub(replace_section, html)


# ---------- Email documents ----------

def _page_open(title):
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{esc(title)}</title></head>"
        "<body id='top' style='font-family:-apple-system,BlinkMacSystemFont,"
        "\"Segoe UI\",sans-serif;max-width:760px;margin:0 auto;padding:24px;"
        "line-height:1.55;color:#222;background:#fafafa;'>"
    )


def _header_block(subtitle_bits):
    today = now_utc().strftime("%A %-d %B %Y")
    return (
        "<div style='border-bottom:3px solid #111;padding-bottom:16px;margin-bottom:24px;'>"
        "<div style='font-size:11px;font-weight:700;text-transform:uppercase;"
        "letter-spacing:1.5px;color:#888;margin-bottom:4px;'>Podcast Daily</div>"
        "<h1 style='font-size:30px;font-weight:800;margin:0 0 4px;color:#000;"
        f"letter-spacing:-0.5px;'>{esc(today)}</h1>"
        f"<p style='margin:0;color:#666;font-size:14px;'>{' · '.join(subtitle_bits)}</p>"
        "</div>"
    )


def build_digest(summaries, shows_checked, lookback_hours, feed_errors, deferred):
    """HTML + plain-text digest for a day WITH new episodes."""
    show_names = []
    for s in summaries:
        if s["show"] not in show_names:
            show_names.append(s["show"])

    n = len(summaries)
    html_parts = [_page_open("Podcast Daily")]
    html_parts.append(_header_block([
        f"<strong>{n}</strong> new episode{'s' if n != 1 else ''}",
        f"{shows_checked} shows checked",
        f"last {lookback_hours}h",
    ]))

    for i, s in enumerate(summaries, 1):
        s["_anchor"] = f"e{i}"

    # Contents box when there's more than one episode
    if n > 1:
        html_parts.append(
            "<div style='background:#fff;border:1px solid #e5e5e5;border-radius:8px;"
            "padding:18px 24px;margin-bottom:28px;'>"
            "<div style='font-size:11px;font-weight:700;text-transform:uppercase;"
            "letter-spacing:1px;color:#888;margin-bottom:10px;'>In this digest</div>"
            "<ul style='margin:0;padding-left:18px;'>"
        )
        for s in summaries:
            dur = format_duration(s.get("duration_seconds"))
            html_parts.append(
                f"<li style='margin-bottom:5px;font-size:14px;'>"
                f"<span style='font-weight:700;color:#111;'>{esc(s['show'])}</span> — "
                f"<a href='#{s['_anchor']}' style='color:#0066cc;text-decoration:none;'>"
                f"{esc(s['title'])}</a>"
                f"<span style='color:#999;font-size:12px;'>"
                f"{(' · ' + dur) if dur else ''}</span></li>"
            )
        html_parts.append("</ul></div>")

    text_parts = [
        f"Podcast Daily — {now_utc().strftime('%A %-d %B %Y')}",
        f"{n} new episode(s) · {shows_checked} shows checked · last {lookback_hours}h",
        "",
    ]

    for s in summaries:
        date_str = s["published"].strftime("%-d %b, %H:%M UTC")
        dur = format_duration(s.get("duration_seconds"))
        ep_link = s.get("episode_url") or s.get("audio_url")
        speaker_chip = ""
        if s["speaker"] not in ("Unknown", "Host"):
            speaker_chip = (
                f"<span style='background:#eef4ff;padding:2px 8px;border-radius:10px;"
                f"color:#0050b3;font-weight:600;margin-right:8px;'>{esc(s['speaker'])}</span>"
            )
        html_parts.append(
            f"<div id='{s['_anchor']}' style='background:#fff;border:1px solid #e5e5e5;"
            f"border-radius:8px;padding:24px;margin-bottom:16px;'>"
            f"<div style='font-size:12px;color:#888;margin-bottom:6px;'>"
            f"<span style='background:#111;color:#fff;padding:2px 10px;border-radius:10px;"
            f"font-weight:700;letter-spacing:0.3px;margin-right:8px;'>{esc(s['show'])}</span>"
            f"{speaker_chip}"
            f"{esc(date_str)}{(' · ' + dur) if dur else ''}</div>"
            f"<h3 style='font-size:20px;font-weight:700;margin:0 0 6px;color:#111;"
            f"line-height:1.3;'>{esc(s['title'])}</h3>"
            f"<p style='margin:0 0 20px;font-size:14px;'>"
            f"<a href='{esc(ep_link)}' style='color:#0066cc;text-decoration:none;'>"
            f"♪ Listen to episode</a>"
            f"<a href='#top' style='color:#999;text-decoration:none;float:right;"
            f"font-size:13px;'>↑ back to top</a></p>"
        )
        body_html = markdown.markdown(s["summary"], extensions=["extra", "nl2br"])
        body_html = style_summary_html(body_html)
        html_parts.append(f"<div>{body_html}</div></div>")

        text_parts += [
            "-" * 60,
            f"{s['show']} — {s['title']}",
            f"{date_str}{(' · ' + dur) if dur else ''}"
            + (f" · {s['speaker']}" if s['speaker'] not in ('Unknown',) else ""),
            ep_link or "",
            "-" * 60,
            s["summary"],
            "",
        ]

    footer_bits = []
    if deferred:
        footer_bits.append(
            f"{deferred} further new episode{'s' if deferred != 1 else ''} deferred "
            f"to tomorrow's run (per-run cap)."
        )
    for err in feed_errors:
        footer_bits.append(f"⚠ {esc(err)}")
    if footer_bits:
        html_parts.append(
            "<div style='margin-top:20px;padding:14px 18px;background:#fff8e6;"
            "border:1px solid #f0d890;border-radius:8px;font-size:13px;color:#664d00;'>"
            + "<br>".join(footer_bits) + "</div>"
        )
        text_parts += [""] + [re.sub(r"<[^>]+>", "", b) for b in footer_bits]

    html_parts.append(
        "<p style='margin-top:28px;font-size:12px;color:#aaa;'>"
        "Podcast Daily · summaries generated by Gemini from the episode audio · "
        "edit podcasts.csv in the repo to change the shows</p></body></html>"
    )

    subject = f"Podcast Daily · {n} new: " + ", ".join(show_names)
    if len(subject) > 120:
        subject = f"Podcast Daily · {n} new episodes"
    return "\n".join(html_parts), "\n".join(text_parts), subject


def build_quiet_note(show_status, lookback_hours, feed_errors):
    """Short 'nothing new' email: proof of life plus when each show last published."""
    html_parts = [_page_open("Podcast Daily")]
    html_parts.append(_header_block([
        "nothing new today",
        f"{len(show_status)} shows checked",
        f"last {lookback_hours}h",
    ]))
    html_parts.append(
        "<div style='background:#fff;border:1px solid #e5e5e5;border-radius:8px;"
        "padding:20px 24px;'>"
        "<div style='font-size:11px;font-weight:700;text-transform:uppercase;"
        "letter-spacing:1px;color:#888;margin-bottom:12px;'>Latest from each show</div>"
        "<table style='border-collapse:collapse;width:100%;font-size:14px;'>"
    )
    text_parts = [
        f"Podcast Daily — {now_utc().strftime('%A %-d %B %Y')}",
        f"Nothing new in the last {lookback_hours}h. Latest from each show:",
        "",
    ]
    for st in show_status:
        latest_bit = (
            f"<span style='color:#111;'>{esc(st['latest_title'])}</span> "
            f"<span style='color:#999;'>· {esc(ago(st['latest']))}</span>"
            if st.get("latest_title") else
            "<span style='color:#999;'>no dated episodes found</span>"
        )
        html_parts.append(
            "<tr>"
            f"<td style='padding:7px 14px 7px 0;font-weight:700;color:#111;"
            f"white-space:nowrap;vertical-align:top;'>{esc(st['name'])}</td>"
            f"<td style='padding:7px 0;color:#444;'>{latest_bit}</td></tr>"
        )
        text_parts.append(
            f"  • {st['name']}: "
            + (f"{st['latest_title']} · {ago(st['latest'])}"
               if st.get("latest_title") else "no dated episodes found")
        )
    html_parts.append("</table></div>")

    if feed_errors:
        html_parts.append(
            "<div style='margin-top:20px;padding:14px 18px;background:#fdecea;"
            "border:1px solid #f5b5ae;border-radius:8px;font-size:13px;color:#8a1f11;'>"
            + "<br>".join(f"⚠ {esc(e)}" for e in feed_errors) + "</div>"
        )
        text_parts += [""] + [f"! {e}" for e in feed_errors]

    html_parts.append(
        "<p style='margin-top:28px;font-size:12px;color:#aaa;'>"
        "Podcast Daily ran fine — there was just nothing new. "
        "Edit podcasts.csv in the repo to change the shows.</p></body></html>"
    )
    subject = "Podcast Daily · nothing new today"
    if feed_errors:
        subject += f" ({len(feed_errors)} feed problem{'s' if len(feed_errors) != 1 else ''})"
    return "\n".join(html_parts), "\n".join(text_parts), subject


# ---------- Email ----------

def send_email(html, text, subject):
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"].strip()
    # Gmail displays app passwords with spaces; strip all whitespace
    # (hard-won lesson from the other digests' 535 errors).
    smtp_pass = re.sub(r"\s+", "", os.environ.get("SMTP_PASSWORD")
                       or os.environ["SMTP_PASS"])
    email_to = [a.strip() for a in os.environ["EMAIL_TO"].split(",") if a.strip()]
    email_from = os.environ.get("EMAIL_FROM", smtp_user).strip()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = ", ".join(email_to)
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(email_from, email_to, msg.as_string())


# ---------- Main ----------

def main():
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    print(f"Podcast Daily · model {GEMINI_MODEL} · lookback {LOOKBACK_HOURS}h"
          + (" · DRY RUN" if DRY_RUN else ""))

    shows = read_podcasts()
    print(f"{len(shows)} shows in podcasts.csv")

    seen = load_json(SEEN_PATH, {})
    cache = load_json(CACHE_PATH, {})

    candidates, show_status, feed_errors = [], [], []
    for show in shows:
        label = show["name"] or show["source"]
        try:
            feed_url, _ = resolve_feed_url(show["source"], cache)
            feed = fetch_feed(feed_url)
            feed_title = (feed.feed.get("title") or "").strip()
            name = show["name"] or feed_title or show["source"]
            episodes, latest = list_recent_episodes(name, feed, LOOKBACK_HOURS)
            new_eps = [e for e in episodes if e["guid"] not in seen]

            latest_title = ""
            if latest:
                for entry in feed.entries:
                    ps = entry.get("published_parsed") or entry.get("updated_parsed")
                    if ps and datetime(*ps[:6], tzinfo=timezone.utc) == latest:
                        latest_title = (entry.get("title") or "").strip()
                        break
            show_status.append({"name": name, "latest": latest,
                                "latest_title": latest_title})
            print(f"  {name}: {len(new_eps)} new (latest published {ago(latest)})")

            if len(new_eps) > MAX_PER_SHOW_PER_RUN:
                print(f"    capping at {MAX_PER_SHOW_PER_RUN} for this run; "
                      f"{len(new_eps) - MAX_PER_SHOW_PER_RUN} defer to tomorrow")
                new_eps = new_eps[:MAX_PER_SHOW_PER_RUN]
            candidates.extend(new_eps)
        except Exception as e:
            msg = f"{label}: feed check failed — {e}"
            print(f"  {msg}", file=sys.stderr)
            feed_errors.append(msg)
            show_status.append({"name": label, "latest": None, "latest_title": ""})

    candidates.sort(key=lambda e: e["published"], reverse=True)
    deferred = max(0, len(candidates) - MAX_EPISODES_PER_RUN)
    if deferred:
        print(f"Global cap {MAX_EPISODES_PER_RUN}: deferring {deferred} to tomorrow")
        candidates = candidates[:MAX_EPISODES_PER_RUN]
    if LIMIT:
        print(f"LIMIT={LIMIT} set — processing only the first {LIMIT}")
        candidates = candidates[:LIMIT]

    print(f"\n{len(candidates)} episode(s) to summarise\n")

    summaries = []
    for i, ep in enumerate(candidates, 1):
        print(f"[{i}/{len(candidates)}] {ep['show']} — {ep['title']}")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                audio_path = download_episode(ep["audio_url"], tmp)
                raw = summarize_audio(audio_path, model, ep["show"], ep["title"])
            speaker, summary = extract_speaker(raw)
            print(f"    speaker: {speaker}")
            summaries.append({**ep, "summary": summary, "speaker": speaker})
        except Exception as e:
            # Not marked seen -> retried tomorrow while still inside the window.
            print(f"    failed, will retry tomorrow: {e}", file=sys.stderr)
            feed_errors.append(f"{ep['show']} — \"{ep['title']}\" failed today, "
                               f"will retry tomorrow ({type(e).__name__})")

    if summaries:
        html, text, subject = build_digest(
            summaries, len(shows), LOOKBACK_HOURS, feed_errors, deferred)
    else:
        html, text, subject = build_quiet_note(show_status, LOOKBACK_HOURS, feed_errors)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"digest_{now_utc().strftime('%Y%m%d')}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"\nWrote {out_path}")

    if DRY_RUN:
        print("DRY RUN — no email sent, nothing marked seen.")
        return

    send_email(html, text, subject)
    print(f"Email sent: {subject}")

    # Mark seen ONLY after the email went out: if anything above failed, every
    # unsent episode is retried tomorrow. Worst case is a repeat summary cost,
    # never a lost episode.
    for s in summaries:
        seen[s["guid"]] = {
            "show": s["show"], "title": s["title"],
            "published": s["published"].isoformat(),
            "emailed": now_utc().isoformat(),
        }
    # Prune old entries so the file never grows without bound.
    horizon = now_utc() - timedelta(days=SEEN_RETENTION_DAYS)
    for guid in [g for g, v in seen.items()
                 if (v.get("emailed") or "9999") < horizon.isoformat()]:
        del seen[guid]
    save_json(SEEN_PATH, seen)
    save_json(CACHE_PATH, cache)
    print(f"State saved: {len(seen)} episodes in seen list.")


if __name__ == "__main__":
    main()
