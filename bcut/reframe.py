"""Deciding how a wide video becomes a vertical one, and building the ffmpeg graph for it.

Layouts (what a clipping editor would pick by hand):
- `split`  — gaming / screen streams with a facecam overlay: the cam (or cams, side by side) on top,
             the gameplay below. The cam box is found from its own border lines, wherever it sits.
- `face`   — one person on camera (IRL, podcast, talking head): a 9:16 window that follows the face like a
             camera operator would, smoothly, and never cuts into the face.
- `stack`  — two people too far apart for one 9:16 window (two-shot podcast, two guests): each gets half
             the screen, one above the other.
- `wide`   — nobody on camera, or a shot that must stay whole: the full frame in the middle over a blurred
             copy of itself.
- `center` — a fixed centre crop.
- `fit`    — the source is already vertical: just fill the frame.
`auto` picks one from what it sees in the clip; a streamer can be pinned to one, and any clip can be
re-rendered in another from the Inbox.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from . import media, textscan

OUT_W, OUT_H = 1080, 1920
FPS = 30
LAYOUTS = {
    "auto": "Decide per clip",
    "split": "Facecam on top, gameplay below",
    "face": "Follow the speaker",
    "stack": "Two people, one above the other",
    "speaker": "Two people, cut to whoever is talking",
    "wide": "Whole frame on blurred fill",
    "center": "Centre crop",
}
ALIASES = {"blur": "wide"}

MODEL = Path(__file__).with_name("models") / "face_detection_yunet_2023mar.onnx"
SCAN_W = 640


# ── faces ────────────────────────────────────────────────────────────────────────────────────────────────

def _detector():
    """YuNet (OpenCV's small neural face detector, bundled) with the old Haar cascade as a fallback."""
    import cv2

    if hasattr(cv2, "FaceDetectorYN") and MODEL.exists():
        return ("yunet", cv2.FaceDetectorYN.create(str(MODEL), "", (320, 320), 0.7, 0.3, 50))
    xml = Path(getattr(getattr(cv2, "data", None), "haarcascades", "")) / "haarcascade_frontalface_default.xml"
    if xml.exists():
        return ("haar", cv2.CascadeClassifier(str(xml)))
    return ("none", None)


def _faces(img, det) -> list[tuple[float, float, float, float]]:
    """Faces as (x, y, w, h) in 0..1 of the frame."""
    import cv2

    kind, d = det
    h, w = img.shape[:2]
    boxes = []
    if kind == "yunet":
        d.setInputSize((w, h))
        _, found = d.detect(img)
        boxes = [] if found is None else [f[:4] for f in found]
    elif kind == "haar":
        gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        boxes = d.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=6, minSize=(max(14, w // 50),) * 2)
    out = []
    for x, y, fw, fh in boxes:
        if fw < w / 70:
            continue
        out.append((max(0.0, x / w), max(0.0, y / h), fw / w, fh / h))
    return out


# ── facecam boxes ────────────────────────────────────────────────────────────────────────────────────────

def _clusters(dets: list[list[tuple]], n: int) -> list[dict]:
    """Faces that sit in the same small spot for much of the clip (a webcam overlay looks exactly like this)."""
    cl: list[dict] = []
    for fi, faces in enumerate(dets):
        for (x, y, w, h) in faces:
            cx, cy = x + w / 2, y + h / 2
            for c in cl:
                if abs(cx - c["cx"]) < 0.05 and abs(cy - c["cy"]) < 0.07 and 0.55 < w / c["w"] < 1.8:
                    c["pts"].append((fi, cx, cy, w, h))
                    k = len(c["pts"])
                    c["cx"] += (cx - c["cx"]) / k
                    c["cy"] += (cy - c["cy"]) / k
                    c["w"] += (w - c["w"]) / k
                    break
            else:
                cl.append({"cx": cx, "cy": cy, "w": w, "pts": [(fi, cx, cy, w, h)]})
    out = []
    for c in cl:
        frames = len({p[0] for p in c["pts"]})
        a = np.array([p[1:] for p in c["pts"]])
        out.append({"share": frames / max(1, n), "cx": float(np.median(a[:, 0])), "cy": float(np.median(a[:, 1])),
                    "w": float(np.median(a[:, 2])), "h": float(np.median(a[:, 3])),
                    "sx": float(a[:, 0].std()), "sy": float(a[:, 1].std())})
    return sorted(out, key=lambda c: -c["share"])


def _edge_maps(imgs: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray, float]:
    """Edges that stay put while everything else moves: overlay borders, HUD frames."""
    import cv2

    ex = ey = None
    for img in imgs:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gx = np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3))
        gy = np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
        ex = gx if ex is None else ex + gx
        ey = gy if ey is None else ey + gy
    ex /= len(imgs)
    ey /= len(imgs)
    base = float(np.median(np.concatenate([ex.ravel(), ey.ravel()]))) + 1.0
    return ex, ey, base


def _side(prof_rows: dict, idx: list[int], base: float):
    """Strongest straight, long line among the candidate columns/rows `idx` (each a 1-D profile)."""
    best, best_i = 0.0, None
    for i in idx:
        line = prof_rows[i]
        strong = float((line > 3.0 * base).mean())
        score = float(line.mean()) / base
        if strong >= 0.7 and score > 4.0 and score > best:
            best, best_i = score, i
    return best_i


def _cam_box(c: dict, ex: np.ndarray, ey: np.ndarray, base: float) -> tuple[tuple[float, float, float, float], int]:
    """The webcam rectangle around a steady face: the persistent border lines on each side, or the frame edge.
    Returns (x, y, w, h) in 0..1 and how many of the four sides were real lines."""
    H, W = ex.shape
    fx0, fx1 = (c["cx"] - c["w"] / 2) * W, (c["cx"] + c["w"] / 2) * W
    fy0, fy1 = (c["cy"] - c["h"] / 2) * H, (c["cy"] + c["h"] / 2) * H
    fw, fh = fx1 - fx0, fy1 - fy0
    r0, r1 = int(max(0, fy0 - 0.4 * fh)), int(min(H, fy1 + 0.4 * fh))
    c0, c1 = int(max(0, fx0 - 0.4 * fw)), int(min(W, fx1 + 0.4 * fw))
    found = 0

    def search(lo, hi, step, maps, span, limit):
        nonlocal found
        idx = [i for i in range(int(lo), int(hi), step) if 1 <= i < limit - 1]
        if not idx or span[1] - span[0] < 4:
            return None
        best = _side({i: maps(i, span) for i in idx}, idx, base)
        if best is not None:
            found += 1
        return best

    vcol = lambda i, s: ex[s[0]:s[1], max(0, i - 1):i + 2].max(axis=1)   # noqa: E731
    hrow = lambda i, s: ey[max(0, i - 1):i + 2, s[0]:s[1]].max(axis=0)   # noqa: E731
    left = search(fx0 - 2, fx0 - 4.5 * fw, -1, vcol, (r0, r1), W)
    right = search(fx1 + 2, fx1 + 4.5 * fw, 1, vcol, (r0, r1), W)
    top = search(fy0 - 2, fy0 - 3.0 * fh, -1, hrow, (c0, c1), H)
    bottom = search(fy1 + 2, fy1 + 3.2 * fh, 1, hrow, (c0, c1), H)
    # a side with no line: the frame edge if it is near, otherwise a typical cam proportion around the face
    L = left if left is not None else (0 if fx0 < 4.5 * fw else fx0 - 1.2 * fw)
    R = right if right is not None else (W if W - fx1 < 4.5 * fw else fx1 + 1.2 * fw)
    T = top if top is not None else (0 if fy0 < 3.0 * fh else fy0 - 0.9 * fh)
    B = bottom if bottom is not None else (H if H - fy1 < 3.2 * fh else fy1 + 1.3 * fh)
    edges = sum(1 for v, e in ((left, fx0 < 4.5 * fw), (right, W - fx1 < 4.5 * fw), (top, fy0 < 3 * fh),
                                (bottom, H - fy1 < 3.2 * fh)) if v is not None or e)
    return (L / W, T / H, (R - L) / W, (B - T) / H), min(found, 4) * 10 + edges


def _motion(imgs: list[np.ndarray], box) -> tuple[float, float]:
    """Average frame-to-frame change inside and outside a box."""
    import cv2

    H, W = imgs[0].shape[:2]
    x, y, w, h = box
    m = np.zeros((H, W), bool)
    m[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)] = True
    ins, outs = [], []
    prev = None
    for img in imgs:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev is not None:
            d = np.abs(g - prev)
            ins.append(d[m].mean() if m.any() else 0)
            outs.append(d[~m].mean() if (~m).any() else 0)
        prev = g
    return (float(np.mean(ins)) if ins else 0.0, float(np.mean(outs)) if outs else 0.0)


def find_cams(dets, imgs, force: bool = False) -> list[dict]:
    """Webcam overlays in the clip: [{"box": (x, y, w, h), "face": (cx, cy)}] in 0..1 of the frame.
    Empty if this doesn't look like a facecam stream. A cam needs three straight border lines (or frame edges)
    around a face that barely moves, and the picture around it has to change much more than the picture
    inside it (gameplay moves; a room filmed by a still camera doesn't)."""
    n = len(dets)
    cands = [c for c in _clusters(dets, n) if c["share"] >= 0.12 and c["w"] < 0.16 and c["sx"] < 0.03
             and c["sy"] < 0.04]
    if not cands:
        return []
    ex, ey, base = _edge_maps(imgs[:: max(1, len(imgs) // 60)])
    probe = imgs[:: max(1, len(imgs) // 40)]
    cams = []
    for c in cands[:4]:
        box, q = _cam_box(c, ex, ey, base)
        lines, sides = divmod(q, 10)
        if box[2] * box[3] > 0.25 or c["w"] / max(box[2], 1e-3) < 0.12:
            continue
        # a second cam lined up with one already found needs no motion test (phone cams move a lot)
        aligned = any(abs(box[0] - k["box"][0]) < 0.02 or abs(box[1] - k["box"][1]) < 0.02 or
                      abs(box[1] - (k["box"][1] + k["box"][3])) < 0.02 for k in cams)
        ins, outs = _motion(probe, box)
        cam_like = lines >= 3 and sides >= 3 and (outs >= 2.5 * max(ins, 0.5) or aligned)
        need = 0.4 if not cams else (0.12 if aligned else 0.3)   # a side-profile face is found less often
        if (cam_like and c["share"] >= need) or (force and not cams):
            cams.append({"box": box, "face": (c["cx"], c["cy"])})
    # one cam with two faces in it: merge overlapping boxes
    merged: list[dict] = []
    for cam in sorted(cams, key=lambda k: (k["box"][1], k["box"][0])):
        b = cam["box"]
        for m in merged:
            mb = m["box"]
            if b[0] < mb[0] + mb[2] - 0.01 and mb[0] < b[0] + b[2] - 0.01 and \
                    b[1] < mb[1] + mb[3] - 0.01 and mb[1] < b[1] + b[3] - 0.01:
                x0, y0 = min(mb[0], b[0]), min(mb[1], b[1])
                x1, y1 = max(mb[0] + mb[2], b[0] + b[2]), max(mb[1] + mb[3], b[1] + b[3])
                m["box"] = (x0, y0, x1 - x0, y1 - y0)
                m["face"] = ((m["face"][0] + cam["face"][0]) / 2, (m["face"][1] + cam["face"][1]) / 2)
                break
        else:
            merged.append(dict(cam))
    return merged


# ── following people ─────────────────────────────────────────────────────────────────────────────────────

def _camera(times: np.ndarray, target: np.ndarray, half_free: float, cuts: list[float], lo: float, hi: float,
            vmax: float | None = None) -> np.ndarray:
    """A camera operator. Holds still while the subject stays near the middle and ignores quick lunges; when the
    subject has really moved, glides over (critically damped, never faster than `vmax` per second, never
    overshooting); when they've gone right across the frame, cuts straight there instead of whipping over; and
    jumps on a picture cut. Values are crop centres."""
    out = np.zeros_like(target)
    if not len(target):
        return out
    span = max(1e-6, hi - lo)
    vmax = vmax if vmax is not None else max(0.15, 2.5 * half_free)
    far = max(3.0 * half_free, 0.25 * span)        # this far off for a while: cut rather than pan
    pos, vel = float(target[0]), 0.0
    moving = False
    off_since = None
    omega = 4.0                                     # settles in about a second: calm, not twitchy
    ci = 0
    cuts = sorted(cuts)
    for i, t in enumerate(times):
        dt = float(times[i] - times[i - 1]) if i else 0.0
        while ci < len(cuts) and cuts[ci] <= t:
            ci += 1
            pos, vel, moving, off_since = float(target[i]), 0.0, False, None   # new shot: frame it straight away
        goal = float(target[i])
        err = goal - pos
        if abs(err) > half_free:
            off_since = t if off_since is None else off_since
            held = t - off_since
            if abs(err) > far and held >= 0.5:
                pos, vel, moving, off_since = goal, 0.0, False, None           # a clean cut, not a whip pan
            elif held >= 0.35:
                moving = True                                                    # a real move, not a lunge
        else:
            off_since = None
        if moving and dt > 0:
            # small steps, so the spring can't overshoot or swing back and forth
            n = max(1, int(np.ceil(dt / 0.01)))
            h = dt / n
            for _ in range(n):
                e = goal - pos
                acc = omega * omega * e - 2 * omega * vel
                vel += acc * h
                vel = max(-vmax, min(vmax, vel))
                if (goal - pos) * (goal - (pos + vel * h)) < 0:
                    pos, vel = goal, 0.0                                         # arrived: stop, no overshoot
                    break
                pos += vel * h
            if abs(goal - pos) < 0.15 * half_free and abs(vel) < 0.02:
                moving, vel = False, 0.0
        out[i] = min(hi, max(lo, pos))
    return out


def _subject_track(times, dets, cw: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Where the person to follow is, per sample: centre x, and the face's left/right edges (0..1).
    Sticks with the same person unless someone else is clearly bigger for a while; two people who fit in the
    window together are framed together."""
    cx = np.full(len(times), np.nan)
    l = np.full(len(times), np.nan)
    r = np.full(len(times), np.nan)
    cur = None
    rival_since = None
    for i, faces in enumerate(dets):
        if not faces:
            continue
        faces = sorted(faces, key=lambda f: -f[2] * f[3])
        big = faces[0]
        pick = big
        if cur is not None:
            near = [f for f in faces if abs((f[0] + f[2] / 2) - cur) < 0.08]
            if near:
                pick = near[0]
                if big is not pick and big[2] > 1.5 * pick[2]:
                    rival_since = rival_since if rival_since is not None else times[i]
                    if times[i] - rival_since > 1.2:
                        pick, rival_since = big, None
                else:
                    rival_since = None
        x0, x1 = pick[0], pick[0] + pick[2]
        # a second person of similar size close enough to share the frame: keep both in
        for f in faces:
            if f is pick or f[2] < 0.6 * pick[2]:
                continue
            a, b = min(x0, f[0]), max(x1, f[0] + f[2])
            if b - a < 0.85 * cw:
                x0, x1 = a, b
        cur = pick[0] + pick[2] / 2
        cx[i], l[i], r[i] = (x0 + x1) / 2, x0, x1
    return _fill(cx), _fill(l), _fill(r)


def _fill(a: np.ndarray) -> np.ndarray:
    """Hold the last known value through gaps (and the first known one before it)."""
    a = a.copy()
    ok = ~np.isnan(a)
    if not ok.any():
        return np.full_like(a, 0.5)
    first = int(np.argmax(ok))
    a[:first] = a[first]
    for i in range(first + 1, len(a)):
        if np.isnan(a[i]):
            a[i] = a[i - 1]
    return a


def _smooth(a: np.ndarray, k: int = 3) -> np.ndarray:
    if len(a) < 3:
        return a
    pad = np.pad(a, k, mode="edge")
    return np.array([np.median(pad[i:i + 2 * k + 1]) for i in range(len(a))])


def _no_half_faces(ts, dets, cam, l, r, cw, lo, hi, cuts) -> np.ndarray:
    """A face sliced in half by the window edge looks like a mistake. Where that happens, slide the window
    just enough to take that face in whole or leave it out whole (whichever is the smaller move that still
    keeps the main face fully in), then smooth the result like a camera move."""
    want = cam.copy()
    m = 0.03 * cw
    for i, faces in enumerate(dets):
        c = cam[i]
        for (x, _, w, _) in faces:
            a, b = x, x + w
            left, right = c - cw / 2, c + cw / 2
            if b <= left or a >= right or (a >= left and b <= right):
                continue                                   # fully out or fully in: fine
            opts = [a - m + cw / 2, b + m - cw / 2,        # take it in (from either side)
                    b + m + cw / 2, a - m - cw / 2]        # or leave it out
            ok = [o for o in opts if lo <= o <= hi and o - cw / 2 <= l[i] - m / 2 and o + cw / 2 >= r[i] + m / 2]
            if ok:
                c = min(ok, key=lambda o: abs(o - c))
        want[i] = c
    if np.allclose(want, cam):
        return cam
    return np.clip(_camera(ts, _smooth(want, 2), 0.06 * cw, cuts, lo, hi, vmax=1.2 * cw), lo, hi)


def _two_people(dets, cw: float) -> tuple[bool, list[tuple[float, float, float, float]]]:
    """Two people of similar size, too far apart to share one 9:16 window, for most of the clip."""
    pairs = []
    for faces in dets:
        big = sorted(faces, key=lambda f: -f[2] * f[3])[:2]
        if len(big) == 2 and big[1][2] > 0.5 * big[0][2] and big[1][2] > 0.04:
            a, b = sorted(big, key=lambda f: f[0])
            pairs.append((a, b))
    if len(pairs) < 0.7 * max(1, len(dets)):
        return False, []
    sep = np.median([(b[0] + b[2]) - a[0] for a, b in pairs])
    if sep < 0.95 * cw:                         # they fit in one window together
        return False, []
    ax = np.array([a[0] + a[2] / 2 for a, _ in pairs])
    bx = np.array([b[0] + b[2] / 2 for _, b in pairs])
    if ax.std() > 0.05 or bx.std() > 0.05:        # people moving around (a car, a walk): follow one instead
        return False, []
    A = np.median(np.array([p[0] for p in pairs]), axis=0)
    B = np.median(np.array([p[1] for p in pairs]), axis=0)
    return True, [tuple(A), tuple(B)]


# ── who is talking ───────────────────────────────────────────────────────────────────────────────────────

def _voice(src: Path, start: float, end: float, ts) -> np.ndarray | None:
    """Is someone talking at each sample time (True/False), from the loudness of the sound."""
    try:
        a = media.read_audio(src, start, end - start)
    except Exception:  # noqa: BLE001
        return None
    if a is None or len(a) < media.SR:
        return None
    win = int(media.SR * 0.25)
    lv = []
    for t in ts:
        i = int(t * media.SR)
        seg = a[max(0, i - win // 2): i + win // 2]
        lv.append(20 * np.log10(np.sqrt(float((seg ** 2).mean()) + 1e-10) + 1e-10) if len(seg) else -90)
    lv = np.array(lv)
    return lv > max(np.percentile(lv, 30), np.median(lv) - 6)


def _mouth_motion(imgs, person) -> np.ndarray:
    """How much one person's mouth moves between samples, with head movement taken out
    (mouth change minus forehead/eye change)."""
    import cv2

    x, y, w, h = person
    out = [0.0]
    prev = None
    for img in imgs:
        H, W = img.shape[:2]
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        mx0, mx1 = int((x + 0.2 * w) * W), int((x + 0.8 * w) * W)
        mouth = g[int((y + 0.62 * h) * H):int(min(1.0, y + 1.02 * h) * H), mx0:mx1]
        eyes = g[int((y + 0.15 * h) * H):int((y + 0.45 * h) * H), mx0:mx1]
        if prev is not None and mouth.size and eyes.size and mouth.shape == prev[0].shape:
            dm = float(np.abs(mouth - prev[0]).mean())
            de = float(np.abs(eyes - prev[1]).mean()) if eyes.shape == prev[1].shape else 0.0
            out.append(max(0.0, dm - 0.8 * de))
        elif prev is not None:
            out.append(0.0)
        prev = (mouth, eyes)
    return np.array(out[:len(imgs)])


def _speaker_plan(ts, imgs, people, voiced, cuts, cw, force=False) -> dict | None:
    """Cut between the two people like a podcast editor: on whoever is talking, holding each shot at least
    1.5 seconds, and staying put while nobody talks."""
    if len(imgs) < 8:
        return None
    ma = _smooth(_mouth_motion(imgs, people[0]), 2)
    mb = _smooth(_mouth_motion(imgs, people[1]), 2)
    if voiced is None or len(voiced) != len(ts):
        voiced = np.ones(len(ts), bool)
    scale = max(1e-6, float(np.percentile(np.concatenate([ma, mb]), 90)))
    ma, mb = ma / scale, mb / scale
    raw = []
    for i in range(len(ts)):
        if not voiced[i] or max(ma[i], mb[i]) < 0.25:
            raw.append(None)
        elif ma[i] > 1.25 * mb[i]:
            raw.append("a")
        elif mb[i] > 1.25 * ma[i]:
            raw.append("b")
        else:
            raw.append(None)
    cur = next((r for r in raw if r), "a")
    who, since, lead = [], -9.0, None
    for i, t in enumerate(ts):
        r = raw[i]
        if r and r != cur:
            # switch once the other person has led for half a second and this shot has run 1.5 s; the cut
            # goes back to the moment they started talking
            lead = lead if lead and lead[0] == r else (r, t, i)
            if t - lead[1] >= 0.5 and lead[1] - since >= 1.5:
                cur, since = r, lead[1]
                for j in range(lead[2], i):
                    who[j] = cur
                lead = None
        elif r == cur:
            lead = None
        who.append(cur)
    share_a = who.count("a") / len(who)
    switches = sum(1 for x, y in zip(who, who[1:]) if x != y)
    if not force and (switches == 0 or min(share_a, 1 - share_a) < 0.12):
        return None                       # one person does the talking: the stack shows both instead
    # the window for each person: their face centred, inside the frame
    lo, hi = cw / 2, 1 - cw / 2
    pos = {k: float(np.clip(p[0] + p[2] / 2, lo, hi)) for k, p in zip(("a", "b"), people)}
    track, shots = [], []
    for i, t in enumerate(ts):
        if i and who[i] != who[i - 1]:
            # a hard cut, half a beat before the new speaker's line: a tiny lead feels like a human editor
            tc = max(float(ts[i - 1]) + 0.01, float(t) - 0.12)
            track.append([round(tc - 0.005, 3), round(pos[who[i - 1]], 4)])
            track.append([round(tc, 3), round(pos[who[i]], 4)])
            shots[-1][1] = round(tc, 3)
            shots.append([round(tc, 3), None, who[i]])
        elif not i:
            shots.append([0.0, None, who[0]])
        track.append([round(float(t), 3), round(pos[who[i]], 4)])
    shots[-1][1] = round(float(ts[-1]) + 1.0, 3)
    return {"mode": "speaker", "track": track, "speakers": shots}


# ── the plan ─────────────────────────────────────────────────────────────────────────────────────────────

def analyse(src: Path, start: float, end: float, src_w: int, src_h: int, cuts: list[float] | None = None,
            want: str = "auto") -> dict:
    """Look at the clip and return a plan the renderer follows: the layout, how it moves, and where the
    faces end up on the vertical screen (so captions can stay off them)."""
    want = ALIASES.get(want or "auto", want or "auto")
    if want not in LAYOUTS:
        want = "auto"
    if src_w == 0 or src_h == 0:
        return {"mode": "wide"}
    dur = end - start
    step = 0.25 if dur <= 75 else dur / 300
    times = list(np.arange(start + 0.05, end, step))
    shots = media.frames(src, times, width=SCAN_W)
    if not shots:
        return {"mode": "fit" if src_h >= src_w * 1.2 else "wide"}
    det = _detector()
    ts = np.array([t - start for t, _ in shots])
    imgs = [img for _, img in shots]
    dets = [_faces(img, det) for img in imgs]
    text = textscan.scan(imgs, list(ts))
    plan = _choose(ts, imgs, dets, start, end, src_w, src_h, cuts, want, text,
                   voice=lambda: _voice(src, start, end, ts))
    plan["faces"] = round(sum(1 for d in dets if d) / len(dets), 2)
    plan["faces_out"] = _project(plan, ts, dets, src_w, src_h)
    # text already in the picture: its own subtitles mean we add none; any text is kept clear of
    by_t: dict[float, list] = {}
    for t, x, y, w, h in text.get("text") or []:
        by_t.setdefault(t, []).append((x, y, w, h))
    plan["text_out"] = _project(plan, list(by_t), list(by_t.values()), src_w, src_h)
    plan["burned"] = bool(text.get("captions"))
    return plan


def _choose(ts, imgs, dets, start, end, src_w, src_h, cuts, want, text=None, voice=None) -> dict:
    if src_h >= src_w * 1.2:
        return {"mode": "fit"}
    if want in ("center", "wide"):
        return {"mode": want}
    if want == "auto" and (text or {}).get("captions") and (text or {}).get("width", 0) > 0.9 * (src_h * 9 / 16) / src_w:
        # the video has its own subtitles, wider than a vertical crop: cropping would slice them, keep it whole
        return {"mode": "wide"}
    share = sum(1 for d in dets if d) / len(dets)
    cw = (src_h * 9 / 16) / src_w            # a full-height 9:16 window, as a share of the width
    rel_cuts = [c - start for c in (cuts or []) if start + 0.3 < c < end - 0.3]

    if want in ("auto", "split"):
        cams = find_cams(dets, imgs, force=(want == "split"))
        if cams or want == "split":
            cams = cams or [{"box": (0.0, 0.0, 0.3, 0.33), "face": (0.15, 0.15)}]
            return {"mode": "split", "cams": [{"box": [round(v, 4) for v in c["box"]],
                                                "face": [round(v, 4) for v in c["face"]]} for c in cams]}

    if want in ("auto", "stack", "speaker"):
        two, people = _two_people(dets, cw)
        if two and want in ("auto", "speaker"):
            sp = _speaker_plan(ts, imgs, people, voice() if voice else None, rel_cuts, cw, force=want == "speaker")
            if sp:
                return sp
        if want == "speaker" and not two:
            want = "face"                      # nobody to cut between: just follow the face
        elif two or want in ("stack", "speaker"):
            if not people:
                people = [(0.2, 0.3, 0.1, 0.18), (0.7, 0.3, 0.1, 0.18)]
            return {"mode": "stack", **_stack_tracks(ts, dets, people, rel_cuts, src_w, src_h)}

    if want == "face" or (want == "auto" and share >= 0.3):
        cx, l, r = _subject_track(ts, dets, cw)
        lo, hi = cw / 2, 1 - cw / 2
        cam = _camera(ts, _smooth(cx), 0.22 * cw, rel_cuts, lo, hi, vmax=0.9 * cw)
        # the face must stay in: where it would poke out, ask the camera to move there (smoothly, like any move)
        margin = 0.04 * cw
        want = np.maximum(cam, np.minimum(r - cw / 2 + margin, hi))
        want = np.clip(np.minimum(want, np.maximum(l + cw / 2 - margin, lo)), lo, hi)
        if not np.allclose(want, cam):
            cam = _camera(ts, _smooth(want, 2), 0.05 * cw, rel_cuts, lo, hi, vmax=1.2 * cw)
        cam = _no_half_faces(ts, dets, cam, l, r, cw, lo, hi, rel_cuts)
        return {"mode": "face", "track": [[round(float(t), 3), round(float(x), 4)] for t, x in zip(ts, cam)]}
    return {"mode": "wide"}


def _stack_tracks(ts, dets, people, cuts, src_w, src_h) -> dict:
    """Two panels (1080x960 each), each following one person."""
    aspect = OUT_W / (OUT_H / 2)
    out = {}
    for name, p in zip(("a", "b"), people):
        ph = min(1.0, max(0.42, 3.6 * p[3]))                 # panel height as a share of the frame height
        pw = ph * src_h * aspect / src_w
        # a panel reaches at most halfway to the other person, so nobody shows up sliced at its edge
        d = abs((people[1][0] + people[1][2] / 2) - (people[0][0] + people[0][2] / 2))
        pw = min(1.0, pw, max(d, 0.45 * src_h * aspect / src_w))
        ph = pw * src_w / aspect / src_h
        xs, ys = np.full(len(ts), np.nan), np.full(len(ts), np.nan)
        other = people[1] if name == "a" else people[0]
        for i, faces in enumerate(dets):
            # each face belongs to whichever of the two people it is nearer to
            mine = [f for f in faces if abs(f[0] + f[2] / 2 - (p[0] + p[2] / 2)) < 0.15 and
                    abs(f[0] + f[2] / 2 - (p[0] + p[2] / 2)) < abs(f[0] + f[2] / 2 - (other[0] + other[2] / 2))]
            if mine:
                f = max(mine, key=lambda f: f[2])
                xs[i], ys[i] = f[0] + f[2] / 2, f[1] + f[3] / 2
        xs, ys = _fill(xs), _fill(ys)
        cx = _camera(ts, _smooth(xs), 0.2 * pw, cuts, pw / 2, 1 - pw / 2)
        cy = _camera(ts, _smooth(ys) + 0.1 * ph, 0.2 * ph, cuts, ph / 2, 1 - ph / 2)
        out[name] = {"w": round(pw, 4), "h": round(ph, 4),
                     "track": [[round(float(t), 3), round(float(x), 4), round(float(y), 4)]
                               for t, x, y in zip(ts, cx, cy)]}
    return out


# ── geometry: which part of the source lands where on the 1080x1920 screen ─────────────────────────────

def _even(v: float) -> int:
    return int(round(v / 2)) * 2


def _at(track: list, t: float, col: int = 1) -> float:
    ts = [p[0] for p in track]
    return float(np.interp(t, ts, [p[col] for p in track]))


def _cover_rect(x: float, y: float, w: float, h: float, tw: int, th: int, fx: float, fy: float):
    """The biggest tw:th rectangle inside (x, y, w, h), placed to keep the point (fx, fy) in view."""
    want = tw / th
    cw, ch = (h * want, h) if w / h > want else (w, w / want)
    cx = min(max(fx - cw / 2, x), x + w - cw)
    cy = min(max(fy - ch * 0.42, y), y + h - ch)
    return (cx, cy, cw, ch)


def split_geometry(plan: dict, src_w: int, src_h: int) -> dict:
    cams = plan.get("cams") or [{"box": [0, 0, 0.3, 0.33], "face": [0.15, 0.15]}]
    if len(cams) == 1:
        bw, bh = cams[0]["box"][2] * src_w, cams[0]["box"][3] * src_h
        top = int(min(900, max(640, OUT_W * bh / bw)))
    else:
        top = 760
    return {"top": _even(top), "bottom": OUT_H - _even(top)}


def panels(plan: dict, src_w: int, src_h: int, t: float = 0.0) -> list[dict]:
    """[{"src": (x, y, w, h) in source pixels, "out": (x, y, w, h) on the 1080x1920 screen, "name": ...}]"""
    mode = ALIASES.get(plan.get("mode", "wide"), plan.get("mode", "wide"))
    if mode == "fit":
        s = max(OUT_W / src_w, OUT_H / src_h)
        w, h = OUT_W / s, OUT_H / s
        return [{"src": ((src_w - w) / 2, (src_h - h) / 2, w, h), "out": (0, 0, OUT_W, OUT_H)}]
    if mode in ("face", "center", "speaker"):
        cw = min(src_w, src_h * 9 / 16)
        x = (src_w - cw) / 2
        if mode in ("face", "speaker") and plan.get("track"):
            x = min(src_w - cw, max(0.0, _at(plan["track"], t) * src_w - cw / 2))
        return [{"src": (x, 0, cw, src_h), "out": (0, 0, OUT_W, OUT_H), "name": "f"}]
    if mode == "stack":
        half = OUT_H // 2
        out = []
        for k, name in enumerate(("a", "b")):
            p = plan[name]
            pw, ph = p["w"] * src_w, p["h"] * src_h
            x = min(src_w - pw, max(0.0, _at(p["track"], t, 1) * src_w - pw / 2))
            y = min(src_h - ph, max(0.0, _at(p["track"], t, 2) * src_h - ph / 2))
            out.append({"src": (x, y, pw, ph), "out": (0, k * half, OUT_W, half), "name": name})
        return out
    if mode == "split":
        g = split_geometry(plan, src_w, src_h)
        top, bot = g["top"], g["bottom"]
        cams = plan.get("cams") or [{"box": [0, 0, 0.3, 0.33], "face": [0.15, 0.15]}]
        if len(cams) > 2:          # three or more: one box around them all
            x0 = min(c["box"][0] for c in cams)
            y0 = min(c["box"][1] for c in cams)
            x1 = max(c["box"][0] + c["box"][2] for c in cams)
            y1 = max(c["box"][1] + c["box"][3] for c in cams)
            cams = [{"box": [x0, y0, x1 - x0, y1 - y0],
                     "face": [float(np.mean([c["face"][0] for c in cams])), float(np.mean([c["face"][1] for c in cams]))]}]
        out = []
        tw = OUT_W // len(cams)
        for i, c in enumerate(cams):
            bx, by, bw, bh = c["box"]
            r = _cover_rect(bx * src_w, by * src_h, bw * src_w, bh * src_h, tw, top,
                            c["face"][0] * src_w, c["face"][1] * src_h)
            out.append({"src": r, "out": (i * tw, 0, tw, top), "name": f"c{i}"})
        # gameplay: as much of the middle as fits the lower panel, nudged away from the cam
        gw = min(src_w, src_h * OUT_W / bot)
        centre = (src_w - gw) / 2
        gx = centre
        lim = 0.18 * src_w
        for c in cams:
            a, b = c["box"][0] * src_w, (c["box"][0] + c["box"][2]) * src_w
            if a < gx + gw and b > gx:
                cand = [v for v in (a - gw, b) if 0 <= v <= src_w - gw and abs(v - centre) <= lim]
                if cand:
                    gx = min(cand, key=lambda v: abs(v - centre))
                else:
                    gx = min(max(centre - lim if (a + b) / 2 > src_w / 2 else centre + lim, 0), src_w - gw)
        out.append({"src": (gx, 0, gw, src_h), "out": (0, top, OUT_W, bot), "name": "g"})
        return out
    # wide
    fg_h = OUT_W * src_h / src_w
    return [{"src": (0, 0, src_w, src_h), "out": (0, (OUT_H - fg_h) / 2 - 60, OUT_W, fg_h), "name": "w"}]


def _project(plan: dict, ts, dets, src_w: int, src_h: int) -> list[list[int]]:
    """Every face seen, as [t, x0, y0, x1, y1] on the vertical screen."""
    out = []
    for t, faces in zip(ts, dets):
        if not faces:
            continue
        ps = panels(plan, src_w, src_h, float(t))
        for (x, y, w, h) in faces:
            fx0, fy0, fx1, fy1 = x * src_w, y * src_h, (x + w) * src_w, (y + h) * src_h
            cx, cy = (fx0 + fx1) / 2, (fy0 + fy1) / 2
            for p in ps:
                sx, sy, sw, sh = p["src"]
                if not (sx <= cx <= sx + sw and sy <= cy <= sy + sh):
                    continue
                ox, oy, ow, oh = p["out"]
                kx, ky = ow / sw, oh / sh
                X0, X1 = ox + (max(fx0, sx) - sx) * kx, ox + (min(fx1, sx + sw) - sx) * kx
                Y0, Y1 = oy + (max(fy0, sy) - sy) * ky, oy + (min(fy1, sy + sh) - sy) * ky
                out.append([round(float(t), 2), int(X0), int(Y0), int(X1), int(Y1)])
    return out


# ── rendering ────────────────────────────────────────────────────────────────────────────────────────────

def commands(plan: dict, dur: float, src_w: int, src_h: int) -> str:
    """sendcmd lines that move the crop windows frame by frame (empty when the layout doesn't move)."""
    mode = plan.get("mode")
    if mode not in ("face", "stack", "speaker") or (mode in ("face", "speaker") and not plan.get("track")):
        return ""
    lines, last = [], {}
    for t in np.arange(0, dur + 1 / FPS, 1 / FPS):
        cmds = []
        for p in panels(plan, src_w, src_h, float(t)):
            x, y = _even(p["src"][0]), _even(p["src"][1])
            n = p["name"]
            if last.get(n) != (x, y):
                cmds.append(f"crop@{n} x {x}" + (f", crop@{n} y {y}" if mode == "stack" else ""))
                last[n] = (x, y)
        if cmds:
            lines.append(f"{t:.3f} " + ", ".join(cmds) + ";")
    return "\n".join(lines) if len(lines) > 1 else ""


def _crop(rect, src_w, src_h, name: str = "") -> str:
    x, y, w, h = rect
    w, h = min(_even(w), src_w - src_w % 2), min(_even(h), src_h - src_h % 2)
    x, y = min(max(0, _even(x)), src_w - w), min(max(0, _even(y)), src_h - h)
    return f"crop{'@' + name if name else ''}={w}:{h}:{x}:{y}"


def filter_graph(plan: dict, src_w: int, src_h: int, has_cmds: bool = False) -> str:
    """The ffmpeg -filter_complex that produces [v] at 1080x1920."""
    mode = ALIASES.get(plan.get("mode", "wide"), plan.get("mode", "wide"))
    send = "sendcmd=f=moves.txt," if has_cmds else ""
    ps = panels(plan, src_w, src_h, 0.0)
    if mode == "fit":
        return (f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                f"crop={OUT_W}:{OUT_H},setsar=1[v]")
    if mode in ("face", "center", "speaker"):
        return f"[0:v]{send}{_crop(ps[0]['src'], src_w, src_h, 'f')},scale={OUT_W}:{OUT_H},setsar=1[v]"
    if mode == "wide":
        return (f"[0:v]split=2[a][b];[a]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                f"crop={OUT_W}:{OUT_H},boxblur=28:3,eq=brightness=-0.12[bg];"
                f"[b]scale={OUT_W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2-60,setsar=1[v]")
    names = [p["name"] for p in ps]
    chains = [f"[0:v]{send}split={len(ps)}" + "".join(f"[i{n}]" for n in names)]
    for p in ps:
        n, (ox, oy, ow, oh) = p["name"], p["out"]
        chains.append(f"[i{n}]{_crop(p['src'], src_w, src_h, n if mode == 'stack' else '')},"
                      f"scale={int(ow)}:{int(oh)},setsar=1[o{n}]")
    if mode == "stack":
        chains.append("[oa][ob]vstack=inputs=2[v]")
    else:
        cams = [n for n in names if n.startswith("c")]
        if len(cams) == 2:
            chains.append(f"[o{cams[0]}][o{cams[1]}]hstack=inputs=2[top]")
        else:
            chains.append(f"[o{cams[0]}]null[top]")
        chains.append("[top][og]vstack=inputs=2[v]")
    return ";".join(chains)


def text_spots(plan: dict, src_w: int = 1920, src_h: int = 1080) -> dict:
    """Preferred spots for captions and the hook in this layout, inside the area Reels/Shorts/TikTok leave
    uncovered (their buttons and caption text cover roughly the bottom 22% and the top 10%). The renderer
    moves them off any face that is there at the time."""
    mode = ALIASES.get(plan.get("mode"), plan.get("mode"))
    if mode == "split":
        top = split_geometry(plan, src_w, src_h)["top"]
        return {"cap": top, "hook": top + 300}
    if mode == "stack":
        return {"cap": OUT_H // 2, "hook": 300}
    if mode == "wide":
        fg_h = OUT_W * src_h / max(1, src_w)
        y0 = (OUT_H - fg_h) / 2 - 60
        return {"cap": int(min(1400, y0 + fg_h + 120)), "hook": int(max(300, y0 - 160))}
    return {"cap": 1300, "hook": 330}


def caption_y(plan: dict) -> int:   # kept for older callers
    return text_spots(plan)["cap"]
