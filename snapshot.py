#!/usr/bin/env python3
"""
Takes a snapshot of the current MCSR Ranked ELO leaderboard (top N players)
and saves it as a dated JSON file under snapshots/.

Meant to be run on a schedule (e.g. daily at 2:00 AM local time via a
GitHub Actions cron job) so you always have a record of exactly who was
ranked where at the start of each "day" for your channel.

Usage:
    python snapshot.py [--top 150] [--out-dir snapshots] [--tag 2026-08-30]
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

API_BASE = "https://api.mcsrranked.com"


def fetch_leaderboard():
    url = f"{API_BASE}/leaderboard"
    req = urllib.request.Request(url, headers={"User-Agent": "mcsr-daily-highlights/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"Failed to fetch leaderboard: {e}", file=sys.stderr)
        sys.exit(1)

    if payload.get("status") != "success":
        print(f"API returned an error: {payload}", file=sys.stderr)
        sys.exit(1)

    return payload["data"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=150, help="How many ranks to keep")
    parser.add_argument("--out-dir", default="snapshots", help="Output directory")
    parser.add_argument(
        "--tag",
        default=None,
        help="Filename date tag (defaults to today's UTC date, YYYY-MM-DD)",
    )
    args = parser.parse_args()

    data = fetch_leaderboard()
    users = data["users"][: args.top]

    tag = args.tag or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    taken_at = datetime.now(timezone.utc).isoformat()

    snapshot = {
        "takenAt": taken_at,
        "season": data.get("season"),
        "count": len(users),
        # Slim, flat structure that's easy to diff/read later:
        # rank -> {uuid, nickname, eloRate}
        "players": [
            {
                "rank": u["seasonResult"]["eloRank"],
                "uuid": u["uuid"],
                "nickname": u["nickname"],
                "eloRate": u["seasonResult"]["eloRate"],
            }
            for u in users
        ],
    }

    import os

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, f"{tag}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)

    print(f"Saved {len(users)} players to {out_path}")


if __name__ == "__main__":
    main()
