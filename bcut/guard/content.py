"""Community-guideline screen for what is said in a clip.

Three layers, each optional except the first:
1. Word lists in `lexicon.json` (fast, offline, English + Hindi/Hinglish). Add your own terms there.
2. OpenAI's free moderation endpoint, if an OpenAI key is set (OPENAI_MODERATION_KEY).
3. The risk flags the clip-picking AI raised while reading the transcript.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import httpx

from .. import tools

LEXICON = Path(__file__).with_name("lexicon.json")

PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s-]?)?(?:\d[\s-]?){9,11}\d(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

MOD_MAP = {  # OpenAI moderation category -> our risk
    "hate": "hate", "hate/threatening": "hate", "harassment": "harassment", "harassment/threatening": "violence",
    "self-harm": "self_harm", "self-harm/intent": "self_harm", "self-harm/instructions": "self_harm",
    "sexual": "sexual", "sexual/minors": "sexual", "violence": "violence", "violence/graphic": "violence",
    "illicit": "drugs", "illicit/violent": "violence",
}


@lru_cache(maxsize=1)
def _lexicon() -> dict[str, re.Pattern]:
    data = json.loads(LEXICON.read_text(encoding="utf-8"))
    out = {}
    for risk, terms in data.items():
        if risk.startswith("_") or not terms:
            continue
        alt = "|".join(re.escape(t).replace(r"\ ", r"\s+") for t in terms)
        out[risk] = re.compile(rf"(?<![\w])(?:{alt})(?![\w])", re.I)
    return out


def scan_words(text: str) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    for risk, rx in _lexicon().items():
        found = sorted({m.group(0).lower() for m in rx.finditer(text)})
        if found:
            hits[risk] = found[:6]
    if PHONE.search(text) or EMAIL.search(text):
        hits.setdefault("personal_info", []).append("phone/email said on stream")
    return hits


def moderate(text: str) -> dict[str, float]:
    key = tools.key("moderation")
    if not key or not text.strip():
        return {}
    try:
        r = httpx.post("https://api.openai.com/v1/moderations", timeout=30,
                       headers={"authorization": f"Bearer {key}"},
                       json={"model": "omni-moderation-latest", "input": text[:8000]})
        r.raise_for_status()
        res = r.json()["results"][0]
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, float] = {}
    for cat, score in (res.get("category_scores") or {}).items():
        risk = MOD_MAP.get(cat)
        if risk and score >= 0.5:
            out[risk] = max(out.get(risk, 0), round(float(score), 2))
    return out


def screen(text: str, ai_flags: list[str] | None = None) -> dict[str, str]:
    """{risk: "why"} for everything found in the clip's words."""
    found: dict[str, str] = {}
    for risk, words in scan_words(text).items():
        found[risk] = "said: " + ", ".join(words)
    for risk, score in moderate(text).items():
        found.setdefault(risk, f"moderation model {int(score * 100)}% sure")
    for f in ai_flags or []:
        if f == "music":
            f = "music_suspected"
        found.setdefault(f, "flagged by the AI while reading the transcript")
    # profanity on its own is only a note
    return found
