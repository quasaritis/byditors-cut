"""Speech to text on this PC with faster-whisper (word timestamps, 99 languages, Hindi and Hinglish included).

If faster-whisper is not installed the app still works: clips are then chosen from sound, picture and chat only,
and rendered without captions.
"""
from __future__ import annotations

import re

import threading
from pathlib import Path

from . import config

_model = None
_lock = threading.Lock()


def available() -> bool:
    from . import tools

    return tools.installed("whisper")


_on_cpu = False  # set once the graphics card has failed us, so we never try it again this run


def _load(force_cpu: bool = False):
    """Load the speech model on the graphics card if one works, else on the processor.
    Older cards (GTX 10xx) can't do float16 and Windows PCs often lack the CUDA libraries, so every GPU option is
    tried and the CPU is always the fallback. A failure here must never stop a clip job."""
    global _model, _on_cpu
    with _lock:
        if force_cpu and not _on_cpu:
            _model, _on_cpu = None, True
        if _model is None:
            from faster_whisper import WhisperModel

            device = "cpu" if _on_cpu else config.WHISPER_DEVICE
            if device == "auto":
                try:
                    import ctranslate2
                    device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
                except Exception:  # noqa: BLE001
                    device = "cpu"
            tries = [("cuda", "float16"), ("cuda", "int8_float32"), ("cpu", "int8")] if device == "cuda" \
                else [("cpu", "int8")]
            err = None
            for dev, compute in tries:
                try:
                    _model = WhisperModel(config.WHISPER_MODEL or "small", device=dev, compute_type=compute)
                    _on_cpu = dev == "cpu"
                    break
                except Exception as e:  # noqa: BLE001
                    err = e
            if _model is None:
                raise RuntimeError(f"Couldn't load the speech model: {err}")
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
    for attempt in (0, 1):
        try:
            model = _load(force_cpu=attempt == 1)
            lang = language or config.WHISPER_LANGUAGE or None
            segments, info = model.transcribe(
                audio, language=lang, word_timestamps=True,
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 400},
                # Hindi/Urdu need a wider search: greedy decoding turns Hinglish into nonsense
                beam_size=5 if lang in ("hi", "ur") else 1,
            )
            words = []
            for seg in segments:
                for w in seg.words or []:
                    text = (w.word or "").strip()
                    if text:
                        words.append({"w": text, "s": round(w.start + start, 3), "e": round(w.end + start, 3)})
            if (info.language or "") in ("hi", "ur") and words:
                words = _hinglish(words, log)
            return {"language": info.language or "", "words": words}
        except Exception as e:  # noqa: BLE001
            if attempt == 0 and not _on_cpu:
                log(f"Graphics card speech failed ({str(e)[:80]}), switching to the processor")
                continue
            raise
    return {"language": "", "words": []}


NON_LATIN = re.compile(r"[\u0900-\u097F\u0600-\u06FF]")


def _hinglish(words: list[dict], log=print) -> list[dict]:
    """Devanagari/Urdu words → Roman Hinglish through the AI, when it's set up. Timings stay the same."""
    idx = [i for i, w in enumerate(words) if NON_LATIN.search(w["w"])]
    if not idx:
        return words
    try:
        from . import ai

        rom = ai.romanize([words[i]["w"] for i in idx])
    except Exception as e:  # noqa: BLE001
        log(f"Hinglish captions skipped: {str(e)[:120]}")
        return words
    if not rom:
        return words
    out = [dict(w) for w in words]
    for i, r in zip(idx, rom):
        out[i]["w"] = r
    return out


def readable(text: str) -> bool:
    """False when a title is mostly Hindi/Urdu script (unreadable for the Hinglish audience)."""
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(1 for c in letters if NON_LATIN.match(c)) / len(letters) < 0.3


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
