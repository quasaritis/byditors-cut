"""Live clipping: record a stream in short pieces, watch sound + chat for spikes, and cut clips while the stream is
still running. Only the last `BCUT_LIVE_BUFFER_MIN` minutes are kept on disk.

Chat is read anonymously from Twitch (IRC) and Kick (their public websocket) when possible; clipping still works
without it.
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from . import brain, config, db, media, pipeline, signals, transcribe

_sessions: dict[str, "Session"] = {}
_lock = threading.Lock()
_backoff: dict[str, float] = {}      # streamer id -> don't auto-start again before this time
BACKOFF_SEC = 15 * 60
KICK_PUSHER_KEY = "32cbd69e4b950bf97679"


def active() -> list["Session"]:
    with _lock:
        return [s for s in _sessions.values() if s.running]


def backing_off(streamer_id: str) -> bool:
    """True for a while after a live recording couldn't open the stream, so the radar doesn't retry every check."""
    return _backoff.get(streamer_id, 0) > time.time()


def stream_page(url: str) -> str:
    """A channel page (youtube.com/@name, tiktok.com/@name) isn't a stream; its /live page is."""
    if media.platform_of(url) in ("youtube", "tiktok") and not re.search(
            r"/(watch|live)\b|youtu\.be/|/shorts/|/video/", url):
        return url.rstrip("/") + "/live"
    return url


def active_for(streamer_id: str) -> "Session | None":
    return next((s for s in active() if s.streamer and s.streamer["id"] == streamer_id), None)


def get(jid: str) -> "Session | None":
    with _lock:
        return _sessions.get(jid)


LIVE_FORMAT = "best[height<=1080]/bv*[height<=1080]+ba/bv*+ba/best*/bv*"


def resolve(url: str) -> tuple[list[str], dict, dict]:
    """The stream addresses (one muxed HLS, or separate video + audio), the headers they need, and the stream's info."""
    if Path(url).exists() or url.startswith("file://"):
        p = url.replace("file://", "")
        return [p], {}, {"title": Path(p).stem, "is_live": True}
    import yt_dlp

    with yt_dlp.YoutubeDL(media.ytdlp_opts(quiet=True, no_warnings=True, format=LIVE_FORMAT, socket_timeout=20,
                                           noplaylist=True)) as y:
        info = y.extract_info(stream_page(url), download=False)
    if info.get("_type") == "playlist":
        raise RuntimeError("That link is a channel page, not a live stream")
    if not info.get("is_live", True) and info.get("live_status") not in (None, "is_live"):
        raise RuntimeError("Not live right now")
    parts = info.get("requested_formats") or [info]
    srcs = [f["url"] for f in parts if f.get("url")]
    if not srcs:
        raise RuntimeError("The stream gave no playable address")
    return srcs, (parts[0].get("http_headers") or info.get("http_headers") or {}), info


# "clip that", "clip it", "clip this", "that's a clip", "clip kar/karo/kardo/kr", "clip bana", "!clip"
CLIP_CALL = re.compile(r"\b(clip (?:that|it|this|dat|kar\w*|kr\w*|bana\w*|le\w*)|that'?s a clip)\b|क्लिप (?:कर|बना|ले)")
CHAT_CALL = re.compile(r"(?i)(^|\s)(!clip\b|clip (?:it|that|this|dat|kar\w*|kr\w*)|clip\s*$|clippp*\b)")


def _yt_chat_text(line: bytes) -> str:
    """One line of yt-dlp's live_chat.json → the message text ("" for joins, polls and the like)."""
    try:
        d = json.loads(line)
        for act in d.get("replayChatItemAction", {}).get("actions", []):
            item = act.get("addChatItemAction", {}).get("item", {})
            r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer") or {}
            runs = (r.get("message") or {}).get("runs") or []
            text = "".join(x.get("text") or (x.get("emoji") or {}).get("shortcuts", [""])[0] for x in runs)
            if text:
                return text
    except (ValueError, AttributeError, IndexError, TypeError):
        pass
    return ""


class Session:
    def __init__(self, streamer: dict | None, url: str):
        self.streamer = streamer
        self.url = url
        self.job = db.insert("jobs", {
            "id": db.new_id("l"), "kind": "live", "url": url, "title": f"{(streamer or {}).get('name', 'Live')} — live",
            "streamer_id": (streamer or {}).get("id"), "status": "recording", "step": "Connecting",
            "meta": {"started": time.time()}, "created": time.time(), "updated": time.time()})
        self.dir = config.DATA / "live" / self.job["id"]
        self.dir.mkdir(parents=True, exist_ok=True)
        self.running = True
        self.started = time.time()
        self.segs: list[dict] = []           # {"file", "t0", "t1"} in seconds since start
        self.db: list[float] = []
        self.flux: list[float] = []
        self.chat: list[tuple[float, str]] = []
        self.last_clip_at = -1e9
        self.pending: list[float] = []
        self.calls: list[tuple[float, str]] = []    # "clip that" moments: (time, who called it)
        self.last_call_at = -1e9
        self.chat_seen = 0
        self.clips = 0
        self.title = ""
        self.proc: subprocess.Popen | None = None
        self.local = Path(url).exists() or url.startswith("file://")
        self.workers: list[threading.Thread] = []
        self.last_error = ""
        self.got_video = False

    # ------------------------------------------------------------ recording
    def start(self):
        threading.Thread(target=self._record, daemon=True, name=f"rec-{self.job['id']}").start()
        threading.Thread(target=self._analyse, daemon=True, name=f"ana-{self.job['id']}").start()
        threading.Thread(target=self._chat, daemon=True, name=f"chat-{self.job['id']}").start()
        if config.LIVE_VOICE_TRIGGER and transcribe.available():
            threading.Thread(target=self._listen, daemon=True, name=f"ear-{self.job['id']}").start()
        return self

    # ------------------------------------------------------------ "clip that"
    def call(self, t: float, who: str):
        """Someone asked for a clip at time t (the streamer said "clip that", or chat spammed "clip it")."""
        if t - self.last_call_at < 30:
            return
        self.last_call_at = t
        self.calls.append((t, who))
        db.log(f"{(self.streamer or {}).get('name', 'Stream')}: {who} at {int(t // 60)}:{int(t % 60):02d}, clipping it",
               streamer_id=(self.streamer or {}).get("id"))

    def _listen(self):
        """Listen to the live as it records and catch the streamer saying "clip that" / "clip kar"."""
        done: set[str] = set()
        while self.running:
            todo = [s for s in self.segs if s["file"] not in done and s["t1"] <= self.now() + 1
                    and (self.dir / s["file"]).exists()]
            if len(todo) > 8:                       # fell behind: skip ahead, a late call is no use
                for s in todo[:-3]:
                    done.add(s["file"])
                todo = todo[-3:]
            for s in todo:
                done.add(s["file"])
                try:
                    a = media.read_audio(self.dir / s["file"])
                    if len(a) < media.SR or float(np.sqrt((a ** 2).mean())) < 0.01:
                        continue
                    tr = transcribe.transcribe(self.dir / s["file"], language=(self.streamer or {}).get("language", ""))
                except Exception:  # noqa: BLE001
                    continue
                text = " ".join(w["w"] for w in tr.get("words") or []).lower()
                if CLIP_CALL.search(text):
                    hit = next((w for w in tr["words"] if w["w"].lower().strip(".,!?") in ("clip", "clipped")),
                               None)
                    self.call(s["t0"] + (hit["s"] if hit else 0.0), "said “clip that”")
            time.sleep(2)

    def _chat_calls(self):
        """Chat spamming "clip it" means something just happened."""
        new = self.chat[self.chat_seen:]
        self.chat_seen = len(self.chat)
        if not new:
            return
        now = new[-1][0]
        recent = [(t, m) for t, m in self.chat[-400:] if now - t <= 12]
        asks = [t for t, m in recent if CHAT_CALL.search(m)]
        rate = len([1 for t, _ in self.chat[-2000:] if now - t <= 120]) / 120 * 12    # messages per 12 s
        if len(asks) >= max(4, 0.25 * rate):
            self.call(max(0.0, min(asks) - 4), "chat yelled “clip it”")

    def now(self) -> float:
        return time.time() - self.started

    def _record(self):
        run, misses = 0, 0
        while self.running:
            try:
                srcs, headers, info = resolve(self.url)
                self.title = info.get("title") or self.title
                misses = 0
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e).splitlines()[0][:200]
                misses += 1
                if misses >= 3:
                    break
                time.sleep(20)
                continue
            offset = self.now()
            cmd = [media.need("ffmpeg"), "-v", "error", "-y"]
            for src in srcs:
                if self.local:
                    cmd += ["-re"]
                else:
                    if headers:
                        cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
                    cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
                cmd += ["-i", src]
            audio_in = len(srcs) - 1
            cmd += ["-map", "0:v:0?", "-map", f"{audio_in}:a:0?", "-c", "copy", "-f", "segment",
                    "-segment_time", str(config.LIVE_SEGMENT_SEC), "-reset_timestamps", "1",
                    "-segment_list", f"r{run}.csv", "-segment_list_type", "csv", f"r{run}_%06d.ts"]
            db.update("jobs", self.job["id"], step="Recording" + (f" — {self.title[:60]}" if self.title else ""))
            self.proc = subprocess.Popen(cmd, cwd=self.dir, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            while self.proc.poll() is None and self.running:
                self._collect(run, offset)
                time.sleep(2)
            self._collect(run, offset)
            run += 1
            if self.local or not self.running:
                break
            time.sleep(5)  # stream hiccup: try again; three failed resolves in a row = stream over
        self.running = False
        self._finish()

    def _collect(self, run: int, offset: float):
        lst = self.dir / f"r{run}.csv"
        if not lst.exists():
            return
        known = {s["file"] for s in self.segs}
        with lst.open() as f:
            for row in csv.reader(f):
                if len(row) >= 3 and row[0] not in known:
                    self.got_video = True
                    self.segs.append({"file": row[0], "t0": offset + float(row[1]), "t1": offset + float(row[2]),
                                      "read": False})

    # ------------------------------------------------------------ chat
    def _youtube_chat(self):
        """YouTube live chat, read the way the page does (yt-dlp follows it as it scrolls); no API key and none
        of your YouTube upload quota is used."""
        import subprocess
        import sys

        out = self.dir / "chat"
        while self.running:
            proc = subprocess.Popen(
                [sys.executable, "-m", "yt_dlp", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                 "--no-warnings", "-q", "-o", str(out), self.url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000000 if sys.platform == "win32" else 0)
            pos, seen = 0, None
            try:
                while self.running and proc.poll() is None:
                    time.sleep(2)
                    f = seen or next(iter(sorted(self.dir.glob("chat.live_chat.json*"))), None)
                    if not f or not f.exists():
                        continue
                    seen = f
                    with open(f, "rb") as fh:
                        fh.seek(pos)
                        data = fh.read()
                    cut = data.rfind(b"\n")
                    if cut < 0:
                        continue
                    pos += cut + 1
                    for line in data[:cut].splitlines():
                        text = _yt_chat_text(line)
                        if text:
                            self.chat.append((self.now(), text[:200]))
            finally:
                if proc.poll() is None:
                    proc.kill()
            if self.running:
                time.sleep(30)      # the stream may still be starting; try again

    def _chat(self):
        plat = media.platform_of(self.url)
        if plat == "youtube":
            try:
                self._youtube_chat()
            except Exception:  # noqa: BLE001
                pass
            return
        name = (re.findall(r"(?:twitch\.tv|kick\.com)/([^/?#]+)", self.url) or [""])[0].lower()
        if not name:
            return
        try:
            from websockets.sync.client import connect
        except ImportError:
            return
        while self.running:
            try:
                if plat == "twitch":
                    with connect("wss://irc-ws.chat.twitch.tv:443", open_timeout=15) as ws:
                        for line in ("PASS SCHMOOPIIE", "NICK justinfan" + str(int(time.time()) % 99999),
                                     f"JOIN #{name}"):
                            ws.send(line)
                        while self.running:
                            for msg in str(ws.recv(timeout=60)).split("\r\n"):
                                if msg.startswith("PING"):
                                    ws.send("PONG :tmi.twitch.tv")
                                elif " PRIVMSG " in msg:
                                    self.chat.append((self.now(), msg.split(" :", 1)[-1][:200]))
                elif plat == "kick":
                    import httpx

                    r = httpx.get(f"https://kick.com/api/v2/channels/{name}", timeout=15,
                                  headers={"accept": "application/json", "user-agent": "Mozilla/5.0"})
                    room = r.json()["chatroom"]["id"]
                    url = (f"wss://ws-us2.pusher.com/app/{KICK_PUSHER_KEY}?protocol=7&client=js&version=8.4.0"
                           "&flash=false")
                    with connect(url, open_timeout=15) as ws:
                        ws.send(json.dumps({"event": "pusher:subscribe",
                                            "data": {"auth": "", "channel": f"chatrooms.{room}.v2"}}))
                        while self.running:
                            m = json.loads(ws.recv(timeout=90))
                            if m.get("event") == "pusher:ping":
                                ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                            elif "ChatMessage" in m.get("event", ""):
                                content = json.loads(m.get("data") or "{}").get("content", "")
                                self.chat.append((self.now(), str(content)[:200]))
                else:
                    return
            except Exception:  # noqa: BLE001
                time.sleep(30)  # chat is a bonus; keep trying quietly
            if len(self.chat) > 50000:
                self.chat = self.chat[-20000:]

    # ------------------------------------------------------------ spotting moments
    def _analyse(self):
        last_step = 0
        while self.running or any(not s["read"] for s in self.segs):
            for s in list(self.segs):
                if s["read"] or s["t1"] > self.now() + 1:
                    continue
                a = media.read_audio(self.dir / s["file"])
                s["read"] = True
                i0 = int(s["t0"] / signals.HOP)
                d, fl = signals.loudness(a), signals._flux(a)
                need = i0 + len(d)
                if need > len(self.db):
                    pad = need - len(self.db)
                    self.db += [float(np.median(self.db[-60:])) if self.db else -60.0] * pad
                    self.flux += [0.0] * pad
                self.db[i0:i0 + len(d)] = d.tolist()
                self.flux[i0:i0 + len(fl)] = fl.tolist()
            recorded = max((s["t1"] for s in self.segs), default=0)
            n = len(self.db)
            if n > 120:
                tail = slice(max(0, n - 1200), n)
                dbt = np.array(self.db[tail])
                sig = signals._mix(dbt, signals.zscore(dbt), signals.zscore(np.log1p(np.array(self.flux[tail]))),
                                   np.zeros(len(dbt)),
                                   signals.zscore(signals.chat_velocity(
                                       [(t - tail.start * signals.HOP, m) for t, m in self.chat
                                        if t >= tail.start * signals.HOP], len(dbt))) if self.chat else np.zeros(len(dbt)))
                base = tail.start
                for i in range(max(last_step, base + 60), n):
                    k = i - base
                    t = i * signals.HOP
                    strong = sig["loud_z"][k] > 2.2 or sig["chat_z"][k] > 2.5 or sig["burst_z"][k] > 2.8
                    if sig["excite"][k] >= 0.95 and strong and t - self.last_clip_at > config.LIVE_CLIP_COOLDOWN_SEC:
                        self.pending.append(t)
                        self.last_clip_at = t
                last_step = n
            self._chat_calls()
            for t, who in list(self.calls):
                if recorded >= t + 5:
                    self.calls.remove((t, who))
                    self.last_clip_at = max(self.last_clip_at, t)
                    self._cut(max(0.0, t - 60), t + 5, auto=True, focus=max(0.0, t - 8), why=f"You asked: {who}")
            for t in list(self.pending):
                if recorded >= t + 22:
                    self.pending.remove(t)
                    self._cut(max(0.0, t - 45), t + 22, auto=True, focus=t)
            self._trim()
            db.update("jobs", self.job["id"], progress=min(0.99, recorded / 3600),
                      meta={**self.job["meta"], "recorded": round(recorded), "clips": self.clips,
                            "chat_msgs": len(self.chat), "title": self.title})
            time.sleep(4)

    def _window(self, a: float, b: float) -> Path | None:
        parts = [s for s in self.segs if s["t1"] > a and s["t0"] < b and (self.dir / s["file"]).exists()]
        if not parts:
            return None
        lst = self.dir / f"w_{int(a)}.txt"
        lst.write_text("".join(f"file '{p['file']}'\n" for p in parts))
        out = self.dir / f"w_{int(a)}_{int(b)}.mp4"
        subprocess.run([media.need("ffmpeg"), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst.name,
                        "-c", "copy", "-bsf:a", "aac_adtstoasc", out.name], cwd=self.dir, capture_output=True)
        if not out.exists():
            subprocess.run([media.need("ffmpeg"), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst.name,
                            "-c", "copy", out.name], cwd=self.dir, capture_output=True)
        lst.unlink(missing_ok=True)
        return out if out.exists() else None

    def _cut(self, a: float, b: float, auto: bool, exact: bool = False, focus: float | None = None, why: str = ""):
        def work():
            try:
                win = self._window(a, b)
                if not win:
                    return
                pr = media.probe(win)
                tr = transcribe.transcribe(win, language=(self.streamer or {}).get("language", ""))
                cuts = media.scene_cuts(win)
                chat = [(t - a, m) for t, m in self.chat if a <= t <= b]
                info = {"title": self.title, "webpage_url": self.url, "uploader": (self.streamer or {}).get("name", ""),
                        "duration": None}
                if exact:
                    pick = {"start": 0.0, "end": round(pr["duration"], 2), "score": 70, "hook": "",
                            "title": f"{(self.streamer or {}).get('name', 'Live')} moment", "why": "You pressed Clip it",
                            "flags": []}
                else:
                    sig = signals.combined(media.read_audio(win), cuts, chat)
                    picks = brain.find_clips(tr["words"], sig, pr["duration"], self.title,
                                             (self.streamer or {}).get("name", ""), n=1,
                                             min_len=config.CLIP_MIN, max_len=min(config.CLIP_MAX, 50),
                                             focus=None if focus is None else focus - a)
                    if not picks:
                        return
                    pick = picks[0]
                    pick["why"] = ((why + ". ") if why else "Live spike: ") + pick.get("why", "")
                    pick["why"] = pick["why"].strip()
                pipeline.make_clip(self.job, win, pr, info, pick, tr["words"], self.streamer, cuts)
                self.clips += 1
                at = focus if focus is not None else a
                db.log(f"Live clip from {(self.streamer or {}).get('name', 'stream')} at "
                       f"{int(at // 60)}:{int(at % 60):02d} into the stream", streamer_id=(self.streamer or {}).get("id"))
            except Exception as e:  # noqa: BLE001
                db.log(f"Live clip failed: {str(e)[:200]}", "error", (self.streamer or {}).get("id"))
        th = threading.Thread(target=work, daemon=True)
        self.workers.append(th)
        th.start()

    def clip_last(self, seconds: float = 45):
        """The dashboard's "Clip it" button: the last N seconds, exactly."""
        end = max((s["t1"] for s in self.segs), default=0)
        self._cut(max(0.0, end - seconds), end, auto=False, exact=True)

    def _trim(self):
        keep_from = self.now() - config.LIVE_BUFFER_MIN * 60
        busy = min(self.pending + [t for t, _ in self.calls], default=1e12) - 70
        for s in list(self.segs):
            if s["t1"] < min(keep_from, busy):
                (self.dir / s["file"]).unlink(missing_ok=True)
                self.segs.remove(s)
        for w in self.dir.glob("w_*.mp4"):
            # kept a day so a clip can be rendered again in another layout from the Inbox
            if time.time() - w.stat().st_mtime > 86400:
                w.unlink(missing_ok=True)

    def stop(self):
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _finish(self):
        time.sleep(6)  # let the analyser read the last pieces
        for th in list(self.workers):
            th.join(timeout=900)
        if not self.got_video and not self.clips and not self.local:
            sid = (self.streamer or {}).get("id")
            if sid:
                _backoff[sid] = time.time() + BACKOFF_SEC
            why = self.last_error or "the stream gave no video"
            db.update("jobs", self.job["id"], status="failed", step="Couldn't record", error=why, progress=1.0)
            db.log(f"Couldn't record {(self.streamer or {}).get('name', 'the stream')}: {why}. "
                   f"Trying again in {BACKOFF_SEC // 60} min.", "warn", sid)
            return
        db.update("jobs", self.job["id"], status="stopped", step=f"Stream ended — {self.clips} clips",
                  progress=1.0)


def start(streamer: dict | None, url: str) -> Session:
    if len(active()) >= config.LIVE_MAX_PARALLEL:
        raise RuntimeError(f"Already recording {config.LIVE_MAX_PARALLEL} streams (BCUT_LIVE_MAX_PARALLEL)")
    s = Session(streamer, url)
    with _lock:
        _sessions[s.job["id"]] = s
    return s.start()


def recover() -> None:
    """After a restart, recordings that were running are closed; the radar starts them again if still live."""
    for j in db.query("SELECT id FROM jobs WHERE kind='live' AND status='recording'"):
        db.update("jobs", j["id"], status="stopped", step="Stopped (app restarted)")
