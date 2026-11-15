"""Cheap, local signals that say "something is happening here": loudness, sudden bursts (screams, laughs,
crowd), picture cuts and chat spikes. They work with no AI and no transcript, and they steer the AI when it is there.
"""
from __future__ import annotations

import re

import numpy as np

from .media import SR

HOP = 0.5  # seconds per analysis step

HYPE_EMOTES = re.compile(
    r"\b(kekw|lul|lmao+|lmfao|omegalul|pog+|poggers|pogchamp|w+|letsgo+|lets go+|clip( it| that)?|"
    r"wtf|no way|bro+|hahaha+|haha+|xd+|💀|😂|🤣|🔥|insane|crazy)\b",
    re.I,
)


def loudness(audio: np.ndarray) -> np.ndarray:
    """dB level for every 0.5 s step."""
    step = int(SR * HOP)
    n = len(audio) // step
    if n == 0:
        return np.zeros(0)
    frames = audio[: n * step].reshape(n, step)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-10)
    return 20 * np.log10(rms + 1e-10)


def zscore(x: np.ndarray, window: int = 240) -> np.ndarray:
    """How unusual each step is compared with the surrounding two minutes (robust: median and MAD)."""
    if len(x) == 0:
        return x
    out = np.zeros_like(x, dtype=float)
    half = window // 2
    for i in range(len(x)):
        seg = x[max(0, i - half): i + half + 1]
        med = np.median(seg)
        mad = np.median(np.abs(seg - med)) * 1.4826 + 1e-6
        out[i] = (x[i] - med) / mad
    return np.clip(out, -5, 8)


def bursts(audio: np.ndarray) -> np.ndarray:
    """Spectral flux per 0.5 s step, as a z-score: high when the sound changes abruptly (laughs, screams, hits)."""
    return zscore(np.log1p(_flux(audio)))


def scene_density(cuts: list[float], steps: int) -> np.ndarray:
    d = np.zeros(steps)
    for c in cuts:
        i = int(c / HOP)
        if 0 <= i < steps:
            d[max(0, i - 4): i + 5] += 1
    return d


def chat_velocity(messages: list[tuple[float, str]], steps: int) -> np.ndarray:
    """Messages per step, with hype emotes counted triple. `messages` are (seconds, text)."""
    v = np.zeros(steps)
    for t, text in messages:
        i = int(t / HOP)
        if 0 <= i < steps:
            v[i] += 3 if HYPE_EMOTES.search(text or "") else 1
    if steps == 0:
        return v
    kernel = np.ones(10) / 10
    return np.convolve(v, kernel, mode="same")


def combined(audio: np.ndarray, cuts: list[float] | None = None,
             chat: list[tuple[float, str]] | None = None) -> dict:
    """All signals on one 0.5 s grid plus a single `excite` curve (0..~1) used to rank moments."""
    db = loudness(audio)
    steps = len(db)
    lz = zscore(db)
    bz = bursts(audio)[:steps] if steps else np.zeros(0)
    if len(bz) < steps:
        bz = np.pad(bz, (0, steps - len(bz)))
    sd = scene_density(cuts or [], steps)
    cv = chat_velocity(chat or [], steps)
    cz = zscore(cv) if chat else np.zeros(steps)
    return _mix(db, lz, bz, sd, cz)


def peaks(excite: np.ndarray, n: int, min_gap_sec: float = 45.0) -> list[float]:
    """The n strongest moments, at least `min_gap_sec` apart, as times in seconds."""
    order = np.argsort(excite)[::-1]
    picked: list[int] = []
    gap = int(min_gap_sec / HOP)
    for i in order:
        if excite[i] <= 0.05:
            break
        if all(abs(i - j) >= gap for j in picked):
            picked.append(int(i))
        if len(picked) >= n:
            break
    return [i * HOP for i in sorted(picked)]


def mean_between(curve: np.ndarray, s: float, e: float) -> float:
    a, b = int(s / HOP), max(int(s / HOP) + 1, int(e / HOP))
    seg = curve[a:b]
    return float(seg.mean()) if len(seg) else 0.0


def from_file(path, duration: float, cuts: list[float] | None = None,
              chat: list[tuple[float, str]] | None = None, chunk: float = 600.0) -> dict:
    """Same as `combined`, but reads a long file ten minutes at a time so a 6-hour stream fits in memory."""
    from .media import read_audio

    dbs, flux = [], []
    t = 0.0
    while t < max(duration, 0.1):
        a = read_audio(path, t, chunk)
        if len(a) == 0:
            break
        dbs.append(loudness(a))
        flux.append(_flux(a))
        t += chunk
    db = np.concatenate(dbs) if dbs else np.zeros(0)
    fl = np.concatenate(flux)[: len(db)] if flux else np.zeros(0)
    steps = len(db)
    if len(fl) < steps:
        fl = np.pad(fl, (0, steps - len(fl)))
    lz, bz = zscore(db), zscore(np.log1p(fl))
    sd = scene_density(cuts or [], steps)
    cv = chat_velocity(chat or [], steps)
    cz = zscore(cv) if chat else np.zeros(steps)
    return _mix(db, lz, bz, sd, cz)


def _flux(audio: np.ndarray) -> np.ndarray:
    step = int(SR * HOP)
    n = len(audio) // step
    win = 1024
    out = np.zeros(max(n, 0))
    prev = None
    for i in range(n):
        chunk = audio[i * step: i * step + win]
        if len(chunk) < win:
            chunk = np.pad(chunk, (0, win - len(chunk)))
        spec = np.abs(np.fft.rfft(chunk * np.hanning(win)))
        if prev is not None:
            out[i] = np.sum(np.maximum(spec - prev, 0))
        prev = spec
    return out


def _mix(db, lz, bz, sd, cz) -> dict:
    score = 0.45 * np.clip(lz, 0, None) + 0.30 * np.clip(bz, 0, None) + 0.10 * np.minimum(sd, 3) \
        + 0.45 * np.clip(cz, 0, None)
    if len(score):
        score = np.convolve(score, np.ones(6) / 6, mode="same")
        top = np.percentile(score, 99) or 1.0
        score = np.clip(score / top, 0, 1.5)
    return {"db": db, "loud_z": lz, "burst_z": bz, "scene": sd, "chat_z": cz, "excite": score}
