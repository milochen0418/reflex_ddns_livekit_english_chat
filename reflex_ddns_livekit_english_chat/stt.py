"""Live English subtitles: Whisper speech-to-text inside this app's backend.

Each speaker's browser streams its own microphone here (besides sending it to
LiveKit), shows the text that comes back as its subtitles, and passes them on
to the room, so everyone reads what everyone says:

    WS /stt?token=<the LiveKit token the browser joined the room with>
        browser -> here: binary messages, 16 kHz mono 16-bit little-endian PCM
        here -> browser: {"type": "status", "phase": "downloading" | "loading" | "ready" | "error", ...}
                         {"type": "subtitle", "seg": 3, "text": "...", "final": false}

A pause ends a segment: its text is final. While someone speaks, the segment
so far is transcribed again about once a second (partial), so the subtitle
grows as the words come. Only room participants (a valid token) may connect.

The model (faster-whisper, ``WHISPER_MODEL``, default ``small.en``, about
470 MB) is downloaded once into the app's data directory and loaded when the
backend starts, so the very first start takes a minute or two.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import numpy as np
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from reflex_ddns_livekit_english_chat import livekit_server

logger = logging.getLogger(__name__)

MODEL = os.environ.get("WHISPER_MODEL", "").strip() or "small.en"
THREADS = int(os.environ.get("WHISPER_THREADS", "") or min(8, os.cpu_count() or 4))
# Transcriptions that may run at once (several people speaking).
WORKERS = int(os.environ.get("WHISPER_WORKERS", "") or 2)
# Outside the project directory, so a download never triggers a hot reload.
MODELS_DIR = livekit_server.HOME / "models"

RATE = 16000
FRAME = RATE * 30 // 1000  # 30 ms: the unit of the pause detector
PREROLL_FRAMES = 10  # 300 ms kept from before the speech starts, so the first word is whole
END_SILENCE_S = 0.7  # a pause this long ends a segment
MAX_SEGMENT_S = 15.0  # longer speech is cut into segments anyway
MIN_SPEECH_S = 0.25  # shorter sounds (a cough, a click) are dropped
PARTIAL_EVERY_S = 1.0  # how often a growing segment is transcribed again
IDLE_END_S = 1.0  # no audio for this long (muted, tab asleep) ends a segment


class _Engine:
    """The one Whisper model of this process, shared by every speaker."""

    def __init__(self) -> None:
        self.model = None
        self.phase = "loading"
        self.message = ""
        self._pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="whisper")
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """Load the model in the background, once."""
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._load, name="whisper-load", daemon=True).start()

    def _load(self) -> None:
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model

        try:
            if os.path.isdir(MODEL):
                path = MODEL
            else:
                try:
                    path = download_model(MODEL, cache_dir=str(MODELS_DIR), local_files_only=True)
                except Exception:  # noqa: BLE001 - not downloaded yet
                    self.phase = "downloading"
                    logger.info("Downloading speech model %s into %s", MODEL, MODELS_DIR)
                    path = download_model(MODEL, cache_dir=str(MODELS_DIR))
            self.phase = "loading"
            self.model = WhisperModel(
                path, device="cpu", compute_type="int8", cpu_threads=THREADS, num_workers=WORKERS
            )
            self.phase = "ready"
            logger.info("Speech model %s ready (%s threads, %s workers)", MODEL, THREADS, WORKERS)
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            logger.exception("Speech model %s unavailable", MODEL)
            self.message = f"Speech model {MODEL} unavailable: {exc}"
            self.phase = "error"

    def _run(self, audio: np.ndarray, prompt: str, final: bool) -> str:
        segments, _info = self.model.transcribe(
            audio,
            language="en",
            # A final subtitle gets a more careful decode; partials are replaced within a second.
            beam_size=3 if final else 1,
            temperature=(0.0, 0.2, 0.4) if final else 0.0,
            without_timestamps=True,
            condition_on_previous_text=False,
            # What this person said last: helps with names and punctuation.
            initial_prompt=prompt or None,
            # Drops noise Whisper would otherwise "hear" words in.
            vad_filter=True,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def transcribe(self, audio: np.ndarray, prompt: str, *, final: bool) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self._run, audio, prompt, final)


engine = _Engine()


def status() -> dict[str, str]:
    return {"phase": engine.phase, "message": engine.message, "model": MODEL}


class _Speaker:
    """Cuts one microphone into segments at its pauses, by loudness."""

    def __init__(self, on_start: Callable[[], None], on_end: Callable[[np.ndarray | None], None]) -> None:
        self._on_start = on_start
        self._on_end = on_end
        self._noise = 0.003  # loudness of the background, followed while nobody speaks
        self._rest = np.zeros(0, np.float32)  # samples short of a frame
        self._preroll: collections.deque[np.ndarray] = collections.deque(maxlen=PREROLL_FRAMES)
        self._frames: list[np.ndarray] = []
        self._voiced = 0  # frames with speech in the segment
        self._quiet = 0  # frames of silence since the last speech
        self.speaking = False

    @property
    def speech_s(self) -> float:
        return self._voiced * FRAME / RATE

    def audio(self) -> np.ndarray:
        """The segment so far."""
        return np.concatenate(self._frames) if self._frames else np.zeros(0, np.float32)

    def feed(self, samples: np.ndarray) -> None:
        data = np.concatenate([self._rest, samples]) if self._rest.size else samples
        whole = len(data) // FRAME * FRAME
        self._rest = data[whole:]
        for frame in data[:whole].reshape(-1, FRAME):
            level = float(np.sqrt(np.mean(frame * frame)))
            voiced = level > max(0.008, self._noise * 3.5)
            if not voiced:
                self._noise += (min(level, 0.05) - self._noise) * 0.05
            if not self.speaking:
                self._preroll.append(frame)
                if voiced:
                    self.speaking = True
                    self._frames = list(self._preroll)
                    self._preroll.clear()
                    self._voiced, self._quiet = 1, 0
                    self._on_start()
                continue
            self._frames.append(frame)
            if voiced:
                self._voiced += 1
                self._quiet = 0
            else:
                self._quiet += 1
            if self._quiet * FRAME >= END_SILENCE_S * RATE or len(self._frames) * FRAME >= MAX_SEGMENT_S * RATE:
                self.finish()

    def finish(self) -> None:
        """End the segment being spoken (its audio, or None for a mere blip)."""
        if not self.speaking:
            return
        # Keep 300 ms of the pause that ended it; the rest carries nothing.
        keep = len(self._frames) - max(0, self._quiet - 10)
        audio = np.concatenate(self._frames[:keep]) if keep > 0 else None
        enough = self.speech_s >= MIN_SPEECH_S
        self.speaking = False
        self._frames = []
        self._voiced = self._quiet = 0
        self._on_end(audio if enough else None)


class _Session:
    """One browser's microphone: audio in, subtitles out."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.speaker = _Speaker(self._started, self._ended)
        self.seg = 0  # the segment being spoken, or the next one
        self.finals: collections.deque[tuple[int, np.ndarray | None]] = collections.deque()
        self.partial: tuple[int, np.ndarray] | None = None
        self.partial_at = 0.0
        self.shown: set[int] = set()  # segments with a partial on screen
        self.context = ""
        self.last_audio = time.monotonic()
        self.phase_sent = ""
        self.wake = asyncio.Event()
        self.send_lock = asyncio.Lock()

    def _started(self) -> None:
        self.partial_at = time.monotonic()

    def _ended(self, audio: np.ndarray | None) -> None:
        seg = self.seg
        self.seg += 1
        # A blip needs no final, unless a partial of it is on screen and must go.
        if audio is not None or seg in self.shown:
            self.finals.append((seg, audio))
            self.wake.set()

    async def _send(self, data: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(data))

    async def _send_status(self) -> None:
        if engine.phase != self.phase_sent:
            self.phase_sent = engine.phase
            await self._send({"type": "status", "phase": engine.phase, "message": engine.message, "model": MODEL})

    async def _receive(self) -> None:
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if not data:
                continue
            self.last_audio = time.monotonic()
            if engine.phase == "ready":
                usable = len(data) // 2 * 2
                self.speaker.feed(np.frombuffer(data[:usable], dtype="<i2").astype(np.float32) / 32768.0)

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            await self._send_status()
            if not self.speaker.speaking:
                continue
            now = time.monotonic()
            if now - self.last_audio > IDLE_END_S:
                self.speaker.finish()
            elif now - self.partial_at >= PARTIAL_EVERY_S and self.speaker.speech_s >= 2 * MIN_SPEECH_S:
                self.partial = (self.seg, self.speaker.audio())
                self.partial_at = now
                self.wake.set()

    async def _work(self) -> None:
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.finals or self.partial:
                if self.finals:
                    seg, audio = self.finals.popleft()
                    text = await engine.transcribe(audio, self.context, final=True) if audio is not None else ""
                    if text:
                        self.context = text[-200:]
                    if text or seg in self.shown:
                        await self._send({"type": "subtitle", "seg": seg, "text": text, "final": True})
                    self.shown.discard(seg)
                    continue
                seg, audio = self.partial
                self.partial = None
                if seg != self.seg or not self.speaker.speaking:
                    continue  # that segment is over: its final comes instead
                text = await engine.transcribe(audio, self.context, final=False)
                if text and seg == self.seg and self.speaker.speaking:
                    self.shown.add(seg)
                    await self._send({"type": "subtitle", "seg": seg, "text": text, "final": False})

    async def run(self) -> None:
        tasks = [asyncio.create_task(job()) for job in (self._receive, self._tick, self._work)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, Exception):
                    await task


async def _subtitle_socket(ws: WebSocket) -> None:
    if livekit_server.verify_token(ws.query_params.get("token", "")) is None:
        await ws.close(code=1008)  # policy violation: not a participant
        return
    await ws.accept()
    engine.start()
    await _Session(ws).run()


@contextlib.asynccontextmanager
async def lifespan():
    """Reflex lifespan task: load the speech model while the backend starts."""
    engine.start()
    yield


def register_routes(app) -> None:  # noqa: ANN001 - Reflex App
    route = WebSocketRoute("/stt", _subtitle_socket)
    if all(getattr(r, "path", None) != route.path for r in app._api.routes):
        app._api.routes.append(route)
