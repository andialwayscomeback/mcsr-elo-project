# MCSR Ranked — Daily Upset Highlights Tooling

Two scripts that automate the two annoying parts of the channel workflow:

1. **`snapshot.py`** — takes a snapshot of the top 150 ELO leaderboard and
   saves it as `snapshots/YYYY-MM-DD.json`. Runs automatically every day at
   2:00 AM your time via the included GitHub Actions workflow, so you always
   have a record of who was ranked where at your day boundary — no more
   needing to check back in time.

2. **`find_candidates.py`** — given a day's snapshot and a time window, pulls
   all ranked matches in that window, keeps only the ones where:
   - both players were in that day's top 150,
   - the match has a public VOD attached (the API tracks this itself — no
     manual Twitch VOD hunting needed),
   - the winner was ranked *worse* than the loser (i.e. an upset),

   then sorts the results by the winner's ELO gain (or rank gap, with
   `--sort-by rank_gap`) so the biggest upset of the day is first.

## One-time setup

1. Create a new GitHub repo and push this folder to it.
2. In the repo's Settings → Actions → General, make sure "Read and write
   permissions" is enabled for the `GITHUB_TOKEN` (needed so the workflow can
   commit snapshots back to the repo).
3. That's it — the workflow will start firing at 00:00 UTC (2:00 AM CEST).
   You can also trigger it manually anytime from the Actions tab
   ("Run workflow") to test it before the season starts.

## Daily use (once the season is running)

Each morning, run:

```bash
python find_candidates.py \
  --snapshot snapshots/2026-08-30.json \
  --since "2026-08-30T00:00:00+00:00" \
  --until "2026-08-31T00:00:00+00:00" \
  --limit 10
```

This prints a JSON list of candidate matches — winner, loser, ranks going in,
ELO gained, and the VOD URL(s) — that you can skim in a couple minutes to
pick your video(s) for that day.

## Season start day (the odd one)

Since the season starts at 2:00 AM, day 1's window is naturally
`[season start, next 2:00 AM]` — same logic, just point `--since` at the
season's actual start timestamp instead of a snapshot time. You'll want to
grab a leaderboard snapshot right at season start too (run the workflow
manually, or run `snapshot.py` yourself) since the very first day has no
"previous day's 2 AM" snapshot to fall back on.

## Notes / things worth double-checking once the season is live

- I built this from the [official API docs](https://docs.mcsrranked.com/)
  but couldn't hit `api.mcsrranked.com` from my own sandbox to test live
  responses — run it manually once before you rely on it daily.
- Daylight saving: the cron schedule is set for CEST (UTC+2). When Czechia
  falls back to CET (UTC+1) in late October, 2:00 AM local shifts to
  01:00 UTC — update the cron line in `.github/workflows/snapshot.yml` then.
- `find_candidates.py` currently only flags matches where *both* players
  were in the top 150. If you'd also want to catch, say, a rank-200 player
  upsetting a top-10 player, increase `--top` in the snapshot step.
