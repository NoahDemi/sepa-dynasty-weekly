"""Step 2 - analyze: turn a snapshot into the episode's facts, deterministically.

Every number the hosts say comes from here. The LLM never does arithmetic.
All functions are pure (snapshot in, dict out) so they can be tested on fixtures.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from statistics import mean

from .config import facts_path

log = logging.getLogger("podcast.analyze")

SLOT_ELIGIBILITY = {
    "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "K": {"K"}, "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"}, "REC_FLEX": {"WR", "TE"}, "WRRB_FLEX": {"RB", "WR"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
}
NON_STARTING_SLOTS = {"BN", "IR", "TAXI"}
ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}
GRADE_FLOORS = [(2500, "A+"), (1500, "A"), (750, "A-"), (-750, "B"),
                (-1500, "C+"), (-2500, "C"), (-4000, "D")]


def r2(x) -> float:
    return round(float(x or 0), 2)


# --------------------------------------------------------------------------- #
# Small pure building blocks (these are what the tests pin down)
# --------------------------------------------------------------------------- #

def grade(net_value: float) -> str:
    """Letter grade from net dynasty value (received - given), at today's values."""
    for floor, letter in GRADE_FLOORS:
        if net_value >= floor:
            return letter
    return "F"


def median_results(scores: dict) -> tuple[float, dict]:
    """League-median game: beat the median for a win. Median of 10 = mean of 5th and 6th."""
    ordered = sorted(scores.values(), reverse=True)
    n = len(ordered)
    med = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    return r2(med), {rid: ("W" if s > med else "L" if s < med else "T") for rid, s in scores.items()}


def optimal_lineup(roster_positions: list, points: dict, positions: dict) -> tuple[float, list]:
    """Best possible lineup from `points` {player_id: pts}. Returns (total, [(slot, pid)]).

    Greedy by most-restrictive slot first, which is exact when eligibility sets are
    nested (QB/RB/WR/TE inside FLEX inside SUPER_FLEX), as they are on Sleeper.
    """
    slots = [s for s in roster_positions if s not in NON_STARTING_SLOTS]
    order = sorted(range(len(slots)), key=lambda i: len(SLOT_ELIGIBILITY.get(slots[i], ())))
    available = sorted(points, key=lambda p: -points[p])
    lineup, total = [], 0.0
    for i in order:
        elig = SLOT_ELIGIBILITY.get(slots[i], set())
        pick = next((p for p in available if positions.get(p) in elig), None)
        if pick is not None:
            available.remove(pick)
            total += points[pick]
        lineup.append((slots[i], pick))
    return r2(total), lineup


def parse_trade(t: dict) -> dict:
    """Normalize a Sleeper trade into per-roster sides.

    adds:  player_id -> roster that RECEIVED the player
    drops: player_id -> roster that GAVE the player
    draft_picks: roster_id = ORIGINAL owner, owner_id = new owner, previous_owner_id = giver
    """
    sides = {rid: {"roster_id": rid, "players_in": [], "players_out": [],
                   "picks_in": [], "picks_out": [], "faab_in": 0}
             for rid in t.get("roster_ids") or []}

    def side(rid):
        return sides.setdefault(rid, {"roster_id": rid, "players_in": [], "players_out": [],
                                      "picks_in": [], "picks_out": [], "faab_in": 0})

    for pid, rid in (t.get("adds") or {}).items():
        side(rid)["players_in"].append(pid)
    for pid, rid in (t.get("drops") or {}).items():
        side(rid)["players_out"].append(pid)
    for p in t.get("draft_picks") or []:
        pick = {"season": str(p["season"]), "round": p["round"], "original_roster_id": p["roster_id"]}
        side(p["owner_id"])["picks_in"].append(pick)
        side(p["previous_owner_id"])["picks_out"].append(pick)
    for w in t.get("waiver_budget") or []:
        side(w["receiver"])["faab_in"] += w["amount"]
        side(w["sender"])["faab_in"] -= w["amount"]
    return {"transaction_id": t.get("transaction_id"), "week": t.get("leg"),
            "created": t.get("created"), "sides": sides}


def is_skippable_trade(parsed: dict) -> bool:
    """FAAB-only trades and equal pick swaps (same season+round each way) aren't graded."""
    sides = list(parsed["sides"].values())
    if any(s["players_in"] or s["players_out"] for s in sides):
        return False
    if not any(s["picks_in"] for s in sides):
        return True  # FAAB only (or empty)
    key = lambda picks: sorted((p["season"], p["round"]) for p in picks)
    return all(key(s["picks_in"]) == key(s["picks_out"]) for s in sides)


def resolve_pick(pick: dict, drafts_by_season: dict) -> str | None:
    """If a traded pick has already been used, return the player_id it became.

    draft.slot_to_roster_id maps draft slot -> ORIGINAL roster; the pick is the one
    in that round with that draft_slot (whoever ended up making it).
    """
    draft = drafts_by_season.get(str(pick["season"]))
    if not draft or draft.get("status") != "complete" or not draft.get("slot_to_roster_id"):
        return None
    slot = next((int(s) for s, rid in draft["slot_to_roster_id"].items()
                 if rid == pick["original_roster_id"]), None)
    if slot is None:
        return None
    hit = next((p for p in draft["picks"]
                if p["round"] == pick["round"] and p["draft_slot"] == slot), None)
    return hit["player_id"] if hit else None


# --------------------------------------------------------------------------- #
# Context: lookups shared by all sections
# --------------------------------------------------------------------------- #

class Ctx:
    def __init__(self, snap: dict, cfg: dict):
        self.snap, self.cfg = snap, cfg
        self.week = snap["meta"]["week"]
        self.season = str(snap["meta"]["season"])
        self.last_reg = snap["meta"]["last_regular_season_week"]
        self.league = snap["league"]
        self.scoring = self.league["scoring_settings"]
        users = {u["user_id"]: u["display_name"] for u in snap["users"]}
        self.rosters = {r["roster_id"]: r for r in snap["rosters"]}
        self.manager = {rid: users.get(r["owner_id"], f"Team {rid}") for rid, r in self.rosters.items()}
        self.players = snap["players"]
        self.values = {v["sleeper_id"]: v for v in snap["values"] if v.get("sleeper_id")}
        self.pick_values = {v["name"]: v["value"] for v in snap["values"] if v["position"] == "PICK"}
        self.owner_of = {pid: rid for rid, r in self.rosters.items() for pid in r.get("players") or []}
        self.drafts_by_season = {str(d["season"]): d for h in reversed(snap["history"]) for d in h["drafts"]}
        self.drafts_by_season.update({str(d["season"]): d for d in snap["drafts"]})

    def pname(self, pid) -> str:
        p = self.players.get(pid) or {}
        return (p.get("full_name") or " ".join(filter(None, [p.get("first_name"), p.get("last_name")]))
                or (self.values.get(pid) or {}).get("name") or f"Player {pid}")

    def pos(self, pid) -> str | None:
        p = self.players.get(pid) or {}
        return p.get("position") or ((p.get("fantasy_positions") or [None])[0])

    def value(self, pid) -> int:
        return (self.values.get(pid) or {}).get("value", 0)

    def player_card(self, pid) -> dict:
        v = self.values.get(pid) or {}
        p = self.players.get(pid) or {}
        owner = self.owner_of.get(pid)
        return {"player": self.pname(pid), "position": self.pos(pid), "nfl_team": p.get("team"),
                "age": int(v.get("age") or p.get("age") or 0) or None, "dynasty_value": v.get("value", 0),
                "position_rank": f"{self.pos(pid)}{v['position_rank']}" if v.get("position_rank") else None,
                "value_change_30d": v.get("trend_30d", 0),
                "current_manager": self.manager[owner] if owner else None}


# --------------------------------------------------------------------------- #
# Weekly results + standings
# --------------------------------------------------------------------------- #

def week_scores(snap: dict, week: int) -> dict:
    return {m["roster_id"]: r2(m["points"]) for m in snap["matchups"].get(str(week), [])
            if m.get("matchup_id") is not None}


def week_pairs(snap: dict, week: int) -> list:
    by_id = {}
    for m in snap["matchups"].get(str(week), []):
        if m.get("matchup_id") is not None:
            by_id.setdefault(m["matchup_id"], []).append(m)
    return [pair for _, pair in sorted(by_id.items()) if len(pair) == 2]


def compute_standings(ctx: Ctx, through_week: int) -> list:
    """Records from raw matchups: head-to-head plus the weekly median game."""
    snap = ctx.snap
    rows = {rid: {"roster_id": rid, "manager": ctx.manager[rid], "wins": 0, "losses": 0, "ties": 0,
                  "h2h": [0, 0, 0], "median": [0, 0, 0], "points_for": 0.0, "points_against": 0.0,
                  "weekly_scores": [], "h2h_results": []} for rid in ctx.rosters}
    use_median = bool(ctx.league["settings"].get("league_average_match"))
    for w in range(1, min(through_week, ctx.last_reg) + 1):
        scores = week_scores(snap, w)
        if not scores:
            continue
        _, med = median_results(scores)
        for a, b in week_pairs(snap, w):
            for me, opp in ((a, b), (b, a)):
                row = rows[me["roster_id"]]
                mine, theirs = r2(me["points"]), r2(opp["points"])
                res = "W" if mine > theirs else "L" if mine < theirs else "T"
                row["h2h"]["WLT".index(res)] += 1
                row["h2h_results"].append(res)
                row["points_for"] += mine
                row["points_against"] += theirs
                row["weekly_scores"].append(mine)
                if use_median:
                    row["median"]["WLT".index(med[me["roster_id"]])] += 1
    out = []
    for row in rows.values():
        h, m = row.pop("h2h"), row.pop("median")
        row["wins"], row["losses"], row["ties"] = h[0] + m[0], h[1] + m[1], h[2] + m[2]
        row["record"] = _rec(row["wins"], row["losses"], row["ties"])
        row["h2h_record"], row["median_record"] = _rec(*h), _rec(*m)
        row["points_for"], row["points_against"] = r2(row["points_for"]), r2(row["points_against"])
        scores, results = row.pop("weekly_scores"), row.pop("h2h_results")
        row["avg_points"] = r2(mean(scores)) if scores else 0.0
        row["last3_avg_points"] = r2(mean(scores[-3:])) if scores else 0.0
        row["h2h_streak"] = _streak(results)
        out.append(row)
    out.sort(key=lambda r: (-(r["wins"] + 0.5 * r["ties"]), -r["points_for"]))
    for i, row in enumerate(out, 1):
        row["rank"] = i
    return out


def _rec(w, l, t) -> str:
    return f"{w}-{l}" + (f"-{t}" if t else "")


def _streak(results: list) -> str:
    if not results:
        return ""
    n = 1
    while n < len(results) and results[-n - 1] == results[-1]:
        n += 1
    return f"{results[-1]}{n}"


def playoff_picture(ctx: Ctx, standings: list) -> dict:
    """Seeds if the season ended today, plus conservative clinch/elimination math.

    Each remaining week is worth up to 2 wins (opponent + median). A team has
    clinched when fewer than `spots` other teams can still catch its win total;
    it is eliminated when `spots` teams already have more wins than its ceiling.
    Tiebreakers (points for) are not assumed, so these flags are never premature.
    """
    spots = ctx.league["settings"]["playoff_teams"]
    byes = ctx.cfg["league"].get("playoff_bye_seeds", 0)
    weeks_left = max(0, ctx.last_reg - ctx.week)
    per_week = 2 if ctx.league["settings"].get("league_average_match") else 1
    left = weeks_left * per_week
    teams = []
    for row in standings:
        others = [o for o in standings if o is not row]
        can_catch = sum(1 for o in others if o["wins"] + left >= row["wins"])
        ahead_for_sure = sum(1 for o in others if o["wins"] > row["wins"] + left)
        teams.append({
            "seed": row["rank"], "manager": row["manager"], "record": row["record"],
            "max_possible_wins": row["wins"] + left,
            "status": ("clinched playoff spot" if can_catch < spots else
                       "eliminated" if ahead_for_sure >= spots else
                       "in playoff position" if row["rank"] <= spots else "outside looking in"),
            "clinched_bye": byes > 0 and sum(1 for o in others if o["wins"] + left >= row["wins"]) < byes,
            "wins_behind_last_playoff_spot": max(0, standings[spots - 1]["wins"] - row["wins"])
            if row["rank"] > spots else 0,
        })
    return {"playoff_spots": spots, "bye_seeds": byes, "weeks_remaining": weeks_left,
            "games_remaining_per_team": left, "playoffs_start_week": ctx.last_reg + 1,
            "note": "Each week has two results: head-to-head and versus the league median.",
            "teams": teams}


# --------------------------------------------------------------------------- #
# Matchup recaps
# --------------------------------------------------------------------------- #

def team_week(ctx: Ctx, m: dict, exclude: set) -> dict:
    """One team's week: starters, top performer, and points left on the bench."""
    pts = {pid: r2(p) for pid, p in (m.get("players_points") or {}).items()}
    starters = [s for s in m.get("starters") or [] if s and s != "0"]
    slots = [s for s in ctx.league["roster_positions"] if s not in NON_STARTING_SLOTS]
    eligible = {pid: p for pid, p in pts.items() if pid in starters or pid not in exclude}
    positions = {pid: ctx.pos(pid) for pid in eligible}
    optimal, lineup = optimal_lineup(ctx.league["roster_positions"], eligible, positions)
    actual = r2(m["points"])
    optimal = max(optimal, actual)
    optimal_ids = {pid for _, pid in lineup if pid}

    top = max(starters, key=lambda p: pts.get(p, 0), default=None)
    out = {"manager": ctx.manager[m["roster_id"]], "roster_id": m["roster_id"], "points": actual,
           "top_starter": {"player": ctx.pname(top), "position": ctx.pos(top), "points": pts.get(top, 0)} if top else None,
           "optimal_points": optimal, "points_left_on_bench": r2(optimal - actual),
           "worst_bench_decision": None}

    # The single most painful sit/start: best benched player who belonged in the lineup,
    # paired with the lowest-scoring starter in a slot that player could have filled.
    benched = [p for p in optimal_ids if p not in starters]
    if benched:
        sat = max(benched, key=lambda p: pts[p])
        swappable = [s for s, slot in zip(m.get("starters") or [], slots)
                     if s and s != "0" and s not in optimal_ids
                     and positions.get(sat) in SLOT_ELIGIBILITY.get(slot, set())]
        if swappable:
            started = min(swappable, key=lambda p: pts.get(p, 0))
            out["worst_bench_decision"] = {
                "benched": ctx.pname(sat), "benched_position": ctx.pos(sat), "benched_points": pts[sat],
                "started_instead": ctx.pname(started), "started_position": ctx.pos(started),
                "started_points": pts.get(started, 0),
                "points_lost": r2(pts[sat] - pts.get(started, 0))}
    return out


def matchup_recaps(ctx: Ctx) -> dict:
    snap, week = ctx.snap, ctx.week
    scores = week_scores(snap, week)
    med, med_res = median_results(scores)
    taxi = {pid for r in ctx.rosters.values() for pid in r.get("taxi") or []}
    games = []
    for a, b in week_pairs(snap, week):
        ta, tb = team_week(ctx, a, taxi), team_week(ctx, b, taxi)
        for t in (ta, tb):
            t["vs_median"] = {"W": "beat the median", "L": "lost to the median", "T": "tied the median"}[med_res[t["roster_id"]]]
        win, lose = (ta, tb) if ta["points"] >= tb["points"] else (tb, ta)
        margin = r2(win["points"] - lose["points"])
        stars = [t["top_starter"] | {"manager": t["manager"]} for t in (ta, tb) if t["top_starter"]]
        blunder = max((ta, tb), key=lambda t: t["points_left_on_bench"])
        games.append({
            "winner": win["manager"], "winner_points": win["points"],
            "loser": lose["manager"], "loser_points": lose["points"],
            "margin": margin, "tie": margin == 0,
            "top_performer": max(stars, key=lambda s: s["points"]) if stars else None,
            "biggest_bench_blunder": {"manager": blunder["manager"],
                                      "points_left_on_bench": blunder["points_left_on_bench"],
                                      "detail": blunder["worst_bench_decision"]},
            "loser_would_have_won_with_optimal_lineup": lose["optimal_points"] > win["points"],
            "teams": [_strip(win), _strip(lose)],
        })
    games.sort(key=lambda g: -(g["winner_points"] + g["loser_points"]))
    return {"week": week, "league_median": med, "games": games}


def _strip(t: dict) -> dict:
    return {k: v for k, v in t.items() if k != "roster_id"}


# --------------------------------------------------------------------------- #
# Dynasty values, picks, power rankings
# --------------------------------------------------------------------------- #

def pick_tiers(standings: list, week: int) -> dict:
    """Project next year's pick slot from the standings (once there's enough season)."""
    if week < 6:
        return {}
    n = len(standings)
    third = max(1, round(n * 0.3))
    return {row["roster_id"]: ("Late" if row["rank"] <= third else
                               "Early" if row["rank"] > n - third else "Mid") for row in standings}


def pick_value(ctx: Ctx, season: str, rnd: int, original_rid: int, tiers: dict, next_draft: str) -> tuple[str, int]:
    base = f"{season} {ORDINALS.get(rnd, f'{rnd}th')}"
    tier = tiers.get(original_rid) if str(season) == next_draft else None
    for name in ([f"{base} ({tier})"] if tier else []) + [base]:
        if name in ctx.pick_values:
            return name, ctx.pick_values[name]
    return base, 0


def future_picks(ctx: Ctx, tiers: dict) -> dict:
    """roster_id -> list of future picks owned (next 3 drafts), with FantasyCalc values."""
    done = [int(s) for s, d in ctx.drafts_by_season.items() if d.get("status") == "complete"]
    first = max(done + [int(ctx.season)]) + 1
    rounds = ctx.league["settings"].get("draft_rounds", 4)
    owner = {(str(s), r, rid): rid for s in range(first, first + 3)
             for r in range(1, rounds + 1) for rid in ctx.rosters}
    for tp in ctx.snap["traded_picks"]:
        key = (str(tp["season"]), tp["round"], tp["roster_id"])
        if key in owner:
            owner[key] = tp["owner_id"]
    out = {rid: [] for rid in ctx.rosters}
    for (season, rnd, orig), rid in sorted(owner.items()):
        name, val = pick_value(ctx, season, rnd, orig, tiers, str(first))
        out[rid].append({"pick": name, "value": val,
                         "via": ctx.manager[orig] if orig != rid else None})
    return out


def roster_values(ctx: Ctx, picks: dict) -> dict:
    out = {}
    for rid, r in ctx.rosters.items():
        ps = sorted(r.get("players") or [], key=lambda p: -ctx.value(p))
        pv = sum(ctx.value(p) for p in ps)
        kv = sum(p["value"] for p in picks[rid])
        out[rid] = {"player_value": pv, "pick_value": kv, "total_value": pv + kv,
                    "top_assets": [f"{ctx.pname(p)} ({ctx.value(p)})" for p in ps[:3]],
                    "first_round_picks_owned": sum(1 for p in picks[rid] if " 1st" in p["pick"])}
    return out


def _norm(vals: dict) -> dict:
    lo, hi = min(vals.values()), max(vals.values())
    return {k: (100 * (v - lo) / (hi - lo) if hi > lo else 50.0) for k, v in vals.items()}


def power_rankings(ctx: Ctx, standings: list, rvalues: dict, prev: list | None = None) -> list:
    w = ctx.cfg["power_rankings"]["weights"]

    def score(rows):
        games = {r["roster_id"]: max(1, r["wins"] + r["losses"] + r["ties"]) for r in rows}
        rec = _norm({r["roster_id"]: (r["wins"] + 0.5 * r["ties"]) / games[r["roster_id"]] for r in rows})
        pf = _norm({r["roster_id"]: r["points_for"] for r in rows})
        dv = _norm({rid: v["total_value"] for rid, v in rvalues.items()})
        return {rid: w["record"] * rec[rid] + w["points_for"] * pf[rid] + w["dynasty_value"] * dv[rid]
                for rid in rec}, rec, pf, dv

    scores, rec, pf, dv = score(standings)
    prev_rank = {}
    if prev:
        ps = score(prev)[0]
        prev_rank = {rid: i for i, rid in enumerate(sorted(ps, key=lambda r: -ps[r]), 1)}
    by_rid = {r["roster_id"]: r for r in standings}
    value_rank = {rid: i for i, rid in enumerate(sorted(rvalues, key=lambda r: -rvalues[r]["total_value"]), 1)}
    pf_rank = {r["roster_id"]: i for i, r in enumerate(sorted(standings, key=lambda r: -r["points_for"]), 1)}
    out = []
    for i, rid in enumerate(sorted(scores, key=lambda r: -scores[r]), 1):
        s = by_rid[rid]
        out.append({"rank": i, "manager": s["manager"], "power_score": round(scores[rid], 1),
                    "previous_rank": prev_rank.get(rid),
                    "movement": (prev_rank[rid] - i) if rid in prev_rank else None,
                    "record": s["record"], "standings_rank": s["rank"],
                    "points_for": s["points_for"], "points_for_rank": pf_rank[rid],
                    "roster_dynasty_value": rvalues[rid]["total_value"], "dynasty_value_rank": value_rank[rid],
                    "top_assets": rvalues[rid]["top_assets"],
                    "first_round_picks_owned": rvalues[rid]["first_round_picks_owned"]})
    return out


# --------------------------------------------------------------------------- #
# Trades
# --------------------------------------------------------------------------- #

def grade_trades(ctx: Ctx, transactions: dict, season: str, managers: dict, tiers: dict, next_draft: str) -> list:
    out = []
    for wk in sorted(transactions, key=int):
        for t in transactions[wk]:
            if t.get("type") != "trade" or t.get("status") != "complete":
                continue
            parsed = parse_trade(t)
            if is_skippable_trade(parsed):
                continue

            def asset_player(pid):
                return {"asset": ctx.pname(pid), "type": "player", "position": ctx.pos(pid), "value": ctx.value(pid)}

            def asset_pick(p):
                label = f"{p['season']} {ORDINALS.get(p['round'], str(p['round']))}"
                orig = managers.get(p["original_roster_id"], "?")
                became = resolve_pick(p, ctx.drafts_by_season)
                if became:
                    return {"asset": f"{label}-round pick (originally {orig}'s), which became {ctx.pname(became)}",
                            "type": "pick_used", "became": ctx.pname(became), "value": ctx.value(became)}
                name, val = pick_value(ctx, p["season"], p["round"], p["original_roster_id"], tiers, next_draft)
                tier = re.search(r"\((\w+)\)", name)
                proj = f", projected {tier.group(1).lower()}" if tier else ""
                return {"asset": f"{label}-round pick{proj} (originally {orig}'s)", "type": "pick", "value": val}

            sides = []
            for rid, s in sorted(parsed["sides"].items()):
                got = [asset_player(p) for p in s["players_in"]] + [asset_pick(p) for p in s["picks_in"]]
                gave = [asset_player(p) for p in s["players_out"]] + [asset_pick(p) for p in s["picks_out"]]
                vin, vout = sum(a["value"] for a in got), sum(a["value"] for a in gave)
                sides.append({"manager": managers.get(rid, f"Team {rid}"), "received": got, "gave": gave,
                              "faab_received": s["faab_in"], "value_received": vin, "value_given": vout,
                              "net_value": vin - vout, "grade": grade(vin - vout)})
            out.append({"season": season, "week": parsed["week"],
                        "date": datetime.fromtimestamp((parsed["created"] or 0) / 1000, timezone.utc).strftime("%Y-%m-%d"),
                        "sides": sides})
    return out


def all_trades(ctx: Ctx, tiers: dict, next_draft: str) -> list:
    trades = []
    for h in reversed(ctx.snap["history"]):
        users = {u["user_id"]: u["display_name"] for u in h["users"]}
        managers = {r["roster_id"]: users.get(r["owner_id"], f"Team {r['roster_id']}") for r in h["rosters"]}
        trades += grade_trades(ctx, h["transactions"], str(h["season"]), managers, tiers, next_draft)
    trades += grade_trades(ctx, ctx.snap["transactions"], ctx.season, ctx.manager, tiers, next_draft)
    return trades


def waiver_moves(ctx: Ctx) -> list:
    week_pts = {}
    for m in ctx.snap["matchups"].get(str(ctx.week), []):
        for pid, p in (m.get("players_points") or {}).items():
            week_pts[pid] = (r2(p), pid in (m.get("starters") or []))
    out = []
    for leg in (ctx.week, ctx.week + 1):
        for t in ctx.snap["transactions"].get(str(leg), []):
            if t["type"] not in ("waiver", "free_agent") or t["status"] != "complete" or not t.get("adds"):
                continue
            drops = list((t.get("drops") or {}).keys())
            for pid, rid in t["adds"].items():
                pts, started = week_pts.get(pid, (None, False))
                out.append({"manager": ctx.manager.get(rid), "added": ctx.pname(pid), "position": ctx.pos(pid),
                            "nfl_team": (ctx.players.get(pid) or {}).get("team"),
                            "type": "waiver claim" if t["type"] == "waiver" else "free agent add",
                            "faab_bid": (t.get("settings") or {}).get("waiver_bid", 0),
                            "dynasty_value": ctx.value(pid),
                            "dropped": [ctx.pname(d) for d in drops] or None,
                            "points_this_week": pts if leg == ctx.week else None,
                            "started_this_week": started if leg == ctx.week else None,
                            "still_on_roster": ctx.owner_of.get(pid) == rid})
    out.sort(key=lambda m: (-m["faab_bid"], -m["dynasty_value"]))
    return out


# --------------------------------------------------------------------------- #
# Rookie watch + market
# --------------------------------------------------------------------------- #

def season_points(ctx: Ctx, pid) -> float | None:
    s = ctx.snap["season_stats"].get(pid)
    if not s:
        return None
    return r2(sum(v * ctx.scoring.get(k, 0) for k, v in s.items() if k != "gp"))


def rookie_watch(ctx: Ctx) -> dict:
    week_pts = {pid: r2(p) for m in ctx.snap["matchups"].get(str(ctx.week), [])
                for pid, p in (m.get("players_points") or {}).items()}
    classes = {}
    for year in ctx.cfg["league"]["rookie_classes"]:
        draft = ctx.drafts_by_season.get(str(year))
        if not draft:
            continue
        teams = len(draft.get("slot_to_roster_id") or {}) or len(ctx.rosters)
        rows = []
        for p in draft["picks"]:
            pid = p["player_id"]
            card = ctx.player_card(pid)
            rows.append(card | {
                "league_draft_pick": f"{p['round']}.{((p['pick_no'] - 1) % teams) + 1:02d}",
                "drafted_by": ctx.manager.get(p["roster_id"]),
                "points_this_week": week_pts.get(pid),
                "season_points": season_points(ctx, pid)})
        valued = [r for r in rows if r["dynasty_value"] > 0]
        classes[str(year)] = {
            "biggest_risers_30d": sorted(valued, key=lambda r: -r["value_change_30d"])[:3],
            "biggest_fallers_30d": sorted(valued, key=lambda r: r["value_change_30d"])[:3],
            "most_valuable": sorted(valued, key=lambda r: -r["dynasty_value"])[:3],
            "best_week": sorted([r for r in rows if r["points_this_week"]],
                                key=lambda r: -r["points_this_week"])[:2],
        }
    return {"note": "Rookies selected in this league's rookie drafts. Value changes are 30-day FantasyCalc trends.",
            "classes": classes}


def players_to_watch(ctx: Ctx) -> dict:
    """Standout weeks from rookies, breakout games, and players newly in starting lineups.

    Built from league matchup data, so it covers rostered players only. "Prior" numbers
    are from earlier weeks this season while the player was on a league roster.
    """
    prior_pts, prior_starts = {}, {}
    for w in range(1, ctx.week):
        for m in ctx.snap["matchups"].get(str(w), []):
            starters = set(m.get("starters") or [])
            for pid, p in (m.get("players_points") or {}).items():
                if p:
                    prior_pts.setdefault(pid, []).append(float(p))
                if pid in starters:
                    prior_starts[pid] = prior_starts.get(pid, 0) + 1
    drafted = {}
    for year in ctx.cfg["league"]["rookie_classes"]:
        d = ctx.drafts_by_season.get(str(year))
        teams = len((d or {}).get("slot_to_roster_id") or {}) or len(ctx.rosters)
        for p in (d or {}).get("picks", []):
            drafted[p["player_id"]] = {"rookie_class": year, "drafted_by": ctx.manager.get(p["roster_id"]),
                                       "league_draft_pick": f"{p['round']}.{((p['pick_no'] - 1) % teams) + 1:02d}"}
    cards = []
    for m in ctx.snap["matchups"].get(str(ctx.week), []):
        starters = set(m.get("starters") or [])
        for pid, pts in (m.get("players_points") or {}).items():
            hist = prior_pts.get(pid, [])
            info = ctx.players.get(pid) or {}
            v = ctx.values.get(pid) or {}
            cards.append({
                "player": ctx.pname(pid), "position": ctx.pos(pid), "nfl_team": info.get("team"),
                "manager": ctx.manager[m["roster_id"]], "started_this_week": pid in starters,
                "points_this_week": r2(pts),
                "prior_avg_points_when_playing": r2(mean(hist)) if hist else None,
                "prior_games_with_points": len(hist), "prior_starts_in_this_league": prior_starts.get(pid, 0),
                "is_rookie": info.get("years_exp") == 0,
                "dynasty_value": v.get("value", 0), "value_change_30d": v.get("trend_30d", 0),
            } | drafted.get(pid, {}))
    by_pts = sorted(cards, key=lambda c: -c["points_this_week"])
    rookies = [c for c in by_pts if c["is_rookie"] and c["points_this_week"] >= 10][:5]
    breakouts = [c | {"points_above_prior_avg": r2(c["points_this_week"] - (c["prior_avg_points_when_playing"] or 0))}
                 for c in cards
                 if not c["is_rookie"] and c["points_this_week"] >= 15
                 and (c["prior_avg_points_when_playing"] or 0) < 10
                 and c["points_this_week"] - (c["prior_avg_points_when_playing"] or 0) >= 8]
    breakouts = sorted(breakouts, key=lambda c: -c["points_above_prior_avg"])[:5]
    new_starters = [c for c in by_pts if c["started_this_week"] and c["prior_starts_in_this_league"] <= 1
                    and ctx.week >= 3][:5]
    return {
        "note": "Rostered players only. Rookie = first NFL season. Breakout = 15+ points this week from a player "
                "who averaged under 10 before. New starter = in a starting lineup this week after at most one "
                "start in this league earlier this season. started_this_week=false means the points were on a bench.",
        "rookie_standouts": rookies, "breakout_games": breakouts, "new_starters": new_starters}


def dynasty_market(ctx: Ctx) -> dict:
    cards = [ctx.player_card(pid) for pid in ctx.owner_of if pid in ctx.values]
    risers = sorted(cards, key=lambda c: -c["value_change_30d"])
    fallers = sorted(cards, key=lambda c: c["value_change_30d"])

    def aging(c):
        return (c["age"] or 0) >= (26 if c["position"] == "RB" else 28)

    sell = [c for c in risers if c["value_change_30d"] >= 250 and aging(c)
            and c["dynasty_value"] >= 2000][:3] or risers[:2]
    buy = [c for c in fallers if c["value_change_30d"] <= -250 and (c["age"] or 99) <= 25
           and c["dynasty_value"] >= 2500][:3] or fallers[:2]
    return {"note": "30-day value movement for players on league rosters (FantasyCalc, superflex).",
            "biggest_risers": risers[:5], "biggest_fallers": fallers[:5],
            "sell_high_candidates": sell, "buy_low_candidates": buy,
            "candidate_rules": "Sell-high: rising value on an aging player (RB 26+, others 28+) worth 2,000+. "
                               "Buy-low: falling value on a player 25 or younger who is still worth 2,500+."}


# --------------------------------------------------------------------------- #
# Storylines, next week, seasonal
# --------------------------------------------------------------------------- #

def storylines(recaps: dict, standings: list, power: list) -> list:
    games = recaps["games"]
    teams = [t for g in games for t in g["teams"]]
    if not teams:
        return []
    hi, lo = max(teams, key=lambda t: t["points"]), min(teams, key=lambda t: t["points"])
    close, blow = min(games, key=lambda g: g["margin"]), max(games, key=lambda g: g["margin"])
    bench = max(teams, key=lambda t: t["points_left_on_bench"])
    rank = {p["manager"]: p["previous_rank"] or p["rank"] for p in power}
    out = [
        {"type": "high_score", "manager": hi["manager"], "points": hi["points"]},
        {"type": "low_score", "manager": lo["manager"], "points": lo["points"]},
        {"type": "closest_game", "winner": close["winner"], "loser": close["loser"], "margin": close["margin"]},
        {"type": "biggest_blowout", "winner": blow["winner"], "loser": blow["loser"], "margin": blow["margin"]},
        {"type": "most_points_left_on_bench", "manager": bench["manager"],
         "points_left_on_bench": bench["points_left_on_bench"], "detail": bench["worst_bench_decision"]},
    ]
    for g in games:
        if g["loser_would_have_won_with_optimal_lineup"]:
            out.append({"type": "bench_cost_the_win", "manager": g["loser"], "lost_by": g["margin"],
                        "points_left_on_bench": g["teams"][1]["points_left_on_bench"]})
        if rank.get(g["winner"], 0) - rank.get(g["loser"], 0) >= 4:
            out.append({"type": "upset", "winner": g["winner"], "loser": g["loser"],
                        "winner_power_rank_before": rank[g["winner"]], "loser_power_rank_before": rank[g["loser"]]})
        w, l = g["teams"]
        if w["vs_median"] == "lost to the median":
            out.append({"type": "won_matchup_but_lost_to_median", "manager": w["manager"], "points": w["points"]})
        if l["vs_median"] == "beat the median":
            out.append({"type": "lost_matchup_but_beat_median", "manager": l["manager"], "points": l["points"]})
    for s in standings:
        if int(s["h2h_streak"][1:] or 0) >= 3:
            out.append({"type": "streak", "manager": s["manager"], "head_to_head_streak": s["h2h_streak"]})
    return out


def next_week(ctx: Ctx, standings: list, power: list) -> dict | None:
    nw = ctx.week + 1
    pairs = week_pairs(ctx.snap, nw)
    if not pairs:
        return None
    st = {s["roster_id"]: s for s in standings}
    pr = {p["manager"]: p["rank"] for p in power}

    def card(m):
        s = st[m["roster_id"]]
        return {"manager": s["manager"], "record": s["record"], "seed": s["rank"],
                "power_rank": pr[s["manager"]], "avg_points": s["avg_points"],
                "last3_avg_points": s["last3_avg_points"],
                "form_projection": r2((s["avg_points"] + s["last3_avg_points"]) / 2)}

    games = []
    for a, b in pairs:
        ca, cb = card(a), card(b)
        fav, dog = (ca, cb) if ca["form_projection"] >= cb["form_projection"] else (cb, ca)
        games.append({"teams": [ca, cb], "form_favorite": fav["manager"],
                      "form_projection_edge": r2(fav["form_projection"] - dog["form_projection"]),
                      "combined_power_rank": ca["power_rank"] + cb["power_rank"]})
    games.sort(key=lambda g: (g["combined_power_rank"], g["form_projection_edge"]))
    return {"week": nw, "is_playoffs": nw > ctx.last_reg, "marquee_matchup": games[0], "other_matchups": games[1:],
            "note": "form_projection = average of season scoring average and last-3-weeks average. "
                    "It is a simple form guide, not a real projection."}


def seasonal(ctx: Ctx, standings: list) -> dict:
    st = ctx.league["settings"]
    out = {}
    deadline = st.get("trade_deadline")
    upcoming = ctx.week + 1
    if deadline and 0 < deadline < 99 and 0 <= deadline - upcoming <= 3:
        out["trade_deadline"] = {"deadline_week": deadline, "upcoming_week": upcoming,
                                 "weeks_until_deadline": deadline - upcoming,
                                 "is_deadline_week": deadline == upcoming}
    if ctx.week >= ctx.last_reg - 1:
        spots = st["playoff_teams"]
        out["playoff_bracket"] = {
            "final": ctx.week >= ctx.last_reg, "playoffs_start_week": ctx.last_reg + 1,
            "seeds": [{"seed": s["rank"], "manager": s["manager"], "record": s["record"]}
                      for s in standings[:spots]]}
    if ctx.snap["state"].get("season_type") in ("off", "pre") or ctx.week > 17:
        out["offseason"] = {"draft_order_if_reverse_standings":
                            [s["manager"] for s in reversed(standings)]}
    return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def build_facts(snap: dict, cfg: dict) -> dict:
    ctx = Ctx(snap, cfg)
    standings = compute_standings(ctx, ctx.week)
    prev_standings = compute_standings(ctx, ctx.week - 1) if ctx.week > 1 else None
    _crosscheck(ctx, standings)

    tiers = pick_tiers(standings, ctx.week)
    picks = future_picks(ctx, tiers)
    next_draft = picks and str(max([int(s) for s, d in ctx.drafts_by_season.items()
                                    if d.get("status") == "complete"] + [int(ctx.season)]) + 1)
    rvalues = roster_values(ctx, picks)
    power = power_rankings(ctx, standings, rvalues, prev_standings)
    recaps = matchup_recaps(ctx)
    trades = all_trades(ctx, tiers, next_draft)
    this_week = [t for t in trades if t["season"] == ctx.season and t["week"] in (ctx.week, ctx.week + 1)]
    earlier = [t for t in trades if t["season"] == ctx.season and t["week"] < ctx.week]

    strip = ("roster_id",)
    return {
        "meta": {"show": cfg["show"]["name"], "league": ctx.league["name"], "season": ctx.season,
                 "week": ctx.week, "generated_at": snap["meta"]["fetched_at"],
                 "format": "10-team dynasty, superflex, half-PPR, TE premium, weekly median game",
                 "values_source": "FantasyCalc dynasty values (superflex, 10 teams, 0.5 PPR)"},
        "storylines": storylines(recaps, standings, power),
        "matchup_recaps": recaps,
        "standings": [{k: v for k, v in s.items() if k not in strip} for s in standings],
        "playoff_picture": playoff_picture(ctx, standings),
        "power_rankings": power,
        "transactions": {
            "grading_note": "Trade grades use TODAY's dynasty values, in hindsight: net = value received minus value given.",
            "trades_this_week": this_week,
            "recent_trades_this_season": earlier[-3:],
            "waiver_and_free_agent_pickups": waiver_moves(ctx)[:6],
        },
        "players_to_watch": players_to_watch(ctx),
        "rookie_watch": rookie_watch(ctx),
        "dynasty_market": dynasty_market(ctx),
        "next_week": next_week(ctx, standings, power),
        "seasonal": seasonal(ctx, standings),
        "trade_ledger": trades,          # full graded history, for the show notes
    }


def _crosscheck(ctx: Ctx, standings: list) -> None:
    """Our records should match Sleeper's own when analyzing the latest scored week."""
    if ctx.league["settings"].get("last_scored_leg") != ctx.week:
        return
    for s in standings:
        official = ctx.rosters[s["roster_id"]]["settings"]
        if (s["wins"], s["losses"]) != (official.get("wins"), official.get("losses")):
            log.warning("Record mismatch for %s: computed %s, Sleeper says %s-%s",
                        s["manager"], s["record"], official.get("wins"), official.get("losses"))


def run(snap: dict, cfg: dict) -> tuple[dict, object]:
    facts = build_facts(snap, cfg)
    path = facts_path(facts["meta"]["week"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(facts, indent=1))
    log.info("Wrote %s", path)
    return facts, path
