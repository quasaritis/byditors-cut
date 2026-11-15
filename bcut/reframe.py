"""Deciding how a wide video becomes a vertical one.

Layouts:
- `face`   — follow the speaker: the crop moves to wherever the face is, shot by shot (podcasts, IRL streams).
- `split`  — gaming streams with a small facecam: facecam on top, gameplay below.
- `blur`   — the whole wide frame in the middle over a blurred copy of itself (gameplay with no face).
- `fit`    — the source is already vertical: just fill the frame.
`auto` picks one from what it sees in the clip.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from . import media

OUT_W, OUT_H = 1080, 1920
TOP_H = 760  # facecam panel height in split layout


MODEL = Path(__file__).with_name("models") / "face_detection_yunet_2023mar.onnx"


def _detector():
    """YuNet (OpenCV's small neural face detector, bundled) with the old Haar cascade as a fallback."""
    import cv2

    if hasattr(cv2, "FaceDetectorYN") and MODEL.exists():
        return ("yunet", cv2.FaceDetectorYN.create(str(MODEL), "", (320, 320), 0.75, 0.3, 50))
    xml = Path(getattr(getattr(cv2, "data", None), "haarcascades", "")) / "haarcascade_frontalface_default.xml"
    if xml.exists():
        return ("haar", cv2.CascadeClassifier(str(xml)))
    return ("none", None)


def _faces(img, det) -> list[tuple[int, int, int, int]]:
    import cv2

    kind, d = det
    h, w = img.shape[:2]
    if kind == "yunet":
        d.setInputSize((w, h))
        _, found = d.detect(img)
        if found is None:
            return []
        return [tuple(int(v) for v in f[:4]) for f in found if f[2] > w / 60]
    if kind == "haar":
        gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        return [tuple(int(v) for v in f) for f in
                d.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=6, minSize=(max(18, w // 40),) * 2)]
    return []


def analyse(src: Path, start: float, end: float, src_w: int, src_h: int, cuts: list[float] | None = None,
            want: str = "auto") -> dict:
    """Look at the clip and return a plan the renderer follows."""
    if src_w == 0 or src_h == 0:
        return {"mode": "blur"}
    if src_h >= src_w * 1.2:
        return {"mode": "fit"}
    if want in ("center", "blur"):
        return {"mode": want, "segments": [(0.0, end - start, 0.5)]}
    dur = end - start
    step = 0.5 if dur <= 60 else dur / 120
    times = list(np.arange(start + 0.1, end, step))
    shots = media.frames(src, times)
    if not shots:
        return {"mode": "blur"}
    det = _detector()
    hits = []  # (t, cx, cy, fw) normalised 0..1
    for t, img in shots:
        h, w = img.shape[:2]
        fs = _faces(img, det)
        if fs:
            x, y, fw, fh = max(fs, key=lambda f: f[2] * f[3])
            hits.append((t - start, (x + fw / 2) / w, (y + fh / 2) / h, fw / w, fh / h, x / w, y / h))
    share = len(hits) / len(shots)

    if hits:
        fws = np.array([hh[3] for hh in hits])
        cxs = np.array([hh[1] for hh in hits])
        cys = np.array([hh[2] for hh in hits])
        small = np.median(fws) < 0.11
        steady = cxs.std() < 0.06 and cys.std() < 0.06
        corner = (np.median(cxs) < 0.33 or np.median(cxs) > 0.67) or (np.median(cys) > 0.62 or np.median(cys) < 0.3)
        if want == "split" or (want == "auto" and share >= 0.35 and small and steady and corner):
            # facecam box: about 3x the face, kept inside the frame
            fw, fh = float(np.median(fws)), float(np.median([hh[4] for hh in hits]))
            cx, cy = float(np.median(cxs)), float(np.median(cys))
            bw = min(0.45, fw * 3.4)
            bh = min(0.6, bw * (src_w / src_h) * (TOP_H / OUT_W))
            bx = float(np.clip(cx - bw / 2, 0, 1 - bw))
            by = float(np.clip(cy - bh / 2, 0, 1 - bh))
            # gameplay: the middle of the frame, away from the cam if possible
            return {"mode": "split", "cam": (bx, by, bw, bh), "faces": share}
        if want in ("auto", "face") and share >= 0.3:
            return {"mode": "face", "segments": _follow(hits, cuts or [], start, end), "faces": share}
    if want == "face":
        return {"mode": "face", "segments": [(0.0, dur, 0.5)]}
    return {"mode": "blur", "faces": share}


def _follow(hits, cuts, start, end) -> list[tuple[float, float, float]]:
    """One crop position per shot (between picture cuts), from the median face position in that shot.
    Holding still within a shot looks like a camera operator; jittering looks like a bot."""
    dur = end - start
    edges = [0.0] + [c - start for c in cuts if start + 1 < c < end - 1] + [dur]
    segs = []
    last = 0.5
    for a, b in zip(edges, edges[1:]):
        xs = [h[1] for h in hits if a <= h[0] < b]
        cx = float(np.median(xs)) if xs else last
        if abs(cx - last) < 0.04:
            cx = last  # don't nudge for tiny moves
        segs.append((round(a, 2), round(b, 2), round(cx, 3)))
        last = cx
    # merge neighbours that ended up at the same spot
    merged = [segs[0]]
    for s in segs[1:]:
        if abs(s[2] - merged[-1][2]) < 1e-3:
            merged[-1] = (merged[-1][0], s[1], s[2])
        else:
            merged.append(s)
    return merged[:40]


def filter_graph(plan: dict, src_w: int, src_h: int) -> str:
    """The ffmpeg -filter_complex that produces [v] at 1080x1920."""
    mode = plan.get("mode", "blur")
    if mode == "fit":
        return (f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                f"crop={OUT_W}:{OUT_H},setsar=1[v]")
    if mode in ("face", "center"):
        cw = int(src_h * 9 / 16) // 2 * 2
        segs = plan.get("segments") or [(0, 1e9, 0.5)]

        def xpos(cx):
            return int(np.clip(cx * src_w - cw / 2, 0, src_w - cw))

        expr = str(xpos(segs[-1][2]))
        for a, b, cx in reversed(segs[:-1]):
            expr = f"if(lt(t\\,{b:.2f})\\,{xpos(cx)}\\,{expr})"
        return f"[0:v]crop={cw}:{src_h}:{expr}:0,scale={OUT_W}:{OUT_H},setsar=1[v]"
    if mode == "split":
        bx, by, bw, bh = plan["cam"]
        cam = f"crop=iw*{bw:.4f}:ih*{bh:.4f}:iw*{bx:.4f}:ih*{by:.4f}"
        bot_h = OUT_H - TOP_H
        gw = min(1.0, (src_h * OUT_W / bot_h) / src_w)  # share of width that fills the bottom panel
        gx = 0.5 - gw / 2
        return (f"[0:v]split=2[a][b];"
                f"[a]{cam},scale={OUT_W}:{TOP_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{TOP_H}[top];"
                f"[b]crop=iw*{gw:.4f}:ih:iw*{gx:.4f}:0,scale={OUT_W}:{bot_h}:force_original_aspect_ratio=increase,"
                f"crop={OUT_W}:{bot_h}[bot];[top][bot]vstack=inputs=2,setsar=1[v]")
    # blur
    return (f"[0:v]split=2[a][b];[a]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W}:{OUT_H},boxblur=24:3,eq=brightness=-0.08[bg];"
            f"[b]scale={OUT_W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2-80,setsar=1[v]")


def caption_y(plan: dict) -> int:
    return {"split": TOP_H + 150, "blur": 1480, "fit": 1380}.get(plan.get("mode"), 1360)
