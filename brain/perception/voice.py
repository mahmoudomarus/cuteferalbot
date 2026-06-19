"""Local speech-to-text + optional spoken responses.

Backends are imported lazily so the base brain stays lean. STT prefers
Parakeet on Apple Silicon (Neural Engine, ~130 ms latency via
`parakeet-mlx`) and falls back to `faster-whisper`. Recording uses
`sounddevice`. Speech is captured with a simple energy-based VAD: start
recording, keep going while you hear voice, stop after a short silence.

CLI:
    python -m brain.perception.voice listen          # one transcription
    python -m brain.perception.voice loop --agent    # voice-driven Agent
    python -m brain.perception.voice say "hello"     # TTS smoke test
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

SAMPLE_RATE = 16000


# ---- recording -------------------------------------------------------------


def _import_sounddevice():
    try:
        import sounddevice as sd                     # type: ignore[import-not-found]
        import numpy as np
        return sd, np
    except Exception as e:                          # pragma: no cover
        raise RuntimeError(
            "sounddevice + numpy are required for voice. "
            "pip install sounddevice numpy") from e


def record(seconds: float, samplerate: int = SAMPLE_RATE):
    """Block-record `seconds` of mono float32 audio."""
    sd, np = _import_sounddevice()
    audio = sd.rec(int(seconds * samplerate),
                   samplerate=samplerate, channels=1, dtype="float32")
    sd.wait()
    return audio.reshape(-1)


def listen(max_seconds: float = 12.0,
           silence_s: float = 1.2,
           threshold: float = 0.01,
           samplerate: int = SAMPLE_RATE):
    """Record from the mic with simple energy-VAD.

    Starts capturing immediately. Once voice has been detected (RMS above
    `threshold`), keeps recording while voice continues; stops `silence_s`
    after voice drops back below `threshold`. Returns a 1-D float32 ndarray
    or None if nothing was heard within `max_seconds`."""
    sd, np = _import_sounddevice()
    block_s = 0.05
    block = int(block_s * samplerate)
    chunks: list = []
    started = False
    last_voice = 0.0
    deadline = time.time() + max_seconds

    def cb(indata, frames, t, status):
        nonlocal started, last_voice
        sample = indata[:, 0].copy()
        rms = float(np.sqrt(np.mean(sample * sample)) + 1e-12)
        chunks.append(sample)
        if rms > threshold:
            if not started:
                started = True
            last_voice = time.time()

    with sd.InputStream(samplerate=samplerate, channels=1,
                        blocksize=block, dtype="float32",
                        callback=cb):
        while time.time() < deadline:
            time.sleep(block_s)
            if started and (time.time() - last_voice) > silence_s:
                break
    if not started:
        return None
    return np.concatenate(chunks)


# ---- transcription ---------------------------------------------------------


@dataclass
class _STTState:
    backend: Optional[str] = None
    model: Any = None


_stt = _STTState()


def _load_stt(prefer: Optional[str] = None) -> str:
    """Lazy-load the first STT backend that imports cleanly. Returns the
    backend name."""
    if _stt.backend is not None and (prefer is None or _stt.backend == prefer):
        return _stt.backend
    order = (prefer, "parakeet", "whisper") if prefer else ("parakeet", "whisper")
    errors: list[str] = []
    for name in order:
        if name is None:
            continue
        try:
            if name == "parakeet":
                from parakeet_mlx import from_pretrained  # type: ignore[import-not-found]
                _stt.model = from_pretrained(
                    os.environ.get("QTBOT_PARAKEET",
                                   "mlx-community/parakeet-tdt-0.6b-v2"))
            elif name == "whisper":
                from faster_whisper import WhisperModel  # type: ignore[import-not-found]
                _stt.model = WhisperModel(
                    os.environ.get("QTBOT_WHISPER", "base.en"),
                    device="cpu", compute_type="int8")
            else:
                continue
            _stt.backend = name
            return name
        except Exception as e:                       # pragma: no cover
            errors.append(f"{name}: {e}")
    raise RuntimeError(
        "no STT backend available. Install one:\n"
        "  pip install parakeet-mlx     # Apple Silicon, fastest\n"
        "  pip install faster-whisper   # cross-platform fallback\n"
        + ("\nLast errors:\n  " + "\n  ".join(errors) if errors else ""))


def transcribe(audio, samplerate: int = SAMPLE_RATE,
               backend: Optional[str] = None) -> str:
    """Synchronous transcription. `audio` is a 1-D float32 ndarray of mono
    samples in [-1, 1] at `samplerate`."""
    name = _load_stt(backend)
    if name == "parakeet":
        # parakeet-mlx accepts a numpy array directly
        result = _stt.model.transcribe(audio)
        return getattr(result, "text", str(result)).strip()
    if name == "whisper":
        # faster-whisper takes float32 in [-1,1] mono via numpy
        segments, _ = _stt.model.transcribe(audio, language="en")
        return " ".join(s.text for s in segments).strip()
    return ""                                         # pragma: no cover


def listen_and_transcribe(**kwargs) -> Optional[str]:
    audio = listen(**kwargs)
    if audio is None:
        return None
    return transcribe(audio)


# ---- text-to-speech --------------------------------------------------------


def speak(text: str, *, voice: Optional[str] = None) -> bool:
    """Best-effort spoken output. Uses macOS `say` when available and
    silently falls back to no-op elsewhere. Returns True if something was
    actually spoken."""
    if not text:
        return False
    if shutil.which("say"):
        cmd = ["say"]
        if voice:
            cmd += ["-v", voice]
        cmd.append(text)
        try:
            subprocess.run(cmd, check=False, timeout=30)
            return True
        except Exception:                            # pragma: no cover
            return False
    return False


# ---- voice -> agent loop ---------------------------------------------------


def voice_agent_loop(*, model: Optional[str] = None,
                     base_url: Optional[str] = None,
                     api_key: Optional[str] = None,
                     speak_replies: bool = True,
                     camera: int = 0,
                     no_localize: bool = False,
                     no_detect: bool = False,
                     verbose: bool = False) -> int:
    """Press Enter to speak, then the LLM reasons + acts."""
    from brain.cognition.agent import (
        Agent, DEFAULT_BASE_URL, DEFAULT_MODEL, _live_stack)
    from brain.tools import Toolbelt

    robot, loc, det = _live_stack(camera, no_localize, no_detect)
    try:
        toolbelt = Toolbelt(robot=robot, localizer=loc, detector=det)
        agent = Agent(toolbelt,
                      model=model or DEFAULT_MODEL,
                      base_url=base_url or DEFAULT_BASE_URL,
                      api_key=api_key,
                      verbose=verbose)
        print(f"voice loop ready. press Enter, speak, pause to send. "
              f"Ctrl-C to quit.")
        while True:
            try:
                input("\n[hold to talk, then release]")
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            print("listening...")
            text = listen_and_transcribe()
            if not text:
                print("(no speech)")
                continue
            print(f"you> {text}")
            res = agent.run(text)
            print(f"agent> {res.text}")
            if speak_replies:
                speak(res.text)
    finally:
        try: robot.close()
        except Exception: pass
        if loc: loc.stop()
        if det: det.stop()


# ---- CLI -------------------------------------------------------------------


def cmd_listen(args: argparse.Namespace) -> int:
    print(f"listening (backend={args.backend or 'auto'})...")
    audio = listen(max_seconds=args.seconds, silence_s=args.silence,
                   threshold=args.threshold)
    if audio is None:
        print("no speech")
        return 1
    text = transcribe(audio, backend=args.backend)
    print(text)
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    return 0 if speak(args.text, voice=args.voice) else 1


def cmd_loop(args: argparse.Namespace) -> int:
    return voice_agent_loop(
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        speak_replies=not args.silent,
        camera=args.camera,
        no_localize=args.no_localize,
        no_detect=args.no_detect,
        verbose=args.verbose)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    l = sub.add_parser("listen", help="single transcription")
    l.add_argument("--seconds", type=float, default=12.0)
    l.add_argument("--silence", type=float, default=1.2)
    l.add_argument("--threshold", type=float, default=0.01)
    l.add_argument("--backend", choices=("parakeet", "whisper"))
    l.set_defaults(func=cmd_listen)

    s = sub.add_parser("say", help="speak text via macOS say")
    s.add_argument("text")
    s.add_argument("--voice")
    s.set_defaults(func=cmd_say)

    a = sub.add_parser("loop", help="voice -> agent loop")
    a.add_argument("--model")
    a.add_argument("--base-url")
    a.add_argument("--api-key")
    a.add_argument("--silent", action="store_true",
                   help="don't speak the agent's replies")
    a.add_argument("--camera", type=int, default=0)
    a.add_argument("--no-localize", action="store_true")
    a.add_argument("--no-detect", action="store_true")
    a.add_argument("--verbose", action="store_true")
    a.set_defaults(func=cmd_loop)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
