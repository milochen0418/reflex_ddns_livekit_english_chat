"""/settings: public server status, admin gate, connection info, restart, log.

Uses the same ADMIN_PASSCODE the app reads (pass it in the environment).
"""

import os
import re
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
    output_dir = Path(os.getenv("OUTPUT_DIR", "testcases/settings_admin/output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    passcode = os.getenv("ADMIN_PASSCODE", "")
    if not passcode:
        raise SystemExit("Set ADMIN_PASSCODE (the app must run with the same value).")

    headless = env_bool("HEADLESS", True)
    timeout_ms = int(os.getenv("PW_TIMEOUT_MS", "30000"))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless, slow_mo=int(os.getenv("PW_SLOWMO_MS", "0"))
        )
        context = browser.new_context(
            viewport={"width": 1100, "height": 1200}, ignore_https_errors=True
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        try:
            page.goto(base_url + "/settings", wait_until="domcontentloaded")
            page.get_by_role("heading", name="LiveKit Server").wait_for()
            page.get_by_text("running", exact=True).wait_for(timeout=120_000)
            media_ip = page.locator("dt:has-text('Media IP') + dd").inner_text()
            print("public status: running, media IP", media_ip)

            # Wrong passcode is rejected; nothing secret is shown.
            page.get_by_placeholder("Enter admin passcode").fill("wrong-" + passcode)
            page.get_by_role("button", name="Unlock Settings").click()
            page.get_by_text("Invalid admin passcode.").first.wait_for()
            if page.locator("#api-secret").count():
                raise AssertionError("secret visible without admin access")

            page.get_by_placeholder("Enter admin passcode").fill(passcode)
            page.get_by_role("button", name="Unlock Settings").click()
            page.locator("#api-secret").wait_for()

            livekit_url = page.locator("#livekit-url").input_value()
            api_key = page.locator("#api-key").input_value()
            api_secret = page.locator("#api-secret").input_value()
            print("LiveKit URL:", livekit_url, "| API key:", api_key)
            if not re.match(r"^wss?://", livekit_url):
                raise AssertionError(f"unexpected LiveKit URL {livekit_url!r}")
            if not api_key or len(api_secret) < 32:
                raise AssertionError("API key/secret missing or secret too short")

            pid_before = page.get_by_text(re.compile(r"Server log · pid \d+")).inner_text()
            page.get_by_role("button", name="Restart server").click()
            page.get_by_text("LiveKit server restarted.").wait_for()
            pid_after = page.get_by_text(re.compile(r"Server log · pid \d+")).inner_text()
            print("restart:", pid_before, "->", pid_after)
            if pid_before == pid_after:
                raise AssertionError("pid did not change after restart")

            # The log tail now ends with the restarted server's startup lines.
            log = page.locator("pre").inner_text()
            if "starting LiveKit server" not in log:
                raise AssertionError("server log does not show the restart")

            page.screenshot(path=str(output_dir / "settings_admin.png"), full_page=True)
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
        print(f"settings_admin failed: {exc}", file=sys.stderr)
        raise
