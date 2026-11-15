"""Turning a chosen moment into a finished 1080x1920 clip: reframe, word-by-word captions, hook, credit tag,
loudness to -14 LUFS (what Reels/Shorts/TikTok normalise to), fast-start MP4.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config, reframe
from .media import need

FONTS = Path(__file__).with_name("fonts")
HIGHLIGHT = "&H0000E5FF"   # ASS colours are &HAABBGGRR -> warm yellow
WHITE = "&H00FFFFFF"
INK = "&H00101010"


def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _groups(words: list[dict], per: int = 3) -> list[list[dict]]:
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= per or w["s"] - cur[-1]["e"] > 0.6 or cur[-1]["w"].endswith((".", "?", "!"))):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def build_ass(words: list[dict], start: float, end: float, hook: str, credit: str, cap_y: int) -> str:
    font = config.CAPTION_FONT
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},92,{WHITE},{WHITE},{INK},&H66000000,0,0,0,0,100,100,1,0,1,7,3,5,60,60,0,1
Style: Hook,{font},84,{INK},{INK},&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,1,0,3,22,0,8,90,90,170,1
Style: Credit,Poppins ExtraBold,34,&H33FFFFFF,&H33FFFFFF,&H99000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,9,40,40,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    dur = end - start
    for g in _groups([w for w in words if start - 0.05 <= w["s"] < end]):
        g_end = min(g[-1]["e"] + 0.15, dur + start)
        for i, w in enumerate(g):
            s = w["s"] - start
            e = (g[i + 1]["s"] if i + 1 < len(g) else g_end) - start
            if e <= s:
                continue
            parts = []
            for j, x in enumerate(g):
                word = _esc(x["w"]).upper()
                if j == i:
                    parts.append(f"{{\\c{HIGHLIGHT}\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)}}{word}{{\\c{WHITE}}}")
                else:
                    parts.append(word)
            pop = "\\fad(40,0)" if i == 0 else ""
            ev.append(f"Dialogue: 1,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{{\\pos(540,{cap_y}){pop}}}" + " ".join(parts))
    if hook:
        hold = min(3.2, max(1.8, dur * 0.2))
        ev.append(f"Dialogue: 2,{_ts(0)},{_ts(hold)},Hook,,0,0,0,,"
                  f"{{\\fad(0,220)\\fscx88\\fscy88\\t(0,160,\\fscx100\\fscy100)}}{_esc(hook).upper()}")
    if credit:
        ev.append(f"Dialogue: 0,{_ts(0)},{_ts(dur)},Credit,,0,0,0,,{_esc(credit)}")
    return head + "\n".join(ev) + "\n"


def render(src: Path, out: Path, start: float, end: float, plan: dict, src_w: int, src_h: int,
           words: list[dict], hook: str = "", credit: str = "", has_audio: bool = True) -> Path:
    """Render one clip. Works in a short temporary folder so Windows path limits and filter escaping can't bite."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bcut") as td:
        work = Path(td)
        fonts = work / "fonts"
        fonts.mkdir()
        for f in FONTS.glob("*.ttf"):
            shutil.copy(f, fonts / f.name)
        (work / "subs.ass").write_text(build_ass(words, start, end, hook, credit, reframe.caption_y(plan)),
                                       encoding="utf-8")
        graph = reframe.filter_graph(plan, src_w, src_h) + ";[v]subtitles=subs.ass:fontsdir=fonts,fps=30,format=yuv420p[vo]"
        cmd = [need("ffmpeg"), "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
               "-i", str(src.resolve()), "-filter_complex", graph, "-map", "[vo]"]
        if has_audio:
            cmd += ["-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k",
                    "-ar", "48000"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high", "-movflags",
                "+faststart", "tmp.mp4"]
        p = subprocess.run(cmd, cwd=work, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError("Render failed: " + " | ".join(p.stderr.strip().splitlines()[-4:]))
        shutil.move(str(work / "tmp.mp4"), out)
    return out


def thumbnail(clip: Path, out: Path, at: float = 1.0) -> Path:
    subprocess.run([need("ffmpeg"), "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", str(clip), "-frames:v", "1",
                    "-vf", "scale=360:-2", "-q:v", "4", str(out)], check=False)
    return out
