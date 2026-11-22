"""Finding text that is already burned into the picture: subtitles, titles, watermarks.

Many links people clip are already-edited shorts with their own captions. Putting our captions on top of
theirs looks broken, so a clip whose source already has captions keeps the source's captions and gets none of
ours, and our hook keeps clear of any text that is already there.

Uses PaddleOCR's small text detector (bundled, 2.4 MB, runs on the CPU through onnxruntime, which
faster-whisper already installs). Only finds where text is; it doesn't read it.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

MODEL = Path(__file__).with_name("models") / "text_det_ppocrv3.onnx"
_sess = None
MEAN = np.array([0.485, 0.456, 0.406], np.float32).reshape(3, 1, 1)
STD = np.array([0.229, 0.224, 0.225], np.float32).reshape(3, 1, 1)


def _session():
    global _sess
    if _sess is None:
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2
            opts.log_severity_level = 3
            _sess = ort.InferenceSession(str(MODEL), opts, providers=["CPUExecutionProvider"])
        except Exception:  # noqa: BLE001  no onnxruntime or no model: no text scanning, nothing breaks
            _sess = False
    return _sess or None


def lines(img: np.ndarray) -> list[tuple[float, float, float, float]]:
    """Text lines in one frame (BGR), as (x, y, w, h) in 0..1."""
    import cv2

    sess = _session()
    if sess is None:
        return []
    h, w = img.shape[:2]
    s = 512 / min(h, w)
    nh, nw = max(32, int(round(h * s / 32)) * 32), max(32, int(round(w * s / 32)) * 32)
    x = cv2.resize(img, (nw, nh)).astype(np.float32).transpose(2, 0, 1) / 255.0
    x = ((x - MEAN) / STD)[None]
    prob = sess.run(None, {sess.get_inputs()[0].name: x})[0][0, 0]
    mask = (prob > 0.3).astype(np.uint8)
    # join the words of one line, so a caption is one box
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, nw // 60), 3)))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for i in range(1, n):
        bx, by, bw, bh, area = stats[i]
        if bw < 8 or bh < 6:
            continue
        score = float(prob[lab == i].mean())
        if score < 0.45:
            continue
        pad = bh * 0.3                       # the detector's boxes hug the letters; add the outline back
        out.append([bx - pad, by - pad, bw + 2 * pad, bh + 2 * pad])
    # words of one line that came out as separate boxes (wide spacing): join them
    out.sort(key=lambda b: b[0])
    merged: list[list[float]] = []
    for b in out:
        for m in merged:
            top, bot = max(m[1], b[1]), min(m[1] + m[3], b[1] + b[3])
            if bot - top > 0.5 * min(m[3], b[3]) and b[0] - (m[0] + m[2]) < 1.6 * max(m[3], b[3]):
                x0, y0 = min(m[0], b[0]), min(m[1], b[1])
                x1, y1 = max(m[0] + m[2], b[0] + b[2]), max(m[1] + m[3], b[1] + b[3])
                m[:] = [x0, y0, x1 - x0, y1 - y0]
                break
        else:
            merged.append(list(b))
    return [(b[0] / nw, b[1] / nh, b[2] / nw, b[3] / nh) for b in merged]


def scan(imgs: list[np.ndarray], times: list[float]) -> dict:
    """Look at frames of one clip. Returns {"captions": bool, "band": [y0, y1] or None, "text": [[t, x, y, w, h]]}
    where "captions" means subtitles that change as people talk (not a fixed title or a watermark)."""
    if _session() is None or not imgs:
        return {"captions": False, "band": None, "text": []}
    step = max(1, len(imgs) // 50)
    seen = []                                        # (t, box)
    per_frame = []
    for t, img in list(zip(times, imgs))[::step]:
        ls = lines(img)
        per_frame.append((t, ls))
        seen += [(t, b) for b in ls]
    n = len(per_frame)
    # subtitle-like lines: wide enough, roughly centred, not hugging the very top
    cand = [(t, b) for t, b in seen if b[2] >= 0.12 and abs(b[0] + b[2] / 2 - 0.5) < 0.22 and 0.02 < b[3] < 0.2
            and 0.12 < b[1] + b[3] / 2 < 0.95]
    result = {"captions": False, "band": None, "text": [[round(t, 2), *[round(v, 4) for v in b]] for t, b in seen]}
    if not cand:
        return result
    bins: dict[int, list] = {}
    for t, b in cand:
        bins.setdefault(int((b[1] + b[3] / 2) * 20), []).append((t, b))
    # the busiest band, together with its neighbours (captions move a little between lines)
    k = max(bins, key=lambda i: len({t for t, _ in bins[i]}) + 0.5 * len({t for t, _ in bins.get(i - 1, [])})
            + 0.5 * len({t for t, _ in bins.get(i + 1, [])}))
    band = bins[k] + bins.get(k - 1, []) + bins.get(k + 1, [])
    frames = {t for t, _ in band}
    share = len(frames) / n
    # per frame, everything in the band as one line (words can come out as separate boxes)
    rows = {}
    for t, bx in band:
        x0, x1 = bx[0], bx[0] + bx[2]
        r = rows.get(t)
        rows[t] = (min(r[0], x0), max(r[1], x1), r[2] + [bx[1] + bx[3] / 2]) if r else (x0, x1, [bx[1] + bx[3] / 2])
    widths = np.array([r[1] - r[0] for r in rows.values()])
    cx = np.array([(r[0] + r[1]) / 2 for r in rows.values()])
    cy = np.array([float(np.mean(r[2])) for r in rows.values()])
    # subtitles stay centred and at one height while the words (so the widths) keep changing; printing on a
    # T-shirt or a sign moves with the picture and keeps its width
    centred = float(np.mean(np.abs(cx - 0.5) < 0.07)) >= 0.5 and float(np.median(np.abs(cy - np.median(cy)))) < 0.03
    changing = widths.std() / max(widths.mean(), 1e-3) > 0.14
    result["why"] = {"share": round(share, 2), "centred": round(float(np.mean(np.abs(cx - 0.5) < 0.07)), 2),
                     "cv": round(float(widths.std() / max(widths.mean(), 1e-3)), 2), "band": k / 20}
    if share >= 0.3 and centred and changing:
        y0 = min(b[1] for _, b in band)
        y1 = max(b[1] + b[3] for _, b in band)
        result.update(captions=True, band=[round(y0, 4), round(y1, 4)],
                      width=round(float(max(b[0] + b[2] for _, b in band) - min(b[0] for _, b in band)), 4))
    return result
