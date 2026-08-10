#!/usr/bin/env python3
"""
Daily job, meant to run once a day (via GitHub Actions) at your 2 AM day
boundary. Does two things:

  1. Takes a fresh snapshot of the top N ELO leaderboard AS OF RIGHT NOW,
     saved to snapshots/YYYY-MM-DD.json -- this is the roster used below.

  2. Looks back over the last 24 hours and computes upset candidates: for
     each of today's top-N players, every match they played (win or loss)
     is checked, and it counts as an upset if the winner's rank was worse
     than the loser's rank -- regardless of whether the OPPONENT was in
     the top N. A rank-280 player beating a rank-143 player is caught even
     though 280 was never in the snapshot, because rank143's own match
     history surfaces it, and the opponent's rank is looked up on demand.
     Writes a human-readable summary to candidates/YYYY-MM-DD.md (covering
     the previous 24 hours) plus the raw data to candidates/YYYY-MM-DD.json.

Note on accuracy: a roster player's rank comes from today's snapshot (taken
right when this script runs), but an out-of-roster opponent's rank comes
from a live lookup at the same moment -- i.e. their CURRENT rank, not
necessarily their exact rank at the moment that specific match was played
earlier in the day. For most players this is a very close approximation;
it could be slightly off for someone who played many more matches
themselves later that same day.

Instead of pulling the entire global /matches feed (which includes every
ranked match on the server, not just top-N players -- potentially
thousands a day), this looks up each of today's top-N players individually
via /users/{id}/matches, which only returns matches that player was
actually in. Much smaller, much faster, no manual filtering needed.

Because today's snapshot is both the roster AND the data source for the
last 24 hours, every day (including day one of the season) works the same
way -- no dependency on a previous day's file existing.
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


def get_rank_and_name(uuid, rank_by_uuid, name_by_uuid, lookup_cache):
    """Rank/name for a player who may or may not be in today's top-N
    snapshot. Snapshot players are free (already in memory); anyone else
    gets looked up via the API once and cached."""
    if uuid in rank_by_uuid:
        return rank_by_uuid[uuid], name_by_uuid[uuid]
    if uuid in lookup_cache:
        return lookup_cache[uuid]

    payload = fetch_json(f"{API_BASE}/users/{uuid}")
    if payload.get("status") != "success":
        lookup_cache[uuid] = (None, None)
        return None, None

    data = payload["data"]
    result = (data.get("eloRank"), data.get("nickname"))
    lookup_cache[uuid] = result
    return result


def compute_candidates(snapshot, since_dt, until_dt):
    rank_by_uuid = {p["uuid"]: p["rank"] for p in snapshot["players"]}
    name_by_uuid = {p["uuid"]: p["nickname"] for p in snapshot["players"]}
    lookup_cache = {}
    since_ts, until_ts = int(since_dt.timestamp()), int(until_dt.timestamp())

    seen_match_ids = set()
    candidates = []

    for p in snapshot["players"]:
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
            if len(player_uuids) != 2:
                continue

            loser_uuid = [u for u in player_uuids if u != winner_uuid][0]
            winner_rank, winner_name = get_rank_and_name(winner_uuid, rank_by_uuid, name_by_uuid, lookup_cache)
            loser_rank, loser_name = get_rank_and_name(loser_uuid, rank_by_uuid, name_by_uuid, lookup_cache)
            if winner_rank is None or loser_rank is None:
                continue  # unranked player (no finished placements) -- skip

            if winner_rank <= loser_rank:
                continue  # not an upset

            change_lookup = {c["uuid"]: c["change"] for c in m["changes"]}
            candidates.append(
                {
                    "match_id": m["id"],
                    "date": datetime.fromtimestamp(m["date"], tz=timezone.utc).isoformat(),
                    "winner": winner_name,
                    "winner_rank_before": winner_rank,
                    "winner_elo_gain": change_lookup.get(winner_uuid),
                    "loser": loser_name,
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

    # Roster = top N players AS OF RIGHT NOW (catches anyone who climbed in
    # during the last 24 hours).
    today_snapshot = take_snapshot(args.snapshot_dir, args.top, today_tag)

    until_dt = datetime.fromisoformat(today_snapshot["takenAt"])
    since_dt = until_dt - timedelta(hours=24)

    candidates = compute_candidates(today_snapshot, since_dt, until_dt)
    # Tagged with today's date -- this file represents "the 24 hours that
    # just ended", i.e. yesterday 2 AM through today 2 AM.
    write_candidate_files(args.candidates_dir, today_tag, candidates, since_dt, until_dt)


if __name__ == "__main__":
    main()
