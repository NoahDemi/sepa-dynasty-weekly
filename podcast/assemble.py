"""Step 5 - assemble: stitch the voice clips, add music, normalize loudness, tag the MP3."""
from __future__ import annotations

import json
import logging
import random
import re
import subprocess
from pathlib import Path

from .assets import ensure_assets
from .config import DATA, ROOT

log = logging.getLogger("podcast.assemble")

RATE = 44100
INTRO_SOLO = 3.5      # seconds of music before the first line
INTRO_FADE = 6.0      # music fades out under the cold open over this long
OUTRO_OVERLAP = 2.0   # outro music starts this long before the last line ends
OUTRO_LEN = 14.0


def ff(*args: str, capture: bool = False) -> str:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "info" if capture else "error", *args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(cmd)}\n{p.stderr[-2000:]}")
    return p.stderr


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def episode_slug(season, week: int) -> str:
    return f"{season}-week-{week:02d}"


def build_voice_track(lines: list, cfg: dict, work: Path) -> Path:
    """Concatenate clips with short, slightly varied gaps (longer on speaker and segment changes)."""
    a = cfg["audio"]
    rnd = random.Random(len(lines))  # deterministic per script
    clips = work / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    entries, prev = [], None

    def silence(ms: int) -> Path:
        p = work / f"silence_{ms}.wav"
        if not p.exists():
            ff("-f", "lavfi", "-i", f"anullsrc=r={RATE}:cl=mono", "-t", f"{ms / 1000:.3f}", "-c:a", "pcm_s16le", str(p))
        return p

    n = 0
    for ln in lines:
        for j, src in enumerate(ln["files"]):
            if prev is not None:
                if j > 0:
                    gap = 120
                elif ln["segment"] != prev["segment"]:
                    gap = a["segment_gap_ms"]
                else:
                    lo, hi = a["gap_ms"]
                    same = ln["speaker"] == prev["speaker"]
                    gap = rnd.randrange(lo, (lo + hi) // 2) if not same else rnd.randrange((lo + hi) // 2, hi)
                entries.append(silence(int(round(gap, -1))))
            wav = clips / f"{n:04d}.wav"
            # Trim TTS lead-in/tail silence so the configured gaps are the real gaps.
            ff("-i", str(src), "-af",
               "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.03,"
               "areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.06,areverse",
               "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", str(wav))
            entries.append(wav)
            n += 1
        prev = ln
    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in entries))
    voice = work / "voice.wav"
    ff("-f", "concat", "-safe", "0", "-i", str(listing), "-c:a", "pcm_s16le", str(voice))
    return voice


def mix_with_music(voice: Path, cfg: dict, work: Path) -> Path:
    a = cfg["audio"]
    intro, outro = ROOT / a["intro_music"], ROOT / a["outro_music"]
    vdur = duration(voice)
    outro_at = INTRO_SOLO + vdur - OUTRO_OVERLAP
    graph = (
        f"[0:a]adelay={int(INTRO_SOLO * 1000)}:all=1[v];"
        f"[1:a]aresample={RATE},atrim=0:{INTRO_SOLO + INTRO_FADE},volume=0.55,"
        f"afade=t=out:st={INTRO_SOLO - 0.5}:d={INTRO_FADE}[i];"
        f"[2:a]aresample={RATE},atrim=0:{OUTRO_LEN},volume=0.55,afade=t=in:st=0:d={OUTRO_OVERLAP},"
        f"afade=t=out:st={OUTRO_LEN - 4}:d=4,adelay={int(outro_at * 1000)}:all=1[o];"
        f"[v][i][o]amix=inputs=3:normalize=0:duration=longest[m]"
    )
    mixed = work / "mixed.wav"
    ff("-i", str(voice), "-i", str(intro), "-i", str(outro), "-filter_complex", graph,
       "-map", "[m]", "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", str(mixed))
    return mixed


def loudnorm_filter(src: Path, target: float) -> str:
    """Two-pass EBU R128: measure first, then apply a linear gain to hit the target."""
    base = f"loudnorm=I={target}:TP=-1.5:LRA=11"
    log_text = ff("-i", str(src), "-af", base + ":print_format=json", "-f", "null", "-", capture=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", log_text, re.S)
    if not m:
        log.warning("Could not read loudness measurement; using single-pass normalization")
        return base
    s = json.loads(m.group(0))
    return (f"{base}:measured_I={s['input_i']}:measured_TP={s['input_tp']}:measured_LRA={s['input_lra']}"
            f":measured_thresh={s['input_thresh']}:offset={s['target_offset']}:linear=true")


def run(lines: list, script: dict, facts: dict, cfg: dict, week: int) -> Path:
    ensure_assets(cfg)
    a, show = cfg["audio"], cfg["show"]
    season = facts["meta"]["season"]
    work = DATA / "audio" / f"week_{week:02d}" / "work"
    work.mkdir(parents=True, exist_ok=True)
    log.info("Assembling %s clips", sum(len(l["files"]) for l in lines))
    mixed = mix_with_music(build_voice_track(lines, cfg, work), cfg, work)

    out_dir = ROOT / cfg["publish"]["episodes_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    mp3 = out_dir / f"{episode_slug(season, week)}.mp3"
    title = f"Week {week}: {script['episode_title']}"
    meta = {"title": title, "artist": show["author"], "album": show["name"], "album_artist": show["author"],
            "genre": "Podcast", "date": str(season), "track": str(week), "comment": script["episode_summary"]}
    args = ["-i", str(mixed), "-i", str(ROOT / a["cover"]), "-map", "0:a", "-map", "1:v",
            "-af", (f"atempo={a['tempo']}," if a.get("tempo", 1.0) != 1.0 else "")
            + loudnorm_filter(mixed, a["loudness_lufs"]), "-ar", str(RATE), "-ac", "1",
            "-c:a", "libmp3lame", "-b:a", a["bitrate"], "-c:v", "mjpeg", "-id3v2_version", "3",
            "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)",
            "-disposition:v", "attached_pic"]
    for k, v in meta.items():
        args += ["-metadata", f"{k}={v}"]
    ff(*args, str(mp3))
    log.info("Wrote %s (%.1f min, %.1f MB)", mp3, duration(mp3) / 60, mp3.stat().st_size / 1e6)
    return mp3
