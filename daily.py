#!/usr/bin/env python3
#!/usr/bin/env python3
"""
Daily job, meant to run once a day (via GitHub Actions). Does two things:

  1. Takes a fresh snapshot of the top N ELO leaderboard AS OF RIGHT NOW,
     saved to snapshots/YYYY-MM-DD.json -- this is the roster used below.

  2. Looks back over the last 24 hours and finds upset candidates: for each
     of today's top-N players, every match they played (win or loss) is
     checked, and it counts as an upset if the winner's rank was worse than
     the loser's rank -- regardless of whether the OPPONENT was in the top N.
     Only matches with a public VOD are kept. Results are sorted by the
     winner's ELO gain and written to candidates/YYYY-MM-DD.md (readable)
     and candidates/YYYY-MM-DD.json (raw).

Reliability notes:
  - Network hiccups (timeouts, dropped connections, bad responses) are
    retried a few times. If one player's lookup still fails, that player is
    skipped and listed in the output file, instead of crashing the whole run.
  - Only the very first request (the leaderboard) is allowed to abort the
    run, since nothing can happen without a roster.
  - The candidates files are ALWAYS written, even if empty.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

API_BASE = "https://api.mcsrranked.com"
REQUEST_TIMEOUT = 30  # seconds per request attempt
PAUSE_BETWEEN_CALLS = 0.2  # seconds, keeps us gentle on the API


def fetch_json(url, required=True, attempts=4):
    """Fetch JSON from the API, retrying on any network/parsing problem.

    OSError covers URLError, TimeoutError, ConnectionError and friends;
    ValueError covers malformed JSON. If every attempt fails:
      - required=True  -> abort the whole script (used for the leaderboard)
      - required=False -> return None so the caller can skip just this item
    """
    req = urllib.request.Request(url, headers={"User-Agent": "mcsr-daily-highlights/1.0"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError) as e:
            print(f"Request failed (attempt {attempt}/{attempts}) for {url}: {e!r}", file=sys.stderr)
            if attempt < attempts:
                time.sleep(3 * attempt)  # 3s, 6s, 9s backoff
    if required:
        print(f"Giving up on required request: {url}", file=sys.stderr)
        sys.exit(1)
    print(f"Giving up on {url} -- skipping.", file=sys.stderr)
    return None


def fetch_leaderboard(top_n):
    payload = fetch_json(f"{API_BASE}/leaderboard", required=True, attempts=6)
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
    """Pages one player's ranked match history until the window is covered.
    Returns (matches_in_window, complete). complete=False means a request
    failed even after retries, so this player's data may be incomplete."""
    matches = []
    before = None
    while True:
        url = f"{API_BASE}/users/{uuid}/matches?type=2&count=100"
        if before is not None:
            url += f"&before={before}"
        payload = fetch_json(url, required=False)
        time.sleep(PAUSE_BETWEEN_CALLS)
        if payload is None:
            return matches, False
        if payload.get("status") != "success":
            return matches, False
        batch = payload["data"]
        if not batch:
            break
        for m in batch:
            if since_ts <= m["date"] <= until_ts:
                matches.append(m)
        if min(m["date"] for m in batch) < since_ts:
            break
        before = min(m["id"] for m in batch)
    return matches, True


def get_rank_and_name(uuid, rank_by_uuid, name_by_uuid, lookup_cache):
    """Rank/name for a player who may or may not be in today's snapshot.
    Snapshot players are free; anyone else is looked up once and cached."""
    if uuid in rank_by_uuid:
        return rank_by_uuid[uuid], name_by_uuid[uuid]
    if uuid in lookup_cache:
        return lookup_cache[uuid]

    payload = fetch_json(f"{API_BASE}/users/{uuid}", required=False)
    time.sleep(PAUSE_BETWEEN_CALLS)
    if payload is None or payload.get("status") != "success":
        result = (None, None)
    else:
        data = payload["data"]
        result = (data.get("eloRank"), data.get("nickname"))
    lookup_cache[uuid] = result
    return result


def compute_candidates(snapshot, since_dt, until_dt):
    """Returns (candidates, skipped_players)."""
    rank_by_uuid = {p["uuid"]: p["rank"] for p in snapshot["players"]}
    name_by_uuid = {p["uuid"]: p["nickname"] for p in snapshot["players"]}
    lookup_cache = {}
    since_ts, until_ts = int(since_dt.timestamp()), int(until_dt.timestamp())

    seen_match_ids = set()
    candidates = []
    skipped_players = []

    for p in snapshot["players"]:
        try:
            player_matches, complete = fetch_player_matches_in_window(p["uuid"], since_ts, until_ts)
        except Exception as e:  # safety net: never let one player kill the run
            print(f"Unexpected error for {p['nickname']}: {e!r} -- skipping player.", file=sys.stderr)
            skipped_players.append(p["nickname"])
            continue
        if not complete:
            skipped_players.append(p["nickname"])

        for m in player_matches:
            try:
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
                    continue  # unranked or lookup failed -- skip
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
            except Exception as e:
                print(f"Unexpected error on match {m.get('id')}: {e!r} -- skipping match.", file=sys.stderr)

    candidates.sort(key=lambda c: c["winner_elo_gain"] or 0, reverse=True)
    return candidates, skipped_players


def write_candidate_files(out_dir, tag, candidates, since_dt, until_dt, skipped_players):
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, f"{tag}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(candidates, f, indent=2)

    md_path = os.path.join(out_dir, f"{tag}.md")
    lines = [
        f"# Upset candidates - {tag}",
        f"Window: {since_dt.isoformat()} to {until_dt.isoformat()}",
        "",
    ]
    if skipped_players:
        lines.append(
            f"> WARNING: {len(skipped_players)} player(s) could not be fully checked "
            f"due to network errors, so this list may be incomplete: "
            + ", ".join(skipped_players)
        )
        lines.append("")
    if not candidates:
        lines.append("No qualifying upsets with a public VOD in this window.")
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"## {i}. {c['winner']} (#{c['winner_rank_before']}) def. "
            f"{c['loser']} (#{c['loser_rank_before']}) - +{c['winner_elo_gain']} ELO"
        )
        lines.append(f"- Rank gap: {c['rank_gap']}")
        lines.append(f"- Match time: {c['date']}")
        for url in c["vod_urls"]:
            lines.append(f"- VOD: {url}")
        lines.append("")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Saved candidates: {json_path} and {md_path}")
    if skipped_players:
        print(f"WARNING: {len(skipped_players)} player(s) skipped: {', '.join(skipped_players)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=150)
    parser.add_argument("--snapshot-dir", default="snapshots")
    parser.add_argument("--candidates-dir", default="candidates")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    today_tag = now.strftime("%Y-%m-%d")

    today_snapshot = take_snapshot(args.snapshot_dir, args.top, today_tag)

    until_dt = datetime.fromisoformat(today_snapshot["takenAt"])
    since_dt = until_dt - timedelta(hours=24)

    try:
        candidates, skipped_players = compute_candidates(today_snapshot, since_dt, until_dt)
    except Exception as e:
        print(f"Unexpected error computing candidates: {e!r}", file=sys.stderr)
        candidates, skipped_players = [], ["(entire run failed - see Actions log)"]

    write_candidate_files(args.candidates_dir, today_tag, candidates, since_dt, until_dt, skipped_players)


if __name__ == "__main__":
    main()



if __name__ == "__main__":
    main()
