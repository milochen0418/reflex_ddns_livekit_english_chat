"""Two browsers in one room: each sees the other's avatar and hears their voice.

Alice's fake camera shows a face (testcases/fixtures/face.mjpeg), so her
browser really tracks it with MediaPipe; Bob's shows Chromium's test pattern
(no face). Checks:

- Bob receives Alice's face packets (face found), and Alice receives Bob's
  (camera on, no face): at most 25 a second, fewer while a face holds still
  (unchanged faces are repeated once a second). Audio flows both ways.
- No video anywhere: nobody publishes a video track, the server's grant only
  lists the microphone, and an attempt to publish the camera is refused.
- Expressions set on Alice's side (smile, surprise, head turn) arrive exactly
  (quantized to 1/255) and read as the right emotion on Bob's side.
- Alice's avatar style, camera off (camera released) / on, mute and leave
  reach Bob.

It prints each side's uplink: audio, face data, video (none).
"""

import os
import secrets
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

FACE_VIDEO = Path(__file__).resolve().parent.parent / "fixtures" / "face.mjpeg"

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

# Video publications anywhere in the room, as this browser sees it.
_VIDEO_PUBLICATIONS = """() => {
    const room = window.livekitClient.room;
    let count = room.localParticipant.videoTrackPublications.size;
    room.remoteParticipants.forEach((p) => { count += p.videoTrackPublications.size; });
    return count;
}"""

# What this browser sends: RTP bytes by kind, data channel bytes (face packets).
_UPLINK = """async () => {
    const pc = window.livekitClient.room.engine.pcManager.publisher.pc;
    const out = {audio: 0, video: 0, data: 0, messages: 0};
    (await pc.getStats()).forEach((s) => {
        if (s.type === 'outbound-rtp') out[s.kind] = (out[s.kind] || 0) + (s.bytesSent || 0);
        if (s.type === 'data-channel') { out.data += s.bytesSent || 0; out.messages += s.messagesSent || 0; }
    });
    return out;
}"""

# Selected ICE pair per peer connection (publisher = our uplink: audio and the
# face packets' data channel; subscriber = downlink).
_SELECTED_PAIRS = """async () => {
    const mgr = window.livekitClient.room.engine.pcManager;
    const out = [];
    for (const [label, transport] of [['publisher', mgr.publisher], ['subscriber', mgr.subscriber]]) {
        const pc = transport && transport.pc;
        if (!pc) continue;
        const stats = await pc.getStats();
        const byId = new Map();
        stats.forEach((s) => byId.set(s.id, s));
        let pair = null;
        stats.forEach((s) => {
            if (s.type === 'transport' && s.selectedCandidatePairId) pair = byId.get(s.selectedCandidatePairId);
        });
        if (!pair) continue;
        const local = byId.get(pair.localCandidateId) || {};
        const remote = byId.get(pair.remoteCandidateId) || {};
        out.push({
            label: label,
            local: `${local.candidateType}/${local.protocol} ${local.address || '?'}:${local.port}`,
            remote: `${remote.candidateType}/${remote.protocol} ${remote.address || '?'}:${remote.port}`,
            remote_port: String(remote.port),
            bytes: (pair.bytesSent || 0) + (pair.bytesReceived || 0),
        });
    }
    return out;
}"""

_TRY_CAMERA = """async () => {
    const local = window.livekitClient.room.localParticipant;
    try {
        await local.setCameraEnabled(true);
        return 'published';
    } catch (e) {
        return 'refused: ' + (e && e.message || e);
    }
}"""


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def join(page: Page, base_url: str, name: str, room: str) -> None:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.locator('#server-status[data-phase="running"]').wait_for(timeout=120_000)
    page.get_by_placeholder("Enter your name").fill(name)
    page.get_by_placeholder("Enter room to join").fill(room)
    page.get_by_role("button", name="Join Room").click()
    page.wait_for_function(
        "() => document.getElementById('connection-status')?.textContent === 'Connected'"
    )


def wait_for_tiles(page: Page, count: int) -> None:
    page.wait_for_function(
        f"() => document.querySelectorAll('.participant-tile').length === {count}"
    )


def face_of(page: Page, name: str) -> dict | None:
    return page.evaluate("(name) => window.livekitClient.faceOf(name)", name)


def wait_face(page: Page, name: str, check: str, timeout_ms: int = 30_000) -> dict:
    """Wait until faceOf(name) satisfies the JS expression ``check`` (on ``f``)."""
    page.wait_for_function(
        f"(name) => {{ const f = window.livekitClient.faceOf(name); return !!f && ({check}); }}",
        arg=name,
        timeout=timeout_ms,
    )
    return face_of(page, name)


def wait_for_audio(page: Page, label: str, timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    previous = -1
    while time.time() < deadline:
        current = page.evaluate(_INBOUND_AUDIO_BYTES)
        if previous > 2000 and current > previous:
            print(f"[{label}] inbound audio bytes: {previous} -> {current}")
            return
        previous = current
        time.sleep(1.0)
    raise AssertionError(f"[{label}] no inbound audio (last byte count {previous})")


def uplink_rates(page: Page, seconds: float = 4.0) -> dict:
    first = page.evaluate(_UPLINK)
    time.sleep(seconds)
    second = page.evaluate(_UPLINK)
    kbit = {k: (second.get(k, 0) - first.get(k, 0)) * 8 / 1000 / seconds for k in ("audio", "video", "data")}
    kbit["packets_per_s"] = (second["messages"] - first["messages"]) / seconds
    return kbit


def main() -> int:
    base_url = os.getenv("BASE_URL", "http://127.0.0.1:3000").rstrip("/")
    output_dir = Path(os.getenv("OUTPUT_DIR", "testcases/two_users_avatar/output"))
    output_dir.mkdir(parents=True, exist_ok=True)

    headless = env_bool("HEADLESS", True)
    timeout_ms = int(os.getenv("PW_TIMEOUT_MS", "30000"))
    expect_port = os.getenv("EXPECT_MEDIA_PORT", "")
    room = f"e2e-{secrets.token_hex(3)}"
    media_args = [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
    ]

    with sync_playwright() as p:
        slow = int(os.getenv("PW_SLOWMO_MS", "0"))
        # Separate browsers: the fake camera file is per browser.
        browsers = [
            p.chromium.launch(headless=headless, slow_mo=slow,
                              args=[*media_args, f"--use-file-for-fake-video-capture={FACE_VIDEO}"]),
            p.chromium.launch(headless=headless, slow_mo=slow, args=media_args),
        ]
        alice, bob = (
            b.new_context(viewport={"width": 1100, "height": 800}, ignore_https_errors=True,
                          permissions=["microphone", "camera"]).new_page()
            for b in browsers
        )
        for page in (alice, bob):
            page.set_default_timeout(timeout_ms)
            page.on("console", lambda msg: msg.type == "error" and print("console:", msg.text))

        try:
            join(alice, base_url, "Alice", room)
            join(bob, base_url, "Bob", room)
            wait_for_tiles(alice, 2)
            wait_for_tiles(bob, 2)
            print(f"room {room}: both participants visible")

            # Alice's camera shows a face: her tracker finds it and Bob gets it.
            bob_view = wait_face(bob, "Alice", "f.face && f.camera && f.packets > 20", 90_000)
            print(f"bob receives alice's face: {bob_view['packets']} packets, {bob_view['bytes']} bytes, "
                  f"yaw {bob_view['yaw']:.2f}")
            alice.locator("#face-status").wait_for(state="detached")
            # Bob's camera shows no face: he still sends (camera on, no face).
            bob.locator("#face-status", has_text="Looking for your face").wait_for(timeout=60_000)
            alice_view = wait_face(alice, "Bob", "!f.face && f.camera && f.packets > 0")
            print(f"alice receives bob's state: camera on, no face ({alice_view['packets']} packets)")

            # A face that did not change is only sent again once a second, so the
            # rate depends on how much it moves: a still photo sends fewer packets
            # than Bob's lips, which follow the beeping fake microphone. Never
            # more than 25 a second, and never silent.
            before = {"alice": face_of(bob, "Alice")["packets"], "bob": face_of(alice, "Bob")["packets"]}
            time.sleep(3.0)
            after = {"alice": face_of(bob, "Alice")["packets"], "bob": face_of(alice, "Bob")["packets"]}
            for who in ("alice", "bob"):
                rate = (after[who] - before[who]) / 3.0
                print(f"face packets from {who}: {rate:.1f}/s")
                if not 0.6 <= rate <= 26:
                    raise AssertionError(f"face packets from {who}: {rate}/s, expected 1 to 25")

            wait_for_audio(bob, "bob hears alice")
            wait_for_audio(alice, "alice hears bob")

            # No video anywhere: none published, the server only allows the microphone.
            for label, page in (("alice", alice), ("bob", bob)):
                videos = page.evaluate(_VIDEO_PUBLICATIONS)
                sources = page.evaluate(
                    "() => window.livekitClient.room.localParticipant.permissions.canPublishSources"
                )
                print(f"[{label}] video publications: {videos}, may publish sources: {sources}")
                if videos != 0:
                    raise AssertionError(f"[{label}] sees {videos} video publications")
                if sources != [2]:  # TrackSource.MICROPHONE
                    raise AssertionError(f"[{label}] may publish {sources}, not only the microphone")
            attempt = alice.evaluate(_TRY_CAMERA)
            print("alice tries to publish her camera:", attempt)
            if not attempt.startswith("refused") or bob.evaluate(_VIDEO_PUBLICATIONS) != 0:
                raise AssertionError(f"camera publish was not refused: {attempt}")

            for label, page in (("alice", alice), ("bob", bob)):
                pairs = page.evaluate(_SELECTED_PAIRS)
                for pair in pairs:
                    print(f"[{label}] {pair['label']}: {pair['local']} -> {pair['remote']} ({pair['bytes']} bytes)")
                if expect_port and not any(
                    pair["remote_port"] == expect_port and pair["bytes"] > 0 for pair in pairs
                ):
                    raise AssertionError(f"[{label}] no media over server port {expect_port}")
                up = uplink_rates(page)
                print(f"[{label}] uplink: audio {up['audio']:.1f} kbit/s, face data {up['data']:.1f} kbit/s "
                      f"({up['packets_per_s']:.1f} packets/s), video {up['video']:.1f} kbit/s")
                if up["video"] > 0:
                    raise AssertionError(f"[{label}] sends video")

            alice.screenshot(path=str(output_dir / "alice_in_room.png"))
            bob.screenshot(path=str(output_dir / "bob_in_room.png"))

            # Expressions arrive exactly (quantized to 1/255) and read as emotions.
            alice.evaluate("window.livekitClient.injectFace("
                           "{mouthSmileLeft: 1, mouthSmileRight: 1, jawOpen: 0.6, cheekSquintLeft: 0.5}, 4000)")
            laugh = wait_face(bob, "Alice", "f.emotion === 'laughing'")
            print("bob sees alice laughing:", {k: laugh["shapes"][k] for k in ("mouthSmileLeft", "jawOpen")})
            if laugh["shapes"]["mouthSmileLeft"] < 0.99 or abs(laugh["shapes"]["jawOpen"] - 0.6) > 0.01:
                raise AssertionError(f"expression values changed on the way: {laugh['shapes']}")
            bob.wait_for_timeout(600)  # let the drawn face catch up
            bob.locator(".participant-tile", has_text="Alice").screenshot(path=str(output_dir / "bob_sees_alice_laughing.png"))

            alice.evaluate("window.livekitClient.injectFace({jawOpen: 0.7, mouthFunnel: 0.5, eyeWideLeft: 0.8, "
                           "eyeWideRight: 0.8, browInnerUp: 0.8, yaw: 0.3, roll: -0.2}, 4000)")
            surprise = wait_face(bob, "Alice", "f.emotion === 'surprised'")
            print(f"bob sees alice surprised, head yaw {surprise['yaw']:.3f} roll {surprise['roll']:.3f}")
            if abs(surprise["yaw"] - 0.3) > 0.02 or abs(surprise["roll"] + 0.2) > 0.02:
                raise AssertionError(f"head pose changed on the way: {surprise}")
            bob.wait_for_timeout(600)
            bob.locator(".participant-tile", has_text="Alice").screenshot(path=str(output_dir / "bob_sees_alice_surprised.png"))

            # Avatar style.
            alice.get_by_title("Change avatar").click()
            wait_face(bob, "Alice", "f.style === 'cat'")
            print("bob sees alice's new avatar: cat")

            # Camera off: the camera is released, Bob sees it off; back on: face again.
            alice.get_by_title("Turn off camera").click()
            alice.locator("#face-status", has_text="Camera off").wait_for()
            if alice.evaluate("() => !!window.livekitClient.tracker"):
                raise AssertionError("tracker still running after turning the camera off")
            bob.locator('.participant-tile[data-camera="off"]', has_text="Alice").wait_for()
            wait_face(bob, "Alice", "!f.face && !f.camera")
            print("bob sees alice's camera off (her avatar idles)")
            alice.get_by_title("Turn on camera").click()
            wait_face(bob, "Alice", "f.face && f.camera", 60_000)
            bob.locator('.participant-tile[data-camera="on"]', has_text="Alice").wait_for()
            print("bob sees alice's face again")

            # Mute shows up on the other side.
            alice.get_by_title("Mute").click()
            bob.locator(".participant-tile", has_text="Alice").locator("svg.lucide-mic-off").wait_for()
            print("bob sees alice muted")
            alice.get_by_title("Unmute").click()
            bob.locator(".participant-tile", has_text="Alice").locator(
                "svg.lucide-mic-off"
            ).wait_for(state="detached")
            print("bob sees alice unmuted")

            alice.get_by_title("Leave room").click()
            alice.get_by_role("button", name="Join Room").wait_for()
            wait_for_tiles(bob, 1)
            print("bob sees alice leave")
            return 0
        except Exception:
            for label, page in (("alice", alice), ("bob", bob)):
                try:
                    page.screenshot(path=str(output_dir / f"failure_{label}.png"), full_page=True)
                except Exception:
                    pass
            raise
        finally:
            for b in browsers:
                b.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"two_users_avatar failed: {exc}", file=sys.stderr)
        raise
