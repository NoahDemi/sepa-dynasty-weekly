"""Per-episode cost log (LLM tokens + TTS characters)."""
from __future__ import annotations

import json
import logging

from .config import DATA

log = logging.getLogger("podcast.costs")


class CostLog:
    def __init__(self):
        self.items = []

    def add_llm(self, label: str, model: str, usage, prices: dict) -> None:
        inp = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        out = usage.output_tokens or 0
        usd = inp / 1e6 * prices["input"] + out / 1e6 * prices["output"]
        self.items.append({"kind": "llm", "label": label, "model": model, "input_tokens": inp,
                           "output_tokens": out, "usd": round(usd, 4), "spent": True})

    def add_tts(self, provider: str, chars: int, usd: float, spent: bool = True) -> None:
        self.items.append({"kind": "tts", "label": provider, "characters": chars,
                           "usd": round(usd, 4), "spent": spent})

    def report(self, week: int) -> dict:
        spent = round(sum(i["usd"] for i in self.items if i["spent"]), 4)
        est = round(sum(i["usd"] for i in self.items), 4)
        summary = {"week": week, "items": self.items, "spent_usd": spent, "total_with_estimates_usd": est}
        path = DATA / "costs" / f"week_{week:02d}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, indent=1))
        log.info("---- Cost for week %s ----", week)
        for i in self.items:
            detail = (f"{i['input_tokens']:,} in / {i['output_tokens']:,} out tokens" if i["kind"] == "llm"
                      else f"{i['characters']:,} characters")
            log.info("  %-34s %-34s $%.3f%s", i["label"], detail, i["usd"], "" if i["spent"] else "  (estimate)")
        log.info("  Spent: $%.2f   Full episode estimate: $%.2f", spent, est)
        return summary
