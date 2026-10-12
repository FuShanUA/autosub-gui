# AutoSub GUI

AutoSub GUI is a Windows-friendly desktop workflow for downloading a video, transcribing it with Whisper, translating the subtitles with a configured LLM, and burning the subtitles into the video.

## Features

- Tkinter GUI with task history and live logs
- Video download through `yt-dlp`
- Speech-to-text with `faster-whisper`
- LLM translation through Gemini, DeepSeek, DashScope, Zhipu AI, OpenAI, Moonshot, SiliconFlow, or Vertex AI
- Bilingual SRT generation and ASS subtitle styling
- FFmpeg-based hardsub rendering
- Optional WeChat Channels uploader

## Requirements

- Windows 10 or later
- Python 3.10 or newer
- Git
- FFmpeg

The install script creates a virtual environment under `Library\Tools\autosub\.venv`, installs Python dependencies, installs Chromium for Playwright, and installs FFmpeg with winget when FFmpeg is not already available.

## Quick Start

```powershell
git clone https://github.com/FuShanUA/autosub-gui.git
cd autosub-gui
powershell -ExecutionPolicy Bypass -File .\install.ps1
.\Library\Tools\autosub\.venv\Scripts\python.exe .\Library\Tools\autosub\autosub_gui.py
```

Generated projects are written to `.\Projects`.

## API Keys

Use either method:

1. Open the GUI, go to the API key settings, enter a key, and save it.
2. Copy `.env.example` to `.env`, then fill in one or more keys:

```text
GEMINI_API_KEY=
DEEPSEEK_API_KEY=
DASHSCOPE_API_KEY=
ZHIPUAI_API_KEY=
OPENAI_API_KEY=
SILICONFLOW_API_KEY=
```

`.env`, saved GUI settings, cookies, browser profiles, and generated projects are excluded by Git.

## Optional Integrations

Google Drive sync and Bilibili upload are disabled by default. Google Drive requires an external sync helper and folder ID configured through environment variables. Bilibili upload requires an external uploader script and Python executable configured through `BILI_UPLOAD_SCRIPT` and `BILI_UPLOAD_PYTHON`. The WeChat Channels uploader is included and creates its login profile under an ignored repository path.

## Project Layout

```text
Library/Tools/autosub/       GUI and main workflow
Library/Tools/common/        Shared LLM, SRT, cover, and title utilities
Library/Tools/transcriber/   Whisper transcription engine
Library/Tools/hardsubber/    ASS generation and FFmpeg rendering
Library/Tools/subtranslator/ Subtitle translation helpers
Library/Tools/vdown/         Download helpers
```

## License

Released under the MIT License. See [LICENSE](LICENSE).
