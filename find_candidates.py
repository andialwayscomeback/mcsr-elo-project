#!/usr/bin/env python3
"""
Scans ranked matches in a given time window, and surfaces "upset" candidates:
matches where a player ranked lower on that day's leaderboard snapshot beat a
player ranked higher, and the match has a public VOD attached.

Sorted by the underdog's ELO gain (biggest swing first) by default, since
that's a good proxy for "how big a deal was this win."

Usage:
    python find_candidates.py \
        --snapshot snapshots/2026-08-30.json \
        --since "2026-08-30T00:00:00+00:00" \
        --until "2026-08-31T00:00:00+00:00" \
        --limit 10

Notes:
    - --since/--until should be your "day boundary" in UTC (i.e. 2:00 AM
      your local time, converted to UTC). The snapshot you pass in should be
      the one taken AT --since (the start-of-day rankings), so a player's
      rank reflects where they stood before that day's matches happened.
    - Only matches where BOTH players appear in the snapshot (i.e. both were
      in your top N that day) are considered.
    - Only matches with a non-empty `vod` array are considered, since you
      only want streamed matches.
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

API_BASE = "https://api.mcsrranked.com"


def parse_time(s):
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "mcsr-daily-highlights/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"Request failed for {url}: {e}", file=sys.stderr)
        sys.exit(1)


def fetch_matches_in_window(since_dt, until_dt):
    """Pages backward through ranked matches (type=2) until we've covered
    the window, returning only matches whose `date` falls inside it."""
    since_ts = int(since_dt.timestamp())
    until_ts = int(until_dt.timestamp())

    matches = []
    before = None
    while True:
        url = f"{API_BASE}/matches?type=2&count=100"
        if before is not None:
            url += f"&before={before}"

        payload = fetch_json(url)
        if payload.get("status") != "success":
            print(f"API error: {payload}", file=sys.stderr)
            sys.exit(1)

        batch = payload["data"]
        if not batch:
            break

        oldest_in_batch = None
        for m in batch:
            oldest_in_batch = m["id"] if oldest_in_batch is None else min(oldest_in_batch, m["id"])
            if since_ts <= m["date"] <= until_ts:
                matches.append(m)

        # Stop once every match in this batch is older than our window
        if min(m["date"] for m in batch) < since_ts:
            break

        before = oldest_in_batch

    return matches


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, help="Path to start-of-day snapshot JSON")
    parser.add_argument("--since", required=True, help="ISO timestamp, window start (UTC)")
    parser.add_argument("--until", required=True, help="ISO timestamp, window end (UTC)")
    parser.add_argument("--limit", type=int, default=10, help="How many candidates to show")
    parser.add_argument(
        "--sort-by",
        choices=["gain", "rank_gap"],
        default="gain",
        help="'gain' = underdog's ELO gain, 'rank_gap' = rank difference",
    )
    args = parser.parse_args()

    with open(args.snapshot, encoding="utf-8") as f:
        snapshot = json.load(f)
    rank_by_uuid = {p["uuid"]: p["rank"] for p in snapshot["players"]}
    name_by_uuid = {p["uuid"]: p["nickname"] for p in snapshot["players"]}

    since_dt = parse_time(args.since)
    until_dt = parse_time(args.until)

    matches = fetch_matches_in_window(since_dt, until_dt)
    print(f"Fetched {len(matches)} ranked matches in window.", file=sys.stderr)

    candidates = []
    for m in matches:
        if not m.get("vod"):
            continue  # not streamed, skip
        winner_uuid = m["result"]["uuid"]
        if winner_uuid is None:
            continue  # draw

        player_uuids = [p["uuid"] for p in m["players"]]
        if len(player_uuids) != 2:
            continue
        if not all(u in rank_by_uuid for u in player_uuids):
            continue  # one or both players weren't in that day's top N

        loser_uuid = [u for u in player_uuids if u != winner_uuid][0]
        winner_rank = rank_by_uuid[winner_uuid]
        loser_rank = rank_by_uuid[loser_uuid]

        if winner_rank <= loser_rank:
            continue  # not an upset (winner was already ranked better/equal)

        change_lookup = {c["uuid"]: c["change"] for c in m["changes"]}
        winner_gain = change_lookup.get(winner_uuid)

        candidates.append(
            {
                "match_id": m["id"],
                "date": datetime.fromtimestamp(m["date"], tz=timezone.utc).isoformat(),
                "winner": name_by_uuid[winner_uuid],
                "winner_rank_before": winner_rank,
                "winner_elo_gain": winner_gain,
                "loser": name_by_uuid[loser_uuid],
                "loser_rank_before": loser_rank,
                "rank_gap": winner_rank - loser_rank,
                "vod_urls": [v["url"] for v in m["vod"]],
            }
        )

    key = "winner_elo_gain" if args.sort_by == "gain" else "rank_gap"
    candidates.sort(key=lambda c: c[key] or 0, reverse=True)
    candidates = candidates[: args.limit]

    print(json.dumps(candidates, indent=2))


if __name__ == "__main__":
    main()
