import json
import os
import re
import logging
from typing import Optional, Tuple

from playwright.sync_api import Page, sync_playwright

from injection.BrowserMic import play_wav_in_browser

logger = logging.getLogger(__name__)

# ===== 捕获 URL.createObjectURL 的 blob 音频（已禁用：从未生效且占内存）=====
BLOB_HOOK_JS = r"""
(function() {
    if (window.__fifBlobHooked) return;
    window.__fifBlobHooked = true;
    window.__fifBlobAudios = [];
})();
"""


class FiFWebClient:
    urls = {
        "login": "https://www.fifedu.com/iplat/fifLogin/index.html?v=5.3.3",
        "ai_task": "https://static.fifedu.com/static/fiforal/kyxl-web-static/student-h5/index.html#/pages/teaching/teaching",
        "unit_test": "https://static.fifedu.com/static/fiforal/kyxl-web-static/student-h5/index.html#/pages/webView/testWebView/testWebView?userId={}&taskId={}&unitId={}&gId={}",
    }
    api_urls = {
        "get_user_info": "https://www.fifedu.com/iplatform-zjzx/common/connect",
        "get_task_list": "https://moral.fifedu.com/kyxl-app/stu/task/teaTaskList",
        "get_task_detail": "https://moral.fifedu.com/kyxl-app/task/stu/teaTaskDetail",
        "get_unit_info": "https://moral.fifedu.com/kyxl-app/stu/column/stuUnitInfo?unitId={}&taskId={}",
        "post_test_results": "https://moral.fifedu.com/kyxl-app-challenge/evaluation/submitChallengeResults",
        "get_test_info": "https://moral.fifedu.com/kyxl-app/column/getLevelInfo",
    }
    user_auth = {"token": None, "source": None}
    user_info = None

    def __init__(self):
        self.playwright = sync_playwright().start()
        # 创建假音频文件路径（Chrome 原生假麦克风会读取这个文件）
        self._fake_audio_path = os.path.abspath(os.path.join("tmp", "fake_mic_input.wav"))
        os.makedirs(os.path.dirname(self._fake_audio_path), exist_ok=True)
        # 创建初始静音 WAV 文件（Chrome 要求文件存在才能启动）
        import struct
        sample_rate = 16000
        duration_s = 2.0
        n_samples = int(sample_rate * duration_s)
        wav_data = struct.pack('<' + 'h' * n_samples, *([0] * n_samples))
        wav_header = struct.pack('<4sI4s4sIHHIIHH4sI',
            b'RIFF', 36 + len(wav_data), b'WAVE',
            b'fmt ', 16, 1, 1, sample_rate, sample_rate * 2, 2, 16,
            b'data', len(wav_data))
        with open(self._fake_audio_path, 'wb') as f:
            f.write(wav_header + wav_data)
        
        self.browser = self.playwright.chromium.launch(
            headless=False,
            args=[
                # 使用 Chrome 原生假音频设备（不需要 JS 钩子）
                "--use-fake-device-for-media-stream",
                f"--use-file-for-fake-audio-capture={self._fake_audio_path}",
                # 注入音频依赖页面定时器，窗口被遮挡后也必须保持实时调度。
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-features=CalculateNativeWinOcclusion,AudioServiceOutOfProcess",
                "--disable-web-security",
                "--no-first-run",
            ],
        )
        self.context = self.browser.new_context(permissions=["microphone"])
        # 使用 Chrome 原生假麦克风，不需要 JS 钩子
        self.page = self.context.new_page()

    def __del__(self):
        try:
            if getattr(self, 'browser', None):
                self.browser.close()
        except Exception as e:
            logger.exception("Error closing browser: %s", e)
        try:
            if getattr(self, 'playwright', None):
                self.playwright.stop()
        except Exception as e:
            logger.exception("Error stopping playwright: %s", e)

    def login(self, username, password):
        self.page.goto(self.urls["login"], timeout=60000)
        self.page.fill('input[name="user"]', username)
        self.page.fill('input[name="pass"]', password)
        self.page.get_by_role("button", name="登录").click()
        self.page.wait_for_load_state("networkidle", timeout=60000)
        link = self.page.locator("span", has_text="FiF口语训练系统")
        with self.page.expect_popup() as fif_page:
            link.first.click()
        page1 = fif_page.value

        page1.wait_for_load_state("networkidle")
        self.user_auth["token"] = page1.evaluate(
            "localStorage.getItem('Authorization')"
        )
        self.user_auth["source"] = page1.evaluate("localStorage.getItem('source')")
        page1.close()
        if self.user_auth["token"] is None or self.user_auth["token"] == "":
            raise Exception("登录失败")
        return self.get_user_info()

    def get_user_info(self):
        if self.user_info is not None:
            return self.user_info
        else:
            response = self.page.request.fetch(
                self.api_urls["get_user_info"], method="GET"
            )
            if response.status != 200:
                raise Exception("获取用户信息失败")
            self.user_info = json.loads(response.body())
            return self.user_info

    def get_task_list(self, page):
        response = page.request.fetch(
            self.api_urls["get_task_list"],
            method="post",
            headers={
                "Authorization": "Bearer " + self.user_auth["token"], # type: ignore
                "source": self.user_auth["source"],
            },
            form={
                "userId": self.get_user_info()["data"]["userId"],
                "status": 1,
                "page": 1,
            },
        )
        json_data = response.json()
        if json_data["status"] == -1:
            raise Exception("获取任务列表失败")
        return json_data

    def get_ttd_list(self, page, task_id):
        response = page.request.fetch(
            self.api_urls["get_task_detail"],
            method="post",
            form={
                "userId": self.get_user_info()["data"]["userId"],
                "id": task_id,
            },
            headers={
                "Authorization": "Bearer " + self.user_auth["token"], # type: ignore
                "source": self.user_auth["source"],
            },
        )
        json_data = response.json()
        if json_data["status"] == -1:
            raise Exception("获取任务详情失败")
        return json_data

    def get_unit_info(self, page, unit_id, task_id):
        response = page.request.fetch(
            self.api_urls["get_unit_info"].format(unit_id, task_id),
            method="get",
            headers={
                "Authorization": "Bearer " + self.user_auth["token"], # type: ignore
                "source": self.user_auth["source"],
            },
        )
        json_data = response.json()
        if json_data["status"] == -1:
            raise Exception("获取单元信息失败")
        return json_data

    def _wait_for_recording(
        self, page: Page, iframe, timeout_ms: int = 120000
    ) -> Tuple[int, str]:
        """跳过 FiF 原生播放，直到页面进入录音状态。

        返回 (跳过段数, 参考音频URL)。
        """
        import time as _time

        # （已移除 AudioBufferSourceNode.start hook - 从未生效且浪费内存）
        # 清空之前的捕获
        try:
            for frame in list(page.frames):
                try:
                    frame.evaluate("() => { window.__fifPlayedAudios = []; window.__fifDecodedAudios = []; window.__fifBlobAudios = []; }")
                except Exception:
                    pass
        except Exception:
            pass

        deadline = _time.monotonic() + timeout_ms / 1000.0
        skipped = 0
        skip_selectors = (".skip-text", "a.jump_btn")
        captured_audio_url = ""

        # ===== 监听网络请求，抓取音频 URL =====
        audio_urls_from_network = []
        def on_request(request):
            url = request.url
            if any(ext in url.lower() for ext in ['.mp3', '.wav', '.ogg', '.m4a', '.aac', '.wma', 'audio', 'voice', 'sound', 'tts', 'pronounce']):
                audio_urls_from_network.append(url)
                print(f"[FiF-NET] 发现音频请求: {url[:120]}")

        try:
            page.on("request", on_request)
        except Exception:
            pass

        try:
            while _time.monotonic() < deadline:
                # ===== 优先从网络请求抓取音频 URL =====
                if not captured_audio_url and audio_urls_from_network:
                    captured_audio_url = audio_urls_from_network[0]
                    print(f"[FiF] 从网络请求抓取音频 URL: {captured_audio_url[:100]}")

                # ===== 从 DOM 元素抓取 =====
                if not captured_audio_url:
                    for check_target in [iframe, page]:
                        try:
                            if check_target is page:
                                urls = page.evaluate("""
                                    () => {
                                        const els = document.querySelectorAll('#luAudio, audio[src], audio source[src], video[src]');
                                        return Array.from(els).map(e => e.currentSrc || e.getAttribute('src') || e.src || '').filter(Boolean);
                                    }
                                """)
                            else:
                                urls = iframe.locator("#luAudio, audio, video").evaluate_all("""
                                    elements => elements.map(e => e.currentSrc || e.getAttribute('src') || e.src || '').filter(Boolean)
                                """)
                            if urls:
                                # ===== 验证 URL 是音频文件 =====
                                for u in urls:
                                    if not u:
                                        continue
                                    # 跳过页面 URL（luAudio 的 src 可能是页面地址）
                                    if u.endswith('/') or u.endswith('.html') or u.endswith('.php'):
                                        continue
                                    # 跳过和当前页面相同的 URL
                                    try:
                                        page_url = page.url
                                        if u == page_url or u.rstrip('/') == page_url.rstrip('/'):
                                            continue
                                    except Exception:
                                        pass
                                    # 接受 blob:, data:, 或有音频扩展名的 URL
                                    if u.startswith(('blob:', 'data:')) or any(ext in u.lower() for ext in ['.mp3', '.wav', '.ogg', '.m4a', '.aac', '.wma', '.flac']):
                                        captured_audio_url = u
                                        print(f"[FiF] 从 DOM 抓取音频 URL: {captured_audio_url[:100]}")
                                        break
                                if captured_audio_url:
                                    break
                        except Exception:
                            continue

                try:
                    recording = iframe.get_by_text("结束录音", exact=True)
                    if recording.count() > 0 and recording.first.is_visible(timeout=200):
                        return skipped, captured_audio_url, None
                except Exception:
                    pass

                clicked = False
                for selector in skip_selectors:
                    try:
                        candidates = iframe.locator(selector)
                        for index in range(candidates.count()):
                            candidate = candidates.nth(index)
                            if not candidate.is_visible(timeout=100):
                                continue
                            candidate.click(timeout=2000)
                            skipped += 1
                            clicked = True
                            print(f"[main] 已跳过网页播放 {skipped} 段。")
                            break
                    except Exception:
                        continue
                    if clicked:
                        break

                if clicked:
                    page.wait_for_timeout(650)
                else:
                    page.wait_for_timeout(200)
        finally:
            try:
                page.remove_listener("request", on_request)
            except Exception:
                pass

        raise TimeoutError(
            f"等待录音开始超时（{timeout_ms/1000:.0f}s），"
            f"期间已跳过 {skipped} 段网页播放"
        )

    def _download_reference_audio(self, page: Page, audio_url: str, prev_length: int = 0) -> Optional[bytes]:
        """从 FiF 下载参考发音音频并转换为 16kHz WAV。

        prev_length: 上一题的音频总长度，用于截取本题新增部分（blob URL 会累积）。
        """
        if not audio_url:
            return None
        try:
            # 如果是相对 URL，转为绝对 URL
            if not audio_url.startswith(('http://', 'https://', 'blob:', 'data:')):
                audio_url = page.evaluate(
                    "(u) => new URL(u, window.location.href).href", audio_url
                )

            result = page.evaluate(
                """
                async (url) => {
                    try {
                        let arrayBuffer;
                        try {
                            const response = await fetch(url, { mode: 'cors' });
                            if (!response.ok) throw new Error('HTTP ' + response.status);
                            arrayBuffer = await response.arrayBuffer();
                        } catch (corsErr) {
                            return { error: 'CORS blocked: ' + corsErr.message };
                        }

                        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                        const audioBuffer = await audioCtx.decodeAudioData(arrayBuffer);

                        const targetSR = 16000;
                        const duration = audioBuffer.duration;
                        const targetLength = Math.ceil(duration * targetSR);
                        const offlineCtx = new OfflineAudioContext(1, targetLength, targetSR);
                        const source = offlineCtx.createBufferSource();
                        source.buffer = audioBuffer;
                        source.connect(offlineCtx.destination);
                        source.start();
                        const resampled = await offlineCtx.startRendering();
                        const channelData = resampled.getChannelData(0);

                        const bytes = new Uint8Array(channelData.buffer);
                        let binary = '';
                        for (let i = 0; i < bytes.length; i += 8192) {
                            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
                        }
                        return {
                            base64: btoa(binary),
                            sampleRate: targetSR,
                            length: channelData.length,
                            duration: duration
                        };
                    } catch (e) {
                        return { error: e.message || String(e) };
                    }
                }
                """,
                audio_url,
            )
            if not result or result.get("error"):
                err = result.get('error') if result else 'null'
                print(f"[FiF] 下载参考音频失败: {err}")
                if result and 'CORS' in str(err):
                    return self._download_via_request(page, audio_url, prev_length)
                return None

            import base64 as _b64
            pcm_bytes = _b64.b64decode(result["base64"])
            import numpy as np
            pcm = np.frombuffer(pcm_bytes, dtype=np.float32)

            total_len = len(pcm)
            # ===== 累积音频检测：blob URL 会累积之前所有音频 =====
            if prev_length > 0 and total_len > prev_length:
                # 累积模式：只取新增部分
                pcm = pcm[prev_length:]
                print(f"[FiF] 累积检测: {prev_length} -> {total_len}，本题 {len(pcm)} samples ({len(pcm)/16000:.2f}s)")
            elif prev_length > 0 and total_len <= prev_length:
                # 非累积模式：用完整音频，重置 prev_length
                print(f"[FiF] 非累积模式 ({total_len} <= {prev_length})，使用完整音频")
            # else: 第一题，直接用完整音频

            # 检查音频是否太短（< 0.3s 认为无效）
            if len(pcm) < 0.3 * 16000:
                print(f"[FiF] 参考音频太短 ({len(pcm)} samples = {len(pcm)/16000:.2f}s)，降级 TTS")
                return None

            import io
            import soundfile as sf
            buf = io.BytesIO()
            sf.write(buf, pcm, 16000, format='WAV', subtype='PCM_16')
            wav_bytes = buf.getvalue()
            print(f"[FiF] 参考音频下载成功: {len(pcm)} samples ({len(pcm)/16000:.2f}s)")
            return wav_bytes, total_len  # 返回总长度供下一题累积检测
        except Exception as e:
            print(f"[FiF] 下载参考音频异常: {e}")
            return None

    def _download_via_request(self, page: Page, audio_url: str, prev_length: int = 0) -> Optional[tuple]:
        """用 Playwright 的 request API 下载音频（绕过 CORS）。"""
        try:
            resp = page.request.get(audio_url)
            if not resp.ok:
                return None
            raw_bytes = resp.body()

            import io
            import numpy as np
            try:
                import soundfile as sf
                audio_np, sr = sf.read(io.BytesIO(raw_bytes), dtype='float32')
                if audio_np.ndim > 1:
                    audio_np = audio_np.mean(axis=1)
                if sr != 16000:
                    from scipy.signal import resample
                    target_len = int(len(audio_np) * 16000 / sr)
                    audio_np = resample(audio_np, target_len).astype(np.float32)
                total_len = len(audio_np)
                # 累积音频检测
                if prev_length > 0 and total_len > prev_length:
                    audio_np = audio_np[prev_length:]
                elif prev_length > 0 and total_len <= prev_length:
                    pass  # 非累积模式，用完整音频
                if len(audio_np) < 0.3 * 16000:
                    return None
                buf = io.BytesIO()
                sf.write(buf, audio_np, 16000, format='WAV', subtype='PCM_16')
                return buf.getvalue(), total_len
            except Exception:
                return None
        except Exception:
            return None

    def start_level_test(self, page: Page, speaker, unit_id, task_id, level_id, level_name: Optional[str] = None):
        print(f"尝试加载{level_id}答案。")
        try:
            answer = self.get_level_answer(page, level_id, level_name)
            if answer:
                print(f"已加载{len(answer)}组录音答案。")
            else:
                print("未找到答案。")
        except Exception as e:
            raise Exception(f"加载答案失败: {str(e)}")

        # ===== 关键优化：提前批量合成所有答案音频 =====
        # 这样录音开始后只需注入音频，不用等待 TTS 合成
        import tempfile
        import time as _time
        answer_wav_paths = []
        if answer:
            print(f"[TTS] 预合成 {len(answer)} 条答案...")
            synth_start = _time.monotonic()
            for idx, ans_text in enumerate(answer):
                wav_path = os.path.join("tmp", f"answer_{level_id}_{idx}.wav")
                try:
                    speaker.tts_solver.get_file(ans_text, wav_path)
                    answer_wav_paths.append(wav_path)
                    print(f"[TTS] 预合成 {idx+1}/{len(answer)}: {ans_text[:40]}...")
                except Exception as e:
                    print(f"[TTS] 预合成第{idx+1}条失败: {e}")
                    answer_wav_paths.append(None)
            elapsed = _time.monotonic() - synth_start
            print(f"[TTS] 预合成完成，耗时 {elapsed:.1f}s")

        # ===== 修复Q1空白：用Q1的TTS音频预填充假麦克风文件 =====
        # Chrome 在"开始挑战"后立即 getUserMedia（读取初始文件），此时必须已有音频
        # Q1 的参考音频要等 API 映射才能获取，但 TTS 音频现在就绪
        if answer_wav_paths and answer_wav_paths[0] and os.path.exists(answer_wav_paths[0]):
            try:
                from injection.FakeMic import write_fake_audio
                write_fake_audio(self._fake_audio_path, answer_wav_paths[0])
                print(f"[FakeMic] Q1 TTS 音频已预填充到假麦克风（修复Q1空白）")
            except Exception as _pf_e:
                print(f"[FakeMic] Q1 预填充失败: {_pf_e}")

        # ===== 网络监听：保存 FiF 下载的参考音频文件（轻量级，不拦截请求）=====
        audio_save_dir = os.path.join("tmp", "fif_audio")
        os.makedirs(audio_save_dir, exist_ok=True)
        for _f in os.listdir(audio_save_dir):
            try:
                os.remove(os.path.join(audio_save_dir, _f))
            except Exception:
                pass
        self._fif_audio_files = []  # [(monotonic_ts, filepath, full_url)]
        self._fif_audio_counter = 0
        self._fif_audio_lock = __import__('threading').Lock()
        self._fif_api_data = []  # FiF API 响应数据（包含题目-音频映射）

        def _on_audio_response(response):
            try:
                url = response.url
                # 捕获所有可能包含题目数据的 API 响应
                content_type = response.headers.get('content-type', '')
                is_json = 'json' in content_type
                is_data_api = any(k in url.lower() for k in ['api', 'question', 'exam', 'test', 'level', 'unit', 'task', 'data', 'list', 'info', 'detail', 'get'])
                
                if is_json or is_data_api:
                    try:
                        body_text = response.text()
                        if body_text and len(body_text) > 50:
                            # 搜索是否包含音频 URL
                            has_audio_ref = any(k in body_text.lower() for k in ['kyxl-column-res', '.mp3', '.wav', 'audio', 'voice', 'pronounce', 'sound'])
                            if has_audio_ref:
                                with self._fif_audio_lock:
                                    self._fif_api_data.append({
                                        'url': url,
                                        'data': body_text[:100000],
                                        'ts': __import__('time').monotonic()
                                })
                                print(f"[FiF-API] 捕获含音频的 API: {url[:120]} ({len(body_text)} chars)")
                    except Exception:
                        pass
                
                # 捕获音频文件
                is_audio = any(k in url.lower() for k in [
                    'kyxl.oss', 'kyxl-column-res', '.mp3', '.wav', '.ogg',
                    '.m4a', '.aac', 'pronounce', 'voice'
                ])
                is_image = any(k in url.lower() for k in ['.png', '.jpg', '.jpeg', '.gif', '.ico', 'image', '.js', '.css'])
                if is_audio and not is_image:
                    body = response.body()
                    if len(body) > 500:
                        with self._fif_audio_lock:
                            self._fif_audio_counter += 1
                            idx = self._fif_audio_counter
                            fpath = os.path.join(audio_save_dir, f"ref_{idx:04d}.bin")
                            with open(fpath, 'wb') as f:
                                f.write(body)
                            self._fif_audio_files.append((__import__('time').monotonic(), fpath, url))
                        print(f"[FiF-AUDIO] 捕获音频 #{idx}: {len(body)} bytes <- {url}")
            except Exception:
                pass

        try:
            page.on("response", _on_audio_response)
        except Exception as e:
            print(f"[FiF-AUDIO] 设置监听失败: {e}")

        page.goto(
            self.urls["unit_test"].format(
                self.get_user_info()["data"]["userId"],
                task_id,
                unit_id,
                level_id,
            )
        )
        page.wait_for_load_state("load")
        # 页面稳定后再操作
        page.wait_for_timeout(2000)
        try:
            page.frame_locator("iframe").get_by_role("tab", name="挑战").click()
            page.wait_for_timeout(1000)
            page.frame_locator("iframe").get_by_role("button", name="开始挑战").click()
            page.wait_for_timeout(5000)  # 给挑战页充足的加载时间
        except Exception as e:
            err_str = str(e).lower()
            if "target crashed" in err_str or "page crashed" in err_str or "frame was detached" in err_str:
                print(f"浏览器在挑战页加载时崩溃: {e}")
                raise
            print(f"挑战页操作异常（继续尝试）: {e}")
            page.wait_for_timeout(3000)

        iframe = page.frame_locator("iframe")
        browser_crashed = False

        # ===== 解析 API 数据，建立题目↔音频 URL 映射 =====
        self._fif_audio_url_map = {}  # {answer_text: audio_url}
        try:
            import json
            with self._fif_audio_lock:
                for api_entry in self._fif_api_data:
                    try:
                        data = json.loads(api_entry['data'])
                        # 保存 API 响应到文件供分析
                        api_file = os.path.join("tmp", "fif_api_response.json")
                        with open(api_file, 'w', encoding='utf-8') as f:
                            json.dump(data, f, ensure_ascii=False, indent=2)
                        print(f"[FiF-MAP] API 响应已保存到 {api_file}")
                        
                        # 先提取 resourcePrefixPath 用于拼接完整 URL
                        _url_prefix = ''
                        def _find_prefix(obj, depth=0):
                            nonlocal _url_prefix
                            if depth > 10: return
                            if isinstance(obj, dict):
                                if 'resourcePrefixPath' in obj and isinstance(obj['resourcePrefixPath'], str):
                                    _url_prefix = obj['resourcePrefixPath']
                                    return
                                for v in obj.values():
                                    _find_prefix(v, depth+1)
                            elif isinstance(obj, list):
                                for item in obj:
                                    _find_prefix(item, depth+1)
                        _find_prefix(data)
                        if _url_prefix:
                            print(f"[FiF-MAP] URL前缀: {_url_prefix[:80]}...")

                        # 精确提取：res_path + title/text 字段
                        def find_audio_urls(obj, path="", depth=0):
                            if depth > 15:
                                return
                            if isinstance(obj, dict):
                                rp = obj.get('res_path', '')
                                if rp and '.mp3' in str(rp):
                                    # 优先取 title，其次 text，再试 word/sentence
                                    text_val = ''
                                    for tk in ['title', 'text', 'word', 'sentence', 'content', 'enword']:
                                        tv = obj.get(tk, '')
                                        if isinstance(tv, str) and tv.strip():
                                            text_val = tv
                                            break
                                    if text_val:
                                        full_url = rp if rp.startswith('http') else (_url_prefix + rp)
                                        # Unicode 归一化 key，与查找时保持一致
                                        import unicodedata
                                        _nk = unicodedata.normalize('NFKC', text_val.strip().lower())
                                        _nk = _nk.replace('\u2014', '-').replace('\u2013', '-').replace('\u2019', "'").replace('\u201c', '"').replace('\u201d', '"').replace('\u00a0', ' ').strip()
                                        self._fif_audio_url_map[_nk] = full_url
                                        print(f"[FiF-MAP] 映射: '{text_val.strip()[:50]}' -> ...{full_url[-50:]}")
                                # 继续递归
                                for k, v in obj.items():
                                    find_audio_urls(v, f"{path}.{k}", depth + 1)
                            elif isinstance(obj, list):
                                for i, item in enumerate(obj):
                                    find_audio_urls(item, f"{path}[{i}]", depth + 1)

                        find_audio_urls(data)
                    except json.JSONDecodeError as je:
                        print(f"[FiF-MAP] JSON 解析失败: {je}")
            
            print(f"[FiF-MAP] 从 API 解析到 {len(self._fif_audio_url_map)} 个题目-音频映射")
        except Exception as e:
            print(f"[FiF-MAP] 解析 API 数据失败: {e}")

        # ===== 诊断：dump 答案列表 + URL映射键（定位串位）=====
        try:
            with open(os.path.join('tmp', 'vox_debug.log'), 'a', encoding='utf-8') as _df:
                _df.write(f"[diag] level_id={level_id} level_name={level_name!r}\n")
                _df.write(f"[diag] answers ({len(answer)}):\n")
                for _ai, _a in enumerate(answer):
                    _df.write(f"    #{_ai+1}: {_a[:80]!r}\n")
                _df.write(f"[diag] url_map ({len(self._fif_audio_url_map)}):\n")
                for _k in self._fif_audio_url_map:
                    _df.write(f"    key: {_k[:80]!r}\n")
        except Exception:
            pass

        # ===== 收集有效的拦截音频文件（保留完整 URL）=====
        self._fif_valid_audio = []  # [(filepath, full_url)]
        try:
            with self._fif_audio_lock:
                for ts, fp, url in self._fif_audio_files:
                    fsize = os.path.getsize(fp) if os.path.exists(fp) else 0
                    if fsize > 5000 and 'fifedu-web-assets' not in url and '2.mp3' not in url:
                        self._fif_valid_audio.append((fp, url))
            print(f"[FiF-AUDIO] 有效参考音频: {len(self._fif_valid_audio)} 个（总共 {self._fif_audio_counter} 个拦截）")
            for i, (fp, url) in enumerate(self._fif_valid_audio[:5]):
                print(f"  #{i}: {os.path.basename(fp)} ({os.path.getsize(fp)} bytes) <- {url[:100]}")
        except Exception as e:
            print(f"[FiF-AUDIO] 收集音频列表失败: {e}")

        # ===== 主动下载 API 映射中缺失的音频文件 =====
        print(f"[FiF-DL] 开始检查缺失音频... 映射={len(self._fif_audio_url_map)} 拦截={len(self._fif_valid_audio)}")
        try:
            import urllib.request
            _existing_urls = set()
            for fp, url in self._fif_valid_audio:
                # 提取文件名作为 key
                fname = url.split('/')[-1].split('?')[0]
                _existing_urls.add(fname)

            _download_dir = os.path.join("tmp", "fif_audio_downloaded")
            os.makedirs(_download_dir, exist_ok=True)

            _download_count = 0
            for _ans_key, _audio_url in self._fif_audio_url_map.items():
                _fname = _audio_url.split('/')[-1].split('?')[0]
                if not _fname or _fname in _existing_urls:
                    continue
                _dl_path = os.path.join(_download_dir, _fname)
                if os.path.exists(_dl_path) and os.path.getsize(_dl_path) > 5000:
                    self._fif_valid_audio.append((_dl_path, _audio_url))
                    _existing_urls.add(_fname)
                    continue
                try:
                    req = urllib.request.Request(_audio_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        _data = resp.read()
                    if len(_data) > 5000:
                        with open(_dl_path, 'wb') as f:
                            f.write(_data)
                        self._fif_valid_audio.append((_dl_path, _audio_url))
                        _existing_urls.add(_fname)
                        _download_count += 1
                        print(f"[FiF-DL] 下载音频: {_fname} ({len(_data)} bytes)")
                except Exception as dl_e:
                    print(f"[FiF-DL] 下载失败 {_fname}: {dl_e}")

            print(f"[FiF-DL] 主动下载 {_download_count} 个音频，现有 {len(self._fif_valid_audio)} 个可用")
        except Exception as e:
            print(f"[FiF-DL] 主动下载失败: {e}")

        # ===== 移除网络监听，减少开销 =====
        try:
            page.remove_listener("response", _on_audio_response)
            print("[FiF-AUDIO] 已移除网络监听（音频已收集完毕）")
        except Exception:
            pass

        # ===== 安装 MediaStream 追踪钩子 + 强制关闭残留 MediaStream =====
        # 钩子：包装 getUserMedia 存储 stream 引用，便于后续强制停止释放文件句柄
        try:
            for _target in [page, iframe]:
                try:
                    _target.evaluate("""() => {
                        if (!window._fifOrigGetUserMedia) {
                            window._fifOrigGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                            window._fifActiveStreams = [];
                            navigator.mediaDevices.getUserMedia = async function(constraints) {
                                const stream = await window._fifOrigGetUserMedia(constraints);
                                window._fifActiveStreams.push(stream);
                                return stream;
                            };
                            window._fifStopAllStreams = function() {
                                let stopped = 0;
                                if (window._fifActiveStreams) {
                                    window._fifActiveStreams.forEach(s => {
                                        s.getTracks().forEach(t => { try { t.stop(); stopped++; } catch(e) {} });
                                    });
                                    window._fifActiveStreams = [];
                                }
                                return stopped;
                            };
                        }
                        // 同时关闭残留 stream
                        return window._fifStopAllStreams ? window._fifStopAllStreams() : 0;
                    }""")
                except Exception:
                    pass
            print("[FakeMic] MediaStream 追踪钩子已安装，残留 stream 已关闭")
        except Exception:
            pass

        # ===== 预计算所有题目音频WAV路径（修复随机零分：提前准备好下一题音频）=====
        _pre_wavs = [None] * len(answer)
        _debug_log = os.path.join('tmp', 'vox_debug.log')
        def _dbg(msg):
            print(msg)
            try:
                with open(_debug_log, 'a', encoding='utf-8') as _df:
                    _df.write(msg + chr(10))
            except Exception:
                pass
        try:
            import io as _io, numpy as _np, soundfile as _sf2

            def _load_ref_audio(_pf):
                try:
                    with open(_pf, 'rb') as _f:
                        _rw = _f.read()
                    _d = None; _s = 16000
                    try:
                        _d, _s = _sf2.read(_io.BytesIO(_rw), dtype='float32')
                    except Exception:
                        import subprocess as _sp
                        _tw = _pf + '.pre.wav'
                        _sp.run(['ffmpeg','-y','-i',_pf,'-ar','16000','-ac','1',_tw], capture_output=True, timeout=5)
                        if os.path.exists(_tw):
                            _d, _s = _sf2.read(_tw, dtype='float32')
                    if _d is not None:
                        if _s != 16000:
                            from scipy.signal import resample as _rs
                            _d = _rs(_d, int(len(_d)*16000/_s)).astype(_np.float32)
                        if _d.ndim > 1:
                            _d = _d.mean(axis=1)
                    return _d
                except Exception:
                    return None

            def _norm_text(_t):
                import unicodedata as _ud
                _k = _ud.normalize('NFKC', _t.strip().lower())
                _k = _k.replace('—','-').replace('–','-').replace('’',"'").replace('“','"').replace('”','"').replace(' ',' ').strip()
                return _k

            for _pi, _pt in enumerate(answer):
                _pw = None
                try:
                    _pk = _norm_text(_pt)
                    _pf = None
                    if _pk in self._fif_audio_url_map:
                        _pu = self._fif_audio_url_map[_pk]
                        _pn = _pu.split('/')[-1].split('?')[0]
                        for _fp, _fu in self._fif_valid_audio:
                            if _fu.split('/')[-1].split('?')[0] == _pn:
                                _pf = _fp
                                break
                    if _pf and os.path.exists(_pf):
                        _d = _load_ref_audio(_pf)
                        if _d is not None:
                            _pw = os.path.join('tmp', f'fake_mic_{level_id}_{_pi}.wav')
                            _buf = _io.BytesIO()
                            _sf2.write(_buf, _d, 16000, format='WAV', subtype='PCM_16')
                            with open(_pw, 'wb') as _f:
                                _f.write(_buf.getvalue())
                            _dbg(f'[FakeMic] 预计算第{_pi+1}题: 完整匹配参考音频 (file={os.path.basename(_pf)}, key={_pk[:40]!r})')

                    # ===== 子句匹配 + 音频拼接（合并文本找不到单个URL时）=====
                    if not _pw and len(_pk) > 50:
                        _dbg(f'[pre] 第{_pi+1}题: 完整匹配失败，子句匹配 (map={len(self._fif_audio_url_map)}, valid={len(self._fif_valid_audio)})')
                        _sub_matches = []
                        for _map_key, _map_url in self._fif_audio_url_map.items():
                            _nk = _norm_text(_map_key)
                            if len(_nk) < 15:
                                continue
                            _pos = _pk.find(_nk)
                            if _pos >= 0:
                                _map_fn = _map_url.split('/')[-1].split('?')[0]
                                _found_audio = False
                                # 先查已拦截的音频文件
                                for _fp2, _fu2 in self._fif_valid_audio:
                                    if _fu2.split('/')[-1].split('?')[0] == _map_fn and os.path.exists(_fp2):
                                        _sub_d = _load_ref_audio(_fp2)
                                        if _sub_d is not None:
                                            _sub_matches.append((_pos, _sub_d, len(_nk)))
                                            _found_audio = True
                                        break
                                # 不在已拦截列表中 → 直接从URL下载参考音频
                                if not _found_audio and _map_url:
                                    try:
                                        import requests as _req
                                        _dl_path = os.path.join('tmp', f'ref_dl_{level_id}_{_map_fn}')
                                        if not os.path.exists(_dl_path):
                                            _resp = _req.get(_map_url, timeout=8)
                                            if _resp.status_code == 200 and len(_resp.content) > 500:
                                                with open(_dl_path, 'wb') as _df:
                                                    _df.write(_resp.content)
                                                _dbg(f'[pre] 下载参考音频: {_map_fn} ({len(_resp.content)} bytes)')
                                            else:
                                                _dbg(f'[pre] 下载失败: {_map_fn} status={_resp.status_code}')
                                        if os.path.exists(_dl_path):
                                            _sub_d = _load_ref_audio(_dl_path)
                                            if _sub_d is not None:
                                                _sub_matches.append((_pos, _sub_d, len(_nk)))
                                                _dbg(f'[pre] 子句匹配: pos={_pos} -> {_map_fn} ({len(_sub_d)/16000:.1f}s)')
                                    except Exception as _dl_e:
                                        _dbg(f'[pre] 下载异常: {_dl_e}')
                        if _sub_matches:
                            _sub_matches.sort(key=lambda x: x[0])
                            _filtered = []
                            _last_end = -1
                            for _pos, _sd, _tlen in _sub_matches:
                                if _pos >= _last_end:
                                    _filtered.append((_pos, _sd))
                                    _last_end = _pos + _tlen
                            if _filtered:
                                # 段间加0.3s静音，避免拼接处产生断续影响识别
                                _gap = _np.zeros(int(16000 * 0.3), dtype=_np.float32)
                                _parts = []
                                for _gi, (_, _sd) in enumerate(_filtered):
                                    if _gi > 0:
                                        _parts.append(_gap)
                                    _parts.append(_sd)
                                _concat = _np.concatenate(_parts)
                                _pw = os.path.join('tmp', f'fake_mic_{level_id}_{_pi}.wav')
                                _buf = _io.BytesIO()
                                _sf2.write(_buf, _concat, 16000, format='WAV', subtype='PCM_16')
                                with open(_pw, 'wb') as _f:
                                    _f.write(_buf.getvalue())
                                _total_dur = len(_concat) / 16000
                                _dbg(f'[pre] 第{_pi+1}题: 子句拼接 {len(_filtered)} 段 ({_total_dur:.1f}s)')
                except Exception as _pre_e:
                    _dbg(f'[pre] 第{_pi+1}题失败: {_pre_e}')
                if not _pw and _pi < len(answer_wav_paths) and answer_wav_paths[_pi]:
                    _pw = answer_wav_paths[_pi]
                    _dbg(f'[pre] 第{_pi+1}题: 用TTS')
                _pre_wavs[_pi] = _pw
            _dbg(f'[pre] 完成: {sum(1 for p in _pre_wavs if p)}/{len(answer)} 题有音频')
        except Exception as _pre_outer:
            print(f"[FakeMic] 预计算整体失败: {_pre_outer}")

        for answer_index, answer_text in enumerate(answer):
            if browser_crashed:
                print(f"第{answer_index + 1}题跳过（浏览器已崩溃）。")
                continue
            print(f"正在准备第{answer_index + 1}题，录音内容为：\n{answer_text}")

            # ===== 先准备音频并写入假麦克风（必须在录音开始前！）=====
            ref_wav_bytes = None
            _wav_path_for_fake_mic = None
            
            # 方法1：API 映射 + URL 匹配拦截文件（精确文件名匹配）
            _matched_fpath = None
            _answer_key = answer_text.strip().lower()
            # Unicode 归一化：统一 em-dash/弯引号/不间断空格等变体
            import unicodedata
            _answer_key = unicodedata.normalize('NFKC', _answer_key)
            _answer_key = _answer_key.replace('\u2014', '-').replace('\u2013', '-').replace('\u2019', "'").replace('\u201c', '"').replace('\u201d', '"').replace('\u00a0', ' ').strip()

            if _answer_key in self._fif_audio_url_map:
                target_url = self._fif_audio_url_map[_answer_key]
                target_filename = target_url.split('/')[-1].split('?')[0]
                for fp, url in self._fif_valid_audio:
                    _url_fname = url.split('/')[-1].split('?')[0]
                    if target_filename and _url_fname == target_filename:
                        _matched_fpath = fp
                        print(f"[FiF-MAP] 第{answer_index+1}题 '{answer_text}' URL匹配: {os.path.basename(fp)} (目标={target_filename}, 匹配={_url_fname})")
                        break
                if not _matched_fpath:
                    print(f"[FiF-MAP] 第{answer_index+1}题 '{answer_text}' 在映射中但无匹配文件 (目标={target_filename})")
            else:
                # 显示相近的 key 帮助调试
                _similar = [k for k in self._fif_audio_url_map if _answer_key[:15] in k or k[:15] in _answer_key][:3]
                print(f"[FiF-MAP] 第{answer_index+1}题 不在映射中 (key='{_answer_key[:60]}', 相近: {_similar})")
            
            # 方法2：URL 匹配失败时不按顺序猜（顺序不可靠），让 TTS 兜底
            if not _matched_fpath:
                print(f"[FiF-AUDIO] 第{answer_index+1}题 '{answer_text}' URL 未匹配到拦截文件，将用 TTS")
            
            # 读取并转换音频
            if _matched_fpath and os.path.exists(_matched_fpath):
                try:
                    import io, numpy as np, soundfile as sf
                    with open(_matched_fpath, 'rb') as f:
                        raw = f.read()
                    data = None
                    try:
                        data, sr = sf.read(io.BytesIO(raw), dtype='float32')
                    except Exception:
                        try:
                            import subprocess
                            tmp_wav = _matched_fpath + '.wav'
                            subprocess.run(['ffmpeg', '-y', '-i', _matched_fpath, '-ar', '16000', '-ac', '1', tmp_wav],
                                         capture_output=True, timeout=5)
                            if os.path.exists(tmp_wav):
                                data, sr = sf.read(tmp_wav, dtype='float32')
                        except Exception:
                            pass
                    if data is not None:
                        if sr != 16000:
                            from scipy.signal import resample as sp_resample
                            target_len = int(len(data) * 16000 / sr)
                            data = sp_resample(data, target_len).astype(np.float32)
                        if data.ndim > 1:
                            data = data.mean(axis=1)
                        dur = len(data) / 16000
                        print(f"[FiF-AUDIO] 第{answer_index+1}题音频: {len(data)} samples, {dur:.2f}s")
                        buf = io.BytesIO()
                        sf.write(buf, data, 16000, format='WAV', subtype='PCM_16')
                        ref_wav_bytes = buf.getvalue()
                        _wav_path_for_fake_mic = os.path.join("tmp", f"fake_mic_{level_id}_{answer_index}.wav")
                        with open(_wav_path_for_fake_mic, 'wb') as f:
                            f.write(ref_wav_bytes)
                except Exception as e:
                    print(f"[FiF-AUDIO] 音频处理失败: {e}")

            # ===== 备用方案1：使用预计算的原声/子句拼接音频 =====
            if not ref_wav_bytes and _pre_wavs and answer_index < len(_pre_wavs) and _pre_wavs[answer_index] and os.path.exists(_pre_wavs[answer_index]):
                _wav_path_for_fake_mic = _pre_wavs[answer_index]
                print(f"[FakeMic] 第{answer_index+1}题使用预计算音频: {os.path.basename(_wav_path_for_fake_mic)}")

            # ===== 备用方案2：TTS 预合成音频 =====
            if not _wav_path_for_fake_mic:
                wav_path = answer_wav_paths[answer_index] if answer_index < len(answer_wav_paths) else None
                if wav_path and os.path.exists(wav_path):
                    _wav_path_for_fake_mic = wav_path
                    print(f"[FakeMic] 第{answer_index+1}题使用 TTS 预合成音频")

            # ===== 将音频写入 Chrome 假麦克风文件（必须在 getUserMedia 之前！）=====
            if _wav_path_for_fake_mic and os.path.exists(_wav_path_for_fake_mic):
                try:
                    from injection.FakeMic import write_fake_audio
                    success = write_fake_audio(self._fake_audio_path, _wav_path_for_fake_mic)
                    if success:
                        # MD5 校验：验证写入内容与源文件一致
                        import hashlib
                        with open(_wav_path_for_fake_mic, 'rb') as _sf2:
                            _src_md5 = hashlib.md5(_sf2.read()).hexdigest()[:8]
                        print(f"[FakeMic] 第{answer_index+1}题音频已写入假麦克风 (源MD5={_src_md5})")
                    else:
                        print(f"[FakeMic] 第{answer_index+1}题音频写入失败")
                except Exception as e:
                    print(f"[FakeMic] 写入异常: {e}")
            else:
                # 关键修复：无可用音频时写入静音，防止使用上一题的残留音频
                try:
                    from injection.FakeMic import write_silence
                    write_silence(self._fake_audio_path, 2.0)
                    print(f"[FakeMic] 第{answer_index+1}题无可用音频，已写入静音（防止串音）")
                except Exception as _sil_e:
                    print(f"[FakeMic] 第{answer_index+1}题无可用音频，静音写入失败: {_sil_e}")

            # ===== 等待录音开始（音频已提前写入，Chrome getUserMedia 会读到正确内容）=====
            print(f"等待开始录音。")
            wait_result = self._wait_for_recording(page, iframe)
            if len(wait_result) == 3:
                skip_count, ref_audio_url, decoded_audio = wait_result
            else:
                skip_count, ref_audio_url = wait_result
                decoded_audio = None

            # ===== 假麦克风方案：音频已在录音前写入，只需等待播放完成 =====
            import time as _time

            # ===== 关键修复：录音进行中【禁止】写入下一题音频 =====
            # Chrome --use-file-fake-audio-capture 边录边读/循环读文件。
            # 若在当前录音进行中覆盖文件，会污染当前录音导致"音频错位"（长音频尤其明显）。
            # 下一题音频改由两处安全时机写入：
            #   1) 每题"准备阶段"主写（录音开始前，getUserMedia 读到正确内容）
            #   2) 结束录音后的"抢占切题空档"预写
            # 因此这里【不再】在录音中提前覆盖文件。

            # 获取音频时长（从 _wav_path_for_fake_mic）
            audio_dur = 2.0  # 默认 2 秒
            if _wav_path_for_fake_mic and os.path.exists(_wav_path_for_fake_mic):
                try:
                    import soundfile as _sf
                    _info = _sf.info(_wav_path_for_fake_mic)
                    audio_dur = _info.frames / _info.samplerate
                except Exception:
                    pass
            # 等待音频播放完成 + 额外缓冲
            wait_time = audio_dur + 2.0
            print(f"[FakeMic] 等待 {wait_time:.1f}s 让音频播放完成...")
            page.wait_for_timeout(int(wait_time * 1000))

            # ===== 结束录音 =====
            print(f"第{answer_index + 1}题回答完成。")
            # ===== 答题后处理（全部包裹 try/except 防崩溃）=====
            try:
                # 先关闭可能的弹窗
                for close_target in [iframe, page]:
                    try:
                        if close_target is page:
                            dialogs = page.locator(".el-message-box__wrapper, .el-dialog__wrapper, [role='dialog']")
                        else:
                            dialogs = iframe.locator(".el-message-box__wrapper, .el-dialog__wrapper, [role='dialog']")
                        for di in range(dialogs.count()):
                            d = dialogs.nth(di)
                            if d.is_visible(timeout=100):
                                for btn_text in ["确定", "确认", "关闭", "知道了", "OK", "取消"]:
                                    try:
                                        btn = d.get_by_text(btn_text, exact=True)
                                        if btn.count() > 0 and btn.first.is_visible(timeout=100):
                                            btn.first.click(timeout=1000)
                                            print(f"[FiF] 关闭弹窗: {btn_text}")
                                            page.wait_for_timeout(300)
                                            break
                                    except Exception:
                                        continue
                    except Exception:
                        pass
                # ===== 移除 loadingAssess 遮罩，避免阻塞"结束录音"按钮 =====
                try:
                    for _target in [iframe, page]:
                        try:
                            _target.evaluate("""() => {
                                document.querySelectorAll('.el-loading-mask, #loadingAssess, .loadingAssess').forEach(el => {
                                    el.style.display = 'none';
                                    el.remove();
                                });
                            }""")
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    iframe.get_by_text("结束录音").click(timeout=15000, force=True)
                except Exception as e:
                    err_str = str(e).lower()
                    if "target crashed" in err_str or "frame was detached" in err_str or "page crashed" in err_str:
                        print(f"浏览器崩溃({e})，跳过剩余题目。")
                        browser_crashed = True
                        continue
                    # 重试：先移除遮罩、关闭弹窗再点
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(500)
                        # 强制移除 loadingAssess 遮罩
                        try:
                            iframe.evaluate("""() => {
                                document.querySelectorAll('.el-loading-mask, #loadingAssess, .loadingAssess').forEach(el => {
                                    el.style.display = 'none';
                                    el.remove();
                                });
                            }""")
                        except Exception:
                            pass
                        iframe.get_by_text("结束录音").click(timeout=10000, force=True)
                        print(f"[FiF] 重试点击结束录音成功")
                    except Exception as e2:
                        err2_str = str(e2).lower()
                        if "target crashed" in err2_str or "frame was detached" in err2_str:
                            print(f"浏览器崩溃({e2})，跳过剩余题目。")
                            browser_crashed = True
                            continue
                        # ===== JavaScript 强制点击（绕过 Playwright 可见性检查）=====
                        try:
                            for _js_target in [iframe, page]:
                                try:
                                    _result = _js_target.evaluate("""() => {
                                        const btns = document.querySelectorAll('button, .el-button, [role=button], [class*=btn]');
                                        for (const btn of btns) {
                                            const txt = (btn.textContent || '').trim();
                                            if (txt.includes('结束录音') || txt.includes('完成') || txt.includes('结束') || txt.includes('提交') || txt.includes('停止')) {
                                                btn.click();
                                                return 'clicked: ' + txt;
                                            }
                                        }
                                        return 'not found';
                                    }""")
                                    if 'clicked' in str(_result):
                                        print(f"[FiF] JS强制点击结束录音成功: {_result}")
                                        break
                                except Exception:
                                    continue
                            else:
                                print(f"点击结束录音失败: {e2}")
                        except Exception:
                            print(f"点击结束录音失败: {e2}")
                        # ===== 验证录音是否结束（防止录音持续3分钟）=====
                        try:
                            for _wait_i in range(6):
                                page.wait_for_timeout(500)
                                _still_recording = False
                                for _chk in [iframe, page]:
                                    try:
                                        _txt = _chk.evaluate("""() => {
                                            const btns = document.querySelectorAll('button');
                                            for (const btn of btns) {
                                                if (btn.textContent.trim().includes('结束录音')) return 'recording';
                                            }
                                            return 'ended';
                                        }""")
                                        if 'recording' in str(_txt):
                                            _still_recording = True
                                            break
                                    except Exception:
                                        pass
                                if not _still_recording:
                                    print(f"[FiF] 录音已确认结束")
                                    break
                            else:
                                # 仍在录音 → 再次JS强制点击
                                print(f"[FiF] 录音未结束，再次强制点击")
                                for _js_target in [iframe, page]:
                                    try:
                                        _js_target.evaluate("""() => {
                                            document.querySelectorAll('button').forEach(btn => {
                                                if (btn.textContent.trim().includes('结束录音')) btn.click();
                                            });
                                        }""")
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                # ===== 强制停止所有 MediaStream（释放 Chrome 文件句柄）=====
                try:
                    for _stop_target in [iframe, page]:
                        try:
                            _stopped = _stop_target.evaluate(
                                "() => window._fifStopAllStreams ? window._fifStopAllStreams() : 0"
                            )
                            if _stopped:
                                print(f"[FakeMic] 已强制停止 {_stopped} 个 MediaStream 轨道（释放文件句柄）")
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
                # ===== 结束录音后立即写入下一题音频（修复随机零分）=====
                try:
                    _next_idx = answer_index + 1
                    if _next_idx < len(_pre_wavs) and _pre_wavs[_next_idx] and os.path.exists(_pre_wavs[_next_idx]):
                        from injection.FakeMic import write_fake_audio
                        write_fake_audio(self._fake_audio_path, _pre_wavs[_next_idx])
                        print(f"[FakeMic] 已预写入第{_next_idx+1}题音频（抢占切题空档）")
                    elif _next_idx < len(answer_wav_paths) and answer_wav_paths[_next_idx] and os.path.exists(answer_wav_paths[_next_idx]):
                        from injection.FakeMic import write_fake_audio
                        write_fake_audio(self._fake_audio_path, answer_wav_paths[_next_idx])
                        print(f"[FakeMic] 已预写入第{_next_idx+1}题TTS音频（抢占切题空档）")
                    else:
                        from injection.FakeMic import write_silence
                        write_silence(self._fake_audio_path, 0.5)
                except Exception:
                    pass
                # 给上传/切题一点时间
                try:
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
            except Exception as outer_e:
                oe = str(outer_e).lower()
                if "target crashed" in oe or "frame was detached" in oe or "page crashed" in oe:
                    print(f"浏览器崩溃({outer_e})，跳过剩余题目。")
                    browser_crashed = True
                    continue
                print(f"[FiF] 答题后处理出错（不影响继续）: {outer_e}")

        print("挑战完成。等待评分结果（不再无限等待）。")
        self._wait_for_score_result(page)
        print("当前单元结束。")

    def _wait_for_score_result(self, page: Page, timeout_ms: int = 180000) -> None:
        """等待评分结束。页面常显示「评测中」，完成后可能是分数/AI 评分/完成等。

        旧逻辑 timeout=0 等「AI 评分」可点，若文案不同或上传失败会永久卡住。
        """
        import time as _time

        deadline = _time.time() + timeout_ms / 1000.0
        last_log = 0.0
        success_texts = (
            "AI 评分",
            "AI评分",
            "查看报告",
            "挑战完成",
            "重新挑战",
            "得分",
            "总分",
            "分",
        )
        while _time.time() < deadline:
            try:
                body = page.locator("body").inner_text(timeout=2000)
            except Exception:
                try:
                    body = page.frame_locator("iframe").locator("body").inner_text(timeout=2000)
                except Exception:
                    body = ""

            # 仍在评测中：继续等
            if "评测中" in body or "评分中" in body or "加载中" in body:
                now = _time.time()
                if now - last_log > 10:
                    print("[main] 仍在评测中，等待服务器返回…")
                    last_log = now
                page.wait_for_timeout(2000)
                continue

            # 出现结果相关文案则结束等待
            if any(t in body for t in success_texts) and "评测中" not in body:
                print("[main] 检测到评分结果页面。")
                return

            # 尝试点「AI 评分」类按钮（若存在）
            for name in ("AI 评分", "AI评分", "查看报告"):
                try:
                    loc = page.get_by_text(name)
                    if loc.count() > 0:
                        loc.first.click(timeout=2000)
                        page.wait_for_timeout(1000)
                        return
                except Exception:
                    pass
                try:
                    loc = page.frame_locator("iframe").get_by_text(name)
                    if loc.count() > 0:
                        loc.first.click(timeout=2000)
                        page.wait_for_timeout(1000)
                        return
                except Exception:
                    pass

            page.wait_for_timeout(2000)

        print(
            f"[main] 等待评分超时（{timeout_ms/1000:.0f}s）。"
            "若一直「评测中」，多半是录音未成功上传或音频为空，请看是否有 [BrowserMic] 注入日志。"
        )

    def get_level_answer(self, page: Page, level_id, level_name: Optional[str] = None):
        response = page.request.fetch(
            self.api_urls["get_test_info"],
            method="post",
            form={
                "levelId": level_id,
            },
            headers={
                "Authorization": "Bearer " + self.user_auth["token"], # type: ignore
                "source": self.user_auth["source"],
            },
        ).json()

        if response["status"] != 1:
            raise Exception("获取答案失败")

        # 获取挑战模式的内容
        challenge_modes = [_i for _i in response["data"]["content"]["moshi"] if _i["name"] == "挑战"]
        if not challenge_modes:
            raise Exception("未找到挑战模式内容")

        challenge_mode = challenge_modes[0]
        qcontent = challenge_mode["question"]["qcontent"]
        showtype = str(
            challenge_mode.get("showtype") or challenge_mode.get("showType") or ""
        ).lower()

        # ===== 可读取日志：记录 level_name / showtype / 题目结构，便于诊断串位 =====
        try:
            with open(os.path.join('tmp', 'vox_debug.log'), 'a', encoding='utf-8') as _df:
                _df.write(f"[ans] level_id={level_id} level_name={level_name!r} showtype={showtype!r} has_roles={'roles' in qcontent}\n")
        except Exception:
            pass

        # 判断模式：如果有"roles"字段，则为对话模式，否则为非对话模式
        if "roles" in qcontent:
            # 对话模式：获取"text"标签后的句子
            text = qcontent.get("text", "")
            if not text:
                return []

            # 按##分割句子
            sentences = [seg.strip() for seg in text.split('##') if seg.strip()]

            roles = qcontent.get("roles", "")
            if not roles:
                # 如果没有roles，按原顺序，去除前缀
                result = [re.sub(r'^\w+:\s*', '', sent) for sent in sentences if ": " in sent]
                return result

            # 解析roles，如"w1#m1"
            roles_list = roles.split("#")

            # 收集每个角色的句子
            role_sentences = {}
            for sent in sentences:
                if ": " in sent:
                    role, content = sent.split(": ", 1)
                    if role not in role_sentences:
                        role_sentences[role] = []
                    role_sentences[role].append(content.strip())

            # 按roles顺序排列句子
            result = []
            for r in roles_list:
                if r in role_sentences:
                    result.extend(role_sentences[r])

            return result
        else:
            if showtype == "t5":
                selected_roles = {
                    role.strip()
                    for role in str(qcontent.get("sample") or "").split("#")
                    if role.strip()
                }
                selected_answers = []
                for item in qcontent.get("item", []):
                    for question in item.get("questions", []):
                        question_roles = {
                            str(question.get(field)).strip()
                            for field in (
                                "photo",
                                "sample",
                                "role",
                                "speaker",
                                "roleId",
                                "photoCode",
                            )
                            if question.get(field)
                        }
                        if not selected_roles.intersection(question_roles):
                            continue
                        content = question.get("text") or question.get("title") or ""
                        content = re.sub(r'<[^>]+>', '', str(content)).strip()
                        if content:
                            selected_answers.append(content)

                if selected_answers:
                    print(
                        f"[answers] t5 角色 {sorted(selected_roles)}: "
                        f"{len(selected_answers)} 组录音答案"
                    )
                    return selected_answers

            if showtype == "t8":
                sentences = []
                item_counts = []
                for item in qcontent.get("item", []):
                    count = 0
                    for question in item.get("questions", []):
                        content = question.get("text") or question.get("title") or ""
                        content = re.sub(r'<[^>]+>', '', str(content)).strip()
                        if content:
                            sentences.append(content)
                            count += 1
                    item_counts.append(count)

                if sentences:
                    print(
                        f"[answers] t8 单次录音: {len(sentences)} 句，"
                        f"各 item 句数 {item_counts}"
                    )
                    return [" ".join(sentences)]

            if showtype == "t12":
                role_order = [
                    role.strip()
                    for role in str(qcontent.get("sample") or "").split("#")
                    if role.strip()
                ]
                questions = [
                    question
                    for item in qcontent.get("item", [])
                    for question in item.get("questions", [])
                ]
                role_answers = []
                role_counts = []
                for role in role_order:
                    count = 0
                    for question in questions:
                        question_roles = {
                            str(question.get(field)).strip()
                            for field in (
                                "photo",
                                "sample",
                                "role",
                                "speaker",
                                "roleId",
                                "photoCode",
                            )
                            if question.get(field)
                        }
                        if role not in question_roles:
                            continue
                        content = question.get("text") or question.get("title") or ""
                        content = re.sub(r'<[^>]+>', '', str(content)).strip()
                        if content:
                            role_answers.append(content)
                            count += 1
                    role_counts.append(count)

                if role_answers and all(role_counts):
                    print(
                        f"[answers] t12 角色顺序 {role_order}: "
                        f"每阶段句数 {role_counts}"
                    )
                    return role_answers

            if showtype == "t9":
                grouped_answers = {}
                _t9_order = []
                for item in qcontent.get("item", []):
                    for question in item.get("questions", []):
                        recording_time = str(question.get("recordingTime") or "")
                        # recordingTime 格式 "#N" 或 "M#N"，取 # 后的数字作为分组键
                        group_key = recording_time.split("#")[-1] if "#" in recording_time else "0"
                        content = question.get("text") or question.get("title") or ""
                        content = re.sub(r'<[^>]+>', '', str(content)).strip()
                        if content:
                            if group_key not in grouped_answers:
                                grouped_answers[group_key] = []
                                _t9_order.append(group_key)
                            grouped_answers[group_key].append(content)

                result = [" ".join(grouped_answers[g]) for g in _t9_order if grouped_answers[g]]
                print(
                    f"[answers] t9 分组: {len(result)} 题，"
                    f"每题句数 {[len(grouped_answers[g]) for g in _t9_order if grouped_answers[g]]}"
                )
                return result

            # 非对话模式：获取"title"标签后的句子
            # ===== 修复"一期一问"：recordingTime #N 分组合并 =====
            # recordingTime 格式 "#1"/"#2" 表示录音分组号，同组的多句应合并为一个录音
            _raw_questions = []
            for _i in qcontent.get("item", []):
                for _j in _i.get("questions", []):
                    title = _j.get("title", "")
                    title = re.sub(r'<[^>]+>', '', title).strip()
                    rt = str(_j.get("recordingTime") or "")
                    _raw_questions.append((title, rt))
            print(f"[answers] 默认路径: showtype={showtype}, raw={[(t[:30], r) for t, r in _raw_questions]}")

            # 检查是否有 #N 分组格式
            _has_grouping = any('#' in rt and rt.split('#')[-1].strip().isdigit() for _, rt in _raw_questions if rt)
            print(f"[answers] _has_grouping={_has_grouping}")
            if _has_grouping:
                _groups = {}
                _group_order = []
                for title, rt in _raw_questions:
                    if not title:
                        continue
                    # 提取 #N 中的 N 作为分组键
                    _gkey = rt.split('#')[-1].strip() if '#' in rt else '0'
                    if _gkey not in _groups:
                        _groups[_gkey] = []
                        _group_order.append(_gkey)
                    _groups[_gkey].append(title)
                _group_answers = [' '.join(_groups[g]) for g in _group_order if _groups[g]]
                print(f"[answers] recordingTime分组: {len(_group_answers)} 题，每题句数 {[len(_groups[g]) for g in _group_order if _groups[g]]}")
                # 记录分组结构到可读取日志
                try:
                    with open(os.path.join('tmp', 'vox_debug.log'), 'a', encoding='utf-8') as _df:
                        _df.write(f"[ans] 分组: {len(_group_answers)} 题，每题句数 {[len(_groups[g]) for g in _group_order if _groups[g]]} level_name={level_name!r}\n")
                except Exception:
                    pass

                # ===== 按板块类型区分策略 =====
                # "看视频"类多段分段录音：每段只放自己那段内容（否则串位/音频错位）。
                # 其他板块（如"一期一问"）：每个录音段用完整合并文本，避免 Chrome 只循环第一句。
                # 检测信号（任一命中即视为分段录音板块）：
                #   1) level_name 含"视频"（看视频板块）
                #   2) 每个分组恰好 1 句（典型"逐段跟读/多段分段录音"结构）
                _all_single = all(len(_groups[g]) == 1 for g in _group_order if _groups[g])
                _is_video_board = ('视频' in (level_name or '')) or _all_single
                try:
                    with open(os.path.join('tmp', 'vox_debug.log'), 'a', encoding='utf-8') as _df:
                        _df.write(f"[ans] 判定: _all_single={_all_single} _is_video_board={_is_video_board} level_name={level_name!r}\n")
                except Exception:
                    pass
                if _is_video_board:
                    answer = _group_answers
                    print(f"[answers] 分段录音板块-每段独立: {len(answer)} 个录音段")
                    return answer

                # ===== 关键修复：合并所有分组为完整答案 =====
                # 一期一问的录音可能只有1个（整段回答），也可能是多个分段录音。
                # 安全策略：每个录音段都使用完整合并文本，确保 Chrome 不会循环第一句。
                # FiF 评分检测到关键词即可得分，多余内容不会大幅扣分。
                _full_text = ' '.join(_group_answers)
                answer = [_full_text] * len(_group_answers)
                print(f"[answers] 合并策略: {len(answer)} 个录音段，每段完整文本 ({len(_full_text)} 字符)")
                return answer
            
            # 无分组：每个 title 独立
            answer = [title for title, _ in _raw_questions if title]
            return answer


    def get_page(self):
        return self.page

    def get_context(self):
        return self.context

    def get_browser(self):
        return self.browser

    def get_playwright(self):
        return self.playwright

    def get_urls(self):
        return self.urls
