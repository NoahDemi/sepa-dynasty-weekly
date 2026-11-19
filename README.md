# SEPA Dynasty Weekly

An automated weekly podcast for the SEPA Dynasty fantasy football league. Every Tuesday it pulls the
league from Sleeper, computes the week's facts in code, has Claude write a two-host script from those
facts, voices it, mixes it, and publishes an MP3 with show notes to a podcast feed.

Hosts: **Doc** (stats-driven analyst) and **Vinny** (hot-take trash talker). Episodes run 5-7 minutes:
cold open, a short scoreboard (only the results worth talking about), players to watch (rookie
standouts, breakouts, new starters), trades when there are any, the playoff race, and a look ahead.
The longer segments (every matchup, full power rankings, rookie value watch, dynasty market) are off
by default and can be switched back on in `config.yaml`; show notes always include all of it.

## How it works

```
fetch -> analyze -> script -> voice -> assemble -> publish
```

| Step | Module | What it does | Output |
|---|---|---|---|
| fetch | `podcast/fetch.py` | Sleeper league, matchups, transactions, drafts (current + 2 prior seasons), FantasyCalc dynasty values | `data/week_XX.json` |
| analyze | `podcast/analyze.py` | Recaps, bench points vs optimal lineup, median results, standings, clinch/elimination, power rankings, trade grades, rookie and market movers | `data/facts/week_XX.json` |
| script | `podcast/script.py` | Claude writes the script from the facts JSON only; a code check plus a second Claude call verify every number and name | `data/scripts/week_XX.json` and `.md` |
| voice | `podcast/voice.py` | Text-to-speech per line (ElevenLabs, OpenAI, or macOS `say`), cached by content hash, with retries and chunking | `data/audio/week_XX/` |
| assemble | `podcast/assemble.py` | ffmpeg: natural gaps, intro/outro music, two-pass loudness normalization to -16 LUFS, MP3 with ID3 tags and cover | `episodes/2026-week-XX.mp3` |
| publish | `podcast/publish.py` | Show notes, episode metadata, RSS feed and a small web page | `episodes/*.md`, `feed.xml`, `index.html` |

The model never does arithmetic. Every number the hosts say is computed in `analyze.py` and checked
against the facts afterwards; anything the automated check cannot match is listed at the bottom of the
script markdown.

## Setup

Requires Python 3.10+ and ffmpeg.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env     # then add ANTHROPIC_API_KEY and ELEVENLABS_API_KEY
```

## Running

```bash
python -m podcast run                     # latest completed week, full episode
python -m podcast run --week 10           # a specific week
python -m podcast run --dry-run           # stop after the script: no TTS spend
python -m podcast run --facts-only        # fetch + analyze only: no API spend at all
python -m podcast run --skip-fetch        # reuse data/week_XX.json
python -m podcast run --voices-only       # reuse the saved script, redo TTS + audio
python -m podcast feed                    # rebuild feed.xml and index.html
python -m pytest                          # analyze tests (run on a saved real snapshot)
```

A typical edit loop: `--dry-run`, read or hand-edit `data/scripts/week_XX.md`'s source JSON
(`data/scripts/week_XX.json`), then `--voices-only`. Voice clips are cached, so only lines you changed
are re-synthesized.

Each run logs its cost (LLM tokens and TTS characters) and saves it to `data/costs/week_XX.json`.

## Configuration

Everything lives in `config.yaml`: league ID, host names and personalities, segment toggles, length
target, model, TTS provider and voices, power-ranking weights, audio settings. Secrets live in `.env`
(never committed).

Things you will most likely want to tweak:

- **`pronunciations`**: how the hosts say each manager's Sleeper name out loud.
- **`tts.elevenlabs.voices`**: voice IDs for each host. Any voice in your ElevenLabs library works.
- **`tts.provider`**: `elevenlabs`, `openai`, or `say` (free macOS voices, useful for testing the pipeline).
- **`assets/intro.wav`, `assets/outro.wav`, `assets/cover.jpg`**: generated placeholders. Replace the
  files with your own (delete one and it is regenerated).
- **`segments`** and **`show.target_words`**: choose which segments run and how long the episode is.

## How things are calculated

- **Records** include the weekly median game: beat the league median (average of the 5th and 6th scores)
  for a second win. Computed records are cross-checked against Sleeper's.
- **Bench points**: optimal lineup (QB, 2 RB, 3 WR, TE, 2 FLEX, SUPERFLEX) minus actual. Taxi-squad
  players are excluded.
- **Clinched / eliminated**: conservative win-total math (two results per remaining week); tiebreakers
  are never assumed.
- **Power rankings**: 40% record, 35% points for, 25% roster dynasty value (players plus future picks).
- **Trade grades**: net = today's value received minus value given. A+ at +2,500 or more, A +1,500,
  A- +750, B within 750 either way, C+ down to -1,500, C to -2,500, D to -4,000, F below. Picks that have
  already been used are valued as the player they became; future picks use FantasyCalc pick values, with
  next year's picks tiered early/mid/late from the current standings. FAAB-only trades and equal pick
  swaps are skipped.

## Schedule

`.github/workflows/weekly.yml` runs every Tuesday at 8am Eastern during the season, commits the new
episode and data, and deploys `episodes/` to GitHub Pages. It can also be run by hand from the Actions
tab (with optional week, dry-run, and force inputs). If a run fails the job fails loudly and uploads
whatever it produced as a `partial-run-*` artifact.

Repo secrets needed: `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY` (and `OPENAI_API_KEY` if you switch
providers).
