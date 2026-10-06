"""English speech for the fake microphones of the subtitle tests, made at test time.

macOS `say` (or espeak-ng elsewhere) speaks SENTENCES; Chromium plays the file
in a loop with --use-file-for-fake-audio-capture. Write it outside the project
(e.g. a temporary directory): a new file in the project hot-reloads the backend.
"""

import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

RATE = 48000
SENTENCES = [
    "Hi Ben, how was your weekend?",
    "I went hiking in the mountains with my sister, and the weather was beautiful.",
]


def _speak(text: str, path: Path) -> np.ndarray:
    """``text`` spoken, as 48 kHz int16 samples."""
    if shutil.which("say"):
        subprocess.run(["say", "-v", "Samantha", "-o", str(path), f"--data-format=LEI16@{RATE}", text], check=True)
    elif shutil.which("espeak-ng") or shutil.which("espeak"):
        espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        subprocess.run([espeak, "-v", "en-us", "-w", str(path), text], check=True)
    else:
        raise RuntimeError("No speech synthesizer: install espeak-ng (Linux) or run on macOS")
    with wave.open(str(path)) as w:
        samples = np.frombuffer(w.readframes(w.getnframes()), np.int16)
        rate = w.getframerate()
    if rate != RATE:  # espeak speaks at 22050 Hz
        samples = np.interp(np.arange(0, len(samples), rate / RATE), np.arange(len(samples)), samples).astype(np.int16)
    return samples


def _write(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.astype(np.int16).tobytes())


def make_fixtures(folder: Path) -> tuple[Path, Path]:
    """The sentences with a pause after each (a subtitle line each), and silence."""
    pause = np.zeros(int(RATE * 1.3), np.int16)
    parts = []
    for i, text in enumerate(SENTENCES):
        parts += [_speak(text, folder / f"sentence{i}.wav"), pause]
    parts.append(np.zeros(int(RATE * 1.5), np.int16))
    speech, silence = folder / "speech.wav", folder / "silence.wav"
    _write(speech, np.concatenate(parts))
    _write(silence, np.zeros(RATE * 2, np.int16))
    return speech, silence
