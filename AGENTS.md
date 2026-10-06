# AGENTS.md

This file describes how an AI coding agent (and humans) should work in this repository.

## 0) Always read the docs first

Before making changes or running commands, **read all README.md files in this repo** and follow them.

- Repo overview & dev workflow: `README.md`
- E2E test runner conventions: `testcases/README.md`

If you already read them earlier in the session, **re-check** when unsure.

## 1) Python / Poetry requirements (strict)

This project targets **Python 3.11**.

- Prefer Poetry-managed environments.
- If Poetry selected another Python (e.g. 3.12+), switch it:

```bash
poetry env use python3.11
poetry env info
poetry install
```

If `python3.11` is not available on PATH, use an absolute path to the 3.11 executable.

## 2) Default rule: use `poetry run` for scripts

When running any repo scripts, prefer:

- `poetry run <command>`

This ensures the correct virtualenv + dependencies are used.

## 3) Running the app: prefer `reflex_rerun.sh`

When you need to start/restart the Reflex app during development, **prefer the repo helper script**.

Typical usage:

```bash
poetry run ./reflex_rerun.sh
```

Notes:
- Prefer `poetry run ./reflex_rerun.sh` over `poetry run reflex run`.
- This keeps the app startup/restart behavior consistent and helps it run stably on the expected ports: frontend `3000`, backend `8000`.
- If you must run Reflex directly, still do it via Poetry: `poetry run reflex run`.

## 4) Running E2E suites: prefer `run_test_suite.sh`

E2E suites live under `testcases/<suite_name>/run_test.py`.

Do **not** run Playwright suites by starting/stopping the server manually unless you have to.
Instead, use the repo runner, which manages:

- starting/stopping the Reflex server
- setting `OUTPUT_DIR`
- collecting artifacts

Run a suite like this:

```bash
poetry run ./run_test_suite.sh smoke_home
```

Optional environment variables (see `testcases/README.md`):

- `BASE_URL` (default `http://127.0.0.1:3000`)
- `HEADLESS=0` to watch the browser

## 4.5) The embedded LiveKit server

- The app is self-hosted by design: it starts its own `livekit-server` from a Reflex lifespan task (`livekit_server.py`). Do not add LiveKit Cloud settings back.
- Locally (macOS) the binary must be on PATH: `brew install livekit`. Linux downloads the pinned release automatically.
- The server runs detached, so it survives hot reloads. `reflex_rerun.sh` and `run_test_suite.sh` stop it via port `7680`.
- Ports are 7680 (signalling, localhost only), 7681/tcp and 7682/udp (media): below the avatar chat app's (7780-7782; audio chat uses 7880-7882, video chat 7980-7982), so all of them can run on one machine. Keep them apart when changing them.
- If the Docker deployment (`smart-app-english-chat`) runs on the same machine, it owns media ports 7681/7682. Run local copies and suites with `LIVEKIT_RTC_TCP_PORT=7691 LIVEKIT_RTC_UDP_PORT=7692`.
- Runtime files (binary, keys, config, `livekit-server.log`, the speech model in `models/`) live outside the repo: `~/Library/Application Support/reflex_ddns_livekit_english_chat` (macOS) or `~/.local/share/reflex_ddns_livekit_english_chat` (Linux). Never download models into the project: a new file there hot-reloads the backend.

## 4.6) Subtitles and translation

- Speech-to-text is self-hosted on purpose: Whisper (faster-whisper, `WHISPER_MODEL`, default `small.en`) runs in the Reflex backend (`stt.py`), loaded by a lifespan task. Do not swap in a cloud API or the browser's Web Speech API (it sends audio to Google/Apple).
- The first start downloads the model (minutes): `stt.py` calls `huggingface_hub.snapshot_download(tqdm_class=_DownloadBar)` so the lobby and open calls show its progress, and open calls start their subtitles by themselves when it is ready. The re-ddns catalog mounts the named volume `smart-app-english-chat-data` on the data directory, so only the first install downloads it; keep the model under `LIVEKIT_HOME`.
- Each speaker's browser streams its own microphone to `WS /stt` (16 kHz PCM from `assets/subtitle_worklet.js`) and publishes what comes back as LiveKit data packets, topic `subtitle`, reliable: `{"v": 1, "seg", "text", "final"}`. `assets/subtitles.js` renders captions (`[data-subtitle-identity]` in the tiles) and the transcript (`#subtitle-log`); Reflex renders those containers empty.
- Translation is a local LLM through Ollama (`translate.py`, `OLLAMA_URL`, `OLLAMA_MODEL`, default `qwen2.5:7b`). The languages live in `translate.LANGUAGES` (the picker and the prompt). For `zh-TW`, answers are converted to Traditional characters with OpenCC.
- `/stt` and `/translate` only serve holders of a valid room token of this app (`livekit_server.verify_token`); keep them gated. Both are listed in `backend-paths.txt` for re-ddns nginx.
- Bump `_JS_VERSION` in `livekit_bridge.py` when `subtitles.js` or `subtitle_worklet.js` change, too.
- Subtitle tests speak with Chromium's fake microphone (`--use-file-for-fake-audio-capture`), from `testcases/fixtures/speech.py` (macOS `say` / `espeak-ng`, written to a temp dir), and need `--disable-audio-output`: without a usable audio output Web Audio's clock stands still and no audio reaches the worklet. `two_users_subtitles` needs Ollama with `OLLAMA_MODEL` pulled.
- No video ever leaves the browser: the camera only feeds the face tracker (`assets/face_tracker.js`, MediaPipe vendored in `assets/mediapipe/`, see its `NOTICE.md`), and only the 60-byte face packets (topic `face`, format in `assets/livekit_bridge.js`) and the microphone are sent. The token grants `can_publish_sources=["microphone"]`; keep it that way.
- Avatars are drawn by `assets/avatar_face.js` on the tiles' `<canvas data-avatar-identity>` (Reflex renders the tiles, keyed by identity; the bridge draws every animation frame). Faces are drawn as the camera sees the person (their left eye on screen right); only our own tile is mirrored. Check visual changes on several styles and expressions, not one.
- `avatar_style` is an `rx.LocalStorage` var; the style names are listed in both `livekit_bridge.py` (`AVATAR_STYLES`) and `assets/avatar_face.js` (`STYLES`), in the same order (the face packet carries the index).
- Bump `_JS_VERSION` in `livekit_bridge.py` when `livekit_bridge.js`, `avatar_face.js`, `face_tracker.js`, `subtitles.js` or `subtitle_worklet.js` change.
- Tests track a real face from `testcases/fixtures/face.mjpeg` (Chromium fake camera) and set exact expressions with `window.livekitClient.injectFace(...)`.
- Use static icon names: `rx.icon(<Var>)` switches Reflex to lucide's `DynamicIcon`, which makes the Vite dev server load every icon module (about 1,700 requests; pages fail to load behind re-ddns nginx).

## 5) Expectations when acting as an agent

- Prefer the smallest, focused change set.
- After code changes, run the closest relevant check (at minimum the related suite via `run_test_suite.sh`).
- Report outcomes: what you ran, pass/fail, and where artifacts/logs were written.
