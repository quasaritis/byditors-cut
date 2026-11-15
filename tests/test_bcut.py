"""Run with:  python -m pytest -q
Needs ffmpeg. No keys, no internet: everything runs on generated audio/video in a throwaway data folder."""
import os
import subprocess
import sys
import tempfile
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
