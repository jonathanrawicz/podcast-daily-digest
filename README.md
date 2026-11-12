# Podcast Daily Digest

A daily email summarising new episodes from a list of podcasts. Every morning it checks each show's feed, and for anything published in the last 48 hours that it hasn't already covered, it downloads the audio, has Gemini listen to it and write a structured summary (takeaway, why it matters, key points, quotes), and emails one digest. On days with nothing new it sends a short "nothing new" note listing when each show last published — so a silent failure never looks like a quiet day.

Built from the podcast engine in `You-Tube-Year-Digest` (`podcast_year.py`): same iTunes lookup, same Gemini audio call, same episode-card styling.

## The shows

Edit `podcasts.csv` to change them. One line per show: an Apple Podcasts link (easiest — in the app: show → ⋯ → Share Show → Copy Link), a numeric iTunes ID, or a direct RSS URL, plus an optional display name. Commit the change; the next run picks it up.

Current list: Eye on the Market, Another Podcast, BG2Pod, AI + a16z, The Rachman Review.

## Secrets (repo → Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio key (`AIza...`) — same one as the YouTube digest |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | your Gmail address |
| `SMTP_PASSWORD` | Gmail app password (spaces are stripped automatically) |
| `EMAIL_FROM` | from address (usually same as `SMTP_USER`) |
| `EMAIL_TO` | where the digest goes (comma-separate for several) |

## Schedule and manual runs

Runs daily at 05:47 UTC. To run by hand: **Actions → Podcast daily digest → Run workflow**. Inputs:

- **lookback_hours** — widen the window (e.g. `168` to catch the last week on the first run)
- **limit** — set `1` for a smoke test (one episode, one real email)
- **dry_run** — tick for no email and nothing marked seen

## How it protects itself

- **Never double-sends:** every emailed episode's ID goes in `data/seen_episodes.json` (committed back by the workflow, pruned after 120 days).
- **Never silently drops:** episodes are only marked seen *after* the email sends. A failed download, summary or email means that episode is retried on the next run while it's still inside the 48h window, and failures are named in the digest footer.
- **Feed problems are loud:** feeds are fetched with real HTTP error checking (feedparser alone swallows errors), and a broken feed is flagged in the email rather than vanishing.
- **Backlog safety:** at most 3 episodes per show and 10 overall per run; anything beyond that defers to the next day, and the email says so.
- **Cost:** `gemini-2.5-flash` audio input, ~2–4 episodes per week across these five shows — comfortably inside the free tier.

## Testing changes

`python3 tools/simulate.py` runs the whole pipeline offline with a fake feed, fake Gemini and fake SMTP — episode selection, seen-tracking, caps, both email variants, and the dry-run path. Run it after any change to `podcast_daily.py`.
