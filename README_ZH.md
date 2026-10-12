# AutoSub GUI

AutoSub GUI 是一个面向 Windows 的桌面工作流：下载视频、用 Whisper 转写、调用配置的大模型翻译字幕，最后把字幕压制进视频。

## 功能

- Tkinter 图形界面，包含任务历史和实时日志
- 通过 `yt-dlp` 下载视频
- 使用 `faster-whisper` 做语音转文字
- 支持 Gemini、DeepSeek、百炼/DashScope、智谱、OpenAI、Moonshot、SiliconFlow、Vertex AI 等供应商
- 生成双语 SRT 和 ASS 字幕样式
- 使用 FFmpeg 压制硬字幕
- 包含可选的视频号上传器

## 环境要求

- Windows 10 或更新版本
- Python 3.10 或更新版本
- Git
- FFmpeg

安装脚本会在 `Library\Tools\autosub\.venv` 创建虚拟环境，安装 Python 依赖，为 Playwright 安装 Chromium，并在系统没有 FFmpeg 时通过 winget 安装。

## 快速开始

```powershell
git clone https://github.com/FuShanUA/autosub-gui.git
cd autosub-gui
powershell -ExecutionPolicy Bypass -File .\install.ps1
.\Library\Tools\autosub\.venv\Scripts\python.exe .\Library\Tools\autosub\autosub_gui.py
```

生成的项目会保存在 `.\Projects`。

## API Key

可以选择两种方式：

1. 打开 GUI，进入 API Key 设置，填入并保存。
2. 把 `.env.example` 复制为 `.env`，填入一个或多个 key：

```text
GEMINI_API_KEY=
DEEPSEEK_API_KEY=
DASHSCOPE_API_KEY=
ZHIPUAI_API_KEY=
OPENAI_API_KEY=
SILICONFLOW_API_KEY=
```

Git 会排除 `.env`、GUI 保存的设置、cookies、浏览器登录数据和生成的项目文件。

## 可选集成

Google Drive 同步和 Bilibili 上传默认关闭。Google Drive 需要外部同步脚本和文件夹 ID，通过环境变量配置。Bilibili 上传需要外部上传脚本和 Python 解释器，通过 `BILI_UPLOAD_SCRIPT` 和 `BILI_UPLOAD_PYTHON` 配置。视频号上传器已包含，登录数据会保存在仓库内被 Git 忽略的目录。

## 目录结构

```text
Library/Tools/autosub/       GUI 和主流程
Library/Tools/common/        共享的 LLM、SRT、封面和标题工具
Library/Tools/transcriber/   Whisper 转写引擎
Library/Tools/hardsubber/    ASS 生成和 FFmpeg 压制
Library/Tools/subtranslator/ 字幕翻译辅助工具
Library/Tools/vdown/         下载辅助工具
```

## 许可证

MIT License，见 [LICENSE](LICENSE)。
