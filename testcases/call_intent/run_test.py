"""The `call.join` DDNS Intent, opened by another app (a fake caller page).

A fake host on :3999 embeds /intent/call-join the way another
*.reflex-ddns.com app's intent dialog does (cross-origin iframe, camera and
microphone delegated with `allow`). Then:

1. Two callers open the same call: each joins right away under its `name`,
   sees the `title` instead of the call id, hears the other and receives the
   other's face packets (the fake camera shows a face, tracked in the dialog).
   Nobody publishes video. Each reads the other's words as subtitles in the
   dialog (the fake microphones speak English).
2. Hang up posts a `result` ({"status": "ended", "room": <call id>}) to the caller.
3. Private params: with `_intent_wait=1` the room comes by postMessage after
   `ready` (as `Intent.start(private=...)` sends it), never in the URL.
4. A call without `room` fails the `call.join` schema: the page shows the error
   and Close posts `cancel` with it.
"""

import html
import json
import os
import secrets
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlparse

from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fixtures.speech import make_fixtures  # noqa: E402

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:3000").rstrip("/")
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
TIMEOUT_MS = int(os.environ.get("PW_TIMEOUT_MS", "60000"))
HOST_ORIGIN = "http://localhost:3999"
FACE_VIDEO = Path(__file__).resolve().parent.parent / "fixtures" / "face.mjpeg"

HOST_PAGE = """<!doctype html><html><body>
<iframe id="frame" src="%s" allow="autoplay; microphone; camera" style="width:800px;height:620px;border:0"></iframe>
<script>
  window.msgs = [];
  const privateParams = %s;
  window.addEventListener("message", (ev) => {
    if (!(ev.data && ev.data.type === "ddns-intent")) return;
    window.msgs.push({origin: ev.origin, ...ev.data});
    // Like intent_host(): answer the dialog's `ready` with the private params.
    if (privateParams && ev.data.event === "ready") {
      document.getElementById("frame").contentWindow.postMessage(
        {type: "ddns-intent", v: 1, id: ev.data.id, event: "params", data: privateParams}, ev.origin);
    }
  });
</script></body></html>"""

_INBOUND_AUDIO_BYTES = """async () => {
    const room = window.livekitClient && window.livekitClient.room;
    if (!room) return -1;
    let total = 0;
    for (const p of room.remoteParticipants.values()) {
        for (const pub of p.audioTrackPublications.values()) {
            const track = pub.track;
            if (!track || !track.receiver) continue;
            const stats = await track.receiver.getStats();
            stats.forEach((r) => { if (r.type === 'inbound-rtp') total += r.bytesReceived || 0; });
        }
    }
    return total;
}"""

_VIDEO_PUBLICATIONS = """() => {
    const room = window.livekitClient.room;
    let count = room.localParticipant.videoTrackPublications.size;
    room.remoteParticipants.forEach((p) => { count += p.videoTrackPublications.size; });
    return count;
}"""


class HostHandler(BaseHTTPRequestHandler):
    """Serves the fake caller page; ?src= is the iframe URL, ?private= its private params."""

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        src = query.get("src", [""])[0]
        private = json.dumps(json.loads(query.get("private", ["null"])[0])).replace("</", "<\\/")
        body = (HOST_PAGE % (html.escape(src, quote=True), private)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def open_call(page: Page, call_id: str, private: dict | None = None, **params) -> str:
    query = {**params, "_intent_id": call_id, "_intent_origin": HOST_ORIGIN}
    if private:
        query["_intent_wait"] = "1"
    src = f"{BASE_URL}/intent/call-join?{urlencode(query)}"
    host = f"{HOST_ORIGIN}/?src={quote(src, safe='')}"
    if private:
        host += f"&private={quote(json.dumps(private), safe='')}"
    page.goto(host, wait_until="domcontentloaded")
    return src


def wait_for_message(page: Page, event: str) -> dict:
    page.wait_for_function(f"window.msgs.some(m => m.event === '{event}')", timeout=TIMEOUT_MS)
    return next(m for m in page.evaluate("window.msgs") if m["event"] == event)


def inbound_audio(page: Page) -> int:
    return page.query_selector("#frame").content_frame().evaluate(_INBOUND_AUDIO_BYTES)


def face_of(page: Page, name: str) -> dict | None:
    """What the call dialog knows of ``name``'s avatar (see livekitClient.faceOf)."""
    return page.query_selector("#frame").content_frame().evaluate("(n) => window.livekitClient.faceOf(n)", name)


def run():
    output_dir = os.environ.get("OUTPUT_DIR") or os.path.join(os.path.dirname(__file__), "output")
    os.makedirs(output_dir, exist_ok=True)
    shots: dict[str, bytes] = {}

    server = ThreadingHTTPServer(("localhost", 3999), HostHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    room = f"call-{secrets.token_hex(4)}"
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        speech, _silence = make_fixtures(Path(tmp))
        browser = p.chromium.launch(
            headless=HEADLESS,
            # A fake speaker (--disable-audio-output) keeps Web Audio, and so the
            # subtitles' microphone capture, running without a sound device.
            args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                  f"--use-file-for-fake-video-capture={FACE_VIDEO}",
                  f"--use-file-for-fake-audio-capture={speech}", "--disable-audio-output",
                  "--autoplay-policy=no-user-gesture-required"],
        )
        contexts = [browser.new_context(permissions=["microphone", "camera"]) for _ in range(2)]
        try:
            print("Two callers open the same call...")
            pages = []
            for ctx, name in zip(contexts, ("Amy", "Ben")):
                page = ctx.new_page()
                page.set_default_timeout(TIMEOUT_MS)
                open_call(page, f"id-{name}", room=room, title="Design Review", name=name,
                          user=f"{name.lower()}@example.com")
                pages.append(page)
            for page in pages:
                frame = page.frame_locator("#frame")
                expect(frame.locator("#connection-status")).to_have_text("Connected", timeout=120_000)
                expect(frame.get_by_role("heading", name="Design Review")).to_be_visible()
                expect(frame.get_by_text(room)).to_have_count(0)
                expect(frame.locator(".participant-tile")).to_have_count(2, timeout=TIMEOUT_MS)
                for name in ("Amy", "Ben"):
                    expect(frame.locator(".participant-tile", has_text=name)).to_be_visible()
            for page in pages:
                first = inbound_audio(page)
                for _ in range(30):  # a just-joined call may take a moment to deliver its first bytes
                    if first > 0:
                        break
                    page.wait_for_timeout(500)
                    first = inbound_audio(page)
                page.wait_for_timeout(2000)
                second = inbound_audio(page)
                print(f"  inbound audio bytes: {first} -> {second}")
                assert second > first > 0, "no audio received"
            for page, other in zip(pages, ("Ben", "Amy")):
                for _ in range(120):  # the dialog loads the face tracker first
                    face = face_of(page, other)
                    if face and face["face"] and face["packets"] > 20:
                        break
                    page.wait_for_timeout(500)
                print(f"  {other}'s face: tracked={face['face']}, {face['packets']} packets, {face['bytes']} bytes")
                assert face["face"] and face["packets"] > 20, f"no face packets from {other}"
                videos = page.query_selector("#frame").content_frame().evaluate(_VIDEO_PUBLICATIONS)
                assert videos == 0, f"{videos} video publications in the call"
            print("Subtitles in the dialog: each reads the other's words...")
            for page, other in zip(pages, ("Ben", "Amy")):
                frame = page.query_selector("#frame").content_frame()
                lines: list[str] = []
                for _ in range(180):
                    lines = frame.evaluate(
                        "(n) => window.liveSubtitles.transcript().filter((l) => l.name === n && l.final)"
                        ".map((l) => l.text)",
                        other,
                    )
                    if any("weekend" in line.lower() for line in lines):
                        break
                    page.wait_for_timeout(500)
                print(f"  {other} said: {lines[:2]}")
                assert any("weekend" in line.lower() for line in lines), f"no subtitles from {other} in the dialog"
            shots["call"] = pages[0].screenshot()

            print("Hang up answers the caller...")
            pages[1].frame_locator("#frame").get_by_role("button", name="Hang up").click()
            result = wait_for_message(pages[1], "result")
            print("  result:", json.dumps(result))
            assert result["id"] == "id-Ben", result
            assert result["data"] == {"status": "ended", "room": room}, result
            expect(pages[0].frame_locator("#frame").locator(".participant-tile")).to_have_count(1, timeout=TIMEOUT_MS)

            print("Private params: the room arrives by postMessage, not in the URL...")
            secret_room = f"call-{secrets.token_hex(4)}"
            page = contexts[1].new_page()
            page.set_default_timeout(TIMEOUT_MS)
            src = open_call(page, "id-private", private={"room": secret_room}, title="Secret Sync", name="Cat")
            assert secret_room not in src, src
            frame = page.frame_locator("#frame")
            expect(frame.locator("#connection-status")).to_have_text("Connected", timeout=120_000)
            expect(frame.get_by_role("heading", name="Secret Sync")).to_be_visible()
            assert secret_room not in page.query_selector("#frame").get_attribute("src")
            frame.get_by_role("button", name="Hang up").click()
            result = wait_for_message(page, "result")
            assert result["data"] == {"status": "ended", "room": secret_room}, result
            page.close()

            print("A call without a room fails the schema; Close cancels with the error...")
            page = contexts[1].new_page()
            open_call(page, "id-bad", name="Ben")
            frame = page.frame_locator("#frame")
            expect(frame.get_by_text("Missing required parameter: room")).to_be_visible(timeout=TIMEOUT_MS)
            shots["missing_room"] = page.screenshot()
            frame.get_by_role("button", name="Close").click()
            cancel = wait_for_message(page, "cancel")
            assert cancel["id"] == "id-bad", cancel
            assert cancel["data"] == {"error": "Missing required parameter: room"}, cancel

            print("All tests passed!")
        except Exception as e:
            print(f"TEST FAILED: {e}")
            for i, ctx in enumerate(contexts):
                for j, pg in enumerate(ctx.pages):
                    pg.screenshot(path=os.path.join(output_dir, f"failure_{i}_{j}.png"))
            sys.exit(1)
        finally:
            browser.close()
            server.shutdown()
            for name, png in shots.items():
                with open(os.path.join(output_dir, f"{name}.png"), "wb") as f:
                    f.write(png)


if __name__ == "__main__":
    run()
