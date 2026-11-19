"""Step 3 - script: facts JSON -> two-host script via the Claude API, then a fact-check pass.

Three layers keep the numbers honest:
  1. The writer only sees the facts JSON and is told to use nothing else.
  2. A code check flags every number in the script that does not appear in the facts.
  3. A second Claude call audits every number and name against the facts and returns
     line-level corrections, which are applied in code.
"""
from __future__ import annotations

import json
import logging
import re

import anthropic

from .config import script_path

log = logging.getLogger("podcast.script")

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# (id, title, brief, fact keys the segment needs). Order here is the running order.
SEGMENTS = [
    ("cold_open", "Cold open", "The single biggest story of the week from `storylines`, straight into it, then "
     "a one-line welcome naming the show and both hosts. Keep it tight.", ["storylines"]),
    ("scoreboard", "Scoreboard", "Not a recap of every game. Pick the two or three results from `storylines` and "
     "`matchup_recaps` that are worth talking about (an upset, a nail-biter, a blowout, a streak) plus the single "
     "worst bench decision of the week. Routine games get skipped entirely.", ["storylines", "matchup_recaps"]),
    ("matchup_recaps", "Matchup recaps", "Every matchup in `matchup_recaps.games`: the score, the top performer, and "
     "the biggest bench blunder (points left on the bench versus the optimal lineup; use `detail` for the specific "
     "sit/start mistake). Mention who beat or lost to the league median when it is interesting.",
     ["matchup_recaps"]),
    ("players_to_watch", "Players to watch", "The heart of the show. From `players_to_watch`: rookies who had "
     "standout weeks, breakout games from players who had been quiet, and players who are new to a starting "
     "lineup. Say who rosters each one, whether the points were started or left on the bench, and how this week "
     "compares to what the player had been doing (`prior_avg_points_when_playing`). For rookies, mention who "
     "drafted them and where when that is in the facts. Pick the four to six most interesting names across the "
     "three lists; this is a watch list for next week's lineups, so say why each one matters.",
     ["players_to_watch"]),
    ("trades", "Trades", "Only if `transactions.trades_this_week` has trades: walk through each one, who got "
     "what, and each side's letter grade, saying once that grades use today's dynasty values in hindsight. If "
     "there were no trades, do not revisit old ones: write no lines for this segment, or a single passing line. "
     "A waiver pickup gets a mention only if it is genuinely notable.", ["transactions"]),
    ("playoff_race", "Playoff race", "Thirty seconds on the standings: who has clinched or been eliminated "
     "(`playoff_picture` statuses), who is on the bubble, and the one race worth watching. Records include the "
     "weekly median game. Do not read the whole table.", ["standings", "playoff_picture"]),
    ("standings", "Standings and playoff race", "Records (they include the weekly median game), points for, who is "
     "in playoff position, and clinch or elimination news from `playoff_picture`. Tell the story of the race.",
     ["standings", "playoff_picture"]),
    ("power_rankings", "Power rankings", "Count down 10 to 1 from `power_rankings` with a one-liner for each "
     "manager. The ranking blends record, points for, and roster dynasty value; say so once.", ["power_rankings"]),
    ("transactions", "Transactions", "Trades from `transactions.trades_this_week` with each side's letter grade "
     "(say once that grades use today's dynasty values, in hindsight); if none, revisit one or two from "
     "`recent_trades_this_season`. Then the notable waiver and free agent pickups.", ["transactions"]),
    ("rookie_watch", "Rookie watch", "The rookie classes in `rookie_watch`: biggest 30-day dynasty value risers "
     "and fallers and which manager drafted them (and who has them now, if different).", ["rookie_watch"]),
    ("dynasty_market", "Dynasty market", "Biggest value movers on league rosters from `dynasty_market`, then one "
     "buy-low call and one sell-high call chosen from the candidate lists. The calls are the hosts' opinions.",
     ["dynasty_market"]),
    ("outro", "Outro", "Next week's marquee matchup from `next_week.marquee_matchup`, a quick prediction from each "
     "host (their opinion), and a sign-off.", ["next_week"]),
]
DEFAULT_OFF = {"matchup_recaps", "standings", "power_rankings", "transactions", "rookie_watch", "dynasty_market"}


def active_segments(cfg: dict) -> list:
    toggles = cfg["segments"]
    return [s for s in SEGMENTS if toggles.get(s[0], s[0] not in DEFAULT_OFF)]


SEASONAL = {
    "trade_deadline": "Trade deadline: use `seasonal.trade_deadline`. Give it a line or two in the trades or "
                      "playoff race segment: how close the deadline is and who should be buying or selling.",
    "playoff_bracket": "Playoff bracket: use `seasonal.playoff_bracket`. Add it after the standings segment.",
    "offseason": "Offseason rookie draft preview: use `seasonal.offseason`. Add it after rookie watch.",
}

SCRIPT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["episode_title", "episode_summary", "segments"],
    "properties": {
        "episode_title": {"type": "string"},
        "episode_summary": {"type": "string"},
        "segments": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "lines"],
            "properties": {
                "id": {"type": "string"},
                "lines": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["speaker", "text"],
                    "properties": {"speaker": {"type": "string"}, "text": {"type": "string"}}}}}}},
    },
}

FIXES_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "corrections"],
    "properties": {
        "verdict": {"type": "string"},
        "corrections": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["line", "problem", "corrected_text"],
            "properties": {"line": {"type": "integer"}, "problem": {"type": "string"},
                           "corrected_text": {"type": "string"}}}},
    },
}


def writer_system(cfg: dict, facts: dict) -> str:
    show, hosts = cfg["show"], cfg["hosts"]
    lo, hi = show["target_words"]
    toggles = cfg["segments"]
    segs = active_segments(cfg)
    seg_text = "\n".join(f"{i}. id `{sid}` - {title}: {brief}" for i, (sid, title, brief, _) in enumerate(segs, 1))
    seasonal = [SEASONAL[k] for k in (facts.get("seasonal") or {}) if toggles.get("seasonal", True) and k in SEASONAL]
    host_text = "\n".join(f"- {h['name']} (speaker id `{h['id']}`), the {h['role']}. {h['personality'].strip()}"
                          for h in hosts)
    return f"""You are the head writer for "{show['name']}", a weekly podcast made for the ten managers of a \
dynasty fantasy football league ({facts['meta']['format']}). The audience is those ten friends and nobody else: \
they know each other, they know the league, and they are listening to hear their own names, their wins, and \
their mistakes read back to them with love.

The show has two hosts:
{host_text}

They like each other and they disagree constantly. The best episodes sound like two friends at a bar who happen \
to have a spreadsheet open: one makes a point with a number, the other turns it into a grudge. They interrupt, \
react to each other, and call back to things said earlier in the episode.

## Where the facts come from

The user message is a JSON document of facts computed by code from the league's Sleeper data and FantasyCalc \
dynasty values. It is the only source of truth for this episode.

- Every score, record, rank, point total, dynasty value, grade, and player or manager name you write must appear \
in that JSON. Copy numbers exactly; do not add, subtract, average, or round them into new numbers. If a number \
you want is not there, make the point without a number.
- Do not bring in outside knowledge about real NFL games, injuries, or news. You do not know what happened on \
the field beyond the points in the JSON, so do not describe plays, touchdowns, or injuries.
- Opinions are welcome and should sound like opinions: predictions, buy-low and sell-high calls, who should \
panic. Ground them in the facts provided.
- Refer to managers by their Sleeper display name exactly as written in the JSON (for example `kingdemi`). \
Never use or invent fantasy team names. Every manager is fair game and gets equal treatment.

## Tone

Funny, conversational, and roast-y in the way friends roast each other. The target is always a fantasy \
decision (a lineup, a trade, a record), never the person: nothing about anyone's looks, job, family, \
intelligence, or life outside this league, and nothing crude. A manager having a terrible season should come \
away laughing, and every roast should be one the hosts would say to that person's face.

## Segments, in this order

{seg_text}
{("Seasonal this week: " + " ".join(seasonal)) if seasonal else ""}

Use exactly these segment ids, in this order. A segment with nothing worth saying this week can have an \
empty `lines` list. The facts JSON holds more than a short show can use: leave most of it out, and never \
list things just because they are there.

## Writing for the ear

This script goes straight to text-to-speech, so write only words to be spoken: no stage directions, sound \
cues, emojis, markdown, or parentheticals. Keep each line to one to four sentences so the hosts trade off \
often. Write numbers as digits (174.92, 17-3, 9,362); the speech engine handles them. Spoken transitions \
carry the listener between segments, since there are no visible headings.

Length: {lo} to {hi} words of dialogue in total, which is {lo // show['words_per_minute']} to \
{hi // show['words_per_minute'] + 1} minutes on air. This is a hard ceiling, and numbers are slow to say out \
loud: use a number when it is the point, and skip decimals and dynasty values that are only decoration. \
Spend the words where the stories are.

Also write `episode_title` (short, specific to this week's biggest story, no week number needed) and \
`episode_summary` (two sentences for the podcast app). Use the speaker ids {', '.join('`' + h['id'] + '`' for h in hosts)}."""


CHECKER_SYSTEM = """You are the fact-checker for a fantasy football podcast. You receive the facts JSON that the \
script was written from, and the script as numbered lines. The facts JSON is ground truth.

Check every line. A line needs a correction when it contains:
- a number (score, record, rank, margin, points, dynasty value, FAAB bid, week, pick) that does not match the facts \
for the thing it describes, or that was derived by arithmetic the facts do not contain;
- a manager or player name that is misspelled, swapped, or attached to the wrong team, matchup, trade, or stat;
- a trade grade, winner, clinch or elimination status, or riser/faller direction that contradicts the facts;
- a claim about real NFL events (plays, injuries, news) that is not supported by the facts;
- a fantasy team name instead of the manager's display name.

Opinions, jokes, and predictions are fine and are not errors. Do not rewrite for style.

For each line that needs it, return the line number, a short description of the problem, and `corrected_text`: \
the full replacement line with the smallest change that makes it accurate, keeping the speaker's voice. If a \
wrong number cannot be fixed from the facts, rewrite the sentence without the number. Lines that are already \
accurate must not appear in `corrections`. `verdict` is one sentence summarizing what you found."""


def llm_payload(facts: dict, cfg: dict | None = None) -> str:
    """Facts for the model: only the sections the active segments use (never the full trade ledger)."""
    drop = {"trade_ledger"}
    if cfg is not None:
        need = {"meta", "seasonal"} | {k for seg in active_segments(cfg) for k in seg[3]}
        drop |= set(facts) - need
    return json.dumps({k: v for k, v in facts.items() if k not in drop}, separators=(",", ":"))


def call_claude(client, cfg: dict, system: str, user: str, schema: dict, label: str, costs) -> dict:
    llm = cfg["llm"]
    with client.beta.messages.stream(
        model=llm["model"], max_tokens=32000, system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": llm.get("effort", "high"), "format": {"type": "json_schema", "schema": schema}},
        betas=[FALLBACK_BETA], fallbacks="default",
    ) as stream:
        msg = stream.get_final_message()
    if costs is not None:
        costs.add_llm(label, msg.model, msg.usage, llm["price_per_mtok"])
    if msg.stop_reason == "refusal":
        raise RuntimeError(f"{label}: the model declined this request ({msg.stop_details})")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(f"{label}: output was cut off at max_tokens")
    return json.loads(next(b.text for b in msg.content if b.type == "text"))


def flatten(draft: dict, cfg: dict) -> list:
    valid = {h["id"] for h in cfg["hosts"]}
    lines = []
    for seg in draft["segments"]:
        for ln in seg["lines"]:
            speaker = ln["speaker"].strip().lower()
            if speaker not in valid:
                raise ValueError(f"Unknown speaker {ln['speaker']!r} in script")
            if ln["text"].strip():
                lines.append({"speaker": speaker, "segment": seg["id"], "text": ln["text"].strip()})
    return lines


# ---- deterministic number check ---------------------------------------------

NUM_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def _numbers_in(obj, out: set) -> set:
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, (int, float)):
        out.add(round(abs(float(obj)), 2))
    elif isinstance(obj, str):
        for m in NUM_RE.findall(obj):
            try:
                out.add(round(abs(float(m.replace(",", ""))), 2))
            except ValueError:
                pass
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _numbers_in(v, out)
    return out


def unverified_numbers(lines: list, facts: dict, cfg: dict | None = None) -> list:
    """Numbers spoken in the script that appear nowhere in the facts (exact, or a plain rounding)."""
    known = _numbers_in(json.loads(llm_payload(facts, cfg)), set())
    known |= {round(n, 1) for n in known} | {float(round(n)) for n in known}
    known |= {float(n) for n in range(0, 21)}  # small counts and ordinals are not stats
    flagged = []
    for i, ln in enumerate(lines):
        for m in NUM_RE.findall(ln["text"]):
            for part in m.replace(",", "").lstrip("-").split("-"):
                try:
                    val = round(float(part), 2)
                except ValueError:
                    continue
                if val not in known:
                    flagged.append({"line": i, "number": m})
    return flagged


def fact_check(client, cfg: dict, facts: dict, lines: list, flagged: list, costs) -> tuple[list, dict]:
    numbered = "\n".join(f"[{i}] {ln['speaker'].upper()}: {ln['text']}" for i, ln in enumerate(lines))
    hints = ""
    if flagged:
        hints = ("\n\nAn automated check could not find these numbers anywhere in the facts; look at them "
                 "closely:\n" + "\n".join(f"- line {f['line']}: {f['number']}" for f in flagged))
    user = f"<facts>\n{llm_payload(facts, cfg)}\n</facts>\n\n<script>\n{numbered}\n</script>{hints}"
    report = call_claude(client, cfg, CHECKER_SYSTEM, user, FIXES_SCHEMA, "fact-check", costs)
    fixed = [dict(ln) for ln in lines]
    for c in report["corrections"]:
        if 0 <= c["line"] < len(fixed) and c["corrected_text"].strip():
            log.info("Fact-check fix, line %s: %s", c["line"], c["problem"])
            c["original_text"] = fixed[c["line"]]["text"]
            fixed[c["line"]]["text"] = c["corrected_text"].strip()
    return fixed, report


def to_markdown(script: dict, cfg: dict) -> str:
    names = {h["id"]: h["name"] for h in cfg["hosts"]}
    titles = {s[0]: s[1] for s in SEGMENTS}
    out = [f"# {script['episode_title']}", "",
           f"*{cfg['show']['name']} - {script['season']} Week {script['week']} - "
           f"{script['word_count']:,} words (~{script['est_minutes']} min)*", "", script["episode_summary"], ""]
    seg = None
    for ln in script["lines"]:
        if ln["segment"] != seg:
            seg = ln["segment"]
            out += ["", f"## {titles.get(seg, seg.replace('_', ' ').title())}", ""]
        out += [f"**{names[ln['speaker']]}:** {ln['text']}", ""]
    fc = script.get("fact_check")
    if fc:
        out += ["", "---", "", f"## Fact-check report", "", fc["verdict"], ""]
        for c in fc["corrections"]:
            out += [f"- Line {c['line']}: {c['problem']}",
                    f"  - was: {c.get('original_text', '')}", f"  - now: {c['corrected_text']}"]
        if fc.get("unverified_numbers_after"):
            out += ["", "Numbers the automated check still could not match to the facts (review these):"]
            out += [f"- line {u['line']}: {u['number']} - {script['lines'][u['line']]['text']}"
                    for u in fc["unverified_numbers_after"]]
    return "\n".join(out) + "\n"


def run(facts: dict, cfg: dict, costs=None) -> tuple[dict, object]:
    client = anthropic.Anthropic()
    week = facts["meta"]["week"]
    log.info("Writing script with %s", cfg["llm"]["model"])
    draft = call_claude(client, cfg, writer_system(cfg, facts), llm_payload(facts, cfg), SCRIPT_SCHEMA,
                        "script draft", costs)
    lines = flatten(draft, cfg)
    report = None
    if cfg["llm"].get("fact_check", True):
        flagged = unverified_numbers(lines, facts, cfg)
        log.info("Fact-checking %s lines (%s numbers flagged by the automated check)", len(lines), len(flagged))
        lines, report = fact_check(client, cfg, facts, lines, flagged, costs)
        report["unverified_numbers_before"] = flagged
        report["unverified_numbers_after"] = unverified_numbers(lines, facts, cfg)
        for u in report["unverified_numbers_after"]:
            log.warning("Unverified number %s on line %s", u["number"], u["line"])
    words = sum(len(ln["text"].split()) for ln in lines)
    script = {"episode_title": draft["episode_title"], "episode_summary": draft["episode_summary"],
              "season": facts["meta"]["season"], "week": week, "word_count": words,
              "est_minutes": round(words / cfg["show"]["words_per_minute"], 1),
              "lines": lines, "fact_check": report}
    path = script_path(week)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(script, indent=1))
    path.with_suffix(".md").write_text(to_markdown(script, cfg))
    log.info("Script: %s words, about %s minutes", words, script["est_minutes"])
    return script, path
