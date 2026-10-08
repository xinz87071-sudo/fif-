"""Chrome 原生假麦克风音频注入。

原理：Chrome 启动时用 --use-file-for-fake-audio-capture 指定音频文件。
每次 getUserMedia 被调用时，Chrome 重新读取该文件并作为麦克风输入。
因此只需在录音前覆盖文件内容即可实现音频注入。

优点：
- 不修改任何 JS API（不会触发 STATUS_BREAKPOINT）
- 不使用 MediaStreamTrackGenerator（兼容性更好）
- 纯文件操作，极其稳定
"""

import os
import struct
import numpy as np
import soundfile as sf
import io
from pathlib import Path
from typing import Optional, Tuple


def _wav_to_16k_mono(wav_path: str) -> Tuple[np.ndarray, int]:
    """读取 WAV 文件并转换为 16kHz 单声道 float32。"""
    data, sr = sf.read(wav_path, dtype='float32')
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != 16000:
        from scipy.signal import resample
        target_len = int(len(data) * 16000 / sr)
        data = resample(data, target_len).astype(np.float32)
    return data, 16000


def _win_write_shared(file_path: str, data: bytes) -> bool:
    """Windows API 共享写入：即使 Chrome 持有文件句柄也能写入。

    使用 CreateFileW + FILE_SHARE_READ|FILE_SHARE_WRITE 打开文件，
    绕过 Python open() 的独占限制。Chrome 的假麦克风文件读取器
    通常以 FILE_SHARE_READ 打开，此时普通 open('wb') 会失败。
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_ALWAYS = 4
        FILE_ATTRIBUTE_NORMAL = 0x80
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

        handle = kernel32.CreateFileW(
            file_path,
            GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            None
        )

        if handle == INVALID_HANDLE_VALUE:
            return False

        try:
            # 先截断文件到 0 字节
            kernel32.SetEndOfFile(handle)

            # 写入数据
            written = wintypes.DWORD(0)
            result = kernel32.WriteFile(
                handle,
                data,
                len(data),
                ctypes.byref(written),
                None
            )

            # 刷新缓冲区
            kernel32.FlushFileBuffers(handle)

            return bool(result) and written.value == len(data)
        finally:
            kernel32.CloseHandle(handle)

    except Exception:
        return False


def write_fake_audio(fake_audio_path: str, wav_source: str) -> bool:
    """将 WAV 音频写入 Chrome 假麦克风的输入文件。

    Args:
        fake_audio_path: Chrome 启动参数指定的假麦克风音频文件路径
        wav_source: 要注入的 WAV 文件路径（可以是任意采样率，会自动转换）

    Returns:
        True 表示成功写入
    """
    try:
        # 读取并转换音频
        data, sr = _wav_to_16k_mono(wav_source)

        if len(data) == 0:
            print(f"[FakeMic] 音频为空，跳过")
            return False

        # 转换为 16-bit PCM
        pcm = np.clip(data * 32767, -32768, 32767).astype(np.int16)
        
        # ===== 关键修复：末尾添加静音填充，防止 Chrome 循环到开头 =====
        # Chrome 假麦克风读完文件后自动循环，如果录音时长 > 音频时长，
        # 末尾会录到开头的重复片段。加 3 秒静音确保不会循环。
        silence_samples = np.zeros(int(16000 * 3.0), dtype=np.int16)
        pcm = np.concatenate([pcm, silence_samples])
        
        pcm_bytes = pcm.tobytes()

        # 构建 WAV 文件头
        n_channels = 1
        bits_per_sample = 16
        byte_rate = sr * n_channels * bits_per_sample // 8
        block_align = n_channels * bits_per_sample // 8

        wav_header = struct.pack('<4sI4s4sIHHIIHH4sI',
            b'RIFF', 36 + len(pcm_bytes), b'WAVE',
            b'fmt ', 16, 1, n_channels, sr, byte_rate, block_align, bits_per_sample,
            b'data', len(pcm_bytes))

        # 三重写入策略（修复 Windows 文件锁问题）：
        # 1) Windows API 共享写入 — 即使 Chrome 持有文件句柄也能写（FILE_SHARE_READ|WRITE）
        # 2) 截断写入原路径 — 如果 Chrome 未锁文件，直接覆盖
        # 3) 写临时文件再 os.replace — 确保后续新打开的句柄读到新文件
        import time as _t
        _full_data = wav_header + pcm_bytes
        _write_ok = False

        # 策略1: Windows API 共享写入（Chrome 持有句柄时唯一可行方案）
        if os.name == 'nt':
            _write_ok = _win_write_shared(fake_audio_path, _full_data)
            if _write_ok:
                pass  # 成功，跳过后续策略

        # 策略2: 普通截断写入
        if not _write_ok:
            for _attempt in range(3):
                try:
                    with open(fake_audio_path, 'wb') as f:
                        f.write(_full_data)
                        f.flush()
                        os.fsync(f.fileno())
                    _write_ok = True
                    break
                except Exception as _we:
                    if _attempt < 2:
                        _t.sleep(0.15)
                    else:
                        print(f"[FakeMic] 截断写入失败(重试3次): {_we}")

        # 策略3: 原子替换
        _tmp_path = fake_audio_path + '.new'
        try:
            with open(_tmp_path, 'wb') as f:
                f.write(_full_data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(_tmp_path, fake_audio_path)
        except Exception as _re:
            if not _write_ok:
                print(f"[FakeMic] 所有写入策略均失败: {_re}")
                return False

        dur = len(data) / 16000
        print(f"[FakeMic] 已写入假麦克风音频: {len(data)} samples, {dur:.2f}s -> {fake_audio_path}")
        return True

    except Exception as e:
        print(f"[FakeMic] 写入失败: {e}")
        return False


def write_silence(fake_audio_path: str, duration_s: float = 2.0) -> bool:
    """写入静音音频（用于重置假麦克风状态）。"""
    try:
        sr = 16000
        n_samples = int(sr * duration_s)
        pcm = np.zeros(n_samples, dtype=np.int16)
        pcm_bytes = pcm.tobytes()

        wav_header = struct.pack('<4sI4s4sIHHIIHH4sI',
            b'RIFF', 36 + len(pcm_bytes), b'WAVE',
            b'fmt ', 16, 1, 1, sr, sr * 2, 2, 16,
            b'data', len(pcm_bytes))

        with open(fake_audio_path, 'wb') as f:
            f.write(wav_header + pcm_bytes)

        print(f"[FakeMic] 已写入静音: {duration_s}s")
        return True

    except Exception as e:
        print(f"[FakeMic] 写入静音失败: {e}")
        return False
