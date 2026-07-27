"""把合成好的 WAV 直接注入浏览器麦克风。

关键：FiF 口语评测侧常按 16kHz 封装录音。若注入 MediaStream 实际是 48kHz，
录出来会变成约 3 倍慢放。这里用 MediaStreamTrackGenerator 按 16kHz
实时写入 PCM，或在不支持时用 16k AudioContext 兜底。
"""

from __future__ import annotations

import base64
import time
from math import gcd
from pathlib import Path
from typing import Any, Optional, Tuple

try:
    import numpy as np
except Exception:
    np = None

try:
    import soundfile as sf
except Exception:
    sf = None

try:
    from scipy import signal as scipy_signal
except Exception:
    scipy_signal = None

# 与 YourTTS / 多数口语评测一致
TARGET_MIC_RATE = 16000

# ---------------------------------------------------------------------------
# 安装钩子：创建 16k 假麦克风轨道
# ---------------------------------------------------------------------------
_HOOK_BODY = r"""
  if (window.__fifMicReady) return true;
  try {
    if (!navigator.mediaDevices) {
      try {
        Object.defineProperty(navigator, 'mediaDevices', { value: {}, configurable: true });
      } catch (e) {}
    }
    if (!navigator.mediaDevices) return false;

    const TARGET_RATE = 16000;
    const FIF_CONTEXT_RATE = 48000;
    window.__fifMicTargetRate = TARGET_RATE;
    window.__fifMicMode = 'none';

    // FiF 的录音器把 ScriptProcessor 输入硬编码为 48kHz。Windows 设备若运行
    // 在 96kHz，它仍只按 48kHz 下采样，最终录音会变成两倍慢放。
    const NativeAudioContext = window.AudioContext || window.webkitAudioContext;
    if (NativeAudioContext && !window.__fifNativeAudioContext) {
      const ForcedAudioContext = new Proxy(NativeAudioContext, {
        construct(Target, args) {
          const supplied = args.length > 0 && args[0] ? args[0] : {};
          const options = Object.assign({}, supplied);
          if (options.sampleRate == null) options.sampleRate = FIF_CONTEXT_RATE;
          return Reflect.construct(Target, [options], Target);
        },
      });
      window.__fifNativeAudioContext = NativeAudioContext;
      try { window.AudioContext = ForcedAudioContext; } catch (e) {}
      try { window.webkitAudioContext = ForcedAudioContext; } catch (e) {}
    }

    // ---- 路径 A：MediaStreamTrackGenerator（可精确控制 sampleRate）----
    const hasGen = (typeof MediaStreamTrackGenerator !== 'undefined')
      && (typeof AudioData !== 'undefined');

    let generatorTrack = null;
    let writer = null;
    let nextTimestamp = 0; // microseconds

    const ensureWriter = async () => {
      if (writer && generatorTrack && generatorTrack.readyState === 'live') {
        return writer;
      }
      if (!hasGen) return null;
      if (writer) {
        try { writer.releaseLock(); } catch (e) {}
      }
      generatorTrack = new MediaStreamTrackGenerator({ kind: 'audio' });
      writer = generatorTrack.writable.getWriter();
      nextTimestamp = 0;
      window.__fifGenTrack = generatorTrack;
      window.__fifMicMode = 'generator';
      return writer;
    };

    const writeSilence = async (seconds) => {
      const w = await ensureWriter();
      if (!w) return;
      const n = Math.max(1, Math.floor(TARGET_RATE * seconds));
      const data = new Float32Array(n); // zeros
      const audioData = new AudioData({
        format: 'f32',
        sampleRate: TARGET_RATE,
        numberOfFrames: n,
        numberOfChannels: 1,
        timestamp: nextTimestamp,
        data: data,
      });
      nextTimestamp += Math.floor((n / TARGET_RATE) * 1e6);
      await w.write(audioData);
      audioData.close();
    };

    // 保活：空闲时写静音，避免 track 被当成 stalled。
    // 语音写入期间必须暂停，否则静音会插进每个语音块之间，拉长录音。
    let keepAliveTimer = null;
    let keepAlivePending = null;
    let isInjecting = false;
    const startKeepAlive = () => {
      if (keepAliveTimer || !hasGen) return;
      keepAliveTimer = setInterval(() => {
        if (isInjecting || keepAlivePending) return;
        keepAlivePending = writeSilence(0.02)
          .catch(() => {})
          .finally(() => { keepAlivePending = null; });
      }, 20);
    };

    // ---- 路径 B：AudioContext 强制 16k + MediaStreamDestination ----
    let audioCtx = null;
    let dest = null;
    const setupAudioCtxFallback = () => {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return false;
      try {
        audioCtx = new AudioCtx({ sampleRate: TARGET_RATE });
      } catch (e) {
        audioCtx = new AudioCtx();
      }
      dest = audioCtx.createMediaStreamDestination();
      // 极低电平保活
      try {
        const osc = audioCtx.createOscillator();
        const g = audioCtx.createGain();
        g.gain.value = 0.0005;
        osc.frequency.value = 20;
        osc.connect(g);
        g.connect(dest);
        osc.start();
      } catch (e) {}
      window.__fifAudioCtx = audioCtx;
      window.__fifMicDest = dest;
      window.__fifMicSampleRate = audioCtx.sampleRate;
      window.__fifMicMode = 'audiocontext';
      return true;
    };

    if (hasGen) {
      // 先建 track，getUserMedia 才能立刻返回
      ensureWriter().then(() => startKeepAlive()).catch(() => {});
    } else {
      setupAudioCtxFallback();
    }

    const resumeCtx = async () => {
      if (audioCtx && audioCtx.state === 'suspended') {
        try { await audioCtx.resume(); } catch (e) {}
      }
    };
    window.__fifResumeMic = resumeCtx;

    /**
     * 播放原始 float32 mono PCM（小端 base64）。
     * sampleRate 应为 16000。
     */
    window.__fifPlayPcmRaw = async (b64, sampleRate, numFrames) => {
      const binary = atob(b64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      // Float32 view；注意对齐
      const ab = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
      const floats = new Float32Array(ab);
      const n = Math.min(numFrames || floats.length, floats.length);
      const sr = sampleRate || TARGET_RATE;

      // ---- Generator 路径：按真实时间写入 16k AudioData ----
      if (hasGen) {
        isInjecting = true;
        try {
          const w = await ensureWriter();
          if (keepAlivePending) await keepAlivePending;
          startKeepAlive();
          const frameSize = 1600; // 100ms @ 16k
          let offset = 0;
          const t0 = performance.now();
          let framesWritten = 0;
          while (offset < n) {
            const end = Math.min(offset + frameSize, n);
            const chunk = floats.subarray(offset, end);
            // AudioData 需要自己的 buffer 拷贝
            const copy = new Float32Array(chunk.length);
            copy.set(chunk);
            const audioData = new AudioData({
              format: 'f32',
              sampleRate: sr,
              numberOfFrames: copy.length,
              numberOfChannels: 1,
              timestamp: nextTimestamp,
              data: copy,
            });
            nextTimestamp += Math.floor((copy.length / sr) * 1e6);
            await w.write(audioData);
            audioData.close();
            framesWritten += copy.length;
            offset = end;
            // 实时节拍：已写时长 vs 墙上时钟
            const shouldMs = (framesWritten / sr) * 1000;
            const elapsed = performance.now() - t0;
            const delay = shouldMs - elapsed;
            if (delay > 4) {
              await new Promise((r) => setTimeout(r, delay));
            }
          }
          // 尾部再写 80ms 静音，避免被截断
          await writeSilence(0.08);
          return n / sr;
        } finally {
          isInjecting = false;
        }
      }

      // ---- AudioContext 兜底 ----
      if (!audioCtx) setupAudioCtxFallback();
      await resumeCtx();
      if (!audioCtx || !dest) return 0;

      // 若上下文不是 16k，按「评测会把流当 16k 读」做补偿：
      // 实际 ctxRate / 16k 倍速播放，使封装后时长正常。
      const ctxRate = audioCtx.sampleRate || 48000;
      const buffer = audioCtx.createBuffer(1, n, sr);
      buffer.copyToChannel(floats.subarray(0, n), 0);
      const src = audioCtx.createBufferSource();
      const gain = audioCtx.createGain();
      gain.gain.value = 1.4;
      src.buffer = buffer;
      // 关键补偿：ctx=48k 且评测按 16k 解读时，playbackRate = ctx/16000
      // 这样墙上时间变短，采样点数变少，被标成 16k 后时长才对。
      // 若评测正确重采样，此补偿会偏快——仅在 rate 偏离 16k 时启用。
      let rate = 1.0;
      if (Math.abs(ctxRate - TARGET_RATE) > 100) {
        rate = ctxRate / TARGET_RATE;
        console.warn('[fif] AudioContext rate', ctxRate, 'compensate playbackRate', rate);
      }
      src.playbackRate.value = rate;
      src.connect(gain);
      gain.connect(dest);
      // 监听用：不补偿，按正常语速出声（另建 source）
      try {
        const hear = audioCtx.createBufferSource();
        const hg = audioCtx.createGain();
        hg.gain.value = 0.9;
        hear.buffer = buffer;
        hear.playbackRate.value = 1.0;
        hear.connect(hg);
        hg.connect(audioCtx.destination);
        hear.start();
      } catch (e) {}

      const duration = (n / sr) / rate; // 墙上时间
      await new Promise((resolve, reject) => {
        src.onended = () => resolve();
        try { src.start(); } catch (err) { reject(err); }
      });
      return n / sr; // 逻辑音频时长
    };

    // 兼容旧接口：收到 wav base64 时在浏览器 decode（可能被拉到 ctx rate）
    window.__fifPlayPcm = async (b64) => {
      await resumeCtx();
      if (!audioCtx) setupAudioCtxFallback();
      if (!audioCtx) return 0;
      const binary = atob(b64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      const audioBuffer = await audioCtx.decodeAudioData(bytes.buffer.slice(0));
      // 抽出 mono，再走 raw 路径逻辑的简化版
      const ch0 = audioBuffer.getChannelData(0);
      let mono = ch0;
      if (audioBuffer.numberOfChannels > 1) {
        mono = new Float32Array(ch0.length);
        for (let c = 0; c < audioBuffer.numberOfChannels; c++) {
          const d = audioBuffer.getChannelData(c);
          for (let i = 0; i < d.length; i++) mono[i] += d[i];
        }
        for (let i = 0; i < mono.length; i++) mono[i] /= audioBuffer.numberOfChannels;
      }
      // 转 base64 float32
      const outBytes = new Uint8Array(mono.buffer.slice(0));
      let s = '';
      const chunk = 0x8000;
      for (let i = 0; i < outBytes.length; i += chunk) {
        s += String.fromCharCode.apply(null, outBytes.subarray(i, i + chunk));
      }
      return await window.__fifPlayPcmRaw(btoa(s), audioBuffer.sampleRate, mono.length);
    };

    const getFakeStream = () => {
      if (generatorTrack) {
        return new MediaStream([generatorTrack]);
      }
      if (dest) return dest.stream;
      if (hasGen) {
        // 同步兜底：阻塞不了，只能先建 empty 再 async
        try {
          generatorTrack = new MediaStreamTrackGenerator({ kind: 'audio' });
          writer = generatorTrack.writable.getWriter();
          window.__fifMicMode = 'generator';
          startKeepAlive();
          return new MediaStream([generatorTrack]);
        } catch (e) {}
      }
      setupAudioCtxFallback();
      return dest ? dest.stream : new MediaStream();
    };

    window.__fifMicStreamGetter = getFakeStream;

    const handleGUM = async (constraints) => {
      const c = constraints || {};
      const wantAudio = c.audio === true || (typeof c.audio === 'object' && c.audio !== null);
      const wantVideo = !!c.video;
      if (wantAudio && !wantVideo) {
        await resumeCtx();
        if (hasGen) await ensureWriter();
        const stream = getFakeStream();
        window.__fifLastGumStream = stream;
        return stream;
      }
      if (window.__fifOriginalGUM) {
        return window.__fifOriginalGUM.call(navigator.mediaDevices, constraints);
      }
      throw new Error('getUserMedia unavailable');
    };

    if (!window.__fifOriginalGUM && navigator.mediaDevices.getUserMedia) {
      window.__fifOriginalGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    }
    navigator.mediaDevices.getUserMedia = handleGUM;

    const legacy = async function (constraints, success, error) {
      try {
        const stream = await handleGUM(constraints);
        if (success) success(stream);
        return stream;
      } catch (e) {
        if (error) error(e);
        throw e;
      }
    };
    try { navigator.getUserMedia = legacy; } catch (e) {}
    try { navigator.webkitGetUserMedia = legacy; } catch (e) {}
    try { navigator.mozGetUserMedia = legacy; } catch (e) {}

    window.__fifMicReady = true;
    window.__fifMicSampleRate = TARGET_RATE;
    return true;
  } catch (e) {
    console.error('[fif] mic hook failed', e);
    return false;
  }
"""

INSTALL_HOOK_JS = "() => {\n" + _HOOK_BODY + "\n}"
INIT_SCRIPT_JS = "(function() {\n" + _HOOK_BODY + "\n})();"

PLAY_RAW_JS = r"""
async ({ b64, sampleRate, numFrames }) => {
  if (typeof window.__fifPlayPcmRaw !== 'function') return 0;
  try {
    if (typeof window.__fifResumeMic === 'function') await window.__fifResumeMic();
    return await window.__fifPlayPcmRaw(b64, sampleRate, numFrames);
  } catch (e) {
    console.error('[fif] play raw failed', e);
    return 0;
  }
}
"""

STATUS_JS = r"""
() => ({
  ready: !!window.__fifMicReady,
  mode: window.__fifMicMode || 'none',
  ctx: window.__fifAudioCtx ? window.__fifAudioCtx.state : 'n/a',
  rate: window.__fifMicSampleRate || window.__fifMicTargetRate || 0,
  hasGen: typeof MediaStreamTrackGenerator !== 'undefined',
  hasAudioData: typeof AudioData !== 'undefined',
})
"""


def _wav_to_f32_mono_16k(wav_path: str) -> Tuple[bytes, int, int]:
    """读 wav → float32 mono @ 16kHz，返回 (raw_bytes, sample_rate, num_frames)。"""
    if sf is None or np is None:
        raise RuntimeError("需要 soundfile 与 numpy")

    data, sr = sf.read(wav_path, always_2d=True, dtype="float32")
    mono = data.mean(axis=1)
    target = TARGET_MIC_RATE
    if int(sr) != target:
        if scipy_signal is not None:
            g = gcd(int(sr), target)
            up = target // g
            down = int(sr) // g
            mono = scipy_signal.resample_poly(mono, up, down).astype(np.float32)
        else:
            duration = len(mono) / float(sr)
            target_len = max(1, int(round(duration * target)))
            x_old = np.linspace(0.0, 1.0, num=len(mono), endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
            mono = np.interp(x_new, x_old, mono).astype(np.float32)
    mono = np.clip(mono, -1.0, 1.0).astype(np.float32)
    return mono.tobytes(), target, int(mono.shape[0])


def install_browser_mic_hook(page: Any) -> int:
    ok = 0
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    targets = frames if frames else [page]
    for frame in targets:
        try:
            if frame.evaluate(INSTALL_HOOK_JS):
                ok += 1
        except Exception as e:
            try:
                name = frame.url
            except Exception:
                name = ""
            if any(k in str(name).lower() for k in ("fif", "kyxl", "assessment", "dekaron")):
                print(f"[BrowserMic] 安装失败 {name}: {e}")
    return ok


def browser_mic_status(page: Any) -> list:
    out = []
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    for frame in frames:
        try:
            st = frame.evaluate(STATUS_JS)
            url = frame.url or ""
            out.append({"url": url[:120], **(st or {})})
        except Exception:
            continue
    return out


def _pick_play_frames(page: Any) -> list:
    """优先只向真正录音的 assessment/dekaron frame 注入。"""
    try:
        frames = list(page.frames)
    except Exception:
        return []

    preferred = []
    fallback = []
    for frame in frames:
        url = (frame.url or "").lower()
        if any(k in url for k in ("assessment", "dekaron", "dekaronmain")):
            preferred.append(frame)
        elif "fifedu" in url or "kyxl" in url or "fiforal" in url:
            fallback.append(frame)
    return preferred or fallback or frames


def play_wav_in_browser(page: Any, wav_path: str, extra_wait_ms: int = 500) -> bool:
    path = Path(wav_path)
    if not path.exists():
        print(f"[BrowserMic] 文件不存在: {wav_path}")
        return False

    try:
        raw, sr, nframes = _wav_to_f32_mono_16k(str(path))
    except Exception as e:
        print(f"[BrowserMic] 读取/重采样失败: {e}")
        return False

    b64 = base64.b64encode(raw).decode("ascii")
    dur_logic = nframes / float(sr)

    installed = install_browser_mic_hook(page)
    if installed <= 0:
        print("[BrowserMic] 未安装到任何 frame")
        return False

    statuses = browser_mic_status(page)
    if statuses:
        print(f"[BrowserMic] 钩子状态: {statuses}")

    payload = {"b64": b64, "sampleRate": sr, "numFrames": nframes}
    targets = _pick_play_frames(page)
    max_dur = 0.0
    played = 0

    for frame in targets:
        try:
            url = frame.url or ""
            print(f"[BrowserMic] 注入 frame: {url[:100]} ... ({nframes} samples @ {sr}Hz, {dur_logic:.2f}s)")
            dur = frame.evaluate(PLAY_RAW_JS, payload)
            if dur and float(dur) > 0:
                played += 1
                max_dur = max(max_dur, float(dur))
        except Exception as e:
            print(f"[BrowserMic] frame 播放失败: {e}")
            continue

    if played <= 0:
        # 再试全部 frame
        for frame in list(page.frames):
            try:
                dur = frame.evaluate(PLAY_RAW_JS, payload)
                if dur and float(dur) > 0:
                    played += 1
                    max_dur = max(max_dur, float(dur))
            except Exception:
                continue

    if played <= 0 or max_dur <= 0:
        print("[BrowserMic] 注入播放失败")
        return False

    # Generator 路径已在 JS 内实时等待；这里只补一点尾裕量
    tail = extra_wait_ms / 1000.0
    print(
        f"[BrowserMic] 已注入 16k PCM，逻辑时长 {max_dur:.2f}s"
        f"（{played} frame），尾部再等 {tail:.2f}s"
    )
    time.sleep(tail)
    return True
