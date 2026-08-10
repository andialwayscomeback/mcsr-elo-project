#!/usr/bin/env python3
"""
Daily job, meant to run once a day (via GitHub Actions) at your 2 AM day
boundary. Does two things:

  1. Takes a fresh snapshot of the top N ELO leaderboard, saved to
     snapshots/YYYY-MM-DD.json (same as the old snapshot.py).

  2. If YESTERDAY's snapshot file already exists, uses it to compute the
     upset candidates for the day that just ended, and writes a
     human-readable summary to candidates/YYYY-MM-DD.md plus the raw data
     to candidates/YYYY-MM-DD.json.

Instead of pulling the entire global /matches feed (which includes every
ranked match on the server, not just top-150 players -- potentially
thousands a day), this looks up each of yesterday's top-N players
individually via /users/{id}/matches, which only returns matches that
player was actually in. Much smaller, much faster, no manual filtering
needed.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

API_BASE = "https://api.mcsrranked.com"


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "mcsr-daily-highlights/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            if attempt == 2:
                print(f"Request failed for {url}: {e}", file=sys.stderr)
                sys.exit(1)
            time.sleep(2)


def fetch_leaderboard(top_n):
    payload = fetch_json(f"{API_BASE}/leaderboard")
    if payload.get("status") != "success":
        print(f"API error: {payload}", file=sys.stderr)
        sys.exit(1)
    return payload["data"]["users"][:top_n]


def take_snapshot(out_dir, top_n, tag):
    users = fetch_leaderboard(top_n)
    snapshot = {
        "takenAt": datetime.now(timezone.utc).isoformat(),
        "count": len(users),
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
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{tag}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    print(f"Saved snapshot: {path}")
    return snapshot


def fetch_player_matches_in_window(uuid, since_ts, until_ts):
    """Pages a single player's ranked match history until we've covered
    the window. Returns matches with date in [since_ts, until_ts]."""
    matches = []
    before = None
    while True:
        url = f"{API_BASE}/users/{uuid}/matches?type=2&count=100"
        if before is not None:
            url += f"&before={before}"
        payload = fetch_json(url)
        if payload.get("status") != "success":
            break
        batch = payload["data"]
        if not batch:
            break
        for m in batch:
            if since_ts <= m["date"] <= until_ts:
                matches.append(m)
        oldest_date = min(m["date"] for m in batch)
        if oldest_date < since_ts:
            break
        before = min(m["id"] for m in batch)
    return matches


def compute_candidates(prev_snapshot, since_dt, until_dt):
    rank_by_uuid = {p["uuid"]: p["rank"] for p in prev_snapshot["players"]}
    name_by_uuid = {p["uuid"]: p["nickname"] for p in prev_snapshot["players"]}
    since_ts, until_ts = int(since_dt.timestamp()), int(until_dt.timestamp())

    seen_match_ids = set()
    candidates = []

    for p in prev_snapshot["players"]:
        for m in fetch_player_matches_in_window(p["uuid"], since_ts, until_ts):
            if m["id"] in seen_match_ids:
                continue
            seen_match_ids.add(m["id"])

            if not m.get("vod"):
                continue
            winner_uuid = m["result"]["uuid"]
            if winner_uuid is None:
                continue

            player_uuids = [pl["uuid"] for pl in m["players"]]
            if len(player_uuids) != 2 or not all(u in rank_by_uuid for u in player_uuids):
                continue  # one player wasn't in yesterday's top N

            loser_uuid = [u for u in player_uuids if u != winner_uuid][0]
            winner_rank, loser_rank = rank_by_uuid[winner_uuid], rank_by_uuid[loser_uuid]
            if winner_rank <= loser_rank:
                continue  # not an upset

            change_lookup = {c["uuid"]: c["change"] for c in m["changes"]}
            candidates.append(
                {
                    "match_id": m["id"],
                    "date": datetime.fromtimestamp(m["date"], tz=timezone.utc).isoformat(),
                    "winner": name_by_uuid[winner_uuid],
                    "winner_rank_before": winner_rank,
                    "winner_elo_gain": change_lookup.get(winner_uuid),
                    "loser": name_by_uuid[loser_uuid],
                    "loser_rank_before": loser_rank,
                    "rank_gap": winner_rank - loser_rank,
                    "vod_urls": [v["url"] for v in m["vod"]],
                }
            )

    candidates.sort(key=lambda c: c["winner_elo_gain"] or 0, reverse=True)
    return candidates


def write_candidate_files(out_dir, tag, candidates, since_dt, until_dt):
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, f"{tag}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(candidates, f, indent=2)

    md_path = os.path.join(out_dir, f"{tag}.md")
    lines = [
        f"# Upset candidates — {tag}",
        f"Window: {since_dt.isoformat()} to {until_dt.isoformat()}",
        "",
    ]
    if not candidates:
        lines.append("No qualifying upsets with a public VOD today.")
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"## {i}. {c['winner']} (#{c['winner_rank_before']}) def. "
            f"{c['loser']} (#{c['loser_rank_before']}) — +{c['winner_elo_gain']} ELO"
        )
        lines.append(f"- Rank gap: {c['rank_gap']}")
        lines.append(f"- Match time: {c['date']}")
        for url in c["vod_urls"]:
            lines.append(f"- VOD: {url}")
        lines.append("")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Saved candidates: {json_path} and {md_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=150)
    parser.add_argument("--snapshot-dir", default="snapshots")
    parser.add_argument("--candidates-dir", default="candidates")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    today_tag = now.strftime("%Y-%m-%d")
    yesterday_tag = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    today_snapshot = take_snapshot(args.snapshot_dir, args.top, today_tag)

    yesterday_path = os.path.join(args.snapshot_dir, f"{yesterday_tag}.json")
    if not os.path.exists(yesterday_path):
        print("No snapshot from yesterday found — skipping candidate computation "
              "(normal on the very first day).")
        return

    with open(yesterday_path, encoding="utf-8") as f:
        yesterday_snapshot = json.load(f)

    since_dt = datetime.fromisoformat(yesterday_snapshot["takenAt"])
    until_dt = datetime.fromisoformat(today_snapshot["takenAt"])

    candidates = compute_candidates(yesterday_snapshot, since_dt, until_dt)
    write_candidate_files(args.candidates_dir, yesterday_tag, candidates, since_dt, until_dt)


if __name__ == "__main__":
    main()
