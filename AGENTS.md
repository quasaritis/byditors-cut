# Setting up and working on Byditors Cut (for AI coding assistants)

If someone pasted this repo's link and asked you to "use this repo", they almost always want it **installed and running on their computer**. Do this:

## 1. Install and run

1. Check for **Python 3.10+** (`python --version` or `python3 --version`) and **ffmpeg** (`ffmpeg -version`). If ffmpeg is missing, install it: Windows `winget install Gyan.FFmpeg`, macOS `brew install ffmpeg`, Ubuntu/Debian `sudo apt install ffmpeg`.
2. Clone: `git clone https://github.com/quasaritis/byditors-cut.git && cd byditors-cut`
3. Start:
   - Windows: `start.bat`
   - macOS / Linux: `./start.sh`

   The first run creates `.venv`, installs `requirements.txt` (includes faster-whisper, a few hundred MB) and copies `.env.example` to `.env`.
4. The dashboard opens at **http://127.0.0.1:8777**. Paste a video link in the top bar and press **Make clips** to check it works.

Manual install instead of the scripts:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
python -m bcut                     # add --no-browser to skip opening a tab
```

## 2. Optional setup the person may want

- **AI for picking clips (a free Gemini key works).** Dashboard → Guard & settings → AI: choose Google Gemini, paste a key from https://aistudio.google.com/apikey, press **Find models**, then **Save and test**. Or set `BCUT_AI_PROVIDER`, `BCUT_AI_KEY` and `BCUT_AI_MODEL` in `.env`.
- **Posting to Instagram, YouTube, TikTok and Facebook:** follow `docs/CONNECT_ACCOUNTS.md`.
- **Phone alerts (Telegram):** follow `docs/PHONE_ALERTS.md`.
- **Music removal:** `pip install demucs`. **Song ID:** `AUDD_API_TOKEN` in `.env`.
- **NVIDIA GPU:** nothing to do; speech to text uses it automatically.

Never commit `.env` or anything in `data/` (keys, tokens and videos live there).

## 3. Check it works

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The tests need ffmpeg but no keys or internet. They take about a minute.

## 4. Where things are

| Path | What it does |
|---|---|
| `bcut/__main__.py` | Entry point (`python -m bcut`) |
| `bcut/config.py` | Every setting, read from `.env` |
| `bcut/pipeline.py` | Video → signals → transcript → picks → render → Guard → inbox or post queue |
| `bcut/radar.py`, `bcut/live.py` | Watching streamers; recording and clipping lives |
| `bcut/brain.py`, `bcut/ai.py` | Clip picking and captions; the only code that calls an AI service |
| `bcut/reframe.py`, `bcut/render.py` | 9:16 layouts (face, split, blur) and the ffmpeg render |
| `bcut/guard/` | Rights & Safety Guard: rights, music, guideline lexicon, platform rules |
| `bcut/publish/` | Post queue and the Instagram, YouTube, TikTok and Facebook publishers |
| `bcut/telegram.py` | Phone alerts and remote control through the person's own Telegram bot |
| `bcut/server.py`, `web/index.html` | Local API and the dashboard (plain HTML/CSS/JS, no build step) |

## 5. Rules when changing code

- Keep the Guard in front of posting: `publish.allowed()` must stay the last check before any upload. Don't add a path that posts a clip whose verdict is `review` or `block` without the person's approval.
- Only `bcut/ai.py` talks to AI services. Text from videos and chat goes into prompts through `ai.safe()`.
- The server listens on 127.0.0.1 and refuses cross-site requests. Keep it that way unless adding real sign-in.
- Run `python -m pytest -q` before and after changes.
