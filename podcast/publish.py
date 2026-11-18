"""Step 6 - publish: show notes, episode metadata, and the podcast RSS feed.

Everything lands in episodes/, which is also the root of the GitHub Pages site:
  episodes/2026-week-10.mp3 / .md / .json, episodes/feed.xml, episodes/index.html, episodes/cover.jpg
"""
from __future__ import annotations

import html
import json
import logging
import shutil
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

from .assemble import duration, episode_slug
from .config import ROOT

log = logging.getLogger("podcast.publish")


def _table(headers: list, rows: list) -> list:
    return ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)] + \
           ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]


def _assets(side: dict) -> str:
    return ", ".join(a["asset"] for a in side["received"]) or "nothing"


def show_notes(script: dict, facts: dict, cfg: dict, costs: dict | None = None) -> str:
    week, season = facts["meta"]["week"], facts["meta"]["season"]
    league_id = cfg["league"]["id"]
    out = [f"# Week {week}: {script['episode_title']}", "",
           f"**{cfg['show']['name']}** - {season} season, week {week}", "", script["episode_summary"], ""]

    out += ["## Scores", ""]
    out += _table(["Winner", "Pts", "Loser", "Pts", "Top performer", "Most left on bench"],
                  [[g["winner"], g["winner_points"], g["loser"], g["loser_points"],
                    f"{g['top_performer']['player']} ({g['top_performer']['points']})",
                    f"{g['biggest_bench_blunder']['manager']} ({g['biggest_bench_blunder']['points_left_on_bench']})"]
                   for g in facts["matchup_recaps"]["games"]])
    out += ["", f"League median: {facts['matchup_recaps']['league_median']}", ""]

    status = {t["manager"]: t["status"] for t in facts["playoff_picture"]["teams"]}
    out += ["## Standings", ""]
    out += _table(["#", "Manager", "Record", "H2H", "vs Median", "PF", "PA", "Streak", "Playoffs"],
                  [[s["rank"], s["manager"], s["record"], s["h2h_record"], s["median_record"], s["points_for"],
                    s["points_against"], s["h2h_streak"], status[s["manager"]]] for s in facts["standings"]])

    out += ["", "## Power rankings", ""]
    out += _table(["#", "Manager", "Move", "Record", "PF rank", "Dynasty value", "Value rank"],
                  [[p["rank"], p["manager"],
                    "-" if not p["movement"] else f"{'+' if p['movement'] > 0 else ''}{p['movement']}",
                    p["record"], p["points_for_rank"], f"{p['roster_dynasty_value']:,}", p["dynasty_value_rank"]]
                   for p in facts["power_rankings"]])

    tx = facts["transactions"]
    out += ["", "## Trade grades", "", f"*{tx['grading_note']}*", ""]
    shown = tx["trades_this_week"] or tx["recent_trades_this_season"]
    if not tx["trades_this_week"]:
        out += ["No trades this week. Most recent deals:", ""]
    for t in shown:
        out.append(f"- **{t['season']} week {t['week']}**: " + "; ".join(
            f"{s['manager']} got {_assets(s)} (**{s['grade']}**, {s['net_value']:+,})" for s in t["sides"]))
    season_trades = [t for t in facts["trade_ledger"] if t["season"] == season]
    if season_trades:
        out += ["", f"<details><summary>All {season} trades</summary>", ""]
        out += _table(["Week", "Manager", "Received", "Net", "Grade"],
                      [[t["week"], s["manager"], _assets(s), f"{s['net_value']:+,}", s["grade"]]
                       for t in season_trades for s in t["sides"]])
        out += ["", "</details>"]

    pickups = tx["waiver_and_free_agent_pickups"]
    if pickups:
        out += ["", "## Waiver wire", ""]
        out += [f"- {w['manager']} added {w['added']} ({w['position']}, {w['nfl_team'] or 'FA'})"
                + (f" for ${w['faab_bid']} FAAB" if w["faab_bid"] else "")
                + (f", dropped {', '.join(w['dropped'])}" if w["dropped"] else "") for w in pickups]

    m = facts["dynasty_market"]
    out += ["", "## Dynasty market (30-day movers)", ""]
    out += _table(["Player", "Manager", "Value", "30-day"],
                  [[c["player"], c["current_manager"], f"{c['dynasty_value']:,}", f"{c['value_change_30d']:+,}"]
                   for c in m["biggest_risers"][:5] + m["biggest_fallers"][:5]])

    nw = facts.get("next_week")
    if nw:
        a, b = nw["marquee_matchup"]["teams"]
        out += ["", f"## Next week", "",
                f"Marquee matchup, week {nw['week']}: **{a['manager']}** ({a['record']}) vs "
                f"**{b['manager']}** ({b['record']})"]

    out += ["", "## Links", "",
            f"- [League on Sleeper](https://sleeper.com/leagues/{league_id})",
            "- [FantasyCalc dynasty values](https://www.fantasycalc.com/dynasty-rankings)", "",
            "*Generated automatically. Stats are computed from Sleeper data; opinions belong to two AI hosts "
            "and should be held against them.*", ""]
    return "\n".join(out)


def rebuild_feed(cfg: dict) -> Path:
    """Regenerate feed.xml and index.html from the episode metadata files."""
    show, pub = cfg["show"], cfg["publish"]
    site = ROOT / pub["episodes_dir"]
    base = (pub.get("base_url") or "").rstrip("/")
    if not base:
        log.warning("publish.base_url is empty; feed links will be relative until you set it")
    cover = ROOT / cfg["audio"]["cover"]
    if cover.exists():
        shutil.copyfile(cover, site / "cover.jpg")
    eps = sorted((json.loads(p.read_text()) for p in site.glob("*-week-*.json")),
                 key=lambda e: e["published"], reverse=True)

    items = []
    for e in eps:
        url = f"{base}/{e['file']}"
        secs = int(e["duration_seconds"])
        items.append(f"""    <item>
      <title>{escape(e['title'])}</title>
      <description>{escape(e['summary'])}</description>
      <itunes:summary>{escape(e['summary'])}</itunes:summary>
      <enclosure url="{escape(url)}" length="{e['bytes']}" type="audio/mpeg"/>
      <guid isPermaLink="false">{escape(e['guid'])}</guid>
      <pubDate>{format_datetime(datetime.fromisoformat(e['published']))}</pubDate>
      <itunes:duration>{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}</itunes:duration>
      <itunes:season>{e['season']}</itunes:season>
      <itunes:episode>{e['week']}</itunes:episode>
      <itunes:explicit>false</itunes:explicit>
      <link>{escape(base + '/#' + e['slug'])}</link>
    </item>""")
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(show['name'])}</title>
    <link>{escape(base + '/')}</link>
    <atom:link href="{escape(base + '/feed.xml')}" rel="self" type="application/rss+xml"/>
    <language>{show['language']}</language>
    <description>{escape(show['tagline'])}</description>
    <itunes:author>{escape(show['author'])}</itunes:author>
    <itunes:image href="{escape(base + '/cover.jpg')}"/>
    <image><url>{escape(base + '/cover.jpg')}</url><title>{escape(show['name'])}</title><link>{escape(base + '/')}</link></image>
    <itunes:category text="Sports"><itunes:category text="Fantasy Sports"/></itunes:category>
    <itunes:explicit>false</itunes:explicit>
    <itunes:type>episodic</itunes:type>
    <itunes:block>Yes</itunes:block>
    <lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (site / "feed.xml").write_text(feed)
    (site / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    (site / ".nojekyll").write_text("")

    rows = "\n".join(
        f"""<article id="{e['slug']}"><h2>{html.escape(e['title'])}</h2>
<p class="meta">{e['published'][:10]} &middot; {int(e['duration_seconds']) // 60} min</p>
<p>{html.escape(e['summary'])}</p>
<audio controls preload="none" src="{html.escape(e['file'])}"></audio>
<p><a href="{html.escape(e['notes'])}">Show notes</a></p></article>""" for e in eps)
    (site / "index.html").write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{html.escape(show['name'])}</title>
<link rel="alternate" type="application/rss+xml" title="{html.escape(show['name'])}" href="feed.xml">
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:680px;margin:0 auto;padding:24px 16px;background:#0b3d2e;color:#f4f1e8}}
a{{color:#e9b44c}}header{{display:flex;gap:16px;align-items:center}}header img{{width:112px;height:112px;border-radius:12px}}
article{{border-top:1px solid #145c45;padding:16px 0}}h1,h2{{margin:.2em 0}}.meta{{opacity:.7;margin:0}}audio{{width:100%}}
code{{background:#145c45;padding:2px 6px;border-radius:4px;word-break:break-all}}</style></head><body>
<header><img src="cover.jpg" alt=""><div><h1>{html.escape(show['name'])}</h1><p>{html.escape(show['tagline'])}</p></div></header>
<p>Subscribe: in your podcast app choose "Add a show by URL" (Apple Podcasts: Library, then the menu, then
"Follow a Show by URL") and paste <code>{html.escape(base + '/feed.xml')}</code></p>
{rows}
</body></html>
""")
    log.info("Feed rebuilt with %s episode(s): %s", len(eps), site / "feed.xml")
    return site / "feed.xml"


def run(mp3: Path, script: dict, facts: dict, cfg: dict, week: int) -> None:
    season = facts["meta"]["season"]
    slug = episode_slug(season, week)
    site = mp3.parent
    notes = site / f"{slug}.md"
    notes.write_text(show_notes(script, facts, cfg))
    meta_path = site / f"{slug}.json"
    published = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if meta_path.exists():  # re-rendering an episode keeps its original publish date
        published = json.loads(meta_path.read_text()).get("published", published)
    meta_path.write_text(json.dumps({
        "slug": slug, "guid": f"{cfg['league']['id']}-{slug}", "season": int(season), "week": week,
        "title": f"Week {week}: {script['episode_title']}", "summary": script["episode_summary"],
        "file": mp3.name, "notes": notes.name, "bytes": mp3.stat().st_size,
        "duration_seconds": round(duration(mp3), 1), "published": published}, indent=1))
    log.info("Wrote %s", notes)
    rebuild_feed(cfg)
