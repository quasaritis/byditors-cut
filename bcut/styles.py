"""How clips look: saved styles for the captions, the heading (the hook on top) and the layout.

A style is three groups of settings:
- captions: preset, font, size, words per line, colours (text, the word being said, key words, the box
            behind the words), outline, shadow, animation, and where on screen (empty = decided per clip)
- heading:  the hook shown on top: whether to show it, its look (white box, dark box, outline, ...), font,
            size, where it sits and how long it stays
- layout:   how the video fills the vertical screen (smart, whole frame on blur, zoomed in, fill the screen),
            where the streamer's cam goes (top half, bottom half, a corner box with rounded corners and a
            border), how the cam is found (AI, or a box you drew) and which part of the video to show

Built-in templates can't be changed, only copied. Your own styles are stored in the data folder. One style is
the default; a streamer or a single video can use another.
"""
from __future__ import annotations

import copy
import re
import time

from . import config, db

FONTS = ["Anton", "Poppins ExtraBold", "Poppins Black", "Archivo Black", "Lilita One", "Titan One", "Bangers",
         "Righteous", "Permanent Marker"]
# scripts the Latin caption fonts can't draw get these instead
SCRIPT_FONTS = {"devanagari": "Noto Sans Devanagari", "arabic": "Noto Naskh Arabic"}

ANIMS = {"pop": "Pop", "bounce": "Bounce", "slide": "Slide up", "none": "None"}
BOXES = {"none": "No box", "line": "Box behind the line", "word": "Box behind the word being said"}
FRAMINGS = {"smart": "Smart (follow faces)", "blur": "Full + blur", "zoom": "Zoomed in", "fill": "Fill the screen"}
CAM_SPOTS = {"top": "Top half", "bottom": "Bottom half", "corner": "Corner", "off": "No cam"}
HEAD_LOOKS = {"outline": "Outline", "white": "White box", "dark": "Dark box", "orange": "Orange box",
              "comic": "Comic", "banner": "Banner", "plain": "Plain"}

CAPTION_PRESETS = {
    "classic": {"label": "Classic pop", "font": "Anton", "size": 100, "words": 3, "upper": True, "text": "#FFFFFF",
                "lit": "#FFE100", "key": "#FFFFFF", "box": "none", "box_color": "#000000", "outline": 100,
                "shadow": 100, "anim": "pop"},
    "bigword": {"label": "Big word", "font": "Titan One", "size": 125, "words": 1, "upper": True, "text": "#FFFFFF",
                "lit": "#FFE100", "key": "#FFE100", "box": "none", "box_color": "#000000", "outline": 120,
                "shadow": 100, "anim": "pop"},
    "subtitle": {"label": "Subtitle", "font": "Poppins ExtraBold", "size": 62, "words": 5, "upper": False,
                 "text": "#FFFFFF", "lit": "#FFFFFF", "key": "#FFFFFF", "box": "line", "box_color": "#000000",
                 "outline": 0, "shadow": 0, "anim": "none"},
    "purple": {"label": "Purple pop", "font": "Poppins Black", "size": 92, "words": 2, "upper": True,
               "text": "#FFFFFF", "lit": "#B57BFF", "key": "#B57BFF", "box": "none", "box_color": "#000000",
               "outline": 100, "shadow": 100, "anim": "pop"},
    "darkbox": {"label": "Dark box", "font": "Poppins Black", "size": 84, "words": 3, "upper": True,
                "text": "#FFFFFF", "lit": "#FFE100", "key": "#FFFFFF", "box": "line", "box_color": "#111111",
                "outline": 0, "shadow": 0, "anim": "pop"},
    "orangebox": {"label": "Orange box", "font": "Poppins Black", "size": 88, "words": 3, "upper": True,
                  "text": "#FFFFFF", "lit": "#FFFFFF", "key": "#FFFFFF", "box": "word", "box_color": "#FF6A1A",
                  "outline": 70, "shadow": 60, "anim": "pop"},
    "outline": {"label": "Outline", "font": "Archivo Black", "size": 96, "words": 3, "upper": True,
                "text": "#FFFFFF", "lit": "#7CFF5B", "key": "#7CFF5B", "box": "none", "box_color": "#000000",
                "outline": 150, "shadow": 0, "anim": "pop"},
    "comic": {"label": "Comic", "font": "Bangers", "size": 120, "words": 2, "upper": True, "text": "#FFE100",
              "lit": "#FFFFFF", "key": "#FF5A5A", "box": "none", "box_color": "#000000", "outline": 130,
              "shadow": 140, "anim": "bounce"},
    "bounce": {"label": "Bounce", "font": "Lilita One", "size": 105, "words": 2, "upper": True, "text": "#FFFFFF",
               "lit": "#7CFF5B", "key": "#FFE100", "box": "none", "box_color": "#000000", "outline": 110,
               "shadow": 100, "anim": "bounce"},
}

DEFAULT = {
    "captions": {"show": True, "preset": "classic", **{k: v for k, v in CAPTION_PRESETS["classic"].items()
                                                         if k != "label"}, "y": None},
    "heading": {"show": True, "look": "outline", "font": "Anton", "size": 100, "upper": True, "y": None,
                "whole": False},
    "layout": {"framing": "smart", "cam": "top", "cam_find": "ai", "cam_box": None,
               "corner": {"x": 0.04, "y": 0.1, "w": 0.42}, "round": 28, "border": 4, "border_color": "#FFFFFF",
               "part": None},
}


def _with(base: dict, **groups) -> dict:
    out = copy.deepcopy(base)
    for g, vals in groups.items():
        out[g].update(vals)
    return out


def _preset(name: str) -> dict:
    return {"preset": name, **{k: v for k, v in CAPTION_PRESETS[name].items() if k != "label"}}


TEMPLATES = [
    {"id": "t_default", "name": "Default", **copy.deepcopy(DEFAULT)},
    {"id": "t_hype", "name": "Hype", **_with(DEFAULT, captions=_preset("bigword"),
                                             heading={"look": "white", "font": "Poppins ExtraBold", "upper": False,
                                                      "size": 90, "whole": True})},
    {"id": "t_clean", "name": "Clean", **_with(DEFAULT, captions=_preset("subtitle"),
                                               heading={"look": "plain", "font": "Poppins ExtraBold",
                                                        "upper": False, "size": 80},
                                               layout={"framing": "blur"})},
    {"id": "t_box", "name": "Box", **_with(DEFAULT, captions=_preset("orangebox"),
                                           heading={"look": "dark", "font": "Poppins Black", "upper": False,
                                                    "size": 88, "whole": True})},
    {"id": "t_bounce", "name": "Bounce", **_with(DEFAULT, captions=_preset("bounce"),
                                                 heading={"look": "white", "font": "Lilita One", "upper": False,
                                                          "size": 92, "whole": True},
                                                 layout={"cam": "corner"})},
    {"id": "t_comic", "name": "Comic", **_with(DEFAULT, captions=_preset("comic"),
                                               heading={"look": "comic", "font": "Bangers", "size": 110})},
]
TEMPLATES[0]["captions"]["font"] = TEMPLATES[0]["heading"]["font"] = (config.CAPTION_FONT if config.CAPTION_FONT
                                                                       in FONTS else "Anton")
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


# ── reading and cleaning ─────────────────────────────────────────────────────────────────────────────────

def _num(v, lo: float, hi: float, default: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return default if x != x else min(hi, max(lo, x))


def _color(v, default: str) -> str:
    return v.upper() if isinstance(v, str) and _HEX.match(v) else default


def _pick(v, allowed, default):
    return v if v in allowed else default


def _box(v, need_area: bool = True) -> list[float] | None:
    """A rectangle as [x, y, w, h] in 0..1 of the source frame, or None."""
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        return None
    try:
        x, y, w, h = (float(a) for a in v)
    except (TypeError, ValueError):
        return None
    x, y = min(0.98, max(0.0, x)), min(0.98, max(0.0, y))
    w, h = min(1.0 - x, max(0.0, w)), min(1.0 - y, max(0.0, h))
    if need_area and (w < 0.03 or h < 0.03):
        return None
    return [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]


def clean(s: dict | None) -> dict:
    """A complete, safe style from anything the dashboard sends (missing values come from the default)."""
    s = s or {}
    d = DEFAULT
    c_in, h_in, l_in = (s.get(g) if isinstance(s.get(g), dict) else {} for g in ("captions", "heading", "layout"))
    dc, dh, dl = d["captions"], d["heading"], d["layout"]
    cap = {
        "show": bool(c_in.get("show", dc["show"])),
        "preset": _pick(c_in.get("preset"), CAPTION_PRESETS, dc["preset"]),
        "font": _pick(c_in.get("font"), FONTS, dc["font"]),
        "size": round(_num(c_in.get("size"), 40, 180, dc["size"])),
        "words": int(_num(c_in.get("words"), 1, 5, dc["words"])),
        "upper": bool(c_in.get("upper", dc["upper"])),
        "anim": _pick(c_in.get("anim"), ANIMS, dc["anim"]),
        "box": _pick(c_in.get("box"), BOXES, dc["box"]),
        "outline": round(_num(c_in.get("outline"), 0, 250, dc["outline"])),
        "shadow": round(_num(c_in.get("shadow"), 0, 250, dc["shadow"])),
        "y": None if c_in.get("y") in (None, "") else round(_num(c_in.get("y"), 0.08, 0.92, 0.68), 3),
    }
    for k in ("text", "lit", "key", "box_color"):
        cap[k] = _color(c_in.get(k), dc[k])
    corner = l_in.get("corner") if isinstance(l_in.get("corner"), dict) else {}
    head = {
        "show": bool(h_in.get("show", dh["show"])),
        "look": _pick(h_in.get("look"), HEAD_LOOKS, dh["look"]),
        "font": _pick(h_in.get("font"), FONTS, dh["font"]),
        "size": round(_num(h_in.get("size"), 40, 160, dh["size"])),
        "upper": bool(h_in.get("upper", dh["upper"])),
        "whole": bool(h_in.get("whole", dh["whole"])),
        "y": None if h_in.get("y") in (None, "") else round(_num(h_in.get("y"), 0.06, 0.9, 0.17), 3),
    }
    lay = {
        "framing": _pick(l_in.get("framing"), FRAMINGS, dl["framing"]),
        "cam": _pick(l_in.get("cam"), CAM_SPOTS, dl["cam"]),
        "cam_find": _pick(l_in.get("cam_find"), ("ai", "draw"), dl["cam_find"]),
        "cam_box": _box(l_in.get("cam_box")),
        "corner": {"x": round(_num(corner.get("x"), 0, 0.9, 0.04), 3), "y": round(_num(corner.get("y"), 0, 0.9, 0.1), 3),
                   "w": round(_num(corner.get("w"), 0.2, 0.7, 0.42), 3)},
        "round": round(_num(l_in.get("round"), 0, 80, dl["round"])),
        "border": round(_num(l_in.get("border"), 0, 16, dl["border"])),
        "border_color": _color(l_in.get("border_color"), dl["border_color"]),
        "part": _box(l_in.get("part")),
    }
    if lay["cam_find"] == "draw" and not lay["cam_box"]:
        lay["cam_find"] = "ai"
    name = str(s.get("name") or "My style").strip()[:40] or "My style"
    return {"id": str(s.get("id") or ""), "name": name, "captions": cap, "heading": head, "layout": lay}


# ── storage ──────────────────────────────────────────────────────────────────────────────────────────────

def _mine() -> list[dict]:
    got = db.kv_get("styles", []) or []
    return [clean(x) | {"id": x.get("id"), "updated": x.get("updated", 0)} for x in got if isinstance(x, dict)
            and x.get("id")]


def all_styles() -> list[dict]:
    """Your styles first, then the built-in templates (marked "template": True)."""
    default_id = default_id_()
    out = [{**s, "template": False} for s in _mine()]
    out += [{**clean(t), "id": t["id"], "template": True} for t in TEMPLATES]
    for s in out:
        s["default"] = s["id"] == default_id
    return out


def default_id_() -> str:
    did = db.kv_get("default_style", "") or ""
    ids = {s["id"] for s in _mine()} | {t["id"] for t in TEMPLATES}
    return did if did in ids else "t_default"


def get(sid: str | None) -> dict:
    """A style by id; the default one when the id is empty or gone."""
    sid = sid or default_id_()
    for s in _mine():
        if s["id"] == sid:
            return s
    for t in TEMPLATES:
        if t["id"] == sid:
            return {**clean(t), "id": t["id"]}
    return get(default_id_()) if sid != default_id_() else {**clean(TEMPLATES[0]), "id": "t_default"}


def save(style: dict, sid: str | None = None) -> dict:
    """Create (no id) or update one of your styles. Templates are copied, never changed."""
    mine = db.kv_get("styles", []) or []
    s = clean(style)
    if sid and sid.startswith("t_"):
        sid = None
    s["id"] = sid or db.new_id("st")
    s["updated"] = time.time()
    for i, x in enumerate(mine):
        if x.get("id") == s["id"]:
            mine[i] = s
            break
    else:
        if sid:
            raise KeyError("Style not found")
        mine.insert(0, s)
    db.kv_set("styles", mine[:50])
    return {**s, "template": False}


def copy_of(sid: str) -> dict:
    src = get(sid)
    return save({**src, "name": f"{src['name']} copy"[:40]})


def delete(sid: str) -> None:
    mine = [x for x in (db.kv_get("styles", []) or []) if x.get("id") != sid]
    db.kv_set("styles", mine)
    if db.kv_get("default_style", "") == sid:
        db.kv_set("default_style", "")


def set_default(sid: str) -> str:
    if sid not in {s["id"] for s in all_styles()}:
        raise KeyError("Style not found")
    db.kv_set("default_style", sid)
    return sid


def for_job(job: dict | None, streamer: dict | None) -> dict:
    """The style a video's clips use: the one picked for this video, else the streamer's, else the default."""
    pick = ((job or {}).get("meta") or {}).get("style_id") or (streamer or {}).get("style_id") or ""
    return get(pick)


def options() -> dict:
    """Everything the Styles screen needs to draw its pickers."""
    return {"fonts": FONTS, "anims": ANIMS, "boxes": BOXES, "framings": FRAMINGS, "cams": CAM_SPOTS,
            "heads": HEAD_LOOKS,
            "presets": {k: {kk: vv for kk, vv in v.items()} for k, v in CAPTION_PRESETS.items()}}
