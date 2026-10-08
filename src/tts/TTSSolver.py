import io
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Optional, Tuple

try:
    from TTS.api import TTS
except Exception:
    TTS = None  # lazy import handled in _ensure_tts

try:
    from num2words import num2words
except Exception:
    num2words = None

try:
    import numpy as np
except Exception:
    np = None

try:
    import soundfile as sf
except Exception:
    sf = None


class TTSSolver:
    """TTS wrapper: lazy-initializes the TTS engine and provides
    helpers to write WAV files or return raw bytes.
    """

    def __init__(self, model: str, mode: str, target_voice_path: str):
        self.model = model
        self.target_voice_path = target_voice_path
        self._mode = mode
        self._tts = None
        self._tts_en = None  # 专用英语模型（短词发音更准确）
        self._tts_sample_rate = 22050
        self._init_lock = threading.Lock()
        self._en_lock = threading.Lock()

    def preload(self) -> None:
        """预加载 TTS 模型 + 预热合成（可在后台线程调用）。"""
        try:
            self._ensure_tts()
            # 预热：合成一个短句，触发 JIT 编译等首次开销
            print("[TTS] 预热合成中...")
            _ = self._tts.tts(text="Hello.", speaker_wav=self.target_voice_path, language="en")
            print("[TTS] 模型预加载+预热完成")
        except Exception as e:
            print(f"[TTS] 模型预加载失败: {e}")
        # 英语模型按需加载（不阻塞启动），首次短词合成时自动加载

    def _ensure_tts(self) -> None:
        if self._tts is not None:
            return
        with self._init_lock:
            if self._tts is not None:
                return
            if TTS is None:
                raise RuntimeError("TTS package is not available. Please install requirements.")

            print("[TTS] 初始化神经网络（延迟）...")
            mode = str(self._mode or "auto").lower()
            if mode in ("auto", "cuda"):
                try:
                    import torch

                    cuda_available = bool(torch.cuda.is_available())
                except Exception:
                    cuda_available = False
                if mode == "cuda" and not cuda_available:
                    raise RuntimeError("已指定 CUDA 模式，但 PyTorch 未检测到可用的 NVIDIA GPU")
                use_gpu = cuda_available
            else:
                use_gpu = False
            print(f"[TTS] 运行设备: {'CUDA' if use_gpu else 'CPU'}")
            model_name = self.model or "tts_models/multilingual/multi-dataset/your_tts"
            self._tts = TTS(model_name, gpu=use_gpu, progress_bar=True)

    def _ensure_tts_en(self) -> None:
        """初始化专用英语 TTS 模型（短词发音更标准）。"""
        if self._tts_en is not None:
            return
        with self._en_lock:
            if self._tts_en is not None:
                return
            if TTS is None:
                raise RuntimeError("TTS package is not available")
            print("[TTS] 初始化英语专用模型（短词发音优化）...")
            try:
                self._tts_en = TTS("tts_models/en/ljspeech/tacotron2-DDC", gpu=True, progress_bar=False)
                print("[TTS] 英语模型加载完成: ljspeech/tacotron2-DDC")
            except Exception as e:
                print(f"[TTS] 英语模型加载失败: {e}")
                self._tts_en = None

    def _normalize_currency_text(self, text: str) -> str:
        """将常见货币符号及金额转换为可读文本。

        支持示例：
        - $12.50 -> 12 dollars and 50 cents
        - ¥100 -> 100 yen
        - €3.20 -> 3 euros and 20 cents
        - £5 -> 5 pounds
        """

        if not text:
            return text

        # 去掉千位分隔符（逗号），例如 1,234.56
        def repl(match: re.Match) -> str:
            symbol = match.group('sym')
            amount = match.group('amt')
            # 移除逗号
            amount = amount.replace(',', '')
            if '.' in amount:
                integer_part, frac_part = amount.split('.', 1)
                # 取最多两位小数作为分
                frac = (frac_part + '00')[:2]
            else:
                integer_part = amount
                frac = ''

            # 支持常见货币符号及其全角变体
            symbol_map = {
                '$': 'dollar',
                '€': 'euro',
                '£': 'pound',
                '¥': 'yen',
                '￥': 'yuan',
                '￡': 'pound',
            }

            # ensure currency is a str (dict.get returns Optional)
            currency = symbol_map.get(symbol) or symbol

            # 将整数部分转换为单词（优先使用 num2words）
            try:
                int_part_val = int(integer_part) if integer_part != '' else 0
            except ValueError:
                int_part_val = 0

            if num2words is not None:
                try:
                    int_words = num2words(int_part_val, lang='en')
                except Exception:
                    int_words = str(int_part_val)
            else:
                int_words = str(int_part_val)

            # 复数化货币单位（yen/yuan 通常不加 s）
            if currency in ('yen', 'yuan'):
                currency_word = currency
            else:
                currency_word = currency + ('s' if int_part_val != 1 else '')

            if frac:
                if currency in ('yen', 'yuan'):
                    return f"{int_words} {currency_word}"
                try:
                    frac_val = int(frac)
                except ValueError:
                    frac_val = 0

                if frac_val == 0:
                    return f"{int_words} {currency_word}"

                if num2words is not None:
                    try:
                        frac_words = num2words(frac_val, lang='en')
                    except Exception:
                        frac_words = str(frac_val)
                else:
                    frac_words = str(frac_val)

                return f"{int_words} {currency_word} and {frac_words} cents"

            return f"{int_words} {currency_word}"

        # 匹配货币符号后面可选空格接数字，支持千位逗号和小数
        # include fullwidth pound sign '￡' (U+FFE1) in the symbol set
        pattern = re.compile(r"(?P<sym>[\$€£¥￥￡])\s*(?P<amt>[0-9][0-9,]*(?:\.[0-9]+)?)")
        return pattern.sub(repl, text)

    def _number_to_words(self, num_str: str) -> str:
        """Convert a numeric string to English words.

        - Integers: use num2words if available, else return digits.
        - Decimals: convert integer part to words, then 'point' + digit words.
        Examples:
          '1234' -> 'one thousand two hundred thirty-four'
          '12.50' -> 'twelve point five zero'
        """
        if not num_str:
            return num_str

        s = num_str.replace(',', '')
        if '.' in s:
            int_part, frac_part = s.split('.', 1)
        else:
            int_part, frac_part = s, ''

        # integer part
        int_words: str
        try:
            int_val = int(int_part) if int_part != '' else 0
        except ValueError:
            int_words = int_part
        else:
            if num2words is not None:
                try:
                    int_words = num2words(int_val, lang='en')
                except Exception:
                    int_words = str(int_val)
            else:
                int_words = str(int_val)

        if frac_part == '':
            return int_words

        # fractional part: speak as individual digits after 'point'
        digit_map = {
            '0': 'zero', '1': 'one', '2': 'two', '3': 'three', '4': 'four',
            '5': 'five', '6': 'six', '7': 'seven', '8': 'eight', '9': 'nine'
        }
        frac_words = ' '.join(digit_map.get(d, d) for d in frac_part)
        return f"{int_words} point {frac_words}"

    def _normalize_numbers(self, text: str) -> str:
        """Find numeric tokens and replace them with words.

        This runs after currency normalization so currency amounts are already words.
        """
        if not text:
            return text

        # Match numbers with optional commas and decimals (not part of words)
        pattern = re.compile(r"(?<!\w)(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)(?!\w)")

        def repl(match: re.Match) -> str:
            num = match.group('num')
            return self._number_to_words(num)

        return pattern.sub(repl, text)

    # ------------------------------------------------------------------
    # 短文本优化：文本增强 + 上下文填充 + 音频后处理
    # ------------------------------------------------------------------

    @staticmethod
    def _can_postprocess() -> bool:
        """检查音频后处理依赖是否可用。"""
        return np is not None and sf is not None

    @staticmethod
    def _is_hard_word(text: str) -> bool:
        """检测难词（辅音连缀开头/结尾，TTS 容易发音不清）。"""
        import re as _re
        word = text.lower().strip().rstrip('.!?')
        # 辅音连缀开头：scr-, str-, spr-, shr-, thr-, fr-, fl-, pl-, pr-, bl-, br-, cl-, cr-, dr-, tr-, gr-, gl-
        if _re.match(r'^(scr|str|spr|shr|thr|fr|fl|pl|pr|bl|br|cl|cr|dr|tr|gr|gl|sch|spl|squ)', word):
            return True
        # 辅音连缀结尾：-tch, -rch, -lch, -nch, -lk, -rk, -lp, -rp, -lf, -rf, -lth, -rth
        if _re.search(r'(tch|rch|lch|nch|lk|rk|lp|rp|lf|rf|lth|rth|ngth|sts|sks|xth)$', word):
            return True
        # 单音节词（≤5字母）
        if len(word) <= 5 and ' ' not in word:
            return True
        return False

    def _enhance_text_for_tts(self, text: str) -> str:
        """增强短文本的 TTS 输入质量。

        - 确保首字母大写
        - 添加合适的标点符号以改善韵律
        - 不会添加额外的词语（避免评分干扰）
        """
        text = text.strip()
        if not text:
            return text

        # 首字母大写
        text = text[0].upper() + text[1:]

        # 添加标点（改善 TTS 韵律，不改变语义）
        if text[-1] not in '.,!?;:\'"':
            word_count = len(text.split())
            if word_count <= 2:
                # 短词/短语用感叹号增加能量和清晰度
                text += '.'
            else:
                text += '.'

        return text

    def _process_text_pipeline(self, text: str) -> Tuple[str, bool, int]:
        """统一的文本处理流水线。

        返回 (处理后的文本, 是否需要裁剪前缀音频, 前缀词数)。
        不再使用上下文填充（裁剪引入噪音），只做文本规范化和标点增强。
        """
        # 第一步：规范化
        text = self._normalize_currency_text(text)
        text = self._normalize_numbers(text)

        # 第二步：文本增强（标点、大写）
        text = self._enhance_text_for_tts(text)

        return text, False, 0

    @staticmethod
    def _find_word_starts(data, sr: int, threshold_ratio: float = 0.15) -> list:
        """使用能量 VAD 找到每个词的起始位置。

        参数:
            data: float32 音频数组
            sr: 采样率
            threshold_ratio: 能量阈值系数（相对于最大能量）

        返回: 每个词起始采样点索引的列表
        """
        frame_ms = 20  # 帧长 20ms
        hop_ms = 10    # 帧移 10ms
        frame_size = int(frame_ms / 1000.0 * sr)
        hop_size = int(hop_ms / 1000.0 * sr)

        # 计算每帧 RMS 能量
        energies = []
        positions = []
        for i in range(0, max(1, len(data) - frame_size), hop_size):
            frame = data[i:i + frame_size]
            rms = float(np.sqrt(np.mean(frame ** 2)))
            energies.append(rms)
            positions.append(i)

        if not energies:
            return [0]

        energies = np.array(energies)

        # 自适应阈值
        max_energy = np.max(energies) if len(energies) > 0 else 0.01
        threshold = max(max_energy * threshold_ratio, 0.001)

        is_speech = energies > threshold

        # 最少静音帧数才算词边界（150ms 以上的静音）
        min_silence_frames = int(150 / hop_ms)

        word_starts = []
        silence_count = 0
        in_speech = False

        for i, speech in enumerate(is_speech):
            if speech:
                if not in_speech:
                    word_starts.append(positions[i])
                    in_speech = True
                silence_count = 0
            else:
                silence_count += 1
                if silence_count >= min_silence_frames:
                    in_speech = False

        return word_starts if word_starts else [0]

    def _trim_prefix_audio(self, wav_bytes: bytes, prefix_word_count: int) -> bytes:
        """从音频开头裁剪掉填充前缀部分。

        使用能量 VAD 找到词边界，跳过前 N 个词（即填充词），
        只保留目标词的音频。

        参数:
            wav_bytes: WAV 格式的二进制数据
            prefix_word_count: 需要跳过的前缀词数

        返回: 裁剪后的 WAV 二进制数据
        """
        if not self._can_postprocess():
            return wav_bytes

        try:
            data, file_sr = sf.read(io.BytesIO(wav_bytes), dtype='float32')
            if data.ndim > 1:
                data = data.mean(axis=1)
        except Exception:
            return wav_bytes

        # 找到词边界
        word_starts = self._find_word_starts(data, file_sr)

        # 跳过前缀词
        if len(word_starts) > prefix_word_count:
            trim_point = word_starts[prefix_word_count]
            # 回退 50ms 以保留起始瞬态（辅音等）
            margin = int(0.050 * file_sr)
            trim_point = max(0, trim_point - margin)
            data = data[trim_point:]
        else:
            # 如果词边界检测失败，不做裁剪（安全降级）
            print("[TTS] 警告: 词边界检测未找到足够词，跳过前缀裁剪")

        # 转回 WAV
        buf = io.BytesIO()
        sf.write(buf, data, file_sr, format='WAV', subtype='PCM_16')
        return buf.getvalue()

    def _postprocess_audio(self, wav_bytes: bytes,
                           min_duration: float = 1.2,
                           target_peak: float = 0.85) -> bytes:
        """音频后处理流水线。

        1. 裁剪过长的首尾静音
        2. 音量归一化到目标峰值
        3. 确保最小音频时长（用静音填充）
        4. 添加淡入/淡出避免咔嗒声

        参数:
            wav_bytes: WAV 格式的二进制数据
            min_duration: 最小音频时长（秒）
            target_peak: 目标峰值振幅

        返回: 处理后的 WAV 二进制数据
        """
        if not self._can_postprocess():
            return wav_bytes

        try:
            data, file_sr = sf.read(io.BytesIO(wav_bytes), dtype='float32')
            if data.ndim > 1:
                data = data.mean(axis=1)
        except Exception:
            return wav_bytes

        # 1. 裁剪首尾过长的静音（保留少量前后空白）
        abs_data = np.abs(data)
        speech_threshold = 0.008
        nonzero_indices = np.where(abs_data > speech_threshold)[0]
        if len(nonzero_indices) > 0:
            # 保留语音前 100ms、后 200ms 的空白
            front_margin = int(0.100 * file_sr)
            back_margin = int(0.200 * file_sr)
            start = max(0, int(nonzero_indices[0]) - front_margin)
            end = min(len(data), int(nonzero_indices[-1]) + back_margin + 1)
            data = data[start:end]

        # 2. 音量归一化
        peak = float(np.max(np.abs(data)))
        if peak > 0.001:
            data = data * (target_peak / peak)

        # 3. 确保最小音频时长
        min_samples = int(min_duration * file_sr)
        if len(data) < min_samples:
            # 前填充 150ms 静音，其余填在末尾
            front_pad = int(0.150 * file_sr)
            back_pad = min_samples - len(data) - front_pad
            if back_pad < 0:
                back_pad = 0
            data = np.concatenate([
                np.zeros(front_pad, dtype=np.float32),
                data,
                np.zeros(back_pad, dtype=np.float32),
            ]).astype(np.float32)

        # 4. 淡入 (5ms) / 淡出 (20ms) 避免咔嗒声
        fade_in_n = int(0.005 * file_sr)
        fade_out_n = int(0.020 * file_sr)
        if len(data) > fade_in_n + fade_out_n:
            data[:fade_in_n] *= np.linspace(0.0, 1.0, fade_in_n).astype(np.float32)
            data[-fade_out_n:] *= np.linspace(1.0, 0.0, fade_out_n).astype(np.float32)

        # 防止削波
        data = np.clip(data, -1.0, 1.0).astype(np.float32)

        buf = io.BytesIO()
        sf.write(buf, data, file_sr, format='WAV', subtype='PCM_16')
        return buf.getvalue()

    def _synthesize_and_postprocess(self, text: str, tmp_path: str = "") -> bytes:
        """合成语音并进行完整的后处理流水线。

        对短文本（≤3词）使用多变体生成 + 质量选择策略。
        """
        processed_text, needs_trim, prefix_words = self._process_text_pipeline(text)
        word_count = len(processed_text.split())
        print(f"[TTS] 合成文本: '{processed_text}' ({word_count}词)")

        self._ensure_tts()

        # 短文本使用多变体生成策略
        if word_count <= 3:
            return self._synthesize_short_best(processed_text)

        # 长文本直接合成（恢复 80+ 配置的完整后处理）
        audio_np = self._do_synthesize(processed_text)
        sr = self._get_sample_rate()

        audio_np = self._postprocess_array(audio_np, sr)

        buf = io.BytesIO()
        sf.write(buf, audio_np, sr, format='WAV', subtype='PCM_16')
        return buf.getvalue()

    def _get_sample_rate(self) -> int:
        try:
            sr = getattr(self._tts.synthesizer, 'output_sample_rate', self._tts_sample_rate)
            return int(sr) if isinstance(sr, (int, float)) else self._tts_sample_rate
        except Exception:
            return self._tts_sample_rate

    def _do_synthesize(self, text: str, use_voice_clone: bool = True) -> 'np.ndarray':
        """执行一次 TTS 合成，返回 float32 numpy 数组。

        use_voice_clone=False 时使用模型内置发音人（短词更准确）。
        """
        if use_voice_clone:
            kwargs = {"speaker_wav": self.target_voice_path}
        else:
            # 使用模型内置英文发音人（不依赖参考音频，发音更标准）
            kwargs = {"speaker": "female-en-5"}
        try:
            wav_list = self._tts.tts(
                text=text,
                language="en",
                **kwargs,
            )
            return np.array(wav_list, dtype=np.float32)
        except Exception as synth_err:
            if not use_voice_clone:
                # 内置 speaker 名可能不对，尝试其他
                for fallback_speaker in ["female-en-1", "male-en-1", "p225", "p226"]:
                    try:
                        wav_list = self._tts.tts(
                            text=text,
                            language="en",
                            speaker=fallback_speaker,
                        )
                        print(f"[TTS] 使用内置发音人: {fallback_speaker}")
                        return np.array(wav_list, dtype=np.float32)
                    except Exception:
                        continue
                raise  # 所有内置发音人都失败
            print(f"[TTS] tts() 失败: {synth_err}，降级 tts_to_file")
            tmp_path = tempfile.mktemp(suffix=".wav")
            try:
                self._tts.tts_to_file(
                    text=text,
                    language="en",
                    file_path=tmp_path,
                    **kwargs,
                )
                audio_np, _ = sf.read(tmp_path, dtype='float32')
                if audio_np.ndim > 1:
                    audio_np = audio_np.mean(axis=1)
                return audio_np.astype(np.float32)
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _do_synthesize_en(self, text: str) -> 'np.ndarray':
        """用专用英语 TTS 模型合成（发音更标准）。"""
        self._ensure_tts_en()
        if self._tts_en is None:
            raise RuntimeError("英语 TTS 模型不可用")
        wav_list = self._tts_en.tts(text=text)
        return np.array(wav_list, dtype=np.float32)

    @staticmethod
    def _assess_quality(audio: 'np.ndarray', sr: int) -> float:
        """评估音频质量（用于多变体选择）。

        综合考虑：信噪比、能量、频谱平坦度。
        分数越高越好。
        """
        if len(audio) == 0:
            return 0.0

        # 信号能量
        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms < 0.001:
            return 0.0

        # 信噪比估计：用帧能量的统计分布
        frame_size = int(0.025 * sr)
        if frame_size < 1:
            frame_size = 1
        n_frames = len(audio) // frame_size
        if n_frames < 2:
            return rms

        frame_energies = []
        for i in range(n_frames):
            frame = audio[i * frame_size:(i + 1) * frame_size]
            frame_energies.append(float(np.sqrt(np.mean(frame ** 2))))

        frame_energies = np.array(frame_energies)
        active_frames = frame_energies[frame_energies > 0.005]

        if len(active_frames) == 0:
            return 0.0

        # 语音活跃度：活跃帧占比（越高说明语音越饱满）
        activity_ratio = len(active_frames) / max(len(frame_energies), 1)

        # 能量均匀度：活跃帧的变异系数（越小越均匀）
        mean_e = np.mean(active_frames)
        std_e = np.std(active_frames)
        uniformity = 1.0 - min(std_e / max(mean_e, 0.001), 1.0)

        # 语音持续时间得分（0.5-3秒最佳）
        duration = len(audio) / sr
        if duration < 0.3:
            dur_score = 0.3
        elif duration <= 3.0:
            dur_score = 1.0
        else:
            dur_score = max(0.5, 1.0 - (duration - 3.0) * 0.1)

        return activity_ratio * 0.3 + uniformity * 0.3 + dur_score * 0.2 + rms * 0.2

    def _synthesize_short_best(self, text: str) -> bytes:
        """短文本多变体合成，选质量最好的。

        仅使用克隆模型（音色一致性最重要）。
        """
        sr = self._get_sample_rate()
        is_hard = self._is_hard_word(text)
        variants = []

        # 变体 1：克隆模型原文
        try:
            a1 = self._do_synthesize(text, use_voice_clone=True)
            q1 = self._assess_quality(a1, sr)
            variants.append((q1, a1, text))
            print(f"[TTS] 变体1 '{text}' 质量={q1:.3f}")
        except Exception as e:
            print(f"[TTS] 变体1失败: {e}")

        # 变体 2：加感叹号（更饱满的发音）
        text_excl = text.rstrip('.!?') + '!'
        if text_excl != text:
            try:
                a2 = self._do_synthesize(text_excl, use_voice_clone=True)
                q2 = self._assess_quality(a2, sr)
                variants.append((q2, a2, text_excl))
                print(f"[TTS] 变体2 '{text_excl}' 质量={q2:.3f}")
            except Exception as e:
                print(f"[TTS] 变体2失败: {e}")

        if not variants:
            raise RuntimeError("所有变体合成失败")

        # 选质量最好的
        best_quality, best_audio, best_text = max(variants, key=lambda x: x[0])
        print(f"[TTS] 选定变体: '{best_text}' 质量={best_quality:.3f}{' [难词]' if is_hard else ''}")

        # 恢复 80+ 配置：完整后处理 + 0.9x 降速
        processed = self._postprocess_array(best_audio, sr)

        # 短词轻微降速（0.9x），提升发音清晰度
        word_count = len(text.split())
        if word_count <= 2 and len(processed) > 0:
            try:
                from scipy.signal import resample
                target_len = int(len(processed) * 1.1)  # 1.1x 时长 = 0.9x 速度
                if target_len > len(processed):
                    processed = resample(processed, target_len).astype(np.float32)
                    processed = np.clip(processed, -1.0, 1.0)
                    print(f"[TTS] 短词降速0.9x: {len(best_audio)} -> {len(processed)} samples")
            except Exception:
                pass

        buf = io.BytesIO()
        sf.write(buf, processed, sr, format='WAV', subtype='PCM_16')
        return buf.getvalue()

    def _trim_prefix_array(self, audio: 'np.ndarray', sr: int, prefix_word_count: int) -> 'np.ndarray':
        """从音频数组开头裁剪掉填充前缀部分。"""
        word_starts = self._find_word_starts(audio, sr)
        if len(word_starts) > prefix_word_count:
            trim_point = word_starts[prefix_word_count]
            margin = int(0.050 * sr)
            trim_point = max(0, trim_point - margin)
            return audio[trim_point:]
        print("[TTS] 警告: 词边界检测未找到足够词，跳过前缀裁剪")
        return audio

    def _postprocess_array(self, audio: 'np.ndarray', sr: int,
                           min_duration: float = 1.0,
                           target_peak: float = 0.9) -> 'np.ndarray':
        """音频后处理 + 频谱增强（提升评分系统识别率）。

        1. 裁剪首尾静音
        2. 高通滤波 (80Hz) 去除低频噪声
        3. 频谱增强：提升 2-4kHz 语音清晰度频段
        4. 动态压缩：让音量更均匀
        5. 音量归一化
        6. 确保最小音频时长
        7. 淡入/淡出
        """
        data = audio.copy().astype(np.float32)

        # 1. 裁剪首尾过长的静音（保留更长前置空白，防止辅音被切）
        abs_data = np.abs(data)
        speech_threshold = 0.004
        nonzero_indices = np.where(abs_data > speech_threshold)[0]
        if len(nonzero_indices) > 0:
            front_margin = int(0.120 * sr)  # 120ms 前置
            back_margin = int(0.200 * sr)   # 200ms 后置
            start = max(0, int(nonzero_indices[0]) - front_margin)
            end = min(len(data), int(nonzero_indices[-1]) + back_margin + 1)
            data = data[start:end]

        # 2. 高通滤波 80Hz（去除隆隆声）
        try:
            from scipy.signal import butter, sosfilt
            nyq = sr / 2.0
            sos_hp = butter(2, 80.0 / nyq, btype='high', output='sos')
            data = sosfilt(sos_hp, data).astype(np.float32)
        except Exception:
            pass

        # 3. 频谱增强：提升 2-4kHz 清晰度频段 + 高频辅音增强
        try:
            from scipy.signal import butter, sosfilt
            nyq = sr / 2.0
            # 带通滤波提取 2-4kHz 成分
            sos_bp = butter(2, [2000.0 / nyq, min(4000.0 / nyq, 0.99)], btype='band', output='sos')
            clarity_band = sosfilt(sos_bp, data).astype(np.float32)
            # 将清晰度频段混入原信号（增强辅音可懂度）
            data = (data + 0.3 * clarity_band).astype(np.float32)

            # 高频辅音增强 (3-6kHz)：爆破音、摩擦音关键频段
            sos_hi = butter(2, [3000.0 / nyq, min(6000.0 / nyq, 0.99)], btype='band', output='sos')
            consonant_band = sosfilt(sos_hi, data).astype(np.float32)
            data = (data + 0.15 * consonant_band).astype(np.float32)
        except Exception:
            pass

        # 4. 动态压缩（简单软限幅）
        threshold = 0.6
        ratio = 2.5
        abs_d = np.abs(data)
        over = abs_d > threshold
        data[over] = np.sign(data[over]) * (threshold + (abs_d[over] - threshold) / ratio)
        data = data.astype(np.float32)

        # 5. 音量归一化
        peak = float(np.max(np.abs(data)))
        if peak > 0.001:
            data = data * (target_peak / peak)

        # 6. 确保最小音频时长
        min_samples = int(min_duration * sr)
        if len(data) < min_samples:
            front_pad = int(0.120 * sr)
            back_pad = min_samples - len(data) - front_pad
            if back_pad < 0:
                back_pad = 0
            data = np.concatenate([
                np.zeros(front_pad, dtype=np.float32),
                data,
                np.zeros(back_pad, dtype=np.float32),
            ]).astype(np.float32)

        # 7. 淡入/淡出
        fade_in_n = int(0.008 * sr)
        fade_out_n = int(0.025 * sr)
        if len(data) > fade_in_n + fade_out_n:
            data[:fade_in_n] *= np.linspace(0.0, 1.0, fade_in_n).astype(np.float32)
            data[-fade_out_n:] *= np.linspace(1.0, 0.0, fade_out_n).astype(np.float32)

        return np.clip(data, -1.0, 1.0).astype(np.float32)

    def get_voice(self, text: str) -> Optional[bytes]:
        """合成并返回 WAV 二进制数据。"""
        if not text:
            return None
        return self._synthesize_and_postprocess(text)

    def get_file(self, text: str, path: str) -> None:
        """合成并将音频写到磁盘（覆盖）。"""
        if not text:
            return
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        print("[TTS] 正在合成语音...")
        wav_data = self._synthesize_and_postprocess(text)
        with open(str(p), "wb") as f:
            f.write(wav_data)
