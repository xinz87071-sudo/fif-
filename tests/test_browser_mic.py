import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from playwright.sync_api import sync_playwright


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from injection.BrowserMic import INIT_SCRIPT_JS, play_wav_in_browser


START_RECORDING_JS = """
async () => {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  window.__testChunks = [];
  window.__testRecorder = new MediaRecorder(stream);
  window.__testRecorder.ondataavailable = (event) => {
    if (event.data.size) window.__testChunks.push(event.data);
  };
  await new Promise((resolve) => {
    window.__testRecorder.onstart = resolve;
    window.__testRecorder.start(20);
  });
  return stream.getAudioTracks()[0].getSettings();
}
"""


ANALYZE_RECORDING_JS = """
async () => {
  const recorder = window.__testRecorder;
  const stopped = new Promise((resolve) => { recorder.onstop = resolve; });
  recorder.stop();
  await stopped;

  const blob = new Blob(window.__testChunks, { type: recorder.mimeType });
  const encoded = await blob.arrayBuffer();
  const context = new AudioContext({ sampleRate: 16000 });
  const audio = await context.decodeAudioData(encoded.slice(0));
  const samples = audio.getChannelData(0);

  let first = -1;
  let last = -1;
  for (let i = 0; i < samples.length; i++) {
    if (Math.abs(samples[i]) > 0.03) {
      if (first < 0) first = i;
      last = i;
    }
  }
  await context.close();
  return {
    sampleRate: audio.sampleRate,
    activeSpan: first < 0 ? 0 : (last - first + 1) / audio.sampleRate,
  };
}
"""


def _write_tone(path: Path, duration: float = 0.5) -> None:
    sample_rate = 16000
    frames = int(sample_rate * duration)
    pcm = bytearray()
    for index in range(frames):
        sample = int(0.3 * 32767 * math.sin(2 * math.pi * 440 * index / sample_rate))
        pcm.extend(struct.pack("<h", sample))

    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm)


class BrowserMicTest(unittest.TestCase):
    def test_default_audio_context_matches_fif_recorder_input_rate(self) -> None:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as error:
                self.skipTest(f"Chromium is unavailable: {error}")

            try:
                context = browser.new_context()
                context.add_init_script(INIT_SCRIPT_JS)
                page = context.new_page()
                page.goto("https://example.com")

                sample_rates = page.evaluate(
                    """
                    async () => {
                      const defaultContext = new AudioContext();
                      const explicitContext = new AudioContext({ sampleRate: 16000 });
                      const sampleRates = {
                        defaultRate: defaultContext.sampleRate,
                        explicitRate: explicitContext.sampleRate,
                      };
                      await defaultContext.close();
                      await explicitContext.close();
                      return sampleRates;
                    }
                    """
                )

                self.assertEqual(sample_rates["defaultRate"], 48000)
                self.assertEqual(sample_rates["explicitRate"], 16000)
            finally:
                browser.close()

    def test_get_user_media_replaces_a_stopped_generator_track(self) -> None:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--use-fake-ui-for-media-stream"],
                )
            except Exception as error:
                self.skipTest(f"Chromium is unavailable: {error}")

            try:
                context = browser.new_context(permissions=["microphone"])
                context.add_init_script(INIT_SCRIPT_JS)
                page = context.new_page()
                page.goto("https://example.com")

                tracks = page.evaluate(
                    """
                    async () => {
                      const firstStream = await navigator.mediaDevices.getUserMedia({
                        audio: true,
                      });
                      const first = firstStream.getAudioTracks()[0];
                      const firstId = first.id;
                      first.stop();

                      const secondStream = await navigator.mediaDevices.getUserMedia({
                        audio: true,
                      });
                      const second = secondStream.getAudioTracks()[0];
                      return {
                        firstId,
                        firstState: first.readyState,
                        secondId: second.id,
                        secondState: second.readyState,
                      };
                    }
                    """
                )

                self.assertEqual(tracks["firstState"], "ended")
                self.assertEqual(tracks["secondState"], "live")
                self.assertNotEqual(tracks["firstId"], tracks["secondId"])
            finally:
                browser.close()

    def test_generator_does_not_insert_keepalive_silence_during_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tone_path = Path(temp_dir) / "tone.wav"
            _write_tone(tone_path)

            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(
                        headless=True,
                        args=[
                            "--use-fake-ui-for-media-stream",
                            "--autoplay-policy=no-user-gesture-required",
                        ],
                    )
                except Exception as error:
                    self.skipTest(f"Chromium is unavailable: {error}")

                try:
                    context = browser.new_context(permissions=["microphone"])
                    context.add_init_script(INIT_SCRIPT_JS)
                    page = context.new_page()
                    page.goto("https://example.com")

                    settings = page.evaluate(START_RECORDING_JS)
                    self.assertEqual(settings["sampleRate"], 16000)
                    self.assertTrue(
                        play_wav_in_browser(page, str(tone_path), extra_wait_ms=150)
                    )

                    result = page.evaluate(ANALYZE_RECORDING_JS)
                    self.assertEqual(result["sampleRate"], 16000)
                    self.assertAlmostEqual(result["activeSpan"], 0.5, delta=0.04)
                finally:
                    browser.close()


if __name__ == "__main__":
    unittest.main()
