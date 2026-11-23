# Byditors Cut

A clipping studio that runs on your own PC. It turns long videos and live streams into vertical, captioned clips, checks each one for copyright and community-guideline problems, and posts the ones that pass to Instagram, YouTube, TikTok and Facebook.

**New here? Read the [complete setup guide](docs/SETUP_GUIDE.md)** (also in the download as `Byditors Cut - Setup Guide.pdf`): install, free AI, YouTube, Instagram, streamers, daily uploads, Telegram alerts and fixes, step by step.

Add a streamer once and it watches them for you. When they go live on Kick, Twitch, YouTube or TikTok, it records the stream and clips the big moments while the stream is still running. When they upload a new video or VOD, it downloads and clips that too.

Before anything is posted, every clip goes through the **Rights & Safety Guard**.

## What's different from PeakCut

This started from [PeakCut](https://github.com/haziqahmedqasmi/PeakCut) and adds:

| | PeakCut | Byditors Cut |
|---|---|---|
| Copyright and guideline check before posting | – | **Rights & Safety Guard**: permission ledger per streamer, local music detector, song ID, music removal, gambling/hate/sexual/doxxing screen, per-platform rules, reused-content check |
| Streamers watched automatically | Lives | Lives **and new uploads/VODs**, with the back catalogue skipped on first add |
| Vertical framing | Fixed layouts | **Smooth face following** that never slices a face, **facecam + gameplay split** (cam found wherever it sits, two cams side by side), **two-person stack**, blur-fill; any clip can be re-rendered in another layout |
| Moment detection | Loudness, chat, transcript | Loudness, **audio bursts (laughs/screams)**, **scene cuts**, chat velocity with **hype-emote weighting**, transcript AI |
| Editing | Captions, zoom | **Dead air cut** between sentences (screams and laughs kept), **cold open** on the loudest moment with a rewind, **punch-in zooms** toward the face with a shake on the biggest hit, **real CC0 sound hits** (or your own), **seamless loop** ending, **auto cover** on the biggest reaction; each one switchable |
| Two people | Stack | Stack **or active-speaker cuts** (mouth movement + voice, hard cuts like a podcast editor, caption colour per speaker) |
| "Clip that" | Twitch chat command | The streamer **saying** "clip that / clip it / clip kar" (Hindi too) or **chat spamming** "clip it", on YouTube, Twitch and Kick |
| Fixing a clip | – | **Trim editor** in the inbox: drag start and end over the original (15 s either side), change the hook and layout, render again |
| Campaigns | – | **Campaign mode**: paste a Content Rewards / Vyro / Clipster brief, the AI reads the rules; clips follow its length, tags and bans, and views turn into an **earnings estimate** with links ready to submit |
| Captions | English | Hinglish as spoken **or English** (translated), per link or per streamer; AI spell-check for Hinglish |
| Learning | – | Reads your **views, likes and comments** back from Instagram and YouTube every 6 hours: winners' streamers and hook styles post first, hook styles are **A/B tested**, **best posting time** learned per account, the AI sees your best hooks |
| Languages | Parakeet (English-first) | **faster-whisper**: 99 languages, Hindi and Hinglish included, GPU if available |
| Posting | YouTube, Instagram, TikTok | + **Facebook Reels**, a last Guard check before every post, quiet hours, per-account spacing and caps |

## Set it up with your AI assistant

Copy this into ChatGPT, Claude, Gemini, Cursor or any coding assistant that can run commands on your computer:

> Set up and run https://github.com/quasaritis/byditors-cut on my computer. Follow its AGENTS.md.

[AGENTS.md](AGENTS.md) walks the assistant through installing, running and testing it.

## Quick start

You need **Python 3.10+** and **ffmpeg** on your PATH ([Windows build](https://www.gyan.dev/ffmpeg/builds/), `brew install ffmpeg`, `sudo apt install ffmpeg`).

**Windows:** double-click `start.bat`.
**macOS / Linux:** `./start.sh`

The first run sets up a private Python environment and installs everything (a few minutes). Then the dashboard opens at **http://127.0.0.1:8777**.

By hand:

```bash
git clone https://github.com/quasaritis/byditors-cut.git
cd byditors-cut
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # macOS/Linux: cp .env.example .env
python -m bcut
```

Paste a link in the bar at the top and press **Make clips**, or press **Upload** for a file on your PC.

### Make it better (optional)

| Add | How | What you get |
|---|---|---|
| An AI key, any model | Dashboard → Guard & settings → AI: pick the service, paste the key, press **Find models** | Picks from the transcript instead of only loudness, writes hooks and per-platform captions, flags risky moments. A **free Gemini key** from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) works (no card); so do OpenAI, Groq, OpenRouter, Grok, Claude, a local Ollama model, or anything that speaks the OpenAI format. Free-tier "too many requests" answers are waited out and retried. |
| Song identification | `AUDD_API_TOKEN` in `.env` | Names copyrighted songs, so they're blocked for certain instead of "sounds like music" |
| Music removal | `pip install demucs` | **Remove music, keep voice** button on flagged clips |
| Moderation model | `OPENAI_MODERATION_KEY` in `.env` | Catches hate, sexual content and violence the word lists miss |
| NVIDIA GPU | nothing to do | faster-whisper uses it automatically (`BCUT_WHISPER_DEVICE=auto`) |

## How it works

```
link / upload / streamer goes live / streamer uploads
        │
        ▼
download (yt-dlp) ─► listen: loudness, bursts, scene cuts, chat ─► transcribe (faster-whisper)
        │
        ▼
pick moments (AI on the transcript, or the signal peaks) ─► reframe to 9:16 (face / split / blur)
        │
        ▼
render: word-by-word captions, hook, credit tag, −14 LUFS ─► RIGHTS & SAFETY GUARD
        │                                                           │
        ▼                                                           ▼
   pass + permission + auto-post on ──► post queue ──► Instagram · YouTube · TikTok · Facebook
   review / block ──► Clip inbox (you approve, fix, or reject)
```

## The Rights & Safety Guard

Each clip gets a report you can read in the inbox, and one of three verdicts:

- **Ready to post** — every check passed. Auto-posted only if you turned on auto-post for that streamer *and* have permission on record.
- **Needs a look** — waits in the inbox until you approve it.
- **Blocked** — never posted, unless you write down why it's allowed (kept in the activity log).

What it checks:

1. **Rights.** Every streamer has a rights setting: *your content*, *permission* (with proof), *clip program* (they run an open clipping program; link saved), or *no permission*. Clips from creators without permission are still made, so you can show the streamer, but are never posted automatically.
2. **Music.** A local detector finds held musical notes (speech glides, music holds), with no internet needed. With an AudD token, songs are identified by name and blocked. With demucs installed, music can be removed and the voice kept.
3. **Someone else's content on screen.** The AI flags reactions to TV, films, sports broadcasts or other creators' videos for review.
4. **Community guidelines.** Gambling and casino promotion (common on Kick, and banned or restricted on Instagram, YouTube and TikTok, and in India), hate, sexual content, personal info (phone numbers and emails said on stream), violence, dangerous acts, drugs, minors, self-harm talk and heavy swearing. Word lists in `bcut/guard/lexicon.json` (English plus Hindi/Hinglish), plus the moderation model and the AI's flags. You choose block, look, or note per risk; copyrighted songs, sexual content and personal info are always blocked.
5. **Reused content.** Re-posting someone's whole video is flagged. Every clip carries captions, a hook and a credit tag, so it's a transformed clip rather than a re-upload.
6. **Platform rules.** Length, size, caption length and hashtag limits for each platform you post to.

Read [docs/COMPLIANCE.md](docs/COMPLIANCE.md) for what it can and can't promise.

## Connecting accounts

Posting uses each platform's **official API**: no passwords, no browser automation, nothing that gets accounts flagged. Setup takes a few minutes per platform: [docs/CONNECT_ACCOUNTS.md](docs/CONNECT_ACCOUNTS.md).

### Alerts on your phone

Connect your own Telegram bot and every new clip arrives on your phone as a video with **Approve** and **Reject**
buttons, plus messages when a streamer goes live, a video posts or something fails. `/status`, `/today`, `/review` and
`/restart` work from anywhere. Two-minute setup: [docs/PHONE_ALERTS.md](docs/PHONE_ALERTS.md).

## Project map

| Path | What it is |
|---|---|
| `bcut/pipeline.py` | One video in, checked clips out |
| `bcut/radar.py` | Watches streamers: went live? new upload? |
| `bcut/live.py` | Records live streams in pieces, reads Twitch/Kick chat, clips spikes while live |
| `bcut/signals.py` | Loudness, bursts, scene cuts, chat velocity → one "excitement" curve |
| `bcut/transcribe.py` | faster-whisper with word timestamps |
| `bcut/brain.py` | Picking moments, hooks, per-platform captions |
| `bcut/ai.py` | The only code that talks to an AI service |
| `bcut/reframe.py` | Face tracking, facecam detection and the 9:16 layouts |
| `bcut/render.py` | ffmpeg render: captions placed off faces, hook, credit, loudness |
| `bcut/guard/` | The Rights & Safety Guard |
| `bcut/publish/` | Post queue and the four platform publishers |
| `bcut/server.py` | Local API + dashboard |
| `web/index.html` | The dashboard (one file, no build step) |
| `tests/` | `python -m pytest -q`: no keys or internet needed |

Everything you make stays in `data/` on your PC. `.env`, `data/` and tokens are ignored by git.

## Website or local?

Local, on purpose. Recording several streams at once, transcribing and rendering need a real CPU or GPU and lots of disk. On your PC that costs nothing; as a website it means paying for GPU servers around the clock. The dashboard is already a web page (at `127.0.0.1`), so it can later be put on a server for a team or sold through byditorsstudio.shop. It would need sign-in added first.

## Security

- It listens on this computer only and refuses requests from other websites.
- Tokens are stored in `data/bcut.db` on your PC. Keep that folder private.
- Text from videos and chat is treated as data when shown to the AI, never as instructions.

## Using it responsibly

Clip your own content or content you have permission to use. Many streamers run clipping programs and say yes when asked. You're responsible for the rights to what you post and for each platform's terms. The Guard lowers the risk; it doesn't make you immune to a claim.

## Licence

MIT. Bundled fonts (Anton, Archivo Black, Poppins, Inter) are under the SIL Open Font License; the YuNet face model is MIT. Licence texts are next to the files.
