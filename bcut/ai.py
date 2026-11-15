"""The only code that talks to an AI service. Any OpenAI-compatible service works, plus Claude natively.

Settings saved in the dashboard (stored in the local database) win over `.env`.
"""
from __future__ import annotations

import json
import re

import httpx

from . import config, db

PROVIDERS = {
    #  name: (label, base url, example model)
    "openai": ("OpenAI", "https://api.openai.com/v1", "gpt-4.1-mini"),
    "gemini": ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "google/gemini-2.5-flash"),
    "xai": ("Grok (xAI)", "https://api.x.ai/v1", "grok-3-mini"),
    "anthropic": ("Claude", "https://api.anthropic.com/v1", "claude-haiku-4-5"),
    "ollama": ("Ollama (on this PC)", "http://127.0.0.1:11434/v1", "qwen2.5:7b"),
    "custom": ("Custom (OpenAI format)", "", ""),
}


def settings() -> dict:
    saved = db.kv_get("ai", {}) or {}
    return {
        "provider": saved.get("provider") or config.AI_PROVIDER,
        "model": saved.get("model") or config.AI_MODEL,
        "key": saved.get("key") or config.AI_KEY,
        "base_url": saved.get("base_url") or config.AI_BASE_URL,
    }


def enabled() -> bool:
    s = settings()
    return bool(s["provider"] and (s["key"] or s["provider"] == "ollama"))


def public_settings() -> dict:
    s = settings()
    return {"provider": s["provider"], "model": s["model"], "base_url": s["base_url"],
            "has_key": bool(s["key"]), "providers": {k: v[0] for k, v in PROVIDERS.items()}}


def _json_from(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=0)
    return json.loads(text[start:])


def ask_json(system: str, user: str, max_tokens: int = 2500, temperature: float = 0.4):
    """Ask for a JSON answer. Raises on failure so the caller can fall back to the no-AI path."""
    s = settings()
    prov = s["provider"]
    if not prov:
        raise RuntimeError("No AI service set up")
    model = s["model"] or PROVIDERS.get(prov, ("", "", ""))[2]
    if prov == "anthropic":
        r = httpx.post(
            f"{PROVIDERS['anthropic'][1]}/messages", timeout=120,
            headers={"x-api-key": s["key"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": max_tokens, "temperature": temperature, "system": system,
                  "messages": [{"role": "user", "content": user + "\n\nAnswer with JSON only."}]},
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        return _json_from(text)
    base = s["base_url"] or PROVIDERS.get(prov, ("", "", ""))[1]
    if not base:
        raise RuntimeError("Set a base URL for the custom AI service")
    headers = {"content-type": "application/json"}
    if s["key"]:
        headers["authorization"] = f"Bearer {s['key']}"
    body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if prov in ("openai", "groq", "openrouter", "xai", "gemini"):
        body["response_format"] = {"type": "json_object"}
    r = httpx.post(f"{base.rstrip('/')}/chat/completions", headers=headers, json=body, timeout=120)
    r.raise_for_status()
    return _json_from(r.json()["choices"][0]["message"]["content"])


def safe(text: str, limit: int = 400) -> str:
    """Text that came from a video or a stranger: shown to the AI as data, never as instructions."""
    text = re.sub(r"[\x00-\x1f]+", " ", str(text or ""))
    return text[:limit].replace("<", "‹").replace(">", "›")
