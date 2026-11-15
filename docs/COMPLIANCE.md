# What the Guard can and can't promise

The Guard exists so that clips which would get you a copyright strike or a takedown don't go out on autopilot. It's a filter, not a lawyer. Here is exactly what it does.

## The rule that matters most: permission

Clipping another creator's stream is only safe when you have permission. In practice that means one of:

- **Their clipping program.** Many Kick, Twitch and YouTube streamers pay or allow clippers, with rules such as "credit me, link the stream, no gambling segments". Save the link to those rules on the streamer (Rights → *Clip program*). The credit line you set appears on every clip and in every caption.
- **Direct permission.** An email or DM saying yes. Save a link or a note (Rights → *Permission*).
- **A licence.** Videos published under Creative Commons Attribution are detected from their metadata and allowed with credit.
- **Your own content.**

Without one of these, clips are made and held in the inbox. They are **never** auto-posted. You can still approve them by hand, which is a decision you make, not the app.

"Fair use" or "fair dealing" (India's equivalent) is decided case by case. The app doesn't try to judge it.

## Music

Commercial music is the commonest reason clips get muted, blocked or struck. Streamers often have it playing in the background.

| Layer | Needs | Catches |
|---|---|---|
| Local music detector | nothing | Held musical notes and chords. Speech glides between pitches and doesn't trigger it. Gameplay with a melodic soundtrack can. |
| Song identification (AudD) | `AUDD_API_TOKEN` | Names the song. A match blocks the clip. |
| Music removal (demucs) | `pip install demucs` | Separates voice from music. The clip keeps the talking. |
| Mute fallback | nothing | Without demucs, the music-only stretches are silenced. |

Limits: identification needs a few clean seconds of the song. Heavily talked-over music can slip through. Platforms run their own Content ID after upload. If you post a lot from one streamer who always has music on, install demucs and get an AudD token.

## Other people's content on screen

Reaction streams (TV, films, sports, other YouTubers) are the second big risk, because the footage on screen belongs to someone else even when the streamer is fine with you clipping. The AI flags these from the transcript ("watching_media") and they wait for you. Without an AI service this isn't detected, so keep an eye on reaction-heavy streamers.

## Community guidelines

| Risk | Default | Why |
|---|---|---|
| Gambling / casino / betting promos | Block | Instagram, YouTube and TikTok restrict gambling promotion. India's Promotion and Regulation of Online Gaming Act, 2025 bans real-money gaming promotion. Very common on Kick. |
| Sexual content | Always blocked | Removed on every platform |
| Personal info (phone, email, address) | Always blocked | Doxxing rules. Cut or bleep it. |
| Hate / slurs | Block | Add the terms your audience uses to `bcut/guard/lexicon.json` → `hate` |
| Violence, threats, dangerous acts, drugs, minors, harassment, self-harm talk | Look at it | Context decides. A joke and a threat look the same in a word list. |
| Heavy swearing | Note | Allowed, but it limits ads and reach |

You can make these stricter or looser in **Guard & settings**, except the three that are always blocked.

## Reused content

Instagram and YouTube push down (and can demonetise) re-uploads of other people's videos. Every clip gets word-by-word captions, a hook and a credit tag. A clip that's basically the whole original video is flagged.

## Platform rules

Before posting, each clip is checked against the limits of the platforms you post to (length, file size, caption length, hashtag count). The table lives in `bcut/guard/rules.py`. Platforms change limits; when one refuses a post, its own error shows in the post queue.

## One last check

Right before a post is sent, the queue checks the clip again: it must have passed, or you approved it after review, or you overrode a block with a written reason. A clip you rejected after scheduling is never sent.
