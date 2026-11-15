"""Speech to text on this PC with faster-whisper (word timestamps, 99 languages, Hindi and Hinglish included).

If faster-whisper is not installed the app still works: clips are then chosen from sound, picture and chat only,
and rendered without captions.
"""
from __future__ import annotations

import threading
from pathlib import Path

from . import config

_model = None
_lock = threading.Lock()


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _load():
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel

            device = config.WHISPER_DEVICE
            if device == "auto":
                try:
                    import ctranslate2
                    device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
                except Exception:  # noqa: BLE001
                    device = "cpu"
            compute = "float16" if device == "cuda" else "int8"
            _model = WhisperModel(config.WHISPER_MODEL, device=device, compute_type=compute)
        return _model


def transcribe(path: Path, start: float = 0.0, duration: float | None = None, language: str = "",
               log=print) -> dict:
    """Returns {"language": "hi", "words": [{"w": "hello", "s": 1.2, "e": 1.5}, ...]} with times in the
    source's own timeline."""
    if not available():
        log("faster-whisper is not installed: skipping captions (pip install faster-whisper)")
        return {"language": "", "words": []}
    from .media import read_audio

    audio = read_audio(path, start, duration)
    if len(audio) == 0:
        return {"language": "", "words": []}
    model = _load()
    segments, info = model.transcribe(
        audio, language=(language or config.WHISPER_LANGUAGE or None), word_timestamps=True,
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 400}, beam_size=1,
    )
    words = []
    for seg in segments:
        for w in seg.words or []:
            text = (w.word or "").strip()
            if text:
                words.append({"w": text, "s": round(w.start + start, 3), "e": round(w.end + start, 3)})
    return {"language": info.language or "", "words": words}


def lines(words: list[dict], max_gap: float = 0.8, max_len: float = 7.0) -> list[dict]:
    """Group words into sentence-ish lines: [{"s", "e", "text"}]."""
    out, cur = [], []
    for w in words:
        if cur and (w["s"] - cur[-1]["e"] > max_gap or w["e"] - cur[0]["s"] > max_len
                    or cur[-1]["w"].endswith((".", "?", "!", "।"))):
            out.append({"s": cur[0]["s"], "e": cur[-1]["e"], "text": " ".join(x["w"] for x in cur)})
            cur = []
        cur.append(w)
    if cur:
        out.append({"s": cur[0]["s"], "e": cur[-1]["e"], "text": " ".join(x["w"] for x in cur)})
    return out


def words_between(words: list[dict], s: float, e: float) -> list[dict]:
    return [w for w in words if w["s"] >= s - 0.05 and w["e"] <= e + 0.05]


def transcribe_long(path: Path, duration: float, language: str = "", log=print, chunk: float = 600.0) -> dict:
    """Transcribe a long file ten minutes at a time (keeps memory flat), with a small overlap so no word is
    lost at the seams."""
    if not available():
        return transcribe(path, log=log)
    words: list[dict] = []
    lang = language
    t = 0.0
    while t < duration:
        log(f"transcribing {int(t // 60)}–{int(min(duration, t + chunk) // 60)} min")
        part = transcribe(path, t, chunk + 2, lang, log)
        lang = lang or part["language"]
        last = words[-1]["e"] if words else -1
        words += [w for w in part["words"] if w["s"] > last]
        t += chunk
    return {"language": lang, "words": words}
