"""CLI: python -m podcast run --week N [--dry-run] [--skip-fetch] [--voices-only]"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import analyze, fetch
from .config import facts_path, load_config, script_path, snapshot_path

log = logging.getLogger("podcast")


def latest_local_week() -> int:
    from .config import DATA
    weeks = sorted(int(p.stem.split("_")[1]) for p in DATA.glob("week_*.json"))
    if not weeks:
        sys.exit("No snapshot in data/. Run without --skip-fetch first.")
    return weeks[-1]


def episode_exists(cfg: dict, week: int | None) -> bool:
    """Cheap pre-check (two small API calls) so a repeat run changes nothing on disk."""
    from .assemble import episode_slug
    from .config import ROOT
    league = fetch.get_json(f"{fetch.SLEEPER}/league/{cfg['league']['id']}")
    week = week or fetch.latest_completed_week(fetch.get_json(f"{fetch.SLEEPER}/state/nfl"), league)
    mp3 = ROOT / cfg["publish"]["episodes_dir"] / f"{episode_slug(league['season'], week)}.mp3"
    if mp3.exists():
        print(f"Week {week} episode already exists ({mp3.name}); nothing to do.")
    return mp3.exists()


def cmd_run(args) -> None:
    cfg = load_config(args.config)
    from .costs import CostLog
    costs = CostLog()

    if args.voices_only:
        week = args.week or latest_local_week()
        script = json.loads(script_path(week).read_text())
        facts = json.loads(facts_path(week).read_text())
    else:
        if args.skip_fetch:
            week = args.week or latest_local_week()
            snap = json.loads(snapshot_path(week).read_text())
        else:
            if args.skip_existing and episode_exists(cfg, args.week):
                return
            snap, _ = fetch.run(cfg, args.week)
            week = snap["meta"]["week"]
        facts, fpath = analyze.run(snap, cfg)
        if args.facts_only:
            print(f"Facts written to {fpath}")
            return
        from . import script as script_mod
        script, spath = script_mod.run(facts, cfg, costs)
        print(f"Script written to {spath} and {spath.with_suffix('.md')}")

    if args.dry_run:
        from .voice import estimate_tts
        est = estimate_tts(script, cfg)
        costs.add_tts(est["provider"] + " (estimate, not spent)", est["chars"], est["usd"], spent=False)
        costs.report(week)
        return

    from . import assemble, publish, voice
    lines = voice.run(script, cfg, week, costs)
    mp3 = assemble.run(lines, script, facts, cfg, week)
    publish.run(mp3, script, facts, cfg, week)
    costs.report(week)
    print(f"Episode ready: {mp3}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(prog="podcast", description="SEPA Dynasty Weekly generator")
    ap.add_argument("--config", help="path to config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="build an episode")
    run.add_argument("--week", type=int, help="NFL week (default: latest completed)")
    run.add_argument("--dry-run", action="store_true", help="stop after the script; no TTS spend")
    run.add_argument("--skip-fetch", action="store_true", help="reuse data/week_XX.json")
    run.add_argument("--voices-only", action="store_true", help="reuse the saved script; redo TTS + audio")
    run.add_argument("--skip-existing", action="store_true", help="exit quietly if this week's MP3 exists")
    run.add_argument("--facts-only", action="store_true", help="stop after fetch + analyze (no API spend)")
    run.set_defaults(func=cmd_run)
    feed = sub.add_parser("feed", help="rebuild the RSS feed from episodes/")
    feed.set_defaults(func=lambda a: __import__("podcast.publish", fromlist=["x"]).rebuild_feed(load_config(a.config)))
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
