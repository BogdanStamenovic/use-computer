"""Hearing: record what the computer plays (or the mic) and transcribe it.

Privacy rules are enforced here, not just documented: recording happens only
inside an explicit listen() call, for a bounded duration, and the recording is
deleted after transcription unless the caller asks to keep it.

Runs in the calling process (MCP server or CLI), not the daemon: transcription
takes seconds to minutes and would otherwise block screen control.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import UseComputerError

DEFAULT_MODEL = os.environ.get("USE_COMPUTER_WHISPER_MODEL", "large-v3-turbo")
MAX_LISTEN_SECONDS = 300

_models: dict[str, Any] = {}


def _whisper():
    try:
        import faster_whisper
    except ImportError as exc:
        raise UseComputerError(
            "audio support is not installed; run `use-computer audio setup`") from exc
    return faster_whisper


def model_status(model: str = DEFAULT_MODEL) -> dict[str, Any]:
    try:
        fw = _whisper()
    except UseComputerError as exc:
        return {"installed": False, "model": model, "downloaded": False, "detail": str(exc)}
    try:
        path = fw.download_model(model, local_files_only=True)
        return {"installed": True, "model": model, "downloaded": True, "path": path}
    except Exception:  # huggingface_hub raises several unrelated types for "not cached"
        return {"installed": True, "model": model, "downloaded": False}


def download(model: str = DEFAULT_MODEL) -> str:
    return str(_whisper().download_model(model))


def _load(model: str):
    if model not in _models:
        fw = _whisper()
        try:
            path = fw.download_model(model, local_files_only=True)
        except Exception as exc:
            raise UseComputerError(
                f"whisper model {model!r} is not downloaded (~1.6 GB for large-v3-turbo); "
                "ask the user, then run `use-computer audio setup`") from exc
        _models.clear()  # keep at most one model in RAM
        _models[model] = fw.WhisperModel(path, device="cpu", compute_type="int8",
                                         cpu_threads=os.cpu_count() or 4)
    return _models[model]


def transcribe(path: str | Path, model: str = DEFAULT_MODEL,
               language: str | None = None) -> dict[str, Any]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise UseComputerError(f"no such audio file: {p}")
    m = _load(model)
    segments, info = m.transcribe(str(p), language=language, vad_filter=True, beam_size=5)
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
            for s in segments]
    return {
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "duration": round(info.duration, 2),
        "text": " ".join(s["text"] for s in segs).strip(),
        "segments": segs,
    }


def record(seconds: float, source: str = "output", dest: str | Path | None = None) -> Path:
    if shutil.which("pw-record") is None:
        raise UseComputerError("pw-record (PipeWire) is required for listening")
    if not 0 < seconds <= MAX_LISTEN_SECONDS:
        raise UseComputerError(f"seconds must be between 0 and {MAX_LISTEN_SECONDS}")
    if source not in ("output", "mic"):
        raise UseComputerError("source must be 'output' (what the computer plays) or 'mic'")
    out = Path(dest) if dest else Path(tempfile.mkstemp(prefix="use-computer-", suffix=".wav")[1])
    cmd = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16"]
    if source == "output":
        cmd += ["-P", "{ stream.capture.sink=true }"]
    cmd.append(str(out))
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    else:
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        raise UseComputerError(f"pw-record exited early: {err.strip()}")
    if not out.exists() or out.stat().st_size < 1000:
        raise UseComputerError("recording is empty; is anything playing / is the source muted?")
    return out


def listen(seconds: float, source: str = "output", model: str = DEFAULT_MODEL,
           language: str | None = None, keep: bool = False) -> dict[str, Any]:
    _load(model)  # fail before recording, not after
    wav = record(seconds, source)
    try:
        result = transcribe(wav, model, language)
    finally:
        if not keep:
            wav.unlink(missing_ok=True)
    result["source"] = source
    if keep:
        result["recording"] = str(wav)
    return result
