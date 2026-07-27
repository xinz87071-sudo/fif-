import json
import re
import logging
from typing import Optional

from playwright.sync_api import Page, sync_playwright

from injection.BrowserMic import INIT_SCRIPT_JS, install_browser_mic_hook

logger = logging.getLogger(__name__)


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
        self.browser = self.playwright.chromium.launch(
            headless=False,
            args=[
                # 减少真实硬件麦干扰；实际音频由页面内 hook 注入
                "--use-fake-ui-for-media-stream",
                # 注入音频依赖页面定时器，窗口被遮挡后也必须保持实时调度。
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-features=CalculateNativeWinOcclusion",
            ],
        )
        self.context = self.browser.new_context(permissions=["microphone"])
        # 每个新 document/iframe 自动安装麦克风注入钩子
        try:
            self.context.add_init_script(INIT_SCRIPT_JS)
        except Exception as e:
            logger.exception("add_init_script failed: %s", e)
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
    ) -> int:
        """跳过 FiF 原生播放，直到页面进入录音状态。"""
        import time as _time

        deadline = _time.monotonic() + timeout_ms / 1000.0
        skipped = 0
        skip_selectors = (".skip-text", "a.jump_btn")

        while _time.monotonic() < deadline:
            try:
                recording = iframe.get_by_text("结束录音", exact=True)
                if recording.count() > 0 and recording.first.is_visible(timeout=200):
                    return skipped
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
                        if selector == ".skip-text":
                            audio_started = candidate.evaluate(
                                """
                                element => {
                                    const audio = element.ownerDocument
                                        .getElementById("luAudio");
                                    const source = audio && (
                                        audio.currentSrc ||
                                        audio.getAttribute("src")
                                    );
                                    return Boolean(
                                        source &&
                                        !audio.error &&
                                        audio.readyState >= 2 &&
                                        (!audio.paused || audio.ended) &&
                                        audio.currentTime >= 0.2
                                    );
                                }
                                """
                            )
                            if not audio_started:
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
                # FiF editTalk 包含 500ms 动画和 80ms 防抖。
                page.wait_for_timeout(650)
            else:
                page.wait_for_timeout(150)

        raise TimeoutError(
            f"等待录音开始超时（{timeout_ms/1000:.0f}s），"
            f"期间已跳过 {skipped} 段网页播放"
        )

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

        page.goto(
            self.urls["unit_test"].format(
                self.get_user_info()["data"]["userId"],
                task_id,
                unit_id,
                level_id,
            )
        )
        page.wait_for_load_state("load")
        # 开始挑战前装好钩子，这样页面 getUserMedia 会拿到我们的假麦克风
        n = install_browser_mic_hook(page)
        print(f"[BrowserMic] 预安装钩子: {n} 个 frame")
        page.frame_locator("iframe").get_by_role("tab", name="挑战").click()
        page.frame_locator("iframe").get_by_role("button", name="开始挑战").click()
        page.wait_for_timeout(3000)
        # iframe 内挑战页加载后再装一次（必须在页面调 getUserMedia 前后都尽量覆盖）
        n = install_browser_mic_hook(page)
        print(f"[BrowserMic] 挑战页钩子: {n} 个 frame")

        iframe = page.frame_locator("iframe")
        for answer_index, answer_text in enumerate(answer):
            print(f"等待开始录音。")
            self._wait_for_recording(page, iframe)
            print(f"正在回答第{answer_index + 1}题，录音内容为：\n{answer_text}")
            # 注入浏览器麦克风（避免系统环回慢放）
            speaker.speak(answer_text, page=page)
            print(f"第{answer_index + 1}题回答完成。")
            try:
                iframe.get_by_text("结束录音").click(timeout=15000)
            except Exception as e:
                print(f"点击结束录音失败: {e}")
            # 给上传/切题一点时间，减少直接卡在「评测中」
            page.wait_for_timeout(1500)

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
                for item in qcontent.get("item", []):
                    for question in item.get("questions", []):
                        recording_time = str(question.get("recordingTime") or "")
                        group_key = recording_time.split("#", 1)[0] or "0"
                        content = question.get("text") or question.get("title") or ""
                        content = re.sub(r'<[^>]+>', '', str(content)).strip()
                        if content:
                            grouped_answers.setdefault(group_key, []).append(content)

                result = [" ".join(lines) for lines in grouped_answers.values() if lines]
                print(
                    f"[answers] t9 分组: {len(result)} 题，"
                    f"每题句数 {[len(lines) for lines in grouped_answers.values() if lines]}"
                )
                return result

            # 非对话模式：获取"title"标签后的句子
            answer = []
            for _i in qcontent.get("item", []):
                for _j in _i.get("questions", []):
                    title = _j.get("title", "")
                    # 去掉可能的 HTML 标签并修剪
                    title = re.sub(r'<[^>]+>', '', title).strip()
                    if title:
                        answer.append(title)
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
