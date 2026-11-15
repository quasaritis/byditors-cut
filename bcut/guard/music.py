"""Finding music inside a clip, and taking it out.

1. Local detector (always on, no internet): music holds steady notes (spectral peaks that stay on the same pitch
   for many frames) and a regular beat; speech glides between pitches and pulses about four times a second.
   Each second of the clip gets a 0..1 "this is music" score.
2. Song identification (optional, AUDD_API_TOKEN): a short sample of the loudest music part is checked against a
   database of commercial songs. A match means the song is copyrighted, full stop.
3. Removal (optional, `pip install demucs`): the voice is separated from the music so the clip can keep the
   talking and lose the song.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from .. import config
from ..media import SR, need, read_audio

WIN = 2048
HOP = 512


def _stft(audio: np.ndarray) -> np.ndarray:
    n = 1 + max(0, (len(audio) - WIN) // HOP)
    if n <= 1:
        return np.zeros((0, WIN // 2 + 1))
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    frames = audio[idx] * np.hanning(WIN)[None, :]
    return np.abs(np.fft.rfft(frames, axis=1))


def music_curve(audio: np.ndarray) -> np.ndarray:
    """0..1 per second: how much each second sounds like music rather than talk or noise."""
    spec = _stft(audio)
    if len(spec) < 12:
        return np.zeros(0)
    fpers = SR / HOP  # frames per second (~31)
    freqs = np.fft.rfftfreq(WIN, 1 / SR)
    band = (freqs > 60) & (freqs < 5000)
    s = spec[:, band] ** 2
    logs = np.log(s + 1e-12)
    # 1) tonal energy that HOLDS: a spectral peak that stays in the same bin for ~0.35 s. Instruments and sung
    #    notes do this; speech pitch glides from bin to bin, noise never holds.
    thr = np.percentile(logs, 80, axis=1, keepdims=True)
    peak = np.zeros_like(logs, dtype=bool)
    peak[:, 1:-1] = (logs[:, 1:-1] > logs[:, :-2]) & (logs[:, 1:-1] > logs[:, 2:]) & (logs[:, 1:-1] > thr)
    hold_n = 11
    held = peak.copy()
    for k in range(1, hold_n):
        held[:-k] &= peak[k:]
    held[-hold_n:] = False
    # a note that holds is held for all of its frames, not just its first
    full = held.copy()
    for k in range(1, hold_n):
        full[k:] |= held[:-k]
    wide = full | np.roll(full, 1, axis=1) | np.roll(full, -1, axis=1)
    tonal = (s * wide).sum(axis=1) / (s.sum(axis=1) + 1e-12)
    energy = np.log(s.sum(axis=1) + 1e-12)
    floor = np.percentile(energy, 95) - 5.0  # ~22 dB under the loud parts = a pause
    secs = int(len(energy) / fpers)
    out = np.zeros(secs)
    for i in range(secs):
        a, b = int(i * fpers), int((i + 1) * fpers)
        if energy[a:b].mean() < floor:
            continue  # near-silence
        out[i] = float(np.clip((tonal[a:b].mean() - 0.12) * 2.0, 0, 1))
    if len(out) >= 3:
        out = np.convolve(out, np.ones(3) / 3, mode="same")
    return out


def music_ranges(curve: np.ndarray, threshold: float = 0.55, min_len: int = 3) -> list[tuple[float, float]]:
    ranges, start = [], None
    for i, v in enumerate(list(curve) + [0]):
        if v >= threshold and start is None:
            start = i
        elif v < threshold and start is not None:
            if i - start >= min_len:
                ranges.append((float(start), float(i)))
            start = None
    return ranges


def identify(path: Path, at: float, length: float = 12.0) -> dict | None:
    """Ask AudD which song this is. Returns {"artist","title"} or None. Needs AUDD_API_TOKEN."""
    if not config.AUDD_TOKEN:
        return None
    import httpx

    with tempfile.TemporaryDirectory() as td:
        snip = Path(td) / "snip.mp3"
        subprocess.run([need("ffmpeg"), "-v", "error", "-y", "-ss", f"{at:.2f}", "-t", f"{length:.1f}", "-i",
                        str(path), "-vn", "-ac", "1", "-ar", "44100", "-b:a", "128k", str(snip)], check=False)
        if not snip.exists():
            return None
        try:
            r = httpx.post("https://api.audd.io/", data={"api_token": config.AUDD_TOKEN, "return": ""},
                           files={"file": snip.read_bytes()}, timeout=40)
            res = (r.json() or {}).get("result")
        except Exception:  # noqa: BLE001
            return None
    if not res:
        return None
    return {"artist": res.get("artist", ""), "title": res.get("title", ""), "label": res.get("label", "")}


def analyse(path: Path) -> dict:
    audio = read_audio(path)
    curve = music_curve(audio)
    ranges = music_ranges(curve)
    total = max(1.0, len(audio) / SR)
    share = sum(e - s for s, e in ranges) / total
    song = None
    if ranges:
        s, e = max(ranges, key=lambda r: r[1] - r[0])
        song = identify(path, s, min(15.0, e - s + 2))
    return {"share": round(share, 3), "ranges": ranges, "song": song,
            "curve": [round(float(x), 2) for x in curve[:600]]}


# ---------------------------------------------------------------- removal

def can_separate() -> bool:
    try:
        import demucs  # noqa: F401
        return True
    except ImportError:
        return False


def strip_music(video: Path, out: Path, ranges: list[tuple[float, float]] | None = None) -> str:
    """Write `out` with the music taken out. Returns how it was done: "separated" (voice kept, music removed
    by demucs) or "muted" (music-only parts silenced)."""
    if can_separate():
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "a.wav"
            subprocess.run([need("ffmpeg"), "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "2", "-ar", "44100",
                            str(wav)], check=True)
            subprocess.run([sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "-o", td,
                            str(wav)], check=True, capture_output=True)
            voc = next(Path(td).rglob("vocals.wav"), None)
            if voc is None:
                raise RuntimeError("demucs did not produce a vocals track")
            subprocess.run([need("ffmpeg"), "-v", "error", "-y", "-i", str(video), "-i", str(voc), "-map", "0:v",
                            "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)],
                           check=True)
        return "separated"
    ranges = ranges or []
    if not ranges:
        shutil.copy(video, out)
        return "unchanged"
    cond = "+".join(f"between(t,{s:.2f},{e:.2f})" for s, e in ranges)
    subprocess.run([need("ffmpeg"), "-v", "error", "-y", "-i", str(video), "-c:v", "copy",
                    "-af", f"volume=enable='{cond}':volume=0", "-c:a", "aac", "-b:a", "192k", str(out)], check=True)
    return "muted"
