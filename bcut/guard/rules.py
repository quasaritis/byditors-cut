"""What each platform accepts, and how each risk is treated there.

These are working limits for publishing through each platform's official API. Platforms change them; when a post
is refused, the platform's own error is shown in the queue. Keep this table in step with their docs.
"""
from __future__ import annotations

PLATFORMS = {
    "instagram": {
        "label": "Instagram Reels",
        "min_sec": 3, "max_sec": 900, "max_mb": 300,
        "caption_max": 2200, "hashtags_max": 30, "mentions_max": 20,
        "docs": "https://developers.facebook.com/docs/instagram-platform/content-publishing",
    },
    "youtube": {
        "label": "YouTube Shorts",
        "min_sec": 1, "max_sec": 180, "max_mb": 2048,
        "title_max": 100, "description_max": 5000, "hashtags_max": 15,
        "docs": "https://developers.google.com/youtube/v3/docs/videos/insert",
    },
    "tiktok": {
        "label": "TikTok",
        "min_sec": 3, "max_sec": 600, "max_mb": 4096,
        "caption_max": 2200, "hashtags_max": 30,
        "docs": "https://developers.tiktok.com/doc/content-posting-api-reference-direct-post",
    },
    "facebook": {
        "label": "Facebook Reels",
        "min_sec": 3, "max_sec": 90, "max_mb": 1024,
        "caption_max": 2200, "hashtags_max": 30,
        "docs": "https://developers.facebook.com/docs/video-api/guides/reels-publishing",
    },
}

# How a risk found inside a clip is treated. "block" = never posted (unless you override with a reason),
# "review" = waits in the inbox for you, "warn" = noted, still allowed.
RISK_POLICY = {
    "copyright_music": {"label": "Copyrighted music", "default": "block"},
    "music_suspected": {"label": "Background music", "default": "review"},
    "watching_media": {"label": "Reacting to someone else's video / TV / sports", "default": "review"},
    "gambling": {"label": "Gambling / casino / betting", "default": "block"},
    "hate": {"label": "Hate or slurs", "default": "block"},
    "sexual": {"label": "Sexual content", "default": "block"},
    "personal_info": {"label": "Personal info (phone, address, email)", "default": "block"},
    "violence": {"label": "Violence or threats", "default": "review"},
    "dangerous_act": {"label": "Dangerous acts / challenges", "default": "review"},
    "drugs": {"label": "Drugs", "default": "review"},
    "minor": {"label": "May involve a minor", "default": "review"},
    "self_harm": {"label": "Self-harm talk", "default": "review"},
    "harassment": {"label": "Harassment / bullying", "default": "review"},
    "profanity": {"label": "Heavy swearing", "default": "warn"},
}

# Platform-specific tightening on top of the defaults.
PLATFORM_OVERRIDES = {
    "youtube": {"profanity": "warn"},          # strong swearing early in a Short limits ads, does not remove it
    "facebook": {"violence": "block"},
}


def action(risk: str, platform: str | None = None, user_policy: dict | None = None) -> str:
    base = (user_policy or {}).get(risk) or RISK_POLICY.get(risk, {}).get("default", "review")
    if platform:
        over = PLATFORM_OVERRIDES.get(platform, {}).get(risk)
        if over and _rank(over) > _rank(base):
            base = over
    return base


def _rank(a: str) -> int:
    return {"ok": 0, "warn": 1, "review": 2, "block": 3}.get(a, 2)


def worst(actions: list[str]) -> str:
    return max(actions, key=_rank) if actions else "ok"
