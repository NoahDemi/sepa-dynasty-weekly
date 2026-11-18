"""Tests for the analyze step, pinned to a saved real snapshot (2026 week 10)."""
import json
from pathlib import Path

import pytest
import yaml

from podcast import analyze as A

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def snap():
    return json.loads((FIX / "snapshot_2026_week10.json").read_text())


@pytest.fixture(scope="module")
def cfg():
    return yaml.safe_load((ROOT / "config.yaml").read_text())


@pytest.fixture(scope="module")
def facts(snap, cfg):
    return A.build_facts(snap, cfg)


def find_trade(snap, season, pred):
    sources = [snap["transactions"]] if season == snap["meta"]["season"] else [
        h["transactions"] for h in snap["history"] if h["season"] == season]
    for txs in sources:
        for week in txs.values():
            for t in week:
                if t["type"] == "trade" and t["status"] == "complete" and pred(t):
                    return t
    raise AssertionError("fixture trade not found")


# ---- grading -------------------------------------------------------------

@pytest.mark.parametrize("net,letter", [
    (9000, "A+"), (2500, "A+"), (2499, "A"), (1500, "A"), (1499, "A-"), (750, "A-"),
    (749, "B"), (0, "B"), (-750, "B"), (-751, "C+"), (-1500, "C+"), (-1501, "C"),
    (-2500, "C"), (-2501, "D"), (-4000, "D"), (-4001, "F"), (-9000, "F"),
])
def test_grade_thresholds(net, letter):
    assert A.grade(net) == letter


# ---- median game ---------------------------------------------------------

def test_median_is_mean_of_5th_and_6th(snap):
    scores = A.week_scores(snap, 10)
    assert len(scores) == 10
    ordered = sorted(scores.values(), reverse=True)
    med, results = A.median_results(scores)
    assert med == round((ordered[4] + ordered[5]) / 2, 2) == 131.15
    assert sum(r == "W" for r in results.values()) == 5
    assert sum(r == "L" for r in results.values()) == 5
    assert results[9] == "W"   # 135.44: lost head-to-head, still beat the median
    assert results[5] == "L"   # 126.86: won head-to-head, lost to the median


def test_median_tie_and_odd_league():
    med, res = A.median_results({1: 100, 2: 90, 3: 90, 4: 80})
    assert med == 90 and res == {1: "W", 2: "T", 3: "T", 4: "L"}
    assert A.median_results({1: 3, 2: 2, 3: 1})[0] == 2


def test_standings_match_sleeper_official_records(snap, cfg):
    standings = A.compute_standings(A.Ctx(snap, cfg), 10)
    official = {r["roster_id"]: r["settings"] for r in snap["rosters"]}
    for s in standings:
        assert (s["wins"], s["losses"]) == (official[s["roster_id"]]["wins"], official[s["roster_id"]]["losses"])
    assert standings[0]["manager"] == "Djax818" and standings[0]["record"] == "17-3"
    assert standings[0]["h2h_record"] == "9-1" and standings[0]["median_record"] == "8-2"


# ---- trade parsing -------------------------------------------------------

def test_parse_trade_player_for_pick(snap):
    t = find_trade(snap, "2026", lambda t: "1166" in (t.get("adds") or {}))
    sides = A.parse_trade(t)["sides"]
    assert sides[10]["players_in"] == ["1166"] and sides[2]["players_out"] == ["1166"]
    pick = {"season": "2027", "round": 3, "original_roster_id": 10}
    assert sides[2]["picks_in"] == [pick] and sides[10]["picks_out"] == [pick]
    assert not A.is_skippable_trade(A.parse_trade(t))


def test_parse_trade_faab_direction():
    t = {"roster_ids": [1, 2], "adds": None, "drops": None, "draft_picks": [],
         "waiver_budget": [{"sender": 1, "receiver": 2, "amount": 15}]}
    parsed = A.parse_trade(t)
    assert parsed["sides"][2]["faab_in"] == 15 and parsed["sides"][1]["faab_in"] == -15
    assert A.is_skippable_trade(parsed)  # FAAB only


def test_skip_equal_pick_swap_but_not_unequal():
    def pick(rnd, orig, new, prev):
        return {"season": "2027", "round": rnd, "roster_id": orig, "owner_id": new, "previous_owner_id": prev}
    equal = {"roster_ids": [1, 2], "draft_picks": [pick(2, 1, 2, 1), pick(2, 2, 1, 2)]}
    unequal = {"roster_ids": [1, 2], "draft_picks": [pick(1, 1, 2, 1), pick(2, 2, 1, 2)]}
    assert A.is_skippable_trade(A.parse_trade(equal))
    assert not A.is_skippable_trade(A.parse_trade(unequal))


# ---- pick resolution -----------------------------------------------------

def test_resolve_used_pick_to_drafted_player(snap, cfg):
    ctx = A.Ctx(snap, cfg)
    draft = ctx.drafts_by_season["2026"]
    assert draft["slot_to_roster_id"]["9"] == 5          # slot 9 originally belonged to roster 5
    # Roster 5's 2026 1st was traded away; whoever held slot 9 took KC Concepcion.
    pid = A.resolve_pick({"season": "2026", "round": 1, "original_roster_id": 5}, ctx.drafts_by_season)
    made = next(p for p in draft["picks"] if p["round"] == 1 and p["draft_slot"] == 9)
    assert pid == made["player_id"] and made["roster_id"] != 5
    assert ctx.pname(pid).startswith("KC Concepcion")


def test_future_pick_is_unresolved(snap, cfg):
    ctx = A.Ctx(snap, cfg)
    assert A.resolve_pick({"season": "2027", "round": 1, "original_roster_id": 5}, ctx.drafts_by_season) is None


def test_trade_with_used_picks_is_graded_at_player_values(facts):
    chase = next(t for t in facts["trade_ledger"] if t["season"] == "2024"
                 and any(a["asset"] == "Ja'Marr Chase" for s in t["sides"] for a in s["received"]))
    got_chase = next(s for s in chase["sides"] if s["manager"] == "rfrancella97")
    other = next(s for s in chase["sides"] if s["manager"] == "stehr")
    assert any(a.get("became", "").startswith("KC Concepcion") for a in other["received"])
    assert got_chase["net_value"] == -other["net_value"] == 2225
    assert (got_chase["grade"], other["grade"]) == ("A", "C")
    for t in facts["trade_ledger"]:
        for s in t["sides"]:
            assert s["net_value"] == s["value_received"] - s["value_given"]
            assert s["grade"] == A.grade(s["net_value"])


# ---- lineups -------------------------------------------------------------

def test_optimal_lineup_uses_flex_and_superflex():
    slots = ["QB", "RB", "WR", "TE", "FLEX", "SUPER_FLEX", "BN", "BN"]
    pts = {"q1": 20, "q2": 18, "r1": 15, "r2": 14, "w1": 12, "w2": 3, "t1": 5, "t2": 9}
    pos = {"q1": "QB", "q2": "QB", "r1": "RB", "r2": "RB", "w1": "WR", "w2": "WR", "t1": "TE", "t2": "TE"}
    total, lineup = A.optimal_lineup(slots, pts, pos)
    assert total == 20 + 15 + 12 + 9 + 14 + 18
    assert dict(lineup)["SUPER_FLEX"] == "q2" and dict(lineup)["FLEX"] == "r2"


def test_bench_points_never_negative_and_recaps_cover_every_team(facts):
    games = facts["matchup_recaps"]["games"]
    assert len(games) == 5
    teams = [t for g in games for t in g["teams"]]
    assert len({t["manager"] for t in teams}) == 10
    for t in teams:
        assert t["points_left_on_bench"] >= 0 and t["optimal_points"] >= t["points"]
    for g in games:
        assert g["winner_points"] >= g["loser_points"]
        assert g["margin"] == round(g["winner_points"] - g["loser_points"], 2)


def test_power_rankings_and_playoff_picture(facts):
    pr = facts["power_rankings"]
    assert [p["rank"] for p in pr] == list(range(1, 11))
    assert pr[0]["manager"] == "Djax818"
    status = {t["manager"]: t["status"] for t in facts["playoff_picture"]["teams"]}
    assert status["Djax818"] == "clinched playoff spot"   # 17 wins; only 5 teams can still reach 17
    assert status["Cwmessi"] == "eliminated"              # max 9 wins; six teams already have 10+
    assert facts["seasonal"]["trade_deadline"]["is_deadline_week"] is True
