"""Downloading, probing and reading audio. Everything here shells out to ffmpeg/ffprobe or uses yt-dlp."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

SR = 16000  # audio is analysed as 16 kHz mono

PLATFORMS = {
    "kick.com": "kick",
    "twitch.tv": "twitch",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "facebook.com": "facebook",
    "rumble.com": "rumble",
}


def platform_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for dom, name in PLATFORMS.items():
        if host == dom or host.endswith("." + dom):
            return name
    return "web"


def need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise RuntimeError(f"{tool} is not installed or not on PATH. Install ffmpeg (it includes ffprobe).")
    return path


def run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError(f"{Path(cmd[0]).name} failed: " + " | ".join(tail))
    return p


def probe(path: Path) -> dict:
    out = run([need("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]).stdout
    info = json.loads(out)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    w, h = (int(v["width"]), int(v["height"])) if v else (0, 0)
    rot = 0
    if v:
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = abs(int(sd["rotation"])) % 180
        if rot == 90:
            w, h = h, w
    fps = 30.0
    if v and v.get("avg_frame_rate", "0/0") != "0/0":
        n, d = v["avg_frame_rate"].split("/")
        fps = float(n) / float(d) if float(d) else 30.0
    return {"duration": float(info["format"].get("duration", 0) or 0), "width": w, "height": h,
            "fps": fps, "has_audio": a is not None, "size": int(info["format"].get("size", 0) or 0)}


def download(url: str, out_dir: Path, log=print) -> tuple[Path, dict]:
    """Download a video with yt-dlp. Returns the file and yt-dlp's info (title, uploader, id...)."""
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)

    def hook(d):
        if d.get("status") == "downloading" and d.get("total_bytes"):
            log(f"download {100 * d.get('downloaded_bytes', 0) / d['total_bytes']:.0f}%")

    opts = {
        "outtmpl": str(out_dir / "source.%(ext)s"),
        # H.264 + AAC first: every tool (and OpenCV's face tracking) can read it. YouTube's default is often AV1,
        # which many PCs can't decode without a GPU.
        "format": ("bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/bv*[vcodec^=avc1][height<=1080]+ba/"
                   "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "progress_hooks": [hook],
        "retries": 5,
        "fragment_retries": 10,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = sorted(out_dir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("Download finished but no file was written.")
    keep = {k: info.get(k) for k in ("id", "title", "uploader", "channel", "uploader_id", "webpage_url",
                                     "duration", "license", "description", "tags", "is_live", "extractor_key")}
    keep["description"] = (keep.get("description") or "")[:3000]
    return files[0], keep


def read_audio(path: Path, start: float = 0.0, duration: float | None = None) -> np.ndarray:
    """Decode audio to 16 kHz mono float32."""
    cmd = [need("ffmpeg"), "-v", "error", "-ss", f"{start:.3f}"]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


def scene_cuts(path: Path, threshold: float = 0.32, max_seconds: float | None = None) -> list[float]:
    """Times (seconds) where the picture changes hard. Uses a small, fast decode."""
    cmd = [need("ffmpeg"), "-v", "info", "-nostats"]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-i", str(path), "-an", "-vf", f"scale=160:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", p.stderr)]


def frame_at(path: Path, t: float, out: Path, width: int = 640) -> Path:
    run([need("ffmpeg"), "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
         "-vf", f"scale={width}:-2", str(out)])
    return out


def frames(path: Path, times: list[float], width: int = 480) -> list[tuple[float, np.ndarray]]:
    """Small BGR frames at evenly spaced `times` (for face tracking), decoded by ffmpeg in one pass.
    ffmpeg reads every codec yt-dlp can hand us (AV1 and VP9 included); OpenCV's own reader often can't."""
    if not times:
        return []
    start = max(0.0, times[0])
    step = (times[-1] - times[0]) / (len(times) - 1) if len(times) > 1 else 1.0
    step = max(step, 0.05)
    info = probe(path)
    if not info["width"]:
        return []
    h = int(round(info["height"] * width / info["width"] / 2) * 2)
    cmd = [need("ffmpeg"), "-v", "error", "-ss", f"{start:.3f}", "-t", f"{step * len(times) + 0.05:.3f}",
           "-i", str(path), "-an", "-vf", f"fps=1/{step:.4f},scale={width}:{h}", "-frames:v", str(len(times)),
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    p = subprocess.run(cmd, capture_output=True)
    size = width * h * 3
    raw = p.stdout
    got = []
    for i in range(len(raw) // size):
        img = np.frombuffer(raw[i * size:(i + 1) * size], dtype=np.uint8).reshape(h, width, 3).copy()
        got.append((start + i * step, img))
    return got
