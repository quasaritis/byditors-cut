"""Run with:  python -m pytest -q
Needs ffmpeg. No keys, no internet: everything runs on generated audio/video in a throwaway data folder."""
import re
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
    monkeypatch.setattr(platforms, "ig_connect", lambda tok, host="", username="": {
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


def test_hindi_words_become_hinglish_offline():
    from bcut.transcribe import _hinglish, hinglish_word
    got = " ".join(hinglish_word(w) for w in "क्या भाई नहीं यार ठीक है बहुत प्यार अच्छा था।".split())
    assert got == "kya bhai nahin yaar theek hai bahut pyaar accha tha."
    assert hinglish_word("hello") == "hello"
    ws = _hinglish([{"w": "क्या", "s": 1.0, "e": 1.2}, {"w": "bro", "s": 1.2, "e": 1.4}])
    assert [w["w"] for w in ws] == ["kya", "bro"] and ws[0]["s"] == 1.0


def test_every_layout_renders(video, tmp_path):
    """Each layout's ffmpeg graph (including moving crops) renders a 1080x1920 clip."""
    from bcut import reframe, render
    pr = probe(video)
    plans = {
        "split": {"mode": "split", "cams": [{"box": [0.02, 0.6, 0.25, 0.32], "face": [0.15, 0.75]}]},
        "split2": {"mode": "split", "cams": [{"box": [0.64, 0.44, 0.17, 0.22], "face": [0.75, 0.55]},
                                             {"box": [0.64, 0.66, 0.17, 0.22], "face": [0.75, 0.77]}]},
        "face": {"mode": "face", "track": [[0, 0.3], [1, 0.3], [2, 0.7], [3, 0.7]]},
        "stack": {"mode": "stack", "a": {"w": 0.4, "h": 0.6, "track": [[0, 0.25, 0.4], [3, 0.3, 0.45]]},
                  "b": {"w": 0.4, "h": 0.6, "track": [[0, 0.75, 0.4], [3, 0.7, 0.4]]}},
        "wide": {"mode": "wide"}, "center": {"mode": "center"},
    }
    words = [{"w": w, "s": 0.2 + i * 0.4, "e": 0.5 + i * 0.4} for i, w in enumerate("this is a test, okay?".split())]
    for name, plan in plans.items():
        out = render.render(video, tmp_path / f"{name}.mp4", 10, 13, plan, pr["width"], pr["height"], words,
                            hook="Hook line", credit="@someone")
        p = probe(out)
        assert (p["width"], p["height"]) == (1080, 1920), name
    assert reframe.commands(plans["face"], 3, pr["width"], pr["height"]).count("crop@f x") > 5


def test_captions_stay_off_faces():
    """A face low on the screen (the Obama-in-the-bottom-half case) pushes the caption above it."""
    from bcut import render
    faces = [[t, 300, 1250, 780, 1700] for t in np.arange(0, 6, 0.25)]       # face where captions go by default
    y = render._place(1300, 0, 3, 64, 300, faces, None)
    assert y + 64 <= 1250 - 0.12 * 450 or y - 64 >= 1700 + 0.3 * 450
    assert render._place(1300, 0, 3, 64, 300, [], None) == 1300               # nothing in the way: stays put
    words = [{"w": "y", "s": 0.1, "e": 0.3}, {"w": "'all", "s": 0.3, "e": 0.5}, {"w": "ready,", "s": 0.5, "e": 0.9}]
    ass = render.build_ass(words, 0, 2, "", "", {"mode": "fit", "faces_out": faces})
    assert "Y'ALL" in ass and "READY," not in ass and "BorderStyle" in ass and ",3," not in ass.split("Style: Cap")[1][:120]


def test_clip_edges_land_between_words():
    from bcut import brain
    words, t = [], 0.0
    for i in range(80):                       # 4-word sentences with a breath after each
        w = f"w{i}" + ("." if i % 4 == 3 else "")
        words.append({"w": w, "s": t, "e": t + 0.3})
        t += 0.35 + (0.5 if i % 4 == 3 else 0)
    for s, e in [(3.1, 20.4), (7.77, 30.2), (1.0, 60.0)]:
        a, b = brain._snap(s, e, [], t, 10, 20, words)
        assert 10 <= b - a <= 20.8
        for w in words:
            assert not (w["s"] < a < w["e"]) and not (w["s"] < b < w["e"]), (s, e, a, b)


def test_hinglish_drops_nukta_dots():
    from bcut.transcribe import hinglish_word
    assert hinglish_word("बड़ा") == "bada" and hinglish_word("थोड़ा") == "thoda"


def test_facecam_found_on_gameplay_not_on_a_still_room(tmp_path):
    """A webcam box over moving 'gameplay' is a cam; the same face on a still camera is not."""
    import cv2
    from bcut import reframe
    rng = np.random.default_rng(1)
    face = np.full((60, 50, 3), 180, np.uint8)
    game, room = [], []
    for i in range(40):
        g = rng.integers(0, 255, (360, 640, 3), dtype=np.uint8)
        g = cv2.GaussianBlur(g, (0, 0), 3)
        g[230:340, 20:180] = 40
        cv2.rectangle(g, (20, 230), (180, 340), (255, 255, 255), 2)
        game.append(g)
        r = np.full((360, 640, 3), 90, np.uint8)
        r[230:340, 20:180] = 40
        cv2.rectangle(r, (20, 230), (180, 340), (255, 255, 255), 2)
        r[260 + i % 3:320 + i % 3, 70:120] = face
        room.append(r)
    dets = [[(80 / 640, 262 / 360, 40 / 640, 55 / 360)] for _ in range(40)]
    cams = reframe.find_cams(dets, game)
    assert cams and abs(cams[0]["box"][0] - 20 / 640) < 0.02 and abs(cams[0]["box"][3] - 110 / 360) < 0.03
    assert reframe.find_cams(dets, room) == []


def test_instagram_through_a_facebook_page(monkeypatch):
    """A Facebook token finds the Instagram account linked to a Page and keeps the Page's own token."""
    import httpx
    from bcut.publish import platforms

    def fake_get(url, params=None, **kw):
        req = httpx.Request("GET", url)
        if url.endswith("/me/accounts"):
            return httpx.Response(200, request=req, json={"data": [
                {"id": "p1", "name": "Other", "access_token": "EAApage1"},
                {"id": "p2", "name": "GTA Final Boss Clipper", "access_token": "EAApage2",
                 "instagram_business_account": {"id": "1789", "username": "gtafinalbossclipper"}}]})
        return httpx.Response(400, request=req, json={"error": {"message": "nope"}})
    monkeypatch.setattr(platforms.httpx, "get", fake_get)
    res = platforms.ig_connect("EAAuser")
    assert res["name"] == "@gtafinalbossclipper"
    assert res["secrets"] == {**res["secrets"], "token": "EAApage2", "user_id": "1789", "host": "graph.facebook.com"}


def test_moving_the_app_folder_keeps_clips_working():
    """Stored paths from the old folder are rewritten to the current data folder on start."""
    from bcut import config, db
    old = "C:\\Users\\X\\Downloads\\byditors-cut\\data"
    cid = db.new_id("c")
    db.insert("clips", {"id": cid, "job_id": "j", "title": "t", "start": 0, "end": 1,
                        "file": old + "\\clips\\j\\a.mp4", "thumb": old + "\\clips\\j\\a.jpg",
                        "guard": {"edit": {"src": old + "\\sources\\j\\source.mp4"}}, "created": 0})
    db.kv_set("data_dir", old)
    assert db.rebase_paths() >= 1
    c = db.get("clips", cid)
    assert c["file"] == str(config.DATA) + "\\clips\\j\\a.mp4"
    assert c["guard"]["edit"]["src"].startswith(str(config.DATA))
    assert db.rebase_paths() == 0


def test_videos_with_their_own_subtitles_get_none_of_ours(video, tmp_path):
    """A clip of an already-subtitled video keeps its subtitles and gets no second set on top."""
    from bcut import media, reframe, render, textscan
    pr = probe(video)
    words = [{"w": w, "s": 10.3 + i * 0.45, "e": 10.7 + i * 0.45}
             for i, w in enumerate("so we went there and nobody knew what was going on at all honestly bro".split())]
    subbed = render.render(video, tmp_path / "subbed.mp4", 10, 16, {"mode": "fit"}, pr["width"], pr["height"], words)
    shots = media.frames(subbed, [0.5 + i * 0.25 for i in range(20)], width=640)
    assert textscan.scan([i for _, i in shots], [t for t, _ in shots])["captions"]
    plain = media.frames(video, [10.5 + i * 0.25 for i in range(20)], width=640)
    assert not textscan.scan([i for _, i in plain], [t for t, _ in plain])["captions"]
    ass = render.build_ass(words, 10, 16, "", "", {"mode": "fit", "burned": True, "faces_out": [], "text_out": []})
    assert "Style: Cap" in ass and "Dialogue: 1," not in ass
    plan = reframe.analyse(subbed, 0, 6, 1080, 1920, [], "auto")
    assert plan["burned"] and plan["mode"] == "fit"


# ---------------------------------------------------------------- the edit passes

def _talky(tmp: Path) -> Path:
    """30 s: talking (pulsed noise) with silent gaps at 6-9 s and 17-20 s, a scream at 23-25 s."""
    out = tmp / "talk.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=960x540:r=30:d=30",
        "-f", "lavfi", "-i", "anoisesrc=d=30:c=pink:a=0.12",
        "-filter_complex", "[1:a]apulsator=hz=3,volume='if(between(t,6,9)+between(t,17,20),0.02,"
                           "if(between(t,23,25),6,1))':eval=frame[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
        str(out)], check=True)
    return out


def _talk_words():
    ws = []
    for a, b in ((0.3, 5.8), (9.2, 16.8), (20.2, 29.5)):
        t = a
        while t + 0.35 < b:
            ws.append({"w": "word", "s": round(t, 2), "e": round(t + 0.3, 2)})
            t += 0.4
    return ws


def test_dead_air_is_cut_and_screams_are_kept(tmp_path):
    from bcut import fx
    src = _talky(tmp_path)
    st = {**fx.DEFAULTS, "cold_open": False}
    e = fx.plan(src, 0, 30, _talk_words(), True, st)
    joins = [(a[1], b[0]) for a, b in zip(e["pieces"], e["pieces"][1:])]
    assert any(5.5 < g0 < 6.5 and 8.5 < g1 < 9.5 for g0, g1 in joins), e
    assert e["removed"] >= 4.5
    assert not any(g0 < 24 < g1 for g0, g1 in joins)          # the scream stays


def test_produce_cuts_cold_opens_punches_and_loops(tmp_path):
    from bcut import fx
    src = _talky(tmp_path)
    out = tmp_path / "made.mp4"
    made = pipeline.produce(src, out, 0, 30, 960, 540, True, _talk_words(), "Hook line", "@x", "center",
                            st=dict(fx.DEFAULTS))
    p = probe(out)
    assert (p["width"], p["height"]) == (1080, 1920)
    assert made["info"]["cold"] and made["info"]["removed"] > 3
    assert made["fx"]["punches"] and any(s["name"] == "rewind" for s in made["fx"]["sounds"])
    assert abs(p["duration"] - made["dur"]) < 0.4 and made["dur"] < 30
    import json
    json.dumps(made["info"]), json.dumps(made["fx"])            # everything stored is plain JSON


def test_every_pass_can_be_switched_off(tmp_path):
    from bcut import fx
    src = _talky(tmp_path)
    off = {k: False for k in fx.DEFAULTS}
    made = pipeline.produce(src, tmp_path / "plain.mp4", 2, 14, 960, 540, True, _talk_words(), "", "", "center", st=off)
    assert made["info"]["removed"] == 0 and not made["fx"]["punches"] and not made["fx"]["sounds"]
    assert abs(probe(tmp_path / "plain.mp4")["duration"] - 12) < 0.3
    assert fx.set_style({"punch": False})["punch"] is False and fx.style()["tighten"] is True
    fx.set_style({"punch": True})


def test_speaker_cuts_between_whoever_talks():
    from bcut import reframe
    rng = np.random.default_rng(1)
    imgs, ts = [], np.arange(0, 12, 0.25)
    base = (rng.random((360, 640, 3)) * 60 + 80).astype(np.uint8)
    for t in ts:
        im = base.copy()
        talker = 0 if (t // 3) % 2 == 0 else 1             # A talks 0-3, B 3-6, A 6-9, B 9-12
        x0 = 64 if talker == 0 else 512
        im[150:180, x0:x0 + 40] = rng.integers(0, 255, (30, 40, 3), dtype=np.uint8)   # a moving mouth
        imgs.append(im)
    people = [(0.09, 0.3, 0.1, 0.22), (0.79, 0.3, 0.1, 0.22)]
    plan = reframe._speaker_plan(ts, imgs, people, np.ones(len(ts), bool), [], 0.32)
    assert plan and plan["mode"] == "speaker"
    who = [s[2] for s in plan["speakers"]]
    assert who[:4] == ["a", "b", "a", "b"], plan["speakers"]
    assert 2.4 < plan["speakers"][1][0] < 3.6


def test_clip_that_from_the_streamer_and_from_chat():
    from bcut.live import CHAT_CALL, CLIP_CALL
    assert CLIP_CALL.search("yo chat clip that right now") and CLIP_CALL.search("bhai clip karo")
    assert not CLIP_CALL.search("he clipped the car") and not CLIP_CALL.search("nice clip")
    assert CHAT_CALL.search("CLIP IT") and CHAT_CALL.search("!clip") and CHAT_CALL.search("clipppp")
    assert not CHAT_CALL.search("eclipse") and not CHAT_CALL.search("clipboard")


def test_views_steer_the_autopilot_and_the_hook_style():
    from bcut import learn
    import random
    db.kv_set("post_metrics", {})
    for i in range(8):
        style = "question" if i % 2 else "quote"
        c = db.insert("clips", {"id": f"cl{i}", "job_id": "j", "title": "t", "hook": f"hook {i}", "start": 0, "end": 10,
                                "score": 50, "file": "x", "thumb": "x", "captions": {}, "state": "posted",
                                "guard": {"hook_style": style}, "verdict": "pass", "created": time.time()})
        m = learn._metrics()
        m[f"p{i}"] = {"views": 9000 if style == "question" else 1000, "likes": 0, "clip": c["id"],
                      "platform": "instagram", "at": time.time()}
        db.kv_set("post_metrics", m)
    st = learn.style_stats()
    assert st["question"]["lift"] > st["quote"]["lift"]
    picks = [learn.pick_style(random.Random(i)) for i in range(200)]
    assert picks.count("question") > picks.count("quote")
    assert learn.boost({"guard": {"hook_style": "question"}}) > learn.boost({"guard": {"hook_style": "quote"}})
    assert "hook" in learn.prompt_extra("question") and learn.top_hooks()
    db.kv_set("post_metrics", {})


def test_trim_editor_renders_a_new_cut(tmp_path):
    src = _talky(tmp_path)
    pr = probe(src)
    job = {"id": "jtrim", "streamer_id": None, "url": ""}
    c = {"start": 2.0, "end": 14.0, "score": 60, "hook": "First hook", "title": "Trim me", "why": ""}
    clip = pipeline.make_clip(job, src, pr, {"title": "t", "uploaded_by_you": True}, c, _talk_words(), None, [])
    out = pipeline.rerender(clip["id"], start=10.0, end=22.0, hook="New hook", layout="center")
    assert out["start"] == 10.0 and out["end"] == 22.0 and out["hook"] == "New hook"
    assert out["guard"]["edit"]["hook"] == "New hook" and out["guard"]["layout_choice"] == "center"
    with pytest.raises(RuntimeError):
        pipeline.rerender(clip["id"], start=10.0, end=11.0)


def test_caption_lines_never_overlap_and_words_pop_in_place():
    from bcut import render
    ws, t = [], 0.0
    for i, w in enumerate("so i was walking down the street and this guy just runs at me with a giant fish okay".split()):
        ws.append({"w": w, "s": round(t, 2), "e": round(t + 0.22, 2)})
        t += 0.24 if i % 4 else 0.3          # tight speech: groups follow each other within 0.1 s
    ass = render.build_ass(ws, 0, t + 1, "", "", {"faces_out": [], "mode": "center"})
    evs = []
    for ln in ass.splitlines():
        if ln.startswith("Dialogue: 1,"):
            f = ln.split(",", 9)
            sec = lambda s: int(s[0]) * 3600 + int(s[2:4]) * 60 + float(s[5:])  # noqa: E731
            pos = re.search(r"\\(?:pos|move)\((\d+),(\d+)", f[9]).groups()
            evs.append((sec(f[1]), sec(f[2]), int(pos[1]), int(pos[0]), f[9].rsplit("}", 1)[1]))
    assert evs
    x = 0.0
    while x < t:
        on = [e for e in evs if e[0] <= x < e[1]]
        assert len({e[3] for e in on}) == len(on), (x, on)          # one word per spot
        assert len({e[2] for e in on}) <= 1, (x, on)                # one line at a time
        assert len(on) <= 3
        x += 0.01
    assert "\\N" not in "".join(e[4] for e in evs)


def test_caption_line_is_centred_from_its_first_word():
    from bcut import render
    ws = [{"w": "rush", "s": 0.2, "e": 0.5}, {"w": "karo", "s": 0.5, "e": 0.9}, {"w": "bhai", "s": 0.9, "e": 1.2}]
    ass = render.build_ass(ws, 0, 2, "", "", {"faces_out": [], "mode": "center"})
    first = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 1,0:00:00.20")]
    assert len(first) == 3                                     # the whole line shows from the first word
    xs = [int(re.search(r"\\move\((\d+),", ln).group(1)) for ln in first]
    assert abs((xs[0] + xs[-1]) / 2 - 540) < 60


def test_hindi_captions_get_english_words_spelled_in_english(monkeypatch):
    from bcut import ai, brain, transcribe
    words = transcribe._hinglish([{"w": "रश", "s": 0, "e": 0.3}, {"w": "करो", "s": 0.3, "e": 0.6},
                                  {"w": "bro", "s": 0.6, "e": 0.9}])
    assert words[0]["dv"] == "रश" and "dv" not in words[2]
    asked = {}

    def fake(system, user, **k):
        asked["user"] = user
        return {"w": ["rush", "karo", "bro"]}
    monkeypatch.setattr(ai, "enabled", lambda: True)
    monkeypatch.setattr(ai, "ask_json", fake)
    out = brain.fix_spellings(words, log=lambda *a: None)
    assert [w["w"] for w in out] == ["rush", "karo", "bro"] and out[0]["s"] == 0
    assert "रश" in asked["user"]
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: {"w": ["only one"]})       # wrong count: keep as is
    assert [w["w"] for w in brain.fix_spellings(words, log=lambda *a: None)] == [w["w"] for w in words]
    assert transcribe.model_name("cuda") == "large-v3-turbo" or transcribe.config.WHISPER_MODEL != "auto"


def test_backup_ai_takes_over_when_the_free_daily_limit_runs_out(monkeypatch):
    import httpx
    from bcut import ai
    db.kv_set("ai", {"provider": "groq", "model": "openai/gpt-oss-120b", "key": "gsk_main"})
    db.kv_set("ai_backup", {"provider": "gemini", "model": "", "key": "gm_backup"})
    db.kv_set("ai_rest", {})
    used = []

    def fake(system, user, max_tokens, temperature, s=None):
        used.append(s["provider"])
        if s["provider"] == "groq":
            req = httpx.Request("POST", "https://api.groq.com/x")
            resp = httpx.Response(429, request=req, text='{"error":{"message":"Rate limit reached for model on '
                                  'tokens per day (TPD): Limit 200000. Please try again in 1h12m3.5s."}}')
            raise httpx.HTTPStatusError("429", request=req, response=resp)
        return {"ok": s["provider"]}
    monkeypatch.setattr(ai, "_ask_json", fake)
    assert ai.ask_json("s", "u") == {"ok": "gemini"}
    assert used == ["groq", "gemini"]
    assert 3600 < ai.resting() - time.time() < 4500           # back to Groq when its limit resets
    assert ai.ask_json("s", "u") == {"ok": "gemini"} and used[-1] == "gemini" and used.count("groq") == 1
    db.kv_set("ai_rest", {"until": time.time() - 1})
    used.clear()
    ai.ask_json("s", "u")
    assert used[0] == "groq"
    for k in ("ai", "ai_backup", "ai_rest"):
        db.kv_set(k, {})


def test_youtube_live_chat_lines_are_read():
    import json
    from bcut.live import CHAT_CALL, _yt_chat_text
    line = json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {
        "liveChatTextMessageRenderer": {"message": {"runs": [{"text": "CLIP IT "}, {"emoji": {"shortcuts": [":fire:"]}}]}}}}}]},
        "videoOffsetTimeMsec": "1200", "isLive": True}).encode()
    assert _yt_chat_text(line) == "CLIP IT :fire:" and CHAT_CALL.search(_yt_chat_text(line))
    assert _yt_chat_text(b'{"replayChatItemAction": {"actions": [{"addLiveChatTickerItemAction": {}}]}}') == ""
    assert _yt_chat_text(b"not json") == ""


def test_virtual_camera_never_swings_back_and_forth():
    from bcut import reframe
    ts = np.arange(0, 10, 0.25)
    rng = np.random.default_rng(3)
    for target in (np.where(ts < 2, 0.3, 0.7),                      # walks right across: one clean cut
                   np.where(ts < 2, 0.3, 0.42),                     # steps aside: one smooth glide
                   0.5 + 0.15 * rng.standard_normal(len(ts)),       # bouncing around: stays calm
                   0.3 + 0.04 * ts):                                # walks slowly: follows
        cam = reframe._camera(ts, reframe._smooth(target), 0.05, [], 0.1, 0.9, vmax=0.4)
        d = np.diff(cam)
        flips = int(((d[1:] * d[:-1]) < -1e-9).sum())
        assert flips <= 2, (target[:6], np.round(cam, 3))
        assert np.all(np.abs(d[np.abs(d) < 0.2]) <= 0.4 * 0.25 + 1e-6)   # glides respect the speed limit


def test_campaign_rules_shape_every_clip_and_views_turn_into_earnings():
    from bcut import campaigns
    s = db.insert("streamers", {"id": "s_camp", "name": "Kai", "channels": {"kick": "https://kick.com/kai"},
                                "created": time.time(), "seen": [], "status": {}, "rights": "unknown"})
    assert s["captions"] == "auto"                                  # new column added to old databases too
    c = db.insert("campaigns", {"id": "k1", "name": "Kai clips", "created": time.time(), "streamers": ["s_camp"],
                                "rules": campaigns.clean_rules({"cpm": {"instagram": 2, "youtube": "1.5"},
                                                                 "min_views": 1000, "max_per_clip": 50,
                                                                 "min_len": 15, "max_len": 40,
                                                                 "tags": "#kaiclips #ad", "mention": "@kai",
                                                                 "banned": "gambling, stake"})})
    job = {"id": "jx", "meta": {}}
    assert campaigns.for_job(job, s)["id"] == "k1" and campaigns.lengths(c, 10, 90) == (15, 40)
    caps = campaigns.apply_captions({"instagram": "lol #fyp", "youtube": {"title": "t", "description": "d"}}, c)
    assert "#kaiclips" in caps["instagram"] and "@kai" in caps["youtube"]["description"]
    assert campaigns.apply_captions(caps, c) == caps                  # added once, never twice
    assert campaigns.check(c, 30, "funny moment", caps)["status"] == "ok"
    bad = campaigns.check(c, 70, "use code on stake", {"instagram": "no tags"})
    assert bad["status"] == "review" and "longer" in bad["detail"] and "stake" in bad["detail"]
    db.insert("clips", {"id": "ck1", "job_id": "jx", "title": "Kai moment", "start": 0, "end": 30, "score": 50,
                        "file": "x", "thumb": "x", "captions": caps, "state": "posted", "verdict": "pass",
                        "guard": {"campaign": {"id": "k1", "name": "Kai clips"}}, "created": time.time()})
    acc_ig = db.insert("accounts", {"id": "a_ig_k", "platform": "instagram", "name": "me", "secrets": {},
                                    "settings": {}, "created": time.time()})
    acc_yt = db.insert("accounts", {"id": "a_yt_k", "platform": "youtube", "name": "me", "secrets": {},
                                    "settings": {}, "created": time.time()})
    for pid, acc in (("pk1", acc_ig), ("pk2", acc_yt)):
        db.insert("posts", {"id": pid, "clip_id": "ck1", "account_id": acc["id"], "status": "posted", "due": time.time(),
                            "remote_id": pid, "created": time.time(), "updated": time.time()})
    db.kv_set("post_metrics", {"pk1": {"views": 40000, "clip": "ck1"}, "pk2": {"views": 600, "clip": "ck1"}})
    st = campaigns.stats(c)
    assert st["posted"] == 2 and st["views"] == 40600
    assert st["earned"] == 50.0                     # 40k views at $2 = $80, capped at $50; YouTube under the minimum
    db.kv_set("post_metrics", {})


def test_best_posting_time_is_learned_per_account():
    from datetime import datetime
    from bcut import learn
    from bcut.publish import day_slots
    acc = db.insert("accounts", {"id": "a_time", "platform": "instagram", "name": "t", "secrets": {}, "settings": {},
                                 "created": time.time()})
    base = datetime(2026, 11, 2).timestamp()
    m = {}
    for i in range(12):
        hour = 19 if i % 2 else 11                           # evening posts do 5x better than morning ones
        due = base + i * 86400 + hour * 3600
        db.insert("clips", {"id": f"ct{i}", "job_id": "j", "title": "t", "start": 0, "end": 10, "score": 50,
                            "file": "x", "thumb": "x", "captions": {}, "state": "posted", "verdict": "pass",
                            "guard": {}, "created": time.time()})
        db.insert("posts", {"id": f"pt{i}", "clip_id": f"ct{i}", "account_id": acc["id"], "status": "posted",
                            "due": due, "remote_id": "r", "created": due, "updated": due})
        m[f"pt{i}"] = {"views": 5000 if hour == 19 else 1000, "clip": f"ct{i}"}
    db.kv_set("post_metrics", m)
    st = learn.hour_stats(acc["id"])
    assert st[19]["lift"] > st[11]["lift"]
    day = datetime(2026, 12, 1).date()
    slots = day_slots(day, 1, acc["id"])
    assert len(slots) == 1 and 18 <= datetime.fromtimestamp(slots[0]).hour <= 21 or \
        learn.best_hours(acc["id"], 1, day.toordinal()) is not None
    hits = sum(19 <= learn.best_hours(acc["id"], 1, d)[0] < 21 for d in range(200))
    assert hits > 140                                         # mostly the best window, sometimes exploring
    assert len(day_slots(day, 2)) == 2                         # no account: spread evenly as before
    db.kv_set("post_metrics", {})


def test_freeze_frame_callout_and_english_captions(tmp_path, monkeypatch):
    from bcut import ai, brain, fx
    src = _talky(tmp_path)
    st = {**fx.DEFAULTS, "cold_open": False}
    made = pipeline.produce(src, tmp_path / "o.mp4", 0, 30, 960, 540, True, _talk_words(), "", "", "center", st=st,
                            callout="WAIT WHAT")
    assert made["edit"]["freeze"] and made["fx"]["callout"]["text"] == "WAIT WHAT"
    a, b = made["fx"]["callout"]["at"]
    assert abs((b - a) - fx.FREEZE) < 0.01 and any(s["name"] == "rewind" and abs(s["t"] - a) < 0.05
                                                    for s in made["fx"]["sounds"])
    assert abs(probe(tmp_path / "o.mp4")["duration"] - made["dur"]) < 0.4
    words = [{"w": w, "s": i * 0.3, "e": i * 0.3 + 0.25} for i, w in enumerate("bhai ye kya kar diya".split())]
    monkeypatch.setattr(ai, "enabled", lambda: True)
    monkeypatch.setattr(ai, "ask_json", lambda *a_, **k: {"lines": ["bro what did you just do"]})
    en = brain.to_english(words, log=lambda *x: None)
    assert [w["w"] for w in en] == "bro what did you just do".split()
    assert en[0]["s"] == 0 and en[-1]["e"] <= words[-1]["e"] + 0.01
    assert pipeline.caption_lang({"meta": {"captions": "english"}}, None) == "english"
    assert pipeline.caption_lang({"meta": {}}, {"captions": "english"}) == "english"
    assert pipeline.caption_lang({"meta": {"captions": "auto"}}, {"captions": "auto"}) == "auto"


# ---------------------------------------------------------------- styles (how clips look)

def test_styles_are_cleaned_saved_and_picked(client):
    from bcut import styles
    bad = styles.clean({"captions": {"font": "Comic Sans", "size": 9999, "lit": "red", "anim": "spin", "words": 0},
                        "layout": {"cam": "sideways", "cam_find": "draw", "cam_box": [0.5, 0.5, 0.001, 0.2],
                                   "part": [-1, 0, 3, 3]}})
    assert bad["captions"]["font"] == "Anton" and bad["captions"]["size"] == 180 and bad["captions"]["lit"] == "#FFE100"
    assert bad["captions"]["anim"] == "pop" and bad["captions"]["words"] == 1
    assert bad["layout"]["cam"] == "top" and bad["layout"]["cam_find"] == "ai" and bad["layout"]["cam_box"] is None
    assert bad["layout"]["part"] == [0.0, 0.0, 1.0, 1.0]
    d = client.get("/api/styles").json()
    assert {s["id"] for s in d["styles"] if s["template"]} >= {"t_default", "t_hype", "t_clean", "t_box"}
    assert d["fonts"]["Lilita One"]["file"].endswith(".ttf") and "spanish" in d["languages"]
    assert client.patch("/api/styles/t_default", json={}).status_code == 400          # templates stay as they are
    mine = client.post("/api/styles/t_box/copy").json()
    assert mine["name"] == "Box copy" and not mine["template"]
    assert client.post(f"/api/styles/{mine['id']}/default").json()["default"] == mine["id"]
    assert styles.get(None)["id"] == mine["id"]
    job = {"meta": {"style_id": "t_hype"}}
    assert styles.for_job(job, {"style_id": "t_clean"})["id"] == "t_hype"            # the video's pick wins
    assert styles.for_job({"meta": {}}, {"style_id": "t_clean"})["id"] == "t_clean"   # then the streamer's
    assert client.delete(f"/api/styles/{mine['id']}").json()["ok"]
    assert styles.get(None)["id"] == "t_default"
    assert client.get("/fonts/..%2F..%2Fserver.py").status_code == 404


def test_caption_styles_change_the_subtitles():
    from bcut import render, styles
    ws = [{"w": w, "s": 0.2 + i * 0.4, "e": 0.5 + i * 0.4} for i, w in enumerate("bro this is 100 insane okay".split())]
    plan = {"faces_out": [], "mode": "center"}
    plain = render.build_ass(ws, 0, 3, "He did it", "", plan)
    assert plain == render.build_ass(ws, 0, 3, "He did it", "", plan, style=styles.get("t_default"))
    st = styles.clean({"captions": {"preset": "orangebox", "font": "Poppins Black", "box": "word", "upper": False,
                                    "words": 2, "key": "#FF0000", "text": "#FFFFFF", "y": 0.5},
                       "heading": {"look": "white", "whole": True, "upper": False}})
    ass = render.build_ass(ws, 0, 3, "He did it", "", plan, style=st)
    assert "Style: Cap,Poppins Black," in ass and "\\p1}m " in ass          # word boxes are drawn shapes
    assert "He did it" in ass and "HE DID IT" not in ass                 # mixed case heading in a box
    hook = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 2,") and "Hook" in ln][0]
    assert hook.split(",")[2] == "0:00:03.00"                            # kept for the whole clip
    caps = [ln.rsplit("}", 1)[1] for ln in ass.splitlines() if ln.startswith("Dialogue: 1,")]
    assert "bro" in caps and "BRO" not in caps
    assert any("\\c&H000000FF\\" in ln and ln.endswith("}100") for ln in ass.splitlines())   # key word coloured
    lines = {int(re.search(r"\\(?:pos|move)\(\d+,(\d+)", ln).group(1)) for ln in ass.splitlines() if ln.startswith("Dialogue: 1,")}
    assert all(abs(y - 960) < 80 for y in lines)                         # where you dragged them
    off = render.build_ass(ws, 0, 3, "He did it", "", plan, style=styles.clean({"captions": {"show": False},
                                                                                  "heading": {"show": False}}))
    assert "Dialogue: 1," not in off and "Dialogue: 2," not in off
    hindi = render.build_ass([{"w": "भाई", "s": 0.2, "e": 0.6}, {"w": "देखो", "s": 0.6, "e": 1.0}], 0, 2, "", "", plan)
    assert "Style: Cap,Noto Sans Devanagari," in hindi and "भाई" in hindi


def test_style_layouts_render(video, tmp_path):
    """Zoomed in, cam at the bottom, cam in a rounded corner over a picked part, and a picked part alone."""
    from bcut import render, reframe, styles
    pr = probe(video)
    cams = [{"box": [0.02, 0.6, 0.25, 0.32], "face": [0.15, 0.75]}]
    corner = {"mode": "split", "cams": cams, "cam_pos": "corner", "corner": {"x": 0.5, "y": 0.6, "w": 0.4},
              "round": 30, "border": 6, "border_color": "#FFFFFF", "framing": "blur", "part": [300, 0, 900, 720]}
    plans = {"zoom": {"mode": "zoom"}, "bottom": {"mode": "split", "cams": cams, "cam_pos": "bottom"},
             "corner": corner, "part": {"mode": "center", "part": [400, 60, 600, 600]}}
    words = [{"w": w, "s": 0.2 + i * 0.4, "e": 0.5 + i * 0.4} for i, w in enumerate("this is a test okay".split())]
    for name, plan in plans.items():
        out = render.render(video, tmp_path / f"{name}.mp4", 10, 12, plan, pr["width"], pr["height"], words,
                            hook="Hook line", style=styles.get("t_hype"))
        p = probe(out)
        assert (p["width"], p["height"]) == (1080, 1920), name
    g = reframe.panels(plans["bottom"], pr["width"], pr["height"])
    assert g[0]["out"][1] > 0 and g[-1]["out"][1] == 0                   # cam below, game on top
    x, y, w, h = reframe.corner_rect(corner, pr["width"], pr["height"])
    assert abs(x - 540) <= 2 and abs(w - 432) <= 2 and 0 < y < 1920 - h
    assert reframe._part_px([0.1, 0.1, 0.5, 0.5], 1280, 720) == [128, 72, 640, 360]
    assert reframe._part_px([0, 0, 1, 1], 1280, 720) is None


def test_layout_options_steer_the_plan(video):
    from bcut import reframe
    pr = probe(video)
    blur = reframe.analyse(video, 10, 14, pr["width"], pr["height"], [], "auto", opts={"framing": "blur"})
    zoom = reframe.analyse(video, 10, 14, pr["width"], pr["height"], [], "auto", opts={"framing": "zoom"})
    drawn = reframe.analyse(video, 10, 14, pr["width"], pr["height"], [], "auto",
                            opts={"cam": "corner", "cam_find": "draw", "cam_box": [0.7, 0.05, 0.25, 0.3],
                                  "corner": {"x": 0.05, "y": 0.1, "w": 0.4}, "round": 20, "border": 4})
    off = reframe.analyse(video, 10, 14, pr["width"], pr["height"], [], "auto",
                          opts={"cam": "off", "cam_find": "draw", "cam_box": [0.7, 0.05, 0.25, 0.3]})
    part = reframe.analyse(video, 10, 14, pr["width"], pr["height"], [], "auto",
                           opts={"framing": "fill", "part": [0.25, 0, 0.5, 1]})
    assert blur["mode"] == "wide" and zoom["mode"] == "zoom"
    assert drawn["mode"] == "split" and drawn["cam_pos"] == "corner" and drawn["cams"][0]["box"][0] == 0.7
    assert any(r[2] < 400 for r in drawn["text_out"])                    # captions keep off the cam box
    assert off["mode"] != "split"
    assert part["part"] == [320, 0, 640, 720] and part["mode"] in ("center", "face")


def test_make_clips_options(client, monkeypatch):
    from bcut import server
    seen = {}
    monkeypatch.setattr(server.pipeline, "create_job", lambda kind, **kw: seen.update(kw) or {"id": "j"})
    client.post("/api/jobs", json={"url": "https://example.com/v", "captions": "spanish", "style_id": "t_clean",
                                   "count": 99, "min_len": 40, "max_len": 20, "from": 120, "to": 600})
    m = seen["meta"]
    assert m["captions"] == "spanish" and m["style_id"] == "t_clean"
    assert m["options"] == {"count": 30, "min_len": 20, "max_len": 40, "from": 120.0, "to": 600.0}
    client.post("/api/jobs", json={"url": "https://example.com/v", "captions": "klingon", "style_id": "nope", "to": 5})
    assert seen["meta"]["captions"] == "auto" and seen["meta"]["style_id"] == "" and seen["meta"]["options"] == {}
    assert pipeline.caption_lang({"meta": {"captions": "turkish"}}, None) == "turkish"
    assert pipeline.caption_lang({"meta": {"captions": "hinglish"}}, {"captions": "english"}) == "auto"
    assert pipeline._range({"from": 60, "to": 120}, 300) == (60.0, 120.0)
    assert pipeline._range({"from": 60, "to": 65}, 300) == (0.0, 300)
    sig = {"excite": np.ones(100), "db": np.full(100, -30.0)}
    only = pipeline._only(sig, 10, 20)
    assert only["excite"][:20].sum() == 0 and only["excite"][20:41].all() and only["db"][0] == -30
