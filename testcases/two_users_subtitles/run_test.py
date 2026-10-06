"""E2E: Amy speaks English, Ben reads her live subtitles and translates words from them.

Amy's fake microphone plays an English recording (made with macOS `say`, or
espeak-ng, at test time; it loops), Ben's plays silence. Translation needs
Ollama with the model in OLLAMA_MODEL (default qwen2.5:7b):

    OLLAMA_MODEL=qwen2.5:7b poetry run ./run_test_suite.sh two_users_subtitles

1. Ben, then Amy join a room. Amy's words reach Ben as subtitles: partials
   that grow, then final lines in the transcript ("Hi Ben, how was your
   weekend?"), and a caption on Amy's tile. Amy sees her own lines too.
2. Ben selects "mountains" in a line and right-clicks: "Translate to 繁體中文"
   shows the translation (Chinese characters).
3. Ben switches to 日本語: the next translation is Japanese.
4. Amy mutes: no new subtitles of hers come.
"""

import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fixtures.speech import make_fixtures  # noqa: E402

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:3000").rstrip("/")
HEADLESS = os.getenv("HEADLESS", "1") != "0"
CJK = re.compile(r"[\u4e00-\u9fff]")
KANA = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


def join(browser, name: str, room: str) -> Page:
    context = browser.new_context(viewport={"width": 1280, "height": 900}, permissions=["camera", "microphone"])
    page = context.new_page()
    page.set_default_timeout(30000)
    page.goto(BASE_URL + "/", wait_until="domcontentloaded")
    page.locator('#speech-status[data-phase="ready"]').wait_for(timeout=600_000)
    # Record every subtitle shown, partials included.
    page.evaluate("""() => {
        window.__subs = [];
        const show = window.liveSubtitles.show.bind(window.liveSubtitles);
        window.liveSubtitles.show = (identity, name, seg, text, final) => {
            window.__subs.push({name, seg, text, final});
            return show(identity, name, seg, text, final);
        };
    }""")
    page.locator('input[name="username"]').fill(name)
    page.locator('input[name="room_name"]').fill(room)
    page.get_by_role("button", name="Join Room").click()
    expect(page.locator("#connection-status")).to_have_text("Connected", timeout=60000)
    return page


def finals(page: Page, name: str) -> list[str]:
    return page.evaluate(
        "(name) => window.liveSubtitles.transcript().filter((l) => l.name === name && l.final).map((l) => l.text)",
        name,
    )


def wait_for_line(page: Page, name: str, word: str, timeout_s: int = 90) -> str:
    for _ in range(timeout_s * 2):
        for text in finals(page, name):
            if word in text.lower():
                return text
        page.wait_for_timeout(500)
    raise AssertionError(f"no subtitle line of {name} with {word!r}; transcript: {finals(page, name)}")


_SELECT_WORD = """(word) => {
    const lines = [...document.querySelectorAll('#subtitle-log .subtitle-line:not(.subtitle-partial)')].reverse();
    for (const line of lines) {
        const said = line.querySelector('.subtitle-said');
        const at = said.textContent.toLowerCase().indexOf(word);
        if (at < 0) continue;
        line.scrollIntoView({block: 'center'});
        const range = document.createRange();
        range.setStart(said.firstChild, at);
        range.setEnd(said.firstChild, at + word.length);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        const r = range.getBoundingClientRect();
        return [r.x + r.width / 2, r.y + r.height / 2];
    }
    return null;
}"""


def translate_word(page: Page, word: str, language: str) -> str:
    """Select ``word`` in the transcript, right-click, Translate; the translation shown."""
    point = page.evaluate(_SELECT_WORD, word)
    assert point, f"{word!r} is not in the transcript"
    page.mouse.click(point[0], point[1], button="right")
    item = page.get_by_role("menuitem", name=f"Translate to {language}")
    expect(item).to_be_visible(timeout=5000)
    item.click()
    popup = page.get_by_role("dialog", name="Translation")
    expect(popup).to_contain_text(language)
    expect(popup).to_contain_text(word)
    done = popup.locator(".subtitle-popup-text[data-state=done]")
    error = popup.locator(".subtitle-popup-text[data-state=error]")
    expect(done.or_(error)).to_be_visible(timeout=180_000)  # the first request loads the model into memory
    if error.count():
        raise AssertionError(f"translation failed: {error.inner_text()}")
    return done.inner_text()


def run() -> int:
    output_dir = Path(os.getenv("OUTPUT_DIR", "testcases/two_users_subtitles/output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    room = f"english-{uuid.uuid4().hex[:6]}"
    shots: dict[str, bytes] = {}

    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        speech, silence = make_fixtures(Path(tmp))
        # --disable-audio-output gives Chromium a fake speaker: without a usable audio
        # output (headless, CI, some sandboxes) Web Audio's clock stands still, and the
        # microphone never reaches the subtitle worklet.
        flags = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                 "--autoplay-policy=no-user-gesture-required", "--disable-audio-output"]
        # The fake microphone is per browser: one each.
        ben_browser = p.chromium.launch(headless=HEADLESS, args=[*flags, f"--use-file-for-fake-audio-capture={silence}"])
        amy_browser = p.chromium.launch(headless=HEADLESS, args=[*flags, f"--use-file-for-fake-audio-capture={speech}"])
        pages: list[Page] = []
        try:
            print("Ben, then Amy join", room, "...")
            ben = join(ben_browser, "Ben", room)
            amy = join(amy_browser, "Amy", room)
            pages += [ben, amy]
            expect(ben.locator(".participant-tile")).to_have_count(2, timeout=30000)

            print("Amy's words reach Ben as subtitles...")
            first = wait_for_line(ben, "Amy", "weekend")
            second = wait_for_line(ben, "Amy", "mountains")
            print(f"  Ben reads: {first!r} / {second!r}")
            assert "hiking" in second.lower() and "weather" in second.lower(), second
            partials = ben.evaluate("() => window.__subs.filter((s) => s.name === 'Amy' && !s.final).map((s) => s.text)")
            print(f"  partials before the finals: {partials[:4]}")
            assert partials, "no partial subtitles: the text should grow while Amy speaks"
            caption = ben.locator('.participant-tile[data-local="0"] .subtitle-caption')
            expect(caption).not_to_be_empty(timeout=20000)
            print(f"  caption on Amy's tile: {caption.inner_text()!r}")
            mine = wait_for_line(amy, "Amy", "weekend")
            print(f"  Amy reads her own: {mine!r}")
            shots["ben_subtitles"] = ben.screenshot()

            print("Ben translates 'mountains' to 繁體中文...")
            chinese = translate_word(ben, "mountains", "繁體中文")
            print(f"  -> {chinese!r}")
            assert CJK.search(chinese), f"not Chinese: {chinese!r}"
            shots["ben_translation"] = ben.screenshot()
            ben.keyboard.press("Escape")
            expect(ben.get_by_role("dialog", name="Translation")).to_have_count(0)

            print("Ben switches to 日本語...")
            ben.locator("select[data-translate-to]").select_option("ja")
            expect(ben.locator("select[data-translate-to]")).to_have_value("ja")
            japanese = translate_word(ben, "weather", "日本語")
            print(f"  -> {japanese!r}")
            assert KANA.search(japanese), f"not Japanese: {japanese!r}"
            ben.keyboard.press("Escape")

            print("Amy mutes: her subtitles stop...")
            amy.get_by_title("Mute").click()
            ben.wait_for_timeout(4000)  # the segment being spoken may still end
            before = len(finals(ben, "Amy"))
            ben.wait_for_timeout(12000)  # longer than one round of the recording
            after = len(finals(ben, "Amy"))
            print(f"  Amy's lines: {before} -> {after}")
            assert after == before, "subtitles kept coming while muted"

            print("Ben's language is remembered in his browser...")
            ben.reload(wait_until="domcontentloaded")
            expect(ben.locator("select[data-translate-to]")).to_have_value("ja", timeout=30000)

            print("All tests passed!")
            return 0
        except Exception as e:
            print(f"TEST FAILED: {e}")
            for i, page in enumerate(pages):
                shots[f"failure_{i}"] = page.screenshot()
            return 1
        finally:
            ben_browser.close()
            amy_browser.close()
            for name, png in shots.items():
                (output_dir / f"{name}.png").write_bytes(png)


if __name__ == "__main__":
    sys.exit(run())
