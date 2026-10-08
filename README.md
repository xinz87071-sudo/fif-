# VoxPilot (fif-)

> 这是一个 fif 自动化完成任务的脚本。

VoxPilot 是一个面向 FiF 口语训练流程的桌面自动化工具。它使用 TTS 生成回答，
通过浏览器内的 `getUserMedia` 钩子直接写入录音流。

> 本项目**仅供学习和个人用途，请勿用于非法用途**。

## 工作方式

```text
题目数据 -> 答案解析 -> TTS 合成 -> 16 kHz PCM
                                      |
                                      v
FiF 录音器 <- getUserMedia 音频流 <- 浏览器注入钩子
```

## 环境要求

- 64 位 Windows 10 或 Windows 11
- 64 位 Python 3.12
- 至少 8 GB 可用磁盘空间
- 推荐 NVIDIA GPU，也支持纯 CPU 运行

## 安装

克隆项目：

```powershell
git clone https://github.com/EnderPPT/VoxPilot.git
Set-Location VoxPilot
```

创建虚拟环境：

```powershell
py -3.12 -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
```

### CUDA 版（默认、推荐）

```powershell
python -m pip install torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1 --index-url https://download.pytorch.org/whl/cu118
```

PyTorch 已包含 CUDA 运行时，无需单独安装 CUDA Toolkit，但需要安装 NVIDIA 驱动。

### CPU 版（无 NVIDIA 显卡）

```powershell
python -m pip install torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1 --index-url https://download.pytorch.org/whl/cpu
```

CUDA 和 CPU 两种版本只安装一个，VoxPilot 会自动选择可用设备。然后安装项目依赖：

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

检查运行设备：

```powershell
python -c "import torch; print('运行设备:', 'CUDA' if torch.cuda.is_available() else 'CPU')"
```

启动：

```powershell
.\.venv\Scripts\python.exe src\main.py
```

首次合成语音时会自动下载 YourTTS 模型，之后会使用本地缓存。

## 配置

1. 准备一段清晰的目标音色 WAV，建议放在 `draft/target_voice.wav`。
2. 启动程序，在“登录配置”中填写账号、密码和目标音色路径。
3. 根据需要设置跳过分数、跳过规则和等级类型规则。
4. 点击“保存配置”，程序会在项目根目录生成 `config.json`。

也可以从模板开始：

```powershell
Copy-Item config.example.json config.json
```

## 运行

```powershell
.\.venv\Scripts\python.exe src\main.py
```

运行期间浏览器可以被其他窗口覆盖，但不建议将其最小化。Windows 仍可能对最小化
窗口施加额外的资源限制。

## 项目结构

```text
VoxPilot/
|-- src/
|   |-- main.py                 # GUI 与任务调度
|   |-- connector/
|   |   `-- FiFWebClient.py     # 页面操作与答案解析
|   |-- injection/
|   |   `-- BrowserMic.py       # 浏览器音频流注入
|   |-- speaker/
|   |   `-- Speaker.py          # TTS 与注入流程
|   `-- tts/
|       `-- TTSSolver.py        # 语音合成
|-- tests/                      # 解析和注入测试
|-- draft/                      # 目标音色文件
|-- config.example.json         # 配置模板
|-- requirements.txt
`-- run.ps1
```

## 常见问题

### 日志显示“浏览器麦克风注入失败”

检查日志中的 `[BrowserMic]` 状态，确认挑战页面的 iframe 已安装钩子。页面刷新或
登录状态失效后，应重新开始当前任务。

### 页面出现 `STATUS_BREAKPOINT`

先完全退出程序并重新启动，确保 Chromium 后台调度参数生效。运行时不要最小化
浏览器，并关闭会强制冻结后台标签页的第三方浏览器工具。

### 合成速度很慢

CPU 模式下首次加载模型和长文本合成都会较慢。可配置兼容的 CUDA 版 PyTorch，
并确保 TTS 模型已完整下载。

# 免责声明

- **仅供个人学习用途**。

# 致谢与引用

- [microsoft/playwright](https://github.com/microsoft/playwright)
- [coqui-ai/TTS](https://github.com/coqui-ai/TTS)
- [Aurorabili/fuckfif](https://github.com/Aurorabili/fuckfif)
- [1z2y3x4w5/fif_gui](https://github.com/1z2y3x4w5/fif_gui)

## License

本项目使用 MIT License，详见 [LICENSE](LICENSE)。
