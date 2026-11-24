"""Turning a chosen moment into a finished 1080x1920 clip: reframe, word-by-word captions, hook, credit tag,
loudness to -14 LUFS (what Reels/Shorts/TikTok normalise to), fast-start MP4.

Captions follow what clipping editors do by hand: big bold words with a clean outline and no box, two or three
words at a time, the spoken word lit up, a short pop when a new line comes in and no movement after that.
They sit where the layout leaves room and slide off any face that is there at the time, so they never cover
a mouth.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config, reframe, styles
from .media import need

FONTS = Path(__file__).with_name("fonts")
HIGHLIGHT = "&H0000E1FF"   # ASS colours are &HAABBGGRR -> #FFE100, a warm yellow
HIGHLIGHT_B = "&H00FFD23C"  # second speaker: #3CD2FF, a clear sky blue
WHITE = "&H00FFFFFF"
INK = "&H00000000"

# per font: caption size (in ASS units: the font's whole height, so caps are ~half of it) and a fallback width ratio
FONT_METRICS = {"Anton": (156, 0.47), "Archivo Black": (138, 0.74), "Poppins ExtraBold": (180, 0.72)}
CAP_PX = 74               # how tall a capital letter is at 100% size, for fonts without a tuned size above
MAX_LINE_W = 900          # px a caption line may take on the 1080 wide screen
SAFE_TOP, SAFE_BOTTOM = 250, 1480
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
ARABIC = re.compile(r"[؀-ۿ]")
# words that get the "key word" colour: numbers, shouts and the usual hype words
KEY_WORDS = {"insane", "crazy", "no", "way", "what", "bro", "omg", "wow", "never", "always", "money", "free",
             "best", "worst", "first", "last", "biggest", "huge", "dead", "win", "won", "lost", "lose", "fire",
             "bhai", "pagal", "kya", "nahi", "sach", "paisa", "paise", "crore", "lakh"}
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
    if font in FONT_METRICS:
        return FONT_METRICS[font]
    m = _advances(font)
    if m and m.get("cap"):
        return int(round(CAP_PX * m["win"] / m["cap"])), 0.62
    return {"Noto Sans Devanagari": (150, 0.62), "Noto Naskh Arabic": (140, 0.55)}.get(font, (88, 0.62))


def _width(text: str, size: float, ratio: float, font: str | None = None) -> float:
    m = _advances(font or config.CAPTION_FONT)
    if m:
        adv, win = m["adv"], m["win"]
        miss = adv.get("N", win * 0.5)
        return sum(adv.get(ch, miss) for ch in text) * size / win
    return sum(size * (0.32 if ch == " " else ratio * (0.55 if ch in "IJ1.,'!:;" else 1.0)) for ch in text)


def _box_v(font: str, size: float, upper: bool) -> tuple[float, float]:
    """Where the ink of a line sits around the \\an5 anchor: (top, bottom) in px, from the font's own metrics."""
    m = _advances(font) or {}
    win = m.get("win") or 1
    asc, desc = m.get("asc", win * 0.8), m.get("desc", win * 0.2)
    cap = m.get("cap") or win * 0.5
    base = size * ((asc - desc) / 2) / win                 # baseline below the anchor
    top = base - size * cap / win
    bottom = base + (0 if upper else size * min(desc, 0.22 * win) / win * 0.8)
    return top, bottom


def _hex_ass(color: str, alpha: int = 0) -> str:
    """#RRGGBB -> &HAABBGGRR"""
    c = (color or "#FFFFFF").lstrip("#")
    return f"&H{alpha:02X}{c[4:6]}{c[2:4]}{c[0:2]}".upper()


def _script_font(text: str) -> str | None:
    if DEVANAGARI.search(text):
        return styles.SCRIPT_FONTS["devanagari"]
    if ARABIC.search(text):
        return styles.SCRIPT_FONTS["arabic"]
    return None


def _clean(word: str, upper: bool = True) -> str:
    """How a word is shown: upper case (unless the style says not), no trailing commas/full stops (? and ! stay)."""
    w = _esc(word).strip()
    if DEVANAGARI.search(w) or ARABIC.search(w):
        return w.rstrip(",.;:।")
    w = w.upper() if upper else w
    # the caption fonts are Latin: anything else would show as an empty box
    w = "".join(ch for ch in w if ord(ch) < 0x250 or ch in "’‘“”…")
    return w.rstrip(",.;:।") if len(w) > 1 else w


def _line(ws: list[dict], upper: bool = True) -> list[str]:
    """Words as shown, with split contractions ("Y" + "'ALL") put back together."""
    out = []
    for x in ws:
        t = _clean(x["w"], upper)
        if out and t[:1] in "'’":
            out[-1] += t
        elif t:
            out.append(t)
    return out


def _is_key(word: str) -> bool:
    w = word.strip("\"'“”‘’.,;:").lower()
    return bool(w) and (any(ch.isdigit() for ch in w) or word.rstrip("\"'”’").endswith("!")
                        or w.rstrip("?!") in KEY_WORDS)


def _groups(words: list[dict], size: int, ratio: float, per: int = 3, font: str | None = None) -> list[list[dict]]:
    """Two or three words a line: break on pauses and sentence ends, and before a line gets too wide."""
    out, cur = [], []
    for w in words:
        if cur:
            text = " ".join(x["w"] for x in cur + [w]).upper()
            if (len(cur) >= per or w["s"] - cur[-1]["e"] > 0.45 or
                    cur[-1]["w"].rstrip("\"')”’").endswith((".", "?", "!", "।", ",")) or
                    _width(text, size, ratio, font) > MAX_LINE_W):
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


def _wrap(text: str, size: float, ratio: float, max_w: float = MAX_LINE_W, font: str | None = None) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        nxt = f"{cur} {w}".strip()
        if cur and _width(nxt, size, ratio, font) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = nxt
    if cur:
        lines.append(cur)
    return lines


def _rrect(w: float, h: float, r: float) -> str:
    """An ASS vector drawing of a w x h rectangle with rounded corners (radius r), top-left at 0,0."""
    w, h = round(w), round(h)
    r = round(max(0.0, min(r, w / 2, h / 2)))
    if r < 1:
        return f"m 0 0 l {w} 0 l {w} {h} l 0 {h}"
    k = round(r * 0.45)
    return (f"m {r} 0 l {w - r} 0 b {w - k} 0 {w} {k} {w} {r} l {w} {h - r} b {w} {h - k} {w - k} {h} {w - r} {h} "
            f"l {r} {h} b {k} {h} 0 {h - k} 0 {h - r} l 0 {r} b 0 {k} {k} 0 {r} 0")


def _box_event(layer: int, t0: float, t1: float, cx: float, top: float, w: float, h: float, color: str,
               alpha: int = 0, extra: str = "", radius: float | None = None) -> str:
    r = h * 0.28 if radius is None else radius
    return (f"Dialogue: {layer},{_ts(t0)},{_ts(t1)},Box,,0,0,0,,{{\\an7\\pos({round(cx - w / 2)},{round(top)})"
            f"\\bord0\\shad0\\1c{_hex_ass(color)}\\1a&H{alpha:02X}&{extra}\\p1}}{_rrect(w, h, r)}")


HEAD_LOOK = {   # text colour, box colour (None: no box), outline scale, shadow, tilt
    "outline": ("#FFFFFF", None, 1.0, 4, 0),
    "white": ("#111111", "#FFFFFF", 0.0, 0, 0),
    "dark": ("#FFFFFF", "#111111", 0.0, 0, 0),
    "orange": ("#FFFFFF", "#FF6A1A", 0.0, 0, 0),
    "comic": ("#FFE100", None, 1.4, 7, -3),
    "banner": ("#111111", "#FFE100", 0.0, 0, 0),
    "plain": ("#FFFFFF", None, 0.0, 3, 0),
}


def build_ass(words: list[dict], start: float, end: float, hook: str, credit: str, plan: dict | int | None = None,
              src_w: int = 1920, src_h: int = 1080, callout: dict | None = None, style: dict | None = None) -> str:
    """Subtitles for one clip. `plan` is the reframe plan (an int is read as a fixed caption y, as before).
    `style` is a saved style (bcut.styles); without one, the default look."""
    st = styles.clean(style or styles.TEMPLATES[0])
    cs, hs = st["captions"], st["heading"]
    if isinstance(plan, int) or plan is None:
        spots, faces = {"cap": int(plan or 1300), "hook": 330}, []
        plan = {}
    else:
        # faces and any text already in the picture are both kept clear of
        spots, faces = reframe.text_spots(plan, src_w, src_h), (plan.get("faces_out") or []) + (plan.get("text_out") or [])
    if cs.get("y") is not None:
        spots["cap"] = int(cs["y"] * 1920)
    if hs.get("y") is not None:
        spots["hook"] = int(hs["y"] * 1920)
    spoken_text = " ".join(w["w"] for w in words if start - 0.05 <= w["s"] < end)
    font = _script_font(spoken_text) or cs["font"]
    upper = cs["upper"] and font == cs["font"]
    size, ratio = _metrics(font)
    size = int(round(size * cs["size"] / 100))
    outline = round(max(6, round(size / 15)) * cs["outline"] / 100)
    shadow = round(5 * cs["shadow"] / 100)
    text_c, lit_main, key_c = _hex_ass(cs["text"]), _hex_ass(cs["lit"]), _hex_ass(cs["key"])
    hfont = _script_font(hook or "") or hs["font"]
    hupper = hs["upper"] and hfont == hs["font"]
    hsize0, hratio = _metrics(hfont)
    hook_size = int(hsize0 * 0.7 * hs["size"] / 100)
    h_ink, h_box, h_out, h_shad, h_tilt = HEAD_LOOK[hs["look"]]
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},{text_c},{text_c},{INK},&H8C000000,0,0,0,0,100,100,0,0,1,{outline},{shadow},5,0,0,0,1
Style: Hook,{hfont},{hook_size},{_hex_ass(h_ink)},{_hex_ass(h_ink)},{INK},&H8C000000,0,0,0,0,100,100,0,0,1,{round(max(5, round(hook_size / 15)) * h_out)},{h_shad},5,60,60,0,1
Style: Box,{font},20,{WHITE},{WHITE},{INK},{INK},0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Credit,Poppins ExtraBold,32,&H22FFFFFF,&H22FFFFFF,&H88000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,9,40,40,70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    dur = end - start
    half_h = size * 0.3 + outline + 6          # caps are about 0.55 of the font size
    if cs["box"] == "line":
        half_h = size * 0.3 + 0.2 * size + 6
    hook_box, hook_hold = [], 0.0
    hook_text = " ".join(_clean(x, hupper) for x in (hook or "").split()) if hs["show"] else ""
    lines = _wrap(hook_text, hook_size, hratio, 860 if h_box else 900, hfont)[:3]
    if lines:
        hold = hook_hold = dur if hs["whole"] else min(3.0, max(1.8, dur * 0.2))
        line_h = hook_size * 0.34 + 8
        hh = line_h * len(lines)
        hw = max(_width(ln, hook_size, hratio, hfont) for ln in lines) / 2
        pad_x, pad_y = (0.36 * hook_size * 0.5, 0.22 * hook_size * 0.5) if h_box else (0, 0)
        hy = _place(spots["hook"], 0, hold, hh + pad_y, hw + pad_x, faces, None)
        fade_out = "\\fad(0,160)" if not hs["whole"] else ""
        if h_box:
            top, bottom = _box_v(hfont, hook_size, hupper)
            n = len(lines)
            lh = hook_size                       # libass stacks lines one font size apart
            # the ink of the block: first line's top to last line's bottom
            ink_top = hy - (n - 1) * lh / 2 + top
            ink_bot = hy + (n - 1) * lh / 2 + bottom
            bw = (reframe.OUT_W if hs["look"] == "banner" else 2 * hw + 2 * pad_x)
            bh = ink_bot - ink_top + 2 * pad_y
            ev.append(_box_event(2, 0, hold, 540, ink_top - pad_y, bw, bh, h_box,
                                 extra=f"\\fad(120,{0 if hs['whole'] else 160})",
                                 radius=0 if hs["look"] == "banner" else bh * 0.22 / max(1, n)))
            ev.append(f"Dialogue: 2,{_ts(0)},{_ts(hold)},Hook,,0,0,0,,"
                      f"{{\\an5\\pos(540,{hy})\\fad(120,{0 if hs['whole'] else 160})}}" + "\\N".join(lines))
        else:
            ev.append(f"Dialogue: 2,{_ts(0)},{_ts(hold)},Hook,,0,0,0,,"
                      f"{{\\an5\\pos(540,{hy})\\blur0.6{fade_out}\\alpha&HFF&\\fscx70\\fscy70"
                      + (f"\\frz{h_tilt}" if h_tilt else "") +
                      f"\\t(0,80,\\alpha&H00&)\\t(0,120,0.5,\\fscx106\\fscy106)\\t(120,220,1.5,\\fscx100\\fscy100)}}"
                      + "\\N".join(lines))
        # captions keep clear of the hook while it is up (treated like one more face)
        hook_box = [[t, int(540 - hw - pad_x), int(hy - hh - pad_y), int(540 + hw + pad_x), int(hy + hh + pad_y)]
                    for t in [i * 0.5 for i in range(int(hold / 0.5) + 1)] + [hold]]
    prev = None
    # the source already has its own subtitles: ours on top would read twice and look broken
    spoken = [] if plan.get("burned") or not cs["show"] else [w for w in words if start - 0.05 <= w["s"] < end]
    groups = [[x for x in g if _clean(x["w"], upper)] for g in _groups(spoken, size, ratio, cs["words"], font)]
    groups = [g for g in groups if g]
    anim = cs["anim"]
    btop, bbot = _box_v(font, size, upper)
    for gi, g in enumerate(groups):
        # a line leaves before the next one arrives: two lines are never on screen together
        nxt = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else end
        g_end = min(g[-1]["e"] + 0.25, nxt - 0.02, end)
        shown = _line(g, upper)
        # contractions were merged into one shown word: keep each shown word's first spoken timing
        times, k = [], 0
        for x in g:
            if k < len(shown) and not (times and _clean(x["w"], upper)[:1] in "'’"):
                times.append(x["s"])
                k += 1
        times = times[:len(shown)]
        while len(times) < len(shown):
            times.append(times[-1] if times else g[0]["s"])
        gap = _width(" ", size, ratio, font) + 0.08 * size    # room for the lit word to grow
        ws_px = [_width(w, size, ratio, font) for w in shown]
        total = sum(ws_px) + gap * (len(shown) - 1)
        fit = min(1.0, MAX_LINE_W / total) if total else 1.0     # one long word: shrink the line to fit
        g0 = times[0] - start
        y = _place(spots["cap"], g0, g_end - start, half_h * fit, total * fit / 2,
                   faces + (hook_box if g0 < hook_hold + 0.2 else []), prev)
        prev = y
        lit_c = lit_main
        if cs["lit"] == "#FFE100":     # the default yellow: a second speaker gets blue
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
        out_fade_any = nxt - g_end > 0.25
        if cs["box"] == "line":
            pad = 0.22 * size * fit
            ev.append(_box_event(0, g0, g_end - start, 540, y + btop * fit - pad, total * fit + 2.4 * pad,
                                 (bbot - btop) * fit + 2 * pad, cs["box_color"], alpha=0x18,
                                 extra="\\fad(80,90)" if out_fade_any else "\\fad(80,0)"))
        for k in range(len(shown)):
            s0 = times[k] - start
            s1 = (times[k + 1] if k + 1 < len(shown) else g_end) - start
            s1 = min(s1, g_end - start)
            if s1 - s0 < 0.02:
                continue
            first, last = k == 0, k == len(shown) - 1
            # fade out only into a pause; straight into the next line it's a clean cut
            out_fade = "\\fad(0,90)" if last and out_fade_any else ""
            if cs["box"] == "word":
                pad = 0.16 * size * fit
                ev.append(_box_event(0, s0, s1, xs[k], y + btop * fit * 1.1 - pad, ws_px[k] * fit * 1.1 + 2 * pad,
                                     (bbot - btop) * fit * 1.1 + 2 * pad, cs["box_color"],
                                     extra="\\alpha&H60&\\t(0,80,\\alpha&H00&)" if anim != "none" else ""))
            for j in range(len(shown)):
                pos = f"\\an5\\blur0.6{out_fade}"
                if first and anim == "pop":
                    # the whole line arrives together (quick rise and fade-in), centred
                    pos += f"\\move({xs[j]},{y + 18},{xs[j]},{y},0,140)"
                elif first and anim == "slide":
                    pos += f"\\move({xs[j]},{y + 60},{xs[j]},{y},0,200)"
                elif first and anim == "bounce":
                    pos += f"\\move({xs[j]},{y - 50},{xs[j]},{y},0,160)"
                else:
                    pos += f"\\pos({xs[j]},{y})"
                enter = "\\alpha&HFF&\\t(0,90,\\alpha&H00&)" if first and anim != "none" else ""
                if first and anim == "bounce":
                    enter += f"\\fscy{round(sc * 1.25)}\\t(100,170,\\fscy{round(sc * 0.88)})\\t(170,240,\\fscy{sc})"
                key = j != k and _is_key(shown[j]) and cs["key"] != cs["text"]
                color = f"\\c{key_c}" if key else ""
                if j == k:
                    # the word being said: lit, pops a little bigger, then settles
                    tag = f"{pos}{enter}\\c{lit_c}\\fscx{sc}\\fscy{sc}"
                    if anim in ("pop", "bounce"):
                        big = 1.18 if anim == "pop" else 1.26
                        tag += (f"\\t(0,90,0.5,\\fscx{round(sc * big)}\\fscy{round(sc * big)})"
                                f"\\t(90,180,1.6,\\fscx{round(sc * 1.1)}\\fscy{round(sc * 1.1)})")
                    elif anim == "slide":
                        tag += f"\\t(0,120,\\fscx{round(sc * 1.06)}\\fscy{round(sc * 1.06)})"
                else:
                    tag = f"{pos}{enter}{color}\\fscx{sc}\\fscy{sc}"
                ev.append(f"Dialogue: 1,{_ts(s0)},{_ts(s1)},Cap,,0,0,0,,{{{tag}}}{shown[j]}")
    if callout and callout.get("text") and callout.get("at"):
        # the freeze-frame callout: big, slightly tilted, slams in, like an editor pointing at the moment
        a, b = callout["at"]
        txt = " ".join(_clean(x) for x in str(callout["text"]).split())
        cfont = cs["font"]
        cs0, cratio = _metrics(cfont)
        csz = int(cs0 * 1.1)
        ln = _wrap(txt, csz, cratio, 860, cfont)[:2]
        if ln:
            ch = (csz * 0.32 + 8) * len(ln)
            cwid = max(_width(x, csz, cratio, cfont) for x in ln) / 2
            cy = _place(int(1920 * 0.36), a, b + 0.3, ch, cwid, faces, None)
            ev.append(f"Dialogue: 3,{_ts(a)},{_ts(b + 0.35)},Cap,,0,0,0,,"
                      f"{{\\an5\\pos(540,{cy})\\blur0.6\\frz4\\fn{cfont}\\bord{max(6, round(csz / 15))}"
                      f"\\c{lit_main}\\fs{csz}\\alpha&HFF&"
                      f"\\fscx150\\fscy150\\t(0,60,\\alpha&H00&)\\t(0,140,0.4,\\fscx96\\fscy96)"
                      f"\\t(140,220,\\fscx100\\fscy100)\\fad(0,120)}}" + "\\N".join(ln))
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
           fx: dict | None = None, grade: bool = False, style: dict | None = None) -> Path:
    """Render one clip. Works in a short temporary folder so Windows path limits and filter escaping can't bite.
    `fx` adds punch-ins ({"punches": [...]}) and sound hits ({"sounds": [...]}) from bcut.fx; `style` is how
    the captions and the heading look (bcut.styles)."""
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
        (work / "subs.ass").write_text(build_ass(words, start, end, hook, credit, plan, src_w, src_h,
                                                 callout=fx.get("callout"), style=style), encoding="utf-8")
        moves = reframe.commands(plan, dur, src_w, src_h)
        if moves:
            (work / "moves.txt").write_text(moves, encoding="utf-8")
        zoom = fxmod.zoom_filter(fx.get("punches") or [], _face_centre(plan))
        mask = reframe.corner_mask(plan, src_w, src_h, work / "mask.png") if reframe.needs_mask(plan) else None
        graph = (reframe.filter_graph(plan, src_w, src_h, has_cmds=bool(moves), mask_input=1 if mask else None) +
                 ";[v]" + ("eq=contrast=1.06:saturation=1.14:gamma=0.98," if grade else "") +
                 (zoom + "," if zoom else "") + "subtitles=subs.ass:fontsdir=fonts,fps=30,format=yuv420p[vo]")
        cmd = [need("ffmpeg"), "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
               "-i", str(src.resolve())]
        if mask:
            cmd += ["-loop", "1", "-framerate", "30", "-i", "mask.png"]
        first = 2 if mask else 1
        sounds = [s for s in (fx.get("sounds") or []) if s["t"] < dur - 0.1 and fxmod.sound(s["name"])]
        if has_audio:
            for snd in sounds:
                cmd += ["-i", str(fxmod.sound(snd["name"]))]
            fade_out = max(0.0, dur - 0.25)
            chain = f"[0:a:0]afade=t=in:d=0.06,afade=t=out:st={fade_out:.3f}:d=0.25,aresample=48000[voice]"
            mix = ["[voice]"]
            for i, snd in enumerate(sounds, start=first):
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
