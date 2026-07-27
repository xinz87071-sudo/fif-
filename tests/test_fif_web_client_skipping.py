import sys
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from connector.FiFWebClient import FiFWebClient


class RecordingSkipTest(unittest.TestCase):
    def test_native_audio_is_skipped_until_recording_starts(self) -> None:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as error:
                self.skipTest(f"Chromium is unavailable: {error}")

            try:
                context = browser.new_context()
                page = context.new_page()
                page.set_content('<iframe id="assessment"></iframe>')
                frame = page.locator("#assessment").element_handle().content_frame()
                frame.set_content(
                    """
                    <a id="jump" class="jump_btn">跳过</a>
                    <button id="skip" class="btn-content" hidden>
                      <span class="skip-text">点击跳过</span>
                    </button>
                    <div id="recording" hidden>结束录音</div>
                    <script>
                      window.skipCount = 0;
                      const jump = document.getElementById('jump');
                      const skip = document.getElementById('skip');
                      jump.addEventListener('click', () => {
                        window.skipCount += 1;
                        jump.hidden = true;
                        setTimeout(() => { skip.hidden = false; }, 50);
                      });
                      skip.addEventListener('click', () => {
                        window.skipCount += 1;
                        skip.hidden = true;
                        setTimeout(() => {
                          document.getElementById('recording').hidden = false;
                        }, 50);
                      });
                    </script>
                    """
                )

                client = FiFWebClient.__new__(FiFWebClient)
                skipped = client._wait_for_recording(
                    page,
                    page.frame_locator("#assessment"),
                    timeout_ms=5000,
                )

                self.assertEqual(skipped, 2)
                self.assertEqual(frame.evaluate("window.skipCount"), 2)
                self.assertTrue(
                    page.frame_locator("#assessment")
                    .get_by_text("结束录音", exact=True)
                    .is_visible()
                )
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
