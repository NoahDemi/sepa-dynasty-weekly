"""Step 1 - fetch: pull everything the episode needs into one weekly JSON snapshot.

The snapshot (data/week_XX.json) is self-contained: analyze never touches the
network, which is what makes the analysis reproducible and testable.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import CACHE, snapshot_path

log = logging.getLogger("podcast.fetch")

SLEEPER = "https://api.sleeper.app/v1"
STATS_URL = "https://api.sleeper.com/stats/nfl/{season}"
PLAYERS_TTL = 24 * 3600
PLAYER_FIELDS = ("full_name", "first_name", "last_name", "position", "fantasy_positions",
                 "team", "age", "years_exp", "injury_status")


def get_json(url: str, params=None, retries: int = 4):
    """GET with retry/backoff. Sleeper asks clients to stay under 1000 calls/min."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} from {url}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            log.warning("GET %s failed (%s); retrying in %ss", url, e, wait)
            time.sleep(wait)


def _cached(path: Path, ttl: float | None, loader):
    """Return cached JSON at `path` unless older than ttl (None = keep forever)."""
    if path.exists() and (ttl is None or time.time() - path.stat().st_mtime < ttl):
        return json.loads(path.read_text())
    data = loader()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def fetch_players() -> dict:
    """/players/nfl is ~5MB: cache it and refresh at most daily."""
    return _cached(CACHE / "players_nfl.json", PLAYERS_TTL,
                   lambda: get_json(f"{SLEEPER}/players/nfl"))


def fetch_transactions(league_id: str) -> dict:
    return {str(w): get_json(f"{SLEEPER}/league/{league_id}/transactions/{w}") or []
            for w in range(0, 19)}


def fetch_drafts(league_id: str) -> list:
    out = []
    for d in get_json(f"{SLEEPER}/league/{league_id}/drafts") or []:
        detail = get_json(f"{SLEEPER}/draft/{d['draft_id']}")  # list view omits slot_to_roster_id
        detail["picks"] = [
            {k: p.get(k) for k in ("round", "draft_slot", "pick_no", "roster_id", "picked_by", "player_id")}
            for p in get_json(f"{SLEEPER}/draft/{d['draft_id']}/picks") or []
        ]
        out.append({k: detail.get(k) for k in
                    ("draft_id", "season", "status", "type", "slot_to_roster_id", "draft_order", "picks")}
                   | {"rounds": (detail.get("settings") or {}).get("rounds")})
    return out


def fetch_history(league_id: str, seasons: int) -> list:
    """Completed previous seasons (trades + drafts). Immutable, so cached forever."""
    out, lid = [], league_id
    for _ in range(seasons):
        if not lid or lid == "0":
            break

        def load(lid=lid):
            lg = get_json(f"{SLEEPER}/league/{lid}")
            return {
                "league_id": lid, "season": lg["season"],
                "previous_league_id": lg.get("previous_league_id"),
                "users": get_json(f"{SLEEPER}/league/{lid}/users"),
                "rosters": [{"roster_id": r["roster_id"], "owner_id": r["owner_id"]}
                            for r in get_json(f"{SLEEPER}/league/{lid}/rosters")],
                "transactions": {w: [t for t in ts if t["type"] == "trade"]
                                 for w, ts in fetch_transactions(lid).items()},
                "drafts": fetch_drafts(lid),
            }

        season = _cached(CACHE / f"history_{lid}.json", None, load)
        out.append(season)
        lid = season.get("previous_league_id")
    return out


def _trim_values(raw: list) -> list:
    out = []
    for v in raw:
        p = v["player"]
        out.append({
            "sleeper_id": p.get("sleeperId"), "name": p["name"], "position": p["position"],
            "team": p.get("maybeTeam"), "age": p.get("maybeAge"),
            "value": v["value"], "overall_rank": v["overallRank"],
            "position_rank": v["positionRank"], "trend_30d": v.get("trend30Day", 0),
        })
    return out


def build_snapshot(cfg: dict, week: int | None = None) -> dict:
    league_id = str(cfg["league"]["id"])
    state = get_json(f"{SLEEPER}/state/nfl")
    league = get_json(f"{SLEEPER}/league/{league_id}")
    if week is None:
        week = latest_completed_week(state, league)
    log.info("Fetching %s season %s, week %s", league["name"], league["season"], week)

    last_reg = league["settings"]["playoff_week_start"] - 1
    matchups = {}
    for w in range(1, min(week + 1, 18) + 1):  # through NEXT week (for the outro preview)
        matchups[str(w)] = get_json(f"{SLEEPER}/league/{league_id}/matchups/{w}") or []

    snap = {
        "meta": {"season": league["season"], "week": week, "league_id": league_id,
                 "last_regular_season_week": last_reg,
                 "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "state": state,
        "league": {k: league.get(k) for k in
                   ("name", "season", "status", "settings", "scoring_settings",
                    "roster_positions", "previous_league_id", "total_rosters")},
        "users": [{"user_id": u["user_id"], "display_name": u["display_name"]}
                  for u in get_json(f"{SLEEPER}/league/{league_id}/users")],
        "rosters": [{k: r.get(k) for k in
                     ("roster_id", "owner_id", "players", "starters", "taxi", "reserve", "settings")}
                    for r in get_json(f"{SLEEPER}/league/{league_id}/rosters")],
        "matchups": matchups,
        "transactions": fetch_transactions(league_id),
        "traded_picks": get_json(f"{SLEEPER}/league/{league_id}/traded_picks") or [],
        "winners_bracket": get_json(f"{SLEEPER}/league/{league_id}/winners_bracket") or [],
        "drafts": fetch_drafts(league_id),
        "history": fetch_history(league.get("previous_league_id"), cfg["league"]["history_seasons"]),
        "values": _trim_values(get_json(cfg["values"]["url"], cfg["values"]["params"])),
    }

    # Season stats, kept only for stat keys the league actually scores.
    scored = {k for k, v in league["scoring_settings"].items() if v}
    stats_raw = get_json(STATS_URL.format(season=league["season"]),
                         [("season_type", "regular")] + [("position[]", p) for p in ("QB", "RB", "WR", "TE")])
    stats = {s["player_id"]: {k: v for k, v in s["stats"].items() if k in scored or k == "gp"}
             for s in stats_raw}

    # Keep only the players this league can ever mention (keeps the snapshot ~300KB, not 5MB).
    ids = _relevant_player_ids(snap)
    players = fetch_players()
    snap["players"] = {pid: {f: players[pid].get(f) for f in PLAYER_FIELDS}
                       for pid in ids if pid in players}
    snap["season_stats"] = {pid: stats[pid] for pid in ids if pid in stats}
    return snap


def _relevant_player_ids(snap: dict) -> set:
    ids = set()
    for r in snap["rosters"]:
        ids.update(r.get("players") or [])
    for ms in snap["matchups"].values():
        for m in ms:
            ids.update(m.get("players") or [])
    all_tx = list(snap["transactions"].values()) + [
        ts for h in snap["history"] for ts in h["transactions"].values()]
    for ts in all_tx:
        for t in ts:
            ids.update((t.get("adds") or {}).keys())
            ids.update((t.get("drops") or {}).keys())
    for d in snap["drafts"] + [d for h in snap["history"] for d in h["drafts"]]:
        ids.update(p["player_id"] for p in d["picks"] if p.get("player_id"))
    ids.update(v["sleeper_id"] for v in snap["values"] if v.get("sleeper_id"))
    return ids


def latest_completed_week(state: dict, league: dict) -> int:
    """Most recent fully scored week.

    last_scored_leg is the week Sleeper last scored, which may still be in progress
    (Thu-Mon). Sleeper's own week counter rolls over at an unpredictable point on
    Tuesday/Wednesday, so when the two agree we decide by weekday (US Eastern).
    """
    last_scored = (league.get("settings") or {}).get("last_scored_leg") or 1
    if state.get("season") != league.get("season") or state.get("season_type") != "regular":
        return last_scored
    if int(state["week"]) > last_scored:
        return last_scored
    from zoneinfo import ZoneInfo
    games_over = datetime.now(ZoneInfo("America/New_York")).weekday() in (1, 2)  # Tue, Wed
    return last_scored if games_over else max(1, last_scored - 1)


def run(cfg: dict, week: int | None = None) -> tuple[dict, Path]:
    snap = build_snapshot(cfg, week)
    path = snapshot_path(snap["meta"]["week"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, indent=1))
    log.info("Wrote %s (%.0f KB)", path, path.stat().st_size / 1024)
    return snap, path
