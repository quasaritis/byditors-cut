"""Run with:  python -m pytest -q
Needs ffmpeg. No keys, no internet: everything runs on generated audio/video in a throwaway data folder."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

TMP = Path(tempfile.mkdtemp(prefix="bcut-test-"))
os.environ["BCUT_DATA"] = str(TMP / "data")
os.environ["BCUT_NO_BACKGROUND"] = "1"
os.environ["BCUT_AI_PROVIDER"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bcut import db, guard, pipeline, publish, signals  # noqa: E402
from bcut.guard import content, music, rules  # noqa: E402
from bcut.media import SR, probe  # noqa: E402


def tone(freqs, secs, amp=0.2):
    t = np.arange(int(SR * secs)) / SR
    return (sum(np.sin(2 * np.pi * f * t) for f in freqs) * amp / len(freqs)).astype(np.float32)


@pytest.fixture(scope="session")
def video():
    """70 s test video: noise, a loud burst at 30-34 s, a chord (music) at 45-60 s."""
    out = TMP / "src.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=70",
        "-f", "lavfi", "-i", "anoisesrc=d=70:c=pink:a=0.05",
        "-f", "lavfi", "-i", "sine=f=262:d=70", "-f", "lavfi", "-i", "sine=f=330:d=70",
        "-filter_complex",
        "[1:a]volume='if(between(t,30,34),8,1)':eval=frame,apulsator=hz=4[sp];"
        "[2:a][3:a]amix=inputs=2,volume='if(between(t,45,60),0.7,0)':eval=frame[mu];[sp][mu]amix=inputs=2:normalize=0[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out),
    ], check=True)
    return out


# ---------------------------------------------------------------- signals + music

def test_peaks_find_the_burst():
    a = (np.random.default_rng(1).standard_normal(SR * 120) * 0.03).astype(np.float32)
    a[SR * 70: SR * 74] *= 10
    sig = signals.combined(a)
    assert any(68 <= p <= 76 for p in signals.peaks(sig["excite"], 2))


def test_music_detector_tells_chords_from_noise():
    noise = (np.random.default_rng(2).standard_normal(SR * 8) * 0.05).astype(np.float32)
    chord = tone([262, 330, 392], 8) + noise * 0.3
    assert music.music_curve(noise).mean() < 0.2
    assert music.music_curve(chord).mean() > 0.5


# ---------------------------------------------------------------- guard

def test_lexicon_catches_gambling_and_personal_info():
    hits = content.scan_words("use my code on Stake for a deposit bonus, call me at 98765 43210")
    assert "gambling" in hits and "personal_info" in hits


def test_rights_unknown_never_auto_posts():
    clip = {"start": 0, "end": 30, "hook": "x", "captions": {}}
    rep = guard.review(clip, {"name": "X", "rights": "unknown"}, {"duration": 600}, "hello there", ["instagram"],
                       music_report={"share": 0, "ranges": [], "song": None})
    assert rep["verdict"] == "review" and not rep["auto_post_allowed"]


def test_clip_program_with_proof_passes():
    clip = {"start": 0, "end": 30, "hook": "x", "captions": {}}
    s = {"name": "X", "rights": "clip_program", "rights_note": "https://kick.com/x rules"}
    rep = guard.review(clip, s, {"duration": 600}, "what a play", ["instagram", "youtube"],
                       music_report={"share": 0, "ranges": [], "song": None})
    assert rep["verdict"] == "pass" and rep["auto_post_allowed"]


def test_identified_song_blocks():
    clip = {"start": 0, "end": 30, "hook": "", "captions": {}}
    rep = guard.review(clip, {"name": "X", "rights": "own"}, {"duration": 600}, "", ["instagram"],
                       music_report={"share": 0.5, "ranges": [(0, 15)], "song": {"artist": "A", "title": "B"}})
    assert rep["verdict"] == "block"


def test_platform_limits():
    clip = {"start": 0, "end": 200, "hook": "x", "captions": {}}
    checks = guard.platform_checks(clip, ["youtube", "instagram"])
    by = {c["id"]: c["status"] for c in checks}
    assert by["platform_youtube"] == "block" and by["platform_instagram"] == "ok"
    assert rules.action("violence", "facebook") == "block"


def test_publish_gate():
    assert publish.allowed({"verdict": "pass", "state": "new", "guard": {}})[0]
    assert not publish.allowed({"verdict": "review", "state": "new", "guard": {}})[0]
    assert publish.allowed({"verdict": "review", "state": "approved", "guard": {"approved_by_you": True}})[0]
    assert not publish.allowed({"verdict": "block", "state": "approved", "guard": {"approved_by_you": True}})[0]
    assert publish.allowed({"verdict": "block", "state": "approved", "guard": {"override_reason": "my own song"}})[0]


# ---------------------------------------------------------------- pipeline end to end

def test_pipeline_makes_vertical_checked_clips(video):
    job = pipeline.create_job("video", file=str(video), title="test")
    pipeline.run_job(job["id"])
    j = db.get("jobs", job["id"])
    assert j["status"] == "done", j["error"]
    clips = db.query("SELECT * FROM clips WHERE job_id=?", (job["id"],))
    assert clips
    for c in clips:
        info = probe(Path(c["file"]))
        assert (info["width"], info["height"]) == (1080, 1920)
        assert c["verdict"] in ("pass", "review", "block")
        assert {k["id"] for k in c["guard"]["checks"]} >= {"rights", "music", "originality"}


# ---------------------------------------------------------------- API

@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from bcut.server import app
    return TestClient(app, base_url="http://127.0.0.1:8777")


def test_cross_site_post_refused(client):
    r = client.post("/api/streamers", json={"name": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_streamer_flow_and_autopost_needs_rights(client, monkeypatch):
    from bcut import radar
    monkeypatch.setattr(radar, "check_now", lambda sid: None)
    r = client.post("/api/streamers", json={"name": "Rohan", "channels": "kick.com/rohan\nyoutube.com/@rohan"})
    assert r.status_code == 200, r.text
    s = r.json()
    assert set(s["channels"]) == {"kick", "youtube"} and s["rights"] == "unknown"
    assert client.patch(f"/api/streamers/{s['id']}", json={"auto_post": True}).status_code == 400
    ok = client.patch(f"/api/streamers/{s['id']}", json={"auto_post": True, "rights": "permission",
                                                          "rights_note": "email 2026-11-10"})
    assert ok.status_code == 200 and ok.json()["auto_post"] == 1


def test_blocked_clip_needs_written_reason(client):
    c = db.query("SELECT * FROM clips LIMIT 1")[0]
    db.update("clips", c["id"], verdict="block")
    assert client.post(f"/api/clips/{c['id']}/approve", json={"accounts": []}).status_code == 400
    r = client.post(f"/api/clips/{c['id']}/approve", json={"accounts": [], "override_reason": "it's my own beat, I made it"})
    assert r.status_code == 200 and r.json()["clip"]["guard"]["override_reason"]


def test_state_and_dashboard(client):
    assert client.get("/").status_code == 200
    st = client.get("/api/state").json()
    assert "counts" in st and "tools" in st


# ---------------------------------------------------------------- bring-your-own AI key (e.g. a free Gemini key)

class _Resp:
    def __init__(self, code, data, headers=None):
        import httpx
        self.status_code, self._d, self.headers = code, data, headers or {}
        self.request = httpx.Request("POST", "https://generativelanguage.googleapis.com/x")

    def json(self):
        return self._d

    def raise_for_status(self):
        import httpx
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=self.request, response=self)


def test_gemini_key_lists_models_and_answers(monkeypatch):
    import httpx
    from bcut import ai
    db.kv_set("ai", {"provider": "gemini", "key": "AIza-test", "model": ""})
    seen = {}
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp(200, {"data": [
        {"id": "models/gemini-3.8-flash"}, {"id": "models/gemini-4-argon"}, {"id": "models/text-embedding-004"}]}))
    assert ai.list_models("gemini", "AIza-test") == ["gemini-3.8-flash", "gemini-4-argon"]
    calls = []

    def post(url, **kw):
        calls.append(url)
        seen.update(kw)
        if len(calls) == 1:
            return _Resp(429, {}, {"retry-after": "0"})   # free tier: too many requests, then fine
        return _Resp(200, {"choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}]})
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert ai.ask_json("sys", "user") == {"ok": True}
    assert calls[-1].startswith("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
    assert seen["headers"]["authorization"] == "Bearer AIza-test"
    assert seen["json"]["model"] == "gemini-3.8-flash"     # default when no model is chosen
    db.kv_set("ai", {})


# ---------------------------------------------------------------- add-ons and keys from the dashboard

def test_keys_pasted_in_dashboard_are_used(client):
    from bcut import tools
    r = client.post("/api/settings/keys", json={"audd": "tok-123"})
    assert r.json()["audd"] is True and tools.key("audd") == "tok-123"
    assert client.get("/api/state").json()["tools"]["audd"] is True
    client.post("/api/settings/keys", json={"audd": ""})
    assert not tools.key("audd")


def test_install_button_runs_pip_in_the_apps_python(client, monkeypatch):
    import subprocess as sp
    from bcut import tools
    calls = []

    class FakeProc:
        stdout = iter(["Collecting demucs\n", "Successfully installed demucs\n"])

        def wait(self):
            return 0
    monkeypatch.setattr(sp, "Popen", lambda cmd, **kw: calls.append(cmd) or FakeProc())
    monkeypatch.setattr(tools, "installed", lambda name: True)
    assert client.post("/api/tools/demucs/install").status_code == 200
    for _ in range(50):
        if tools.status()["demucs"]["state"] != "installing":
            break
        time.sleep(0.05)
    assert tools.status()["demucs"]["state"] == "done"
    assert calls[0][:4] == [sys.executable, "-m", "pip", "install"] and "demucs>=4.1" in calls[0]
    assert client.post("/api/tools/rm-rf/install").status_code == 404


# ---------------------------------------------------------------- regressions from real use

def test_env_inline_comments_are_ignored(tmp_path, monkeypatch):
    """A .env line like `BCUT_WHISPER_MODEL=small   # tiny / base ...` must give "small" (it crashed transcription)."""
    import importlib
    from bcut import config
    env = config.ROOT / ".env"
    had = env.read_text() if env.exists() else None
    env.write_text('BCUT_TEST_A=small            # tiny / base / small\nBCUT_TEST_B="x # y"\nBCUT_TEST_C=01-07  # hours\n'
                   'BCUT_TEST_D=              # empty = detect\n')
    for k in ("BCUT_TEST_A", "BCUT_TEST_B", "BCUT_TEST_C", "BCUT_TEST_D"):
        monkeypatch.delenv(k, raising=False)
    try:
        config._load_dotenv()
        assert os.environ["BCUT_TEST_A"] == "small"
        assert os.environ["BCUT_TEST_B"] == "x # y"
        assert os.environ["BCUT_TEST_C"] == "01-07"
        assert os.environ["BCUT_TEST_D"] == ""
    finally:
        if had is None:
            env.unlink()
        else:
            env.write_text(had)


def test_frames_decode_without_opencv_codecs(video):
    """Face tracking frames come from ffmpeg, so AV1/VP9 downloads work too."""
    from bcut import media
    got = media.frames(video, [10.0, 10.5, 11.0, 11.5])
    assert len(got) == 4 and got[0][1].shape[1] == 480 and abs(got[-1][0] - 11.5) < 0.01


def test_hook_is_a_whole_sentence_not_cut_off():
    from bcut import brain
    ws, t = [], 0.0
    for sent in ["World Ancient Temple.", "But we are so high up right now and honestly it is terrifying.", "Would you jump?"]:
        for w in sent.split():
            ws.append({"w": w, "s": t, "e": t + 0.3})
            t += 0.35
    hook = brain.pick_hook(ws, 0, t)
    assert hook == "Would you jump?" and "…" not in hook


# ---------------------------------------------------------------- autopilot + pasted-link rights

def test_pasted_link_marked_mine_can_auto_post():
    clip = {"start": 0, "end": 30, "hook": "x", "captions": {}}
    mine = guard.review(clip, None, {"duration": 900, "rights": "own"}, "what a play", ["youtube"],
                        music_report={"share": 0, "ranges": [], "song": None})
    other = guard.review(clip, None, {"duration": 900, "rights": "unknown"}, "what a play", ["youtube"],
                         music_report={"share": 0, "ranges": [], "song": None})
    assert mine["auto_post_allowed"] and not other["auto_post_allowed"]


def test_autopilot_fills_daily_target_with_best_ready_clips():
    from datetime import datetime, timedelta
    now = datetime.combine(datetime.now().date(), datetime.min.time()).timestamp() + 8 * 3600  # 08:00 today
    acc = db.insert("accounts", {"id": db.new_id("a"), "platform": "youtube", "name": "Test channel",
                                 "secrets": {}, "settings": {"daily_target": 3}, "created": now})
    ready = {"auto_post_allowed": True, "checks": []}
    ids = []
    for i, (verdict, g) in enumerate([("pass", ready), ("pass", ready), ("review", {}), ("pass", ready),
                                      ("pass", ready), ("pass", ready), ("pass", ready), ("pass", ready)]):
        c = db.insert("clips", {"id": db.new_id("c"), "job_id": "jt", "streamer_id": None, "title": f"c{i}",
                                "start": 0, "end": 30, "score": 90 - i, "guard": g, "verdict": verdict,
                                "state": "new", "created": now})
        ids.append(c["id"])
    made = publish.autopilot_tick(now)
    assert len(made) == 6                                   # 3 today + 3 tomorrow
    dues = sorted(p["due"] for p in made)
    days = {datetime.fromtimestamp(d).date() for d in dues}
    assert len(days) == 2 and all(9 <= datetime.fromtimestamp(d).hour <= 23 for d in dues)
    assert ids[2] not in {p["clip_id"] for p in made}        # a clip waiting for review is never auto-posted
    assert publish.autopilot_tick(now) == []                 # already full: nothing more
    db.delete("accounts", acc["id"])


def test_retry_a_failed_job(client):
    j = db.insert("jobs", {"id": db.new_id("j"), "kind": "video", "url": "https://example.com/v", "title": "t",
                           "streamer_id": None, "status": "failed", "step": "x", "error": "boom", "meta": {},
                           "created": 0, "updated": 0})
    r = client.post(f"/api/jobs/{j['id']}/retry")
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["error"] == ""


def test_live_channel_page_becomes_stream_page():
    from bcut import live
    assert live.stream_page("https://www.youtube.com/@theRachitroo") == "https://www.youtube.com/@theRachitroo/live"
    assert live.stream_page("https://www.youtube.com/@x/live") == "https://www.youtube.com/@x/live"
    assert live.stream_page("https://www.youtube.com/watch?v=abc") == "https://www.youtube.com/watch?v=abc"
    assert live.stream_page("https://kick.com/xqc") == "https://kick.com/xqc"


def test_live_failure_backs_off(monkeypatch):
    from bcut import live
    monkeypatch.setattr(live, "resolve", lambda url: (_ for _ in ()).throw(RuntimeError("Not live right now")))
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    s = live.Session({"id": "sX", "name": "t"}, "https://www.youtube.com/@t/live")
    s.running = True
    s._record()
    from bcut import db
    j = db.get("jobs", s.job["id"])
    assert j["status"] == "failed" and "Not live" in j["error"]
    assert live.backing_off("sX")


def test_telegram_pairing_commands_and_buttons(monkeypatch):
    from bcut import telegram

    out = []
    monkeypatch.setattr(telegram, "_call", lambda method, **kw: out.append((method, kw)) or {"username": "bcut_bot"})
    db.kv_set("telegram", {"token": "123:abc"})
    code = telegram.pairing_code()
    # a stranger with the wrong code gets nothing useful and isn't paired
    telegram._handle({"update_id": 1, "message": {"chat": {"id": 999}, "text": "/start 000000", "from": {}}})
    assert not telegram.settings().get("chat_id")
    # the owner pairs with the right code
    telegram._handle({"update_id": 2, "message": {"chat": {"id": 42}, "text": f"/start {code}",
                                                   "from": {"username": "harsh"}}})
    assert telegram.settings()["chat_id"] == 42 and telegram.public()["paired"]
    # strangers are ignored after pairing
    out.clear()
    telegram._handle({"update_id": 3, "message": {"chat": {"id": 999}, "text": "/status"}})
    assert out == []
    telegram._handle({"update_id": 4, "message": {"chat": {"id": 42}, "text": "/today"}})
    assert out and out[-1][0] == "sendMessage" and "Today:" in out[-1][1]["text"]
    # a clip waiting for review: Approve from the phone marks it approved by you
    c = db.insert("clips", {"id": db.new_id("c"), "job_id": "jt", "streamer_id": None, "title": "tg clip",
                            "hook": "", "start": 0, "end": 10, "score": 50, "why": "", "file": "/nope.mp4",
                            "thumb": "", "captions": {}, "guard": {"verdict": "review"}, "verdict": "review",
                            "state": "new", "created": time.time()})
    assert telegram._buttons(c)["inline_keyboard"][0][0]["callback_data"] == f"ap:{c['id']}"
    telegram._handle({"update_id": 5, "callback_query": {"id": "q", "data": f"ap:{c['id']}",
                                                         "message": {"chat": {"id": 42}, "message_id": 7}}})
    got = db.get("clips", c["id"])
    assert got["state"] == "approved" and got["guard"]["approved_by_you"]
    # blocked clips have no Approve button
    assert [b["text"] for b in telegram._buttons({**c, "id": "x", "verdict": "block", "state": "new"})["inline_keyboard"][0]] == ["❌ Reject"]
    # repeated events are sent once
    sent = []
    monkeypatch.setattr(telegram, "say", lambda text, *a, **k: sent.append(text))
    monkeypatch.setattr(telegram.threading, "Thread", lambda target, args=(), daemon=None, **k: type("T", (), {"start": lambda self: target(*args)})())
    telegram.on_event("Posting to youtube failed: x", "error")
    telegram.on_event("Posting to youtube failed: x", "error")
    assert len(sent) == 1
    db.kv_set("telegram", {})


def test_google_client_pasted_in_app():
    from fastapi.testclient import TestClient

    from bcut.server import app
    c = TestClient(app, base_url="http://127.0.0.1:8777")
    assert c.post("/api/settings/google", json={"id": "nope", "secret": "x"}).status_code == 400
    r = c.post("/api/settings/google", json={"id": "1-abc.apps.googleusercontent.com", "secret": "GOCSPX-x"})
    assert r.status_code == 200 and r.json()["youtube"]
    assert c.get("/api/accounts").json()["can"]["youtube"]
    loc = c.get("/oauth/youtube/start", follow_redirects=False).headers["location"]
    assert "client_id=1-abc.apps.googleusercontent.com" in loc
    c.post("/api/settings/google", json={"id": ""})


def test_many_accounts_per_platform_and_no_duplicates(monkeypatch):
    from fastapi.testclient import TestClient

    from bcut.publish import platforms
    from bcut.server import app
    c = TestClient(app, base_url="http://127.0.0.1:8777")
    for a in db.query("SELECT id FROM accounts WHERE platform='instagram'"):
        db.delete("accounts", a["id"])
    who = {"t1": ("111", "one"), "t2": ("222", "two")}
    monkeypatch.setattr(platforms, "ig_connect", lambda tok, host="graph.instagram.com": {
        "name": "@" + who[tok][1], "secrets": {"token": tok, "user_id": who[tok][0], "host": host}})
    a1 = c.post("/api/accounts/instagram", json={"token": "t1"}).json()
    a2 = c.post("/api/accounts/instagram", json={"token": "t2"}).json()
    assert a1["id"] != a2["id"]
    c.patch(f"/api/accounts/{a1['id']}", json={"daily_target": 3})
    again = c.post("/api/accounts/instagram", json={"token": "t1"}).json()   # reconnecting the same account
    assert again["id"] == a1["id"] and again["settings"]["daily_target"] == 3
    assert len(db.query("SELECT id FROM accounts WHERE platform='instagram'")) == 2
    assert "select_account" in platforms.yt_auth_url("x")


def test_hindi_titles_fall_back_to_something_readable():
    from bcut import brain, transcribe
    assert not transcribe.readable("तब लिए दी जा जीजे तब")
    assert transcribe.readable("kya bhai, yeh dekho!")
    assert brain._fallback_title("🌴BREAKUP HOGYA 🌴 IRL", "rachitro") == "rachitro: BREAKUP HOGYA IRL"
