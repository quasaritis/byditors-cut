"""Turning a chosen moment into a finished 1080x1920 clip: reframe, word-by-word captions, hook, credit tag,
loudness to -14 LUFS (what Reels/Shorts/TikTok normalise to), fast-start MP4.

Captions follow what clipping editors do by hand: big bold words with a clean outline and no box, two or three
words at a time, the spoken word lit up, a short pop when a new line comes in and no movement after that.
They sit where the layout leaves room and slide off any face that is there at the time, so they never cover
a mouth.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config, reframe
from .media import need

FONTS = Path(__file__).with_name("fonts")
HIGHLIGHT = "&H0000E1FF"   # ASS colours are &HAABBGGRR -> #FFE100, a warm yellow
HIGHLIGHT_B = "&H00FFD23C"  # second speaker: #3CD2FF, a clear sky blue
WHITE = "&H00FFFFFF"
INK = "&H00000000"

# per font: caption size (in ASS units: the font's whole height, so caps are ~half of it) and a fallback width ratio
FONT_METRICS = {"Anton": (156, 0.47), "Archivo Black": (138, 0.74), "Poppins ExtraBold": (180, 0.72)}
MAX_LINE_W = 900          # px a caption line may take on the 1080 wide screen
SAFE_TOP, SAFE_BOTTOM = 250, 1480
_ADV: dict | None = None


def _advances(font: str) -> dict | None:
    """Real glyph widths of the bundled fonts (measured once from the font files), so every word can be placed
    exactly where libass would draw it."""
    global _ADV
    if _ADV is None:
        try:
            _ADV = json.loads((FONTS / "metrics.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _ADV = {}
    return _ADV.get(font)


def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _metrics(font: str) -> tuple[int, float]:
    return FONT_METRICS.get(font, (88, 0.62))


def _width(text: str, size: float, ratio: float, font: str | None = None) -> float:
    m = _advances(font or config.CAPTION_FONT)
    if m:
        adv, win = m["adv"], m["win"]
        miss = adv.get("N", win * 0.5)
        return sum(adv.get(ch, miss) for ch in text) * size / win
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
    hook_size = int(size * 0.7)
    outline = max(6, round(size / 15))
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},{WHITE},{WHITE},{INK},&H8C000000,0,0,0,0,100,100,0,0,1,{outline},5,5,0,0,0,1
Style: Hook,{font},{hook_size},{WHITE},{WHITE},{INK},&H8C000000,0,0,0,0,100,100,0,0,1,{max(5, round(hook_size / 15))},4,5,60,60,0,1
Style: Credit,Poppins ExtraBold,32,&H22FFFFFF,&H22FFFFFF,&H88000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,9,40,40,70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    dur = end - start
    half_h = size * 0.3 + outline + 6          # caps are about 0.55 of the font size
    hook_box, hook_hold = [], 0.0
    lines = _wrap(" ".join(_clean(x) for x in (hook or "").split()), hook_size, ratio, 900)[:3]
    if lines:
        hold = hook_hold = min(3.0, max(1.8, dur * 0.2))
        hh = (hook_size * 0.34 + 8) * len(lines)
        hw = max(_width(ln, hook_size, ratio) for ln in lines) / 2
        hy = _place(spots["hook"], 0, hold, hh, hw, faces, None)
        ev.append(f"Dialogue: 2,{_ts(0)},{_ts(hold)},Hook,,0,0,0,,"
                  f"{{\\an5\\pos(540,{hy})\\blur0.6\\fad(0,160)\\alpha&HFF&\\fscx70\\fscy70"
                  f"\\t(0,80,\\alpha&H00&)\\t(0,120,0.5,\\fscx106\\fscy106)\\t(120,220,1.5,\\fscx100\\fscy100)}}"
                  + "\\N".join(lines))
        # captions keep clear of the hook while it is up (treated like one more face)
        hook_box = [[t, int(540 - hw), int(hy - hh), int(540 + hw), int(hy + hh)] for t in (0.0, hold / 2, hold)]
    prev = None
    # the source already has its own subtitles: ours on top would read twice and look broken
    spoken = [] if plan.get("burned") else [w for w in words if start - 0.05 <= w["s"] < end]
    groups = [[x for x in g if _clean(x["w"])] for g in _groups(spoken, size, ratio)]
    groups = [g for g in groups if g]
    for gi, g in enumerate(groups):
        # a line leaves before the next one arrives: two lines are never on screen together
        nxt = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else end
        g_end = min(g[-1]["e"] + 0.25, nxt - 0.02, end)
        shown = _line(g)
        # contractions were merged into one shown word: keep each shown word's first spoken timing
        times, k = [], 0
        for x in g:
            if k < len(shown) and not (times and _clean(x["w"])[:1] in "'’"):
                times.append(x["s"])
                k += 1
        times = times[:len(shown)]
        while len(times) < len(shown):
            times.append(times[-1] if times else g[0]["s"])
        gap = _width(" ", size, ratio) + 0.08 * size    # room for the lit word to grow
        ws_px = [_width(w, size, ratio) for w in shown]
        total = sum(ws_px) + gap * (len(shown) - 1)
        fit = min(1.0, MAX_LINE_W / total) if total else 1.0     # one long word: shrink the line to fit
        g0 = times[0] - start
        y = _place(spots["cap"], g0, g_end - start, half_h * fit, total * fit / 2,
                   faces + (hook_box if g0 < hook_hold + 0.2 else []), prev)
        prev = y
        lit_c = HIGHLIGHT
        for t0, t1, who in plan.get("speakers") or []:
            if t0 <= g0 + 0.05 < t1:
                lit_c = HIGHLIGHT_B if who == "b" else HIGHLIGHT
                break
        # every word has its own spot, so a word can pop without nudging its neighbours
        xs, cx = [], 540 - total * fit / 2
        for wpx in ws_px:
            xs.append(round(cx + wpx * fit / 2))
            cx += (wpx + gap) * fit
        sc = round(100 * fit)
        for k in range(len(shown)):
            s0 = times[k] - start
            s1 = (times[k + 1] if k + 1 < len(shown) else g_end) - start
            s1 = min(s1, g_end - start)
            if s1 - s0 < 0.02:
                continue
            first, last = k == 0, k == len(shown) - 1
            # fade out only into a pause; straight into the next line it's a clean cut
            out_fade = "\\fad(0,90)" if last and nxt - g_end > 0.25 else ""
            for j in range(len(shown)):
                pos = f"\\an5\\blur0.6{out_fade}"
                # the whole line arrives together (quick rise and fade-in), centred
                pos += f"\\move({xs[j]},{y + 18},{xs[j]},{y},0,140)" if first else f"\\pos({xs[j]},{y})"
                enter = "\\alpha&HFF&\\t(0,90,\\alpha&H00&)" if first else ""
                if j == k:
                    # the word being said: lit, pops a little bigger, then settles
                    tag = (f"{pos}{enter}\\c{lit_c}\\fscx{sc}\\fscy{sc}"
                           f"\\t(0,90,0.5,\\fscx{round(sc * 1.18)}\\fscy{round(sc * 1.18)})"
                           f"\\t(90,180,1.6,\\fscx{round(sc * 1.1)}\\fscy{round(sc * 1.1)})")
                else:
                    tag = f"{pos}{enter}\\fscx{sc}\\fscy{sc}"
                ev.append(f"Dialogue: 1,{_ts(s0)},{_ts(s1)},Cap,,0,0,0,,{{{tag}}}{shown[j]}")
    if credit:
        ev.append(f"Dialogue: 0,{_ts(0)},{_ts(dur)},Credit,,0,0,0,,{_esc(credit)}")
    return head + "\n".join(ev) + "\n"


def _face_centre(plan: dict):
    """Where to push in at time t: the biggest face on screen then (as a share of the frame), else the middle."""
    faces = plan.get("faces_out") or []

    def at(t: float) -> tuple[float, float]:
        near = [f for f in faces if abs(f[0] - t) <= 0.4]
        if not near:
            return 0.5, 0.42
        f = max(near, key=lambda f: (f[3] - f[1]) * (f[4] - f[2]))
        return (min(0.9, max(0.1, (f[1] + f[3]) / 2 / 1080)), min(0.85, max(0.1, (f[2] + f[4]) / 2 / 1920)))
    return at


def render(src: Path, out: Path, start: float, end: float, plan: dict, src_w: int, src_h: int,
           words: list[dict], hook: str = "", credit: str = "", has_audio: bool = True,
           fx: dict | None = None) -> Path:
    """Render one clip. Works in a short temporary folder so Windows path limits and filter escaping can't bite.
    `fx` adds punch-ins ({"punches": [...]}) and sound hits ({"sounds": [...]}) from bcut.fx."""
    from . import fx as fxmod

    out.parent.mkdir(parents=True, exist_ok=True)
    dur = end - start
    fx = fx or {}
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
        zoom = fxmod.zoom_filter(fx.get("punches") or [], _face_centre(plan))
        graph = (reframe.filter_graph(plan, src_w, src_h, has_cmds=bool(moves)) +
                 ";[v]" + (zoom + "," if zoom else "") + "subtitles=subs.ass:fontsdir=fonts,fps=30,format=yuv420p[vo]")
        cmd = [need("ffmpeg"), "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
               "-i", str(src.resolve())]
        sounds = [s for s in (fx.get("sounds") or []) if s["t"] < dur - 0.1 and fxmod.sound(s["name"])]
        if has_audio:
            for snd in sounds:
                cmd += ["-i", str(fxmod.sound(snd["name"]))]
            fade_out = max(0.0, dur - 0.25)
            chain = f"[0:a:0]afade=t=in:d=0.06,afade=t=out:st={fade_out:.3f}:d=0.25,aresample=48000[voice]"
            mix = ["[voice]"]
            for i, snd in enumerate(sounds, start=1):
                ms = int(snd["t"] * 1000)
                chain += (f";[{i}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms},"
                          f"volume={snd['db']}dB[s{i}]")
                mix.append(f"[s{i}]")
            if len(mix) > 1:
                chain += ";" + "".join(mix) + f"amix=inputs={len(mix)}:normalize=0:duration=first[mixed]"
            else:
                chain += ";[voice]anull[mixed]"
            chain += ";[mixed]loudnorm=I=-14:TP=-1.5:LRA=11[ao]"
            graph += ";" + chain
        cmd += ["-filter_complex", graph, "-map", "[vo]"]
        if has_audio:
            cmd += ["-map", "[ao]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high", "-movflags",
                "+faststart", "-t", f"{dur:.3f}", "tmp.mp4"]
        p = subprocess.run(cmd, cwd=work, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError("Render failed: " + " | ".join(p.stderr.strip().splitlines()[-4:]))
        shutil.move(str(work / "tmp.mp4"), out)
    return out


def thumbnail(clip: Path, out: Path, at: float = 1.0) -> Path:
    subprocess.run([need("ffmpeg"), "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", str(clip), "-frames:v", "1",
                    "-vf", "scale=360:-2", "-q:v", "4", str(out)], check=False)
    return out
