import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def main() -> int:
    base_url = os.getenv("BASE_URL", "http://127.0.0.1:3000").rstrip("/")
    output_dir = Path(os.getenv("OUTPUT_DIR", "testcases/smoke_home/output"))
    output_dir.mkdir(parents=True, exist_ok=True)

    headless = env_bool("HEADLESS", True)
    timeout_ms = int(os.getenv("PW_TIMEOUT_MS", "30000"))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless, slow_mo=int(os.getenv("PW_SLOWMO_MS", "0"))
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800}, ignore_https_errors=True
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        try:
            page.goto(base_url + "/", wait_until="domcontentloaded")
            page.get_by_role("heading", name="English Rooms").wait_for()
            page.get_by_role("button", name="Join Room").wait_for()

            # First start may download livekit-server; allow up to 2 minutes.
            page.locator('#server-status[data-phase="running"]').wait_for(timeout=120_000)
            print("server status:", page.locator("#server-status").inner_text())
            # ...and the speech model (about 470 MB) for the subtitles.
            page.locator('#speech-status[data-phase="ready"]').wait_for(timeout=600_000)
            print("speech status:", page.locator("#speech-status").inner_text())

            # Translations go to 繁體中文 unless the person picks another language.
            picker = page.locator("select[data-translate-to]")
            if picker.input_value() != "zh-TW":
                raise AssertionError(f"default translation language: {picker.input_value()}")

            # The vendored SDK, avatar renderer, face tracker, subtitles and bridge must be loaded (no CDN).
            page.wait_for_function(
                "() => !!window.LivekitClient && !!window.AvatarFace && !!window.FaceTracker"
                " && !!window.liveSubtitles && !!window.livekitClient"
            )
            worklet = context.request.get(f"{base_url}/subtitle_worklet.js")
            print("GET /subtitle_worklet.js ->", worklet.status)
            if worklet.status != 200 or "registerProcessor" not in worklet.text():
                raise AssertionError(f"/subtitle_worklet.js: {worklet.status}")
            # The style picker shows the four avatars, drawn (and blinking) by the bridge.
            page.wait_for_function(
                "() => [...document.querySelectorAll('canvas[data-avatar-preview]')]"
                ".filter((c) => c.width > 0).length === 4"
            )

            # The face tracker (MediaPipe) is served by the app itself.
            for name, kind in (
                ("vision_bundle.js", "javascript"),
                ("vision_wasm_internal.js", "javascript"),
                ("vision_wasm_internal.wasm", "wasm"),
                ("face_landmarker.task", ""),
            ):
                asset = context.request.get(f"{base_url}/mediapipe/{name}")
                content_type = asset.headers.get("content-type", "")
                print(f"GET /mediapipe/{name} ->", asset.status, content_type, len(asset.body()), "bytes")
                if asset.status != 200 or kind not in content_type:
                    raise AssertionError(f"/mediapipe/{name}: {asset.status} {content_type}")

            # The backend relays /rtc to livekit-server: without a token it must
            # answer with LiveKit's own 401, not a 404 (no route) or 502 (no server).
            backend_url = os.getenv("BACKEND_URL") or base_url.replace(":3000", ":8000")
            resp = context.request.get(backend_url + "/rtc/validate")
            print("GET /rtc/validate ->", resp.status, resp.text()[:120])
            if resp.status != 401:
                raise AssertionError(f"/rtc/validate relay returned {resp.status}")

            # Only room participants may translate (or send audio to /stt).
            resp = context.request.post(backend_url + "/translate", data={"token": "nope", "text": "hello"})
            print("POST /translate without a room token ->", resp.status)
            if resp.status != 401:
                raise AssertionError(f"/translate without a token returned {resp.status}")

            page.screenshot(path=str(output_dir / "smoke.png"), full_page=True)
            return 0
        except Exception:
            try:
                page.screenshot(path=str(output_dir / "failure.png"), full_page=True)
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"smoke_home failed: {exc}", file=sys.stderr)
        raise
