# E2E testcases

This folder contains Playwright end-to-end suites.

## Structure

Each suite lives under:

- `testcases/<suite_name>/run_test.py`
- `testcases/<suite_name>/output/` (artifacts, ignored by git)

The `run_test.py` script should:
- Exit `0` on success
- Exit non-zero on failure
- Write screenshots/logs into `OUTPUT_DIR` (provided by the runner)

## Suites

| Suite | What it checks |
|-------|----------------|
| `smoke_home` | Lobby renders with the four avatar previews; the embedded LiveKit server reports `running` and the speech model `ready`; translations default to 繁體中文; `/rtc` is relayed; the vendored MediaPipe files and the subtitle worklet are served; `/translate` refuses callers without a room token. |
| `two_users_subtitles` | Ben joins a room, then Amy, whose fake microphone speaks English (`fixtures/speech.py`). Ben reads her words as partial then final subtitles, in the transcript and as a caption on her tile; Amy reads her own. Ben selects a word, right-clicks, *Translate to 繁體中文* shows Chinese; after switching to 日本語, Japanese. Amy mutes: her subtitles stop. Ben's language survives a reload. Needs Ollama with `OLLAMA_MODEL`. |
| `two_users_avatar` | Two browsers join one room. Alice's fake camera shows a face (`fixtures/face.mjpeg`) that her browser tracks; Bob's shows no face. Each receives the other's face packets and audio; no video is published anywhere, the server only allows the microphone and refuses a camera publish; injected expressions (laugh, surprise, head turn) arrive exactly and read as the right emotion; style change, camera off (released) / on, mute and leave reach the other side. Prints each side's uplink (audio, face data, video). |
| `call_intent` | The `call.join` intent opened by a fake caller page (cross-origin iframe, camera + microphone delegated): two callers join under their names, see the title, hear each other, receive each other's tracked face and read each other's subtitles, with no video; hang up posts `result`; a room passed privately (postMessage after `ready`) joins without appearing in the URL; a call without `room` fails the schema and Close posts `cancel` with the error. |
| `settings_admin` | `/settings` status, admin gate (wrong/right `ADMIN_PASSCODE`), connection info for other apps, server restart, server log. |

## Running

Use the repo-level runner (it manages starting/stopping the Reflex server and collecting artifacts):

```bash
poetry run ./run_test_suite.sh smoke_home
OLLAMA_MODEL=qwen2.5:7b poetry run ./run_test_suite.sh two_users_subtitles
poetry run ./run_test_suite.sh two_users_avatar
ADMIN_PASSCODE=test-pass poetry run ./run_test_suite.sh settings_admin
poetry run ./run_test_suite.sh call_intent
```

Locally the embedded server needs a `livekit-server` binary on PATH
(`brew install livekit` on macOS; Linux downloads it automatically).
If the media ports are taken (e.g. by an installed `smart-app-english-chat`
container), move them: `LIVEKIT_RTC_TCP_PORT=7691 LIVEKIT_RTC_UDP_PORT=7692`.
The first run downloads the speech model (about 470 MB); `smoke_home` waits for it.

The subtitle suites start Chromium with `--disable-audio-output` (a fake speaker):
without a usable audio output, as in headless runs or sandboxes, Web Audio's clock
stands still and the microphone never reaches the subtitle worklet.

To test a deployed instance (e.g. behind re-ddns), run the script directly:

```bash
BASE_URL=https://english-chat.reflex-ddns.com OUTPUT_DIR=/tmp/english-chat-e2e \
  poetry run python testcases/two_users_subtitles/run_test.py
```

Keep `OUTPUT_DIR` outside the repo when the target is a re-ddns **dev-mode** install of
this checkout: that container watches the mounted source, so screenshots written under
`testcases/` hot-reload its backend in the middle of the test.

Optional env vars:
- `BASE_URL` (default `http://127.0.0.1:3000`)
- `HEADLESS=0` to watch the browser
- `PW_SLOWMO_MS=100` to slow actions down so you can see interactions
- `PW_TIMEOUT_MS=60000` to increase Playwright timeouts
- `EXPECT_MEDIA_PORT=7682` to assert that audio and face data flow over this server port (on at least one peer connection)
