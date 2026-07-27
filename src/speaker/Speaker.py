from pathlib import Path
import tempfile
from typing import Any

from tts.TTSSolver import TTSSolver
from injection.BrowserMic import play_wav_in_browser


class Speaker:
    """负责调用 TTS 合成，并将音频直接注入浏览器麦克风。"""

    def __init__(self, tts_model_name: str, mode: str, target_voice_path: str):
        self.tts_solver = TTSSolver(tts_model_name, mode, target_voice_path)
        Path("tmp").mkdir(parents=True, exist_ok=True)

    def speak(self, text: str, page: Any = None) -> None:
        """合成文本并送入麦克风链路。

        page: Playwright Page。音频直接注入浏览器麦克风流。
        """
        if not text:
            return
        if page is None:
            raise RuntimeError("浏览器音频注入需要 Playwright Page")

        print("[Speaker] 合成语音 -> 播放")

        with tempfile.NamedTemporaryFile(suffix=".wav", dir="tmp", delete=False) as tf:
            temp_path = tf.name

        try:
            self.tts_solver.get_file(text, temp_path)

            try:
                injected = bool(play_wav_in_browser(page, temp_path))
            except Exception as error:
                raise RuntimeError("浏览器麦克风注入失败") from error
            if not injected:
                raise RuntimeError("浏览器麦克风注入失败，已停止作答")
            print("[Speaker] 已通过浏览器麦克风注入")

        finally:
            try:
                Path(temp_path).unlink()
            except Exception:
                pass
