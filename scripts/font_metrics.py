"""Rebuild bcut/fonts/metrics.json: each bundled caption font's glyph widths, so captions can be laid out word by
word exactly where libass draws them. Run after adding a font: `pip install fonttools`, then
`python scripts/font_metrics.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

from fontTools.ttLib import TTFont

FONTS = Path(__file__).resolve().parent.parent / "bcut" / "fonts"
EXTRA = "’‘“”…"


def family(font: TTFont) -> str:
    name = font["name"]
    # the full name is what libass matches ("Poppins ExtraBold"); single-style families use the family name
    full = name.getDebugName(4) or name.getDebugName(1)
    return full.removesuffix(" Regular")


def main() -> None:
    out = {}
    for path in sorted(FONTS.glob("*.ttf")):
        f = TTFont(path)
        os2, cmap, hmtx = f["OS/2"], f.getBestCmap(), f["hmtx"]
        adv = {}
        for code, glyph in cmap.items():
            ch = chr(code)
            if code < 0x250 or ch in EXTRA or 0x900 <= code < 0x980 or 0x600 <= code < 0x700:
                if code >= 0x20 and code != 0x7f:
                    adv[ch] = hmtx[glyph][0]
        # cap: height of a capital, asc/desc: libass's line box above/below the baseline (all in font units)
        out[family(f)] = {"win": os2.usWinAscent + os2.usWinDescent, "asc": os2.usWinAscent,
                          "desc": os2.usWinDescent, "cap": getattr(os2, "sCapHeight", 0) or 0, "upm": f["head"].unitsPerEm,
                          "file": path.name, "adv": adv}
    (FONTS / "metrics.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(", ".join(out))


if __name__ == "__main__":
    main()
