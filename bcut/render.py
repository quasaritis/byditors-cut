"""Turning a chosen moment into a finished 1080x1920 clip: reframe, word-by-word captions, hook, credit tag,
loudness to -14 LUFS (what Reels/Shorts/TikTok normalise to), fast-start MP4.

Captions follow what clipping editors do by hand: big bold words with a clean outline and no box, two or three
words at a time, the spoken word lit up, a short pop when a new line comes in and no movement after that.
They sit where the layout leaves room and slide off any face that is there at the time, so they never cover
a mouth.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config, reframe
from .media import need

FONTS = Path(__file__).with_name("fonts")
HIGHLIGHT = "&H0000E1FF"   # ASS colours are &HAABBGGRR -> #FFE100, a warm yellow
WHITE = "&H00FFFFFF"
INK = "&H00000000"

# per font: caption size, and average width of an upper-case letter as a share of the size (for fitting)
FONT_METRICS = {"Anton": (104, 0.47), "Archivo Black": (78, 0.74), "Poppins ExtraBold": (80, 0.72)}
MAX_LINE_W = 940          # px a caption line may take on the 1080 wide screen
SAFE_TOP, SAFE_BOTTOM = 250, 1480


def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _metrics(font: str) -> tuple[int, float]:
    return FONT_METRICS.get(font, (88, 0.62))


def _width(text: str, size: float, ratio: float) -> float:
    return sum(size * (0.32 if ch == " " else ratio * (0.55 if ch in "IJ1.,'!:;" else 1.0)) for ch in text)


def _clean(word: str) -> str:
    """How a word is shown: upper case, no trailing commas/full stops (? and ! stay)."""
    w = _esc(word).strip().upper()
    # the caption fonts are Latin: anything else would show as an empty box
    w = "".join(ch for ch in w if ord(ch) < 0x250 or ch in "’‘“”…")
    return w.rstrip(",.;:।") if len(w) > 1 else w


def _line(ws: list[dict]) -> list[str]:
    """Words as shown, with split contractions ("Y" + "'ALL") put back together."""
    out = []
    for x in ws:
        t = _clean(x["w"])
        if out and t[:1] in "'’":
            out[-1] += t
        elif t:
            out.append(t)
    return out


def _groups(words: list[dict], size: int, ratio: float, per: int = 3) -> list[list[dict]]:
    """Two or three words a line: break on pauses and sentence ends, and before a line gets too wide."""
    out, cur = [], []
    for w in words:
        if cur:
            text = " ".join(x["w"] for x in cur + [w]).upper()
            if (len(cur) >= per or w["s"] - cur[-1]["e"] > 0.45 or
                    cur[-1]["w"].rstrip("\"')”’").endswith((".", "?", "!", "।", ",")) or
                    _width(text, size, ratio) > MAX_LINE_W):
                out.append(cur)
                cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def _place(pref: float, t0: float, t1: float, half_h: float, half_w: float, faces: list, prev: float | None,
           lo: int = SAFE_TOP, hi: int = SAFE_BOTTOM) -> int:
    """The y for a line of text shown from t0 to t1: as close to `pref` (and to where the last line was) as
    possible without touching a face on screen at that time. Faces count from forehead to below the chin."""
    blocks = []
    for t, x0, y0, x1, y1 in faces:
        if t0 - 0.3 <= t <= t1 + 0.3 and x1 > 540 - half_w - 20 and x0 < 540 + half_w + 20:
            fh = y1 - y0
            blocks.append((y0 - 0.12 * fh, y1 + 0.3 * fh, y0 + 0.5 * fh))   # top, bottom (chin), mouth line
    lo_c, hi_c = int(lo + half_h), int(hi - half_h)
    if hi_c <= lo_c:
        return int(pref)
    cands = sorted(set(range(lo_c, hi_c + 1, 10)) | {int(min(hi_c, max(lo_c, pref)))})
    gap = 14

    def cost(y):
        c = abs(y - pref)
        if prev is not None:
            c += 0.6 * abs(y - prev)
        return c

    free = [y for y in cands if all(y + half_h + gap <= a or y - half_h - gap >= b for a, b, _ in blocks)]
    if free:
        return min(free, key=cost)
    # nowhere is clear (a face fills the screen): stay off the mouth, cover as little face as possible
    def hit(y):
        top, bot = y - half_h, y + half_h
        mouth = sum(1 for a, b, m in blocks if top - gap < m + 0.25 * (b - a) and bot + gap > m - 0.1 * (b - a))
        over = sum(max(0.0, min(bot, b) - max(top, a)) for a, b, _ in blocks)
        return mouth * 10000 + over
    return min(cands, key=lambda y: (hit(y), cost(y)))


def _wrap(text: str, size: float, ratio: float, max_w: float = MAX_LINE_W) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        nxt = f"{cur} {w}".strip()
        if cur and _width(nxt, size, ratio) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = nxt
    if cur:
        lines.append(cur)
    return lines


def build_ass(words: list[dict], start: float, end: float, hook: str, credit: str, plan: dict | int | None = None,
              src_w: int = 1920, src_h: int = 1080) -> str:
    """Subtitles for one clip. `plan` is the reframe plan (an int is read as a fixed caption y, as before)."""
    if isinstance(plan, int) or plan is None:
        spots, faces = {"cap": int(plan or 1300), "hook": 330}, []
        plan = {}
    else:
        # faces and any text already in the picture are both kept clear of
        spots, faces = reframe.text_spots(plan, src_w, src_h), (plan.get("faces_out") or []) + (plan.get("text_out") or [])
    font = config.CAPTION_FONT
    size, ratio = _metrics(font)
    hook_size = int(size * 0.78)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},{WHITE},{WHITE},{INK},&H00000000,0,0,0,0,100,100,1.5,0,1,{max(6, size // 13)},0,5,40,40,0,1
Style: Hook,{font},{hook_size},{WHITE},{WHITE},{INK},&H00000000,0,0,0,0,100,100,1,0,1,{max(5, hook_size // 13)},0,5,60,60,0,1
Style: Credit,Poppins ExtraBold,32,&H22FFFFFF,&H22FFFFFF,&H88000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,9,40,40,70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    dur = end - start
    half_h = size * 0.62
    hook_box, hook_hold = [], 0.0
    lines = _wrap(" ".join(_clean(x) for x in (hook or "").split()), hook_size, ratio, 900)[:3]
    if lines:
        hold = hook_hold = min(3.0, max(1.8, dur * 0.2))
        hh = hook_size * 0.62 * len(lines)
        hw = max(_width(ln, hook_size, ratio) for ln in lines) / 2
        hy = _place(spots["hook"], 0, hold, hh, hw, faces, None)
        ev.append(f"Dialogue: 2,{_ts(0)},{_ts(hold)},Hook,,0,0,0,,"
                  f"{{\\an5\\pos(540,{hy})\\fad(60,200)\\fscx85\\fscy85\\t(0,140,\\fscx100\\fscy100)}}"
                  + "\\N".join(lines))
        # captions keep clear of the hook while it is up (treated like one more face)
        hook_box = [[t, int(540 - hw), int(hy - hh), int(540 + hw), int(hy + hh)] for t in (0.0, hold / 2, hold)]
    prev = None
    # the source already has its own subtitles: ours on top would read twice and look broken
    spoken = [] if plan.get("burned") else [w for w in words if start - 0.05 <= w["s"] < end]
    for g in _groups(spoken, size, ratio):
        g_end = min(g[-1]["e"] + 0.2, end)
        g = [x for x in g if _clean(x["w"])]
        if not g:
            continue
        line = " ".join(_line(g))
        half_w = min(MAX_LINE_W, _width(line, size, ratio)) / 2
        g0 = g[0]["s"] - start
        y = _place(spots["cap"], g0, g_end - start, half_h, half_w,
                   faces + (hook_box if g0 < hook_hold + 0.2 else []), prev)
        prev = y
        for i, w in enumerate(g):
            s = w["s"] - start
            e = (g[i + 1]["s"] if i + 1 < len(g) else g_end) - start
            if e <= s:
                continue
            parts = []
            for j, x in enumerate(g):
                word = _clean(x["w"])
                lit = f"{{\\c{HIGHLIGHT}}}{word}{{\\c{WHITE}}}" if j == i else word
                if parts and word[:1] in "'’":
                    parts[-1] += lit
                else:
                    parts.append(lit)
            # a new line pops in once (small overshoot, 0.18 s); after that only the lit word changes
            pop = ("\\fad(40,0)\\fscx78\\fscy78\\t(0,110,\\fscx105\\fscy105)\\t(110,180,\\fscx100\\fscy100)"
                   if i == 0 else "")
            ev.append(f"Dialogue: 1,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{{\\an5\\pos(540,{y}){pop}}}" + " ".join(parts))
    if credit:
        ev.append(f"Dialogue: 0,{_ts(0)},{_ts(dur)},Credit,,0,0,0,,{_esc(credit)}")
    return head + "\n".join(ev) + "\n"


def render(src: Path, out: Path, start: float, end: float, plan: dict, src_w: int, src_h: int,
           words: list[dict], hook: str = "", credit: str = "", has_audio: bool = True) -> Path:
    """Render one clip. Works in a short temporary folder so Windows path limits and filter escaping can't bite."""
    out.parent.mkdir(parents=True, exist_ok=True)
    dur = end - start
    with tempfile.TemporaryDirectory(prefix="bcut") as td:
        work = Path(td)
        fonts = work / "fonts"
        fonts.mkdir()
        for f in FONTS.glob("*.ttf"):
            shutil.copy(f, fonts / f.name)
        (work / "subs.ass").write_text(build_ass(words, start, end, hook, credit, plan, src_w, src_h),
                                       encoding="utf-8")
        moves = reframe.commands(plan, dur, src_w, src_h)
        if moves:
            (work / "moves.txt").write_text(moves, encoding="utf-8")
        graph = (reframe.filter_graph(plan, src_w, src_h, has_cmds=bool(moves)) +
                 ";[v]subtitles=subs.ass:fontsdir=fonts,fps=30,format=yuv420p[vo]")
        cmd = [need("ffmpeg"), "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
               "-i", str(src.resolve()), "-filter_complex", graph, "-map", "[vo]"]
        if has_audio:
            fade_out = max(0.0, dur - 0.25)
            cmd += ["-map", "0:a:0", "-af", f"afade=t=in:d=0.06,afade=t=out:st={fade_out:.3f}:d=0.25,"
                                           "loudnorm=I=-14:TP=-1.5:LRA=11",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
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
