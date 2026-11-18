"""Step 4 - voice: text-to-speech per script line, with a swappable provider.

Providers: elevenlabs (default), openai, and say (macOS built-in voices; free, for testing).
Audio is cached by content hash, so re-runs only pay for lines that changed.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import time
from pathlib import Path

import requests

from .config import DATA

log = logging.getLogger("podcast.voice")


# ---- text preparation ---------------------------------------------------

def speakable(text: str, cfg: dict) -> str:
    """Rewrite a script line into what the voice should actually say."""
    for name, said in (cfg.get("pronunciations") or {}).items():
        text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", said, text)
    # Draft slots: "the 1.08" -> "the one-oh-eight" style
    text = re.sub(r"\b(the|pick|at) ([1-5])\.(0[1-9]|1[0-2])\b(?!\d)",
                  lambda m: f"{m.group(1)} {m.group(2)} {'oh ' + m.group(3)[1] if m.group(3)[0] == '0' else m.group(3)}",
                  text)
    # Records: 17-3 -> 17 and 3 (also 9-1-1). Leaves hyphenated words and years alone.
    text = re.sub(r"(?<![\w.,-])(\d{1,2})-(\d{1,2})(?:-(\d{1,2}))?(?![\w.-])",
                  lambda m: " and ".join(g for g in m.groups() if g), text)
    text = text.replace("PPR", "P P R").replace("FAAB", "fab")
    return text.strip()


def chunk_text(text: str, limit: int) -> list:
    """Split long text at sentence boundaries so each request stays under the provider limit."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        while len(sentence) > limit:  # one enormous sentence: split on the last space
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append((cur + " " + sentence[:cut]).strip() if cur else sentence[:cut])
            cur, sentence = "", sentence[cut:].strip()
        if cur and len(cur) + 1 + len(sentence) > limit:
            chunks.append(cur)
            cur = sentence
        else:
            cur = (cur + " " + sentence).strip()
    if cur:
        chunks.append(cur)
    return chunks


# ---- providers ----------------------------------------------------------

class TTSError(RuntimeError):
    pass


def _post_with_retries(url: str, headers: dict, payload: dict, max_retries: int, params=None):
    """POST returning the audio response. Retries rate limits and server errors with backoff."""
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, params=params, timeout=180)
        except requests.RequestException as e:
            err, retry_after = str(e), None
        else:
            if r.status_code == 200:
                return r
            err, retry_after = f"HTTP {r.status_code}: {r.text[:300]}", r.headers.get("retry-after")
            if r.status_code not in (408, 409, 425, 429) and r.status_code < 500:
                raise TTSError(err)  # bad key, bad voice, quota exhausted: retrying will not help
        if attempt == max_retries:
            raise TTSError(f"TTS failed after {max_retries + 1} attempts: {err}")
        wait = float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else min(60, 2 ** attempt * 2)
        log.warning("TTS request failed (%s); retry %s/%s in %.0fs", err, attempt + 1, max_retries, wait)
        time.sleep(wait)


class ElevenLabs:
    name, ext = "elevenlabs", "mp3"

    def __init__(self, cfg: dict):
        self.c = cfg["tts"]["elevenlabs"]
        self.key = os.environ.get("ELEVENLABS_API_KEY")
        if not self.key:
            raise TTSError("ELEVENLABS_API_KEY is not set (put it in .env)")
        self.retries = cfg["tts"]["max_retries"]
        self.billed = 0

    def cache_key(self, host: str) -> str:
        return f"{self.c['model']}:{self.c['voices'][host]}:{self.c.get('voice_settings')}"

    def synth(self, text: str, host: str, out: Path) -> None:
        payload = {"text": text, "model_id": self.c["model"]}
        if self.c.get("voice_settings"):
            payload["voice_settings"] = self.c["voice_settings"].get(host) or self.c["voice_settings"]
        r = _post_with_retries(
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.c['voices'][host]}",
            {"xi-api-key": self.key, "accept": "audio/mpeg"}, payload, self.retries,
            params={"output_format": self.c.get("output_format", "mp3_44100_128")})
        out.write_bytes(r.content)
        # ElevenLabs reports what it actually deducted from the plan for this request.
        self.billed += int(r.headers.get("character-cost") or len(text))


class OpenAITTS:
    name, ext = "openai", "mp3"

    def __init__(self, cfg: dict):
        self.c = cfg["tts"]["openai"]
        self.key = os.environ.get("OPENAI_API_KEY")
        if not self.key:
            raise TTSError("OPENAI_API_KEY is not set (put it in .env)")
        self.retries = cfg["tts"]["max_retries"]
        self.style = {h["id"]: f"You are {h['name']}, a podcast host: {h['role']}. {h['personality'].strip()} "
                               "Speak naturally and conversationally." for h in cfg["hosts"]}

    def cache_key(self, host: str) -> str:
        return f"{self.c['model']}:{self.c['voices'][host]}"

    def synth(self, text: str, host: str, out: Path) -> None:
        payload = {"model": self.c["model"], "voice": self.c["voices"][host], "input": text,
                   "response_format": "mp3"}
        if "gpt-4o" in self.c["model"]:
            payload["instructions"] = self.style[host]
        out.write_bytes(_post_with_retries("https://api.openai.com/v1/audio/speech",
                                           {"Authorization": f"Bearer {self.key}"}, payload, self.retries).content)


class MacSay:
    """Free local voices via macOS `say`. Robotic, but exercises the whole audio pipeline."""
    name, ext = "say", "aiff"

    def __init__(self, cfg: dict):
        self.voices = (cfg["tts"].get("say") or {}).get("voices") or {}
        hosts = [h["id"] for h in cfg["hosts"]]
        self.default = dict(zip(hosts, ["Samantha", "Daniel"]))

    def cache_key(self, host: str) -> str:
        return self.voices.get(host) or self.default[host]

    def synth(self, text: str, host: str, out: Path) -> None:
        subprocess.run(["say", "-v", self.cache_key(host), "-o", str(out), text], check=True)


PROVIDERS = {"elevenlabs": ElevenLabs, "openai": OpenAITTS, "say": MacSay}


def estimate_tts(script: dict, cfg: dict) -> dict:
    provider = cfg["tts"]["provider"]
    chars = sum(len(speakable(ln["text"], cfg)) for ln in script["lines"])
    rate = (cfg["tts"].get(provider) or {}).get("usd_per_1k_chars", 0)
    return {"provider": provider, "chars": chars, "usd": chars / 1000 * rate}


def run(script: dict, cfg: dict, week: int, costs=None) -> list:
    """Synthesize every line. Returns [{speaker, segment, files: [Path, ...]}] in script order."""
    name = cfg["tts"]["provider"]
    if name not in PROVIDERS:
        raise TTSError(f"Unknown TTS provider {name!r}; choose one of {sorted(PROVIDERS)}")
    provider = PROVIDERS[name](cfg)
    out_dir = DATA / "audio" / f"week_{week:02d}" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    limit = cfg["tts"]["max_chars_per_request"]
    rate = (cfg["tts"].get(name) or {}).get("usd_per_1k_chars", 0)
    results, new_chars, cached = [], 0, 0
    total = len(script["lines"])
    for i, ln in enumerate(script["lines"]):
        files = []
        for j, chunk in enumerate(chunk_text(speakable(ln["text"], cfg), limit)):
            digest = hashlib.sha1(f"{provider.cache_key(ln['speaker'])}|{chunk}".encode()).hexdigest()[:12]
            path = out_dir / f"{i:03d}_{j}_{ln['speaker']}_{digest}.{provider.ext}"
            if path.exists() and path.stat().st_size > 0:
                cached += 1
            else:
                tmp = path.with_suffix(".part")
                provider.synth(chunk, ln["speaker"], tmp)
                tmp.rename(path)
                new_chars += len(chunk)
            files.append(path)
        results.append({"speaker": ln["speaker"], "segment": ln["segment"], "files": files})
        if (i + 1) % 20 == 0 or i + 1 == total:
            log.info("TTS %s/%s lines", i + 1, total)
    log.info("TTS done via %s: %s new characters, %s clips reused from cache", name, f"{new_chars:,}", cached)
    billed = getattr(provider, "billed", None)
    if billed is not None and billed != new_chars:
        log.info("%s billed %s credits for those characters", name, f"{billed:,}")
    if costs is not None:
        costs.add_tts(name, new_chars, (new_chars if billed is None else billed) / 1000 * rate)
    return results
