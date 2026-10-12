#!/usr/bin/env python3
"""
微信视频号自动上传工具
基于 Playwright 持久化 browser context，实现扫码登录态保持与自动化上传。

用法:
    # 首次扫码登录
    python wechat_upload.py --login

    # 单个上传
    python wechat_upload.py "/path/to/video.mp4" "视频标题"
    python wechat_upload.py "/path/to/video.mp4" "标题" --desc "描述" --tags "标签1,标签2" --public

    # 批量上传
    python wechat_upload.py --batch /path/to/dir --pattern "*hardsub*.mp4"
"""

import argparse
import asyncio
import base64
import glob
import json
import os
import re
import sys
import subprocess
import shutil
import time
import tempfile
import io

from PIL import Image, ImageChops, ImageOps

# --- 路径配置 ---
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROWSER_DATA_DIR = os.environ.get(
    "WECHAT_BROWSER_DATA_DIR",
    os.path.join(SKILL_DIR, "assets", "browser_data"),
)
QUEUE_PATH = os.path.join(SKILL_DIR, "assets", "failed_queue.json")

# Upload metadata has local fallbacks, so avoid long LLM retry chains here.
os.environ.setdefault("LLM_REQUEST_TIMEOUT", "30")
os.environ.setdefault("LLM_MAX_RETRIES", "1")

# 复用统一 LLM 接口（与 bili_upload.py 对齐）
_COMMON_DIR = os.environ.get(
    "AUTOSUB_COMMON_DIR",
    os.path.abspath(os.path.join(SKILL_DIR, "..", "..", "common")),
)
if os.path.isdir(_COMMON_DIR) and _COMMON_DIR not in sys.path:
    sys.path.insert(0, _COMMON_DIR)
try:
    from llm_utils import get_client as _get_llm_client, LLMProvider as _LLMProvider  # type: ignore
except Exception:
    _get_llm_client = None
    _LLMProvider = None
try:
    from cover_utils import (
        cover_style_matches,
        generate_platform_covers,
    )
    _COVER_UTILS_AVAILABLE = True
except Exception:
    _COVER_UTILS_AVAILABLE = False
try:
    from subtitle_title import (
        SubtitleTitleError,
        build_subtitle_grounded_title,
        read_full_subtitle_text,
        read_subtitle_text as read_strict_subtitle_text,
        validate_title_from_subtitle,
    )
    _SUBTITLE_TITLE_AVAILABLE = True
except Exception:
    SubtitleTitleError = ValueError
    _SUBTITLE_TITLE_AVAILABLE = False

# 微信视频号创作者平台
WECHAT_CREATOR_URL = "https://channels.weixin.qq.com/platform/home"
WECHAT_LOGIN_URL = "https://channels.weixin.qq.com/platform"
WECHAT_HOME_URL = "https://channels.weixin.qq.com/platform/home"
WECHAT_DRAFT_URL = "https://channels.weixin.qq.com/post/draftListManager"
WECHAT_CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"
WECHAT_DRAFT_URL = "https://channels.weixin.qq.com/platform/post/draftListManager"
WECHAT_TITLE_MAX_CHARS = 16
WECHAT_TITLE_FALLBACK = "未命名视频"


def _find_ffprobe():
    """Find ffprobe even when the GUI launches Python with a restricted PATH."""
    found = shutil.which("ffprobe")
    if found:
        return found
    if sys.platform == "darwin":
        for bin_dir in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"):
            candidate = os.path.join(bin_dir, "ffprobe")
            if os.path.exists(candidate):
                return candidate
    return "ffprobe"


def _get_video_duration_seconds(path):
    """Read container duration with ffprobe; return 0 when unavailable."""
    try:
        result = subprocess.run(
            [
                _find_ffprobe(),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return float(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _within_wechat_duration_limit(path):
    """Reject only unreadable durations; WeChat no longer caps uploads at 10 minutes."""
    duration = _get_video_duration_seconds(path)
    if duration <= 0:
        print(f">>> ❌ 无法读取视频时长，已停止上传: {path}")
        return False
    return True


def _autosub_llm_default():
    """读 autosub settings.json，返回 (model, provider_str)。"""
    candidates = [
        os.environ.get("AUTOSUB_SETTINGS"),
        os.path.abspath(os.path.join(SKILL_DIR, "..", "settings.json")),
    ]
    for cand in filter(None, candidates):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                st = json.load(f)
            model = (st.get("llm_model") or "").strip()
            vendor = (st.get("llm_vendor") or "").strip()
            _VENDOR_TO_PROVIDER = {
                "Google (Vertex AI)": "vertex", "Google (Gemini API)": "gemini",
                "OpenAI": "openai", "Anthropic": "anthropic",
                "DeepSeek": "deepseek", "Alibaba (Bailian)": "dashscope",
                "DashScope (Alibaba Cloud)": "dashscope",
                "Zhipu AI": "zhipu", "Moonshot (Kimi)": "moonshot",
                "SiliconFlow": "siliconflow", "MiniMax": "minimax",
                "Google Vertex AI": "vertex", "Google Gemini": "gemini",
                "Silicon Flow": "siliconflow", "Zhipu (GLM)": "zhipu",
            }
            prov = _VENDOR_TO_PROVIDER.get(vendor)
            if model and prov:
                return model, prov
        except Exception:
            continue
    return "glm-5.2", "dashscope"


def _read_subtitle_text(video_path, max_chars=6000):
    """读取与视频同目录的字幕纯文本，优先双语/中文字幕。返回 (text, source)。"""
    base_name = os.path.splitext(video_path)[0]
    orig_base = base_name.replace("_hardsub", "")
    vdir = os.path.dirname(video_path) or "."
    candidates = []
    for bn in (base_name, orig_base):
        for ext in (".bi.srt", ".cn.srt", ".zh.srt", ".srt", ".txt"):
            p = bn + ext
            if os.path.exists(p) and p not in candidates:
                candidates.append(p)
    try:
        dir_files = os.listdir(vdir)
    except Exception:
        dir_files = []
    for fn in sorted(dir_files):
        if fn.endswith((".bi.srt", ".cn.srt", ".zh.srt", ".srt")):
            p = os.path.join(vdir, fn)
            if p not in candidates:
                candidates.append(p)

    for sub_path in candidates:
        try:
            with open(sub_path, "r", encoding="utf-8") as f:
                raw = f.read()
        except Exception:
            continue
        if not raw.strip():
            continue
        is_bilingual = sub_path.endswith(".bi.srt")
        lines = []
        text_lines_in_block = 0
        for ln in raw.splitlines():
            stripped = ln.strip()
            if re.match(r"^\d+$", stripped):
                text_lines_in_block = 0
                continue
            if re.match(r"^[\d:.,\-\s>]+$", stripped) and "-->" in stripped:
                continue
            if not stripped:
                text_lines_in_block = 0
                continue
            if is_bilingual:
                text_lines_in_block += 1
                if text_lines_in_block % 2 == 0:
                    continue
            lines.append(stripped)
        text = " ".join(lines).strip()
        if text:
            return text[:max_chars], sub_path
    return None, None


def _summarize_via_llm(subtitle_text, title, source_label, llm_provider="dashscope", llm_model="glm-5.2"):
    """用 LLM 根据字幕生成视频号描述。失败返回 None。"""
    if not _get_llm_client or not _LLMProvider:
        return None
    try:
        client = _get_llm_client()
        prompt = (
            "你是一位微信视频号视频简介撰写助手。根据下面给定的视频字幕内容，"
            "写一段简洁、有吸引力的中文视频描述（80-150 字），概括核心话题与亮点，"
            "语气自然平实，适合视频号传播，"
            "不要加标题、不要用 Markdown、不要加 emoji、不要署名。\n\n"
            f"视频标题：{title}\n\n"
            f"字幕内容（来自 {source_label}）：\n{subtitle_text}\n\n"
            "请直接输出描述正文："
        )
        try:
            prov = _LLMProvider(llm_provider)
        except Exception:
            prov = _LLMProvider.DASHSCOPE
        out = client.generate_content(prompt, provider=prov, model_name=llm_model, fallback=False)
        if out and out.strip():
            return out.strip()
    except Exception as e:
        print(f">>> LLM 生成描述失败: {e}")
    return None


def _generate_tags_via_llm(subtitle_text, title, source_label, llm_provider="dashscope", llm_model="glm-5.2"):
    """用 LLM 根据字幕生成标签。失败返回 None。"""
    if not _get_llm_client or not _LLMProvider:
        return None
    try:
        client = _get_llm_client()
        prompt = (
            "你是一位微信视频号标签专家。根据下面给定的视频字幕内容，"
            "生成 3-5 个适合微信视频号的中文或英文标签（关键词），用逗号分隔，只输出标签本身，不要编号、不要解释。\n\n"
            f"视频标题：{title}\n\n"
            f"字幕内容（来自 {source_label}）：\n{subtitle_text}\n\n"
            "请直接输出标签（逗号分隔）："
        )
        try:
            prov = _LLMProvider(llm_provider)
        except Exception:
            prov = _LLMProvider.DASHSCOPE
        out = client.generate_content(prompt, provider=prov, model_name=llm_model, fallback=False)
        if out and out.strip():
            tags = [t.strip() for t in re.split(r'[,，、\n]', out.strip()) if t.strip()]
            if tags:
                return tags[:10]
    except Exception as e:
        print(f">>> LLM 生成标签失败: {e}")
    return None


def _fetch_metadata_description(video_path):
    """从原视频元数据提取描述（yt-dlp 的 *.description 或 *.info.json）。"""
    base_name = os.path.splitext(video_path)[0]
    desc_path = base_name + ".description"
    if os.path.exists(desc_path):
        try:
            with open(desc_path, "r", encoding="utf-8") as f:
                d = f.read().strip()
            if d:
                return d
        except Exception:
            pass
    for p in [base_name + ".info.json"]:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                info = json.load(f)
            d = (info.get("description") or "").strip()
            if d:
                return d
        except Exception:
            continue
    return None


def _load_cached_upload_metadata(video_path):
    """Read metadata generated by the batch engine, if present."""
    metadata_path = os.path.join(
        os.path.dirname(video_path) or ".",
        "upload_metadata.json",
    )
    if not os.path.isfile(metadata_path):
        return {}
    try:
        with open(metadata_path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def build_description(video_path, title, description=None, llm_provider="dashscope", llm_model="glm-5.2"):
    """生成视频号描述。优先级：显式传入 > 原视频元数据 > 字幕 LLM 总结 > 标题。"""
    if description and description.strip():
        return description.strip()
    meta_desc = _fetch_metadata_description(video_path)
    if meta_desc:
        print(">>> 描述来源：原视频元数据")
        return meta_desc
    sub_text, sub_src = _read_subtitle_text(video_path)
    if sub_text:
        summary = _summarize_via_llm(sub_text, title, os.path.basename(sub_src), llm_provider, llm_model)
        if summary:
            print(f">>> 描述来源：根据字幕内容总结（{os.path.basename(sub_src)}）")
            return summary
    return f"{title}"


def _normalize_wechat_title(title):
    """Apply WeChat's short-title punctuation and length rules."""
    cleaned = str(title).replace(",", " ").replace("，", " ")
    cleaned = re.sub(
        r"[^0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff《》“”‘’：:+?%℃ ]",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return (cleaned or WECHAT_TITLE_FALLBACK)[:WECHAT_TITLE_MAX_CHARS]


def build_title(video_path, original_title, llm_provider="dashscope", llm_model="glm-5.2"):
    """Generate a WeChat title exclusively from subtitle content.

    The caller-provided title is logged only as context; it is never sent to
    the LLM or used as a fallback. This keeps GUI/Launcher integrations from
    accidentally reintroducing filename-derived titles.
    """
    if not _SUBTITLE_TITLE_AVAILABLE:
        raise SubtitleTitleError("subtitle_title module is unavailable")
    client = _get_llm_client() if _get_llm_client and _LLMProvider else None
    grounded = build_subtitle_grounded_title(
        video_path,
        llm_client=client,
        model_name=llm_model,
        max_title_chars=WECHAT_TITLE_MAX_CHARS,
        min_title_chars=8,
        platform_hint="微信短标题字段最多 16 个字符，且不能依赖后续截断。",
        title_style="plain",
    )
    title = _normalize_wechat_title(grounded.title)
    subtitle_text, _ = read_full_subtitle_text(video_path, grounded.subtitle_path)
    validate_title_from_subtitle(
        title,
        subtitle_text or "",
        grounded.evidence,
        min_chars=5,
        max_chars=WECHAT_TITLE_MAX_CHARS,
    )
    print(f">>> 标题来源：{grounded.source}（{os.path.basename(grounded.subtitle_path)}）")
    print(f"    原标题: {original_title}")
    print(f"    新标题: {title}")
    for fragment in grounded.evidence:
        print(f"    字幕证据: {fragment}")
    return title


def build_cover_title(
    video_path,
    llm_provider="dashscope",
    llm_model="glm-5.2",
    attribution=None,
):
    """Generate the longer editorial headline rendered on cover images."""
    if not _SUBTITLE_TITLE_AVAILABLE:
        raise SubtitleTitleError("subtitle_title module is unavailable")
    client = _get_llm_client() if _get_llm_client and _LLMProvider else None
    grounded = build_subtitle_grounded_title(
        video_path,
        llm_client=client,
        model_name=llm_model,
        max_title_chars=48,
        min_title_chars=12,
        platform_hint=(
            "微信封面标题可以比表单标题更长；优先使用字幕中已有的强钩子"
            "（热点词、身份标签、反差冲突、比喻或原话金句），没有强钩子再输出完整判断句。"
        ),
        title_style="cover_headline",
        attribution=attribution,
    )
    print(f">>> 封面标题来源：{grounded.source}（{os.path.basename(grounded.subtitle_path)}）")
    print(f"    封面标题: {grounded.title}")
    for fragment in grounded.evidence:
        print(f"    字幕证据: {fragment}")
    return grounded.title


# ============================================================================ #
#  Playwright 上传核心
# ============================================================================ #

async def _ensure_browser(playwright, headless=False):
    """启动持久化 browser context。返回 (browser_context, page)。"""
    os.makedirs(BROWSER_DATA_DIR, exist_ok=True)
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=BROWSER_DATA_DIR,
        headless=headless,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-proxy-server",
        ],
    )
    # 拦截可能的自动化检测
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    """)
    if not context.pages:
        page = await context.new_page()
    else:
        page = context.pages[0]
    return context, page


async def _wait_login(page, timeout=120):
    """Wait for QR login, then prove it by opening the create form."""
    print(">>> 请在浏览器中扫码登录微信视频号创作者平台...")
    print(f">>> 等待登录完成（最多 {timeout} 秒）...")
    start = time.time()
    while time.time() - start < timeout:
        url = page.url
        if "login" not in url.lower() and await _check_logged_in(page):
            await page.goto(WECHAT_CREATE_URL, wait_until="domcontentloaded", timeout=30000)
            if await _wait_for_upload_input(page, timeout=10):
                print(">>> 登录成功！")
                return True
            # A stale home page can still contain an avatar. Return to the
            # login route so the QR code remains visible for the user.
            await page.goto(WECHAT_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            continue
        try:
            if await page.query_selector('text="发布"'):
                await page.goto(WECHAT_CREATE_URL, wait_until="domcontentloaded", timeout=30000)
                if await _wait_for_upload_input(page, timeout=10):
                    print(">>> 登录成功！")
                    return True
                await page.goto(WECHAT_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(1)
                continue
        except Exception:
            pass
        await asyncio.sleep(2)
    print(">>> 登录超时")
    return False


async def _check_logged_in(page):
    """Quick UI check; callers still verify the create form when it matters."""
    url = page.url
    if "login" in url.lower():
        return False
    try:
        # 登录后通常有头像或发布按钮
        for sel in ['text="发布动态"', 'text="发表内容"', '[class*="avatar"]', '[class*="user-info"]']:
            if await page.query_selector(sel):
                return True
    except Exception:
        pass
    return False


async def _wait_for_upload_input(page, timeout=15):
    """Wait for the create page's file input, a practical logged-in signal."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            if "login" in page.url.lower():
                return False
            candidate = page.locator('input[type="file"]').first
            if await candidate.count() > 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def _ensure_logged_in(page, allow_qr=True, timeout=45):
    """Verify login by opening the create form, recovering only when needed."""
    await page.goto(WECHAT_CREATE_URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(3)
    if await _wait_for_upload_input(page, timeout=timeout):
        return True

    error_path = os.path.join(SKILL_DIR, "assets", "page_error.html")
    try:
        with open(error_path, "w", encoding="utf-8") as handle:
            handle.write(await page.content())
    except Exception:
        pass

    if not allow_qr:
        print(">>> 微信登录态不可用或发布页加载失败；无头上传已停止。")
        print(">>> 请先执行一次可见登录命令，扫码后再重试上传：")
        print(f">>> {sys.executable} -u {os.path.abspath(__file__)} --login")
        return False
    if "login" not in page.url.lower():
        print(">>> 发布页未出现上传入口，转入登录页复核登录态...")

    print(">>> 微信登录态已失效，请在浏览器中重新扫码...")
    await page.goto(WECHAT_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)
    if not await _wait_login(page, timeout=600):
        print(">>> 微信重新登录失败")
        return False
    return True


async def _do_login(headless=False):
    """首次登录流程：打开浏览器，等待扫码。"""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        context, page = await _ensure_browser(p, headless=False)  # 登录必须 headless=False
        try:
            print(f">>> 打开微信视频号创作者平台: {WECHAT_LOGIN_URL}")
            await page.goto(WECHAT_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            if await _ensure_logged_in(page):
                print(">>> 已经是登录状态，无需重新扫码")
                return True
            return False
        finally:
            await context.close()


# 多组容错选择器
UPLOAD_INPUT_SELECTORS = [
    'input[type="file"][accept*="video"]',
    'input[type="file"]',
    '[class*="upload"] input[type="file"]',
    '[class*="video-input"] input',
]
TITLE_SELECTORS = [
    '[placeholder*="标题"]',
    '[placeholder*="标题"] textarea',
    '[class*="title"] [contenteditable]',
    '[class*="title"] input',
    '[class*="title"] textarea',
    'textarea[placeholder*="标题"]',
]
DESC_SELECTORS = [
    '[placeholder*="描述"]',
    '[placeholder*="简介"]',
    '[placeholder*="说点什么"]',
    '[class*="desc"] [contenteditable]',
    '[class*="desc"] textarea',
    '[class*="description"] textarea',
    'textarea[placeholder*="描述"]',
    'div[contenteditable][data-placeholder*="描述"]',
    'div[contenteditable][data-placeholder*="说"]',
]
TAG_INPUT_SELECTORS = [
    '[placeholder*="标签"]',
    '[placeholder*="话题"]',
    '[class*="tag"] input',
    '[class*="tag"] [contenteditable]',
]
PUBLISH_SELECTORS = [
    'button:has-text("发布")',
    'button:has-text("发表")',
    'button:has-text("发布动态")',
    '[class*="publish"] button',
    'button[class*="primary"]:has-text("发")',
]
SAVE_DRAFT_SELECTORS = [
    'button:has-text("存草稿")',
    'button:has-text("保存草稿")',
    '[class*="draft"] button',
]
UPLOAD_PROGRESS_SELECTORS = [
    '[class*="progress"]',
    '[class*="upload-status"]',
    '[class*="uploading"]',
]
UPLOAD_DONE_INDICATORS = [
    'text="上传完成"',
    '[class*="upload-success"]',
    '[class*="upload-complete"]',
]

# 进度百分比连续这么久不变就判定上传卡死。正常上传每几秒就会跳一个百分点，
# 而卡死时页面仍在轮询自己的接口，看起来"还活着"，只有进度不再前进。
UPLOAD_STALL_SECONDS = 900


async def _try_selectors(page, selectors, description="element"):
    """依次尝试多个选择器，返回第一个找到的 element 或 None。"""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                return el
        except Exception:
            continue
    print(f">>> 未找到 {description}（尝试了 {len(selectors)} 个选择器）")
    return None


async def _set_text(page, selectors, text, description="字段"):
    """在 contenteditable 或 textarea/input 中设置文本。"""
    el = await _try_selectors(page, selectors, description)
    if not el:
        return False
    try:
        await el.fill("")
        await el.fill(text)
        print(f">>> 已填写{description}")
        return True
    except Exception as e:
        print(f">>> 填写{description}失败: {e}")
        return False


async def _add_tags(page, tags):
    """逐个添加标签。"""
    if not tags:
        return
    tag_input = await _try_selectors(page, TAG_INPUT_SELECTORS, "标签输入框")
    if not tag_input:
        print(">>> 未找到标签输入框，跳过标签")
        return
    for tag in tags:
        try:
            await tag_input.click()
            await asyncio.sleep(0.2)
            await page.keyboard.type(tag, delay=10)
            await asyncio.sleep(0.3)
            # 尝试回车确认
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f">>> 添加标签 '{tag}' 失败: {e}")
    print(f">>> 已添加 {len(tags)} 个标签")


async def _wait_upload_complete(page, timeout=10000):
    """等待视频上传完成。"""
    print(f">>> 等待视频上传完成（最多 {timeout} 秒）...")
    start = time.time()
    last_pct = -1
    last_progress_at = start
    while time.time() - start < timeout:
        # 检测上传完成指示器
        for sel in UPLOAD_DONE_INDICATORS:
            try:
                if await page.locator(sel).count() > 0:
                    print(f"\n>>> 上传完成！")
                    return True
            except Exception:
                continue
        # 检测进度条
        for sel in UPLOAD_PROGRESS_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    text = await el.text_content()
                    if text:
                        # 提取百分比
                        m = re.search(r'(\d+)%', text)
                        if m:
                            pct = int(m.group(1))
                            if pct != last_pct:
                                print(f"\r>>> 上传进度: {pct}%", end="", flush=True)
                                last_pct = pct
                                last_progress_at = time.time()
                            if pct >= 100:
                                print()
                                return True
            except Exception:
                continue
        # 快速上传可能在第一次轮询前完成，进度条已经消失。
        # 此时用“保存草稿/发表按钮恢复可用”兜底，但至少等待 45 秒。
        if time.time() - last_progress_at >= 45:
            try:
                for text in ("保存草稿", "发表"):
                    btn = page.locator(f'button:has-text("{text}")').first
                    if await btn.count() == 0:
                        continue
                    disabled = await btn.get_attribute("disabled")
                    aria_disabled = await btn.get_attribute("aria-disabled")
                    class_name = await btn.get_attribute("class") or ""
                    if not disabled and aria_disabled != "true" and "disabled" not in class_name:
                        print(f"\n>>> {text}按钮已可点击，且进度条已结束，上传应已完成")
                        return True
            except Exception:
                pass
        if last_pct >= 0 and time.time() - last_progress_at >= UPLOAD_STALL_SECONDS:
            print(
                f"\n>>> 上传进度在 {UPLOAD_STALL_SECONDS} 秒内停在 {last_pct}%，判定为卡死，放弃等待"
            )
            return False
        try:
            error = page.locator('text="上传失败"').first
            if await error.count() > 0 and await error.is_visible():
                print("\n>>> 页面显示上传失败")
                return False
        except Exception:
            pass
        await asyncio.sleep(2)
    print("\n>>> 上传等待超时")
    return False

async def _wujie_text(page):
    """读取 Wujie 微前端在 shadow DOM 中的可见文本。"""
    try:
        return await page.evaluate(
            "() => document.querySelector('wujie-app')?.shadowRoot?.querySelector('html')?.innerText || ''"
        )
    except Exception:
        return ""


def _normalize_draft_text(text):
    """Normalize user-visible draft text for exact, whitespace-insensitive matching."""
    return re.sub(r"\s+", "", str(text or ""))


def _draft_signature(entry):
    """Build a conservative identity for a draft visible on the first page."""
    return (
        _normalize_draft_text(entry.get("title")),
        _normalize_draft_text(entry.get("time")),
    )


def _new_draft_entries(entries, expected_desc, known_drafts=None):
    """Return only exact-description drafts that were not present before upload."""
    expected = _normalize_draft_text(expected_desc)
    known = {tuple(item) for item in (known_drafts or set())}
    return [
        entry
        for entry in entries or []
        if _normalize_draft_text(entry.get("title")) == expected
        and _draft_signature(entry) not in known
    ]


async def _click_visible_draft_link(page):
    links = page.locator("a", has_text="草稿箱")
    count = await links.count()
    for index in range(count):
        link = links.nth(index)
        try:
            if await link.is_visible():
                box = await link.bounding_box()
                if box and box["width"] > 0 and box["height"] > 0:
                    await link.click()
                    return True
        except Exception:
            continue
    return False


async def _click_menu_draft_link(page):
    """Open the draft list from the menu, including the folded sidebar."""
    try:
        folded = await page.evaluate(
            "() => Boolean(document.querySelector('.app-body.fold-sidebar'))"
        )
        if folded:
            await page.evaluate(
                "() => document.querySelector('.finder-ui-desktop-menu__header')?.click()"
            )
            await asyncio.sleep(1)

        content_opened = await page.evaluate(
            """() => {
                const link = Array.from(
                    document.querySelectorAll('.finder-ui-desktop-menu__link')
                ).find((item) => (item.innerText || item.textContent || '').includes('内容管理'));
                if (!link) return false;
                link.click();
                return true;
            }"""
        )
        if not content_opened:
            return False
        await asyncio.sleep(1)

        return await page.evaluate(
            """() => {
                const link = Array.from(
                    document.querySelectorAll('.finder-ui-desktop-menu__link')
                ).find((item) => (item.innerText || item.textContent || '').includes('草稿箱'));
                if (!link) return false;
                link.click();
                return true;
            }"""
        )
    except Exception:
        return False


async def _wait_draft_list_ready(page, timeout=20):
    start = time.time()
    while time.time() - start < timeout:
        if "/post/draftListManager" in page.url:
            list_start = time.time()
            while time.time() - list_start < 10:
                entries = await _draft_list_entries(page)
                if entries:
                    return True
                text = await _wujie_text(page)
                if "暂无" in text and "草稿" in text:
                    return True
                normalized_text = _normalize_draft_text(text)
                if re.search(r"草稿箱[（(]0[）)]", normalized_text):
                    return True
                await asyncio.sleep(1)
            print(f">>> 草稿箱列表未加载完成: {page.url}")
            return False
        entries = await _draft_list_entries(page)
        if entries:
            return True
        await asyncio.sleep(1)
    print(f">>> 草稿箱页面未就绪: {page.url}")
    return False


async def _open_draft_list(page):
    """Open the draft list through the menu, with a direct-route fallback."""
    try:
        await page.goto(WECHAT_HOME_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_selector(
                ".finder-ui-desktop-menu__link",
                state="attached",
                timeout=20000,
            )
        except Exception:
            pass
        opened = await _click_menu_draft_link(page)
        if not opened:
            opened = await _click_visible_draft_link(page)

        if opened and await _wait_draft_list_ready(page):
            return True

        # The collapsed icon menu can hide both link labels. Use the known
        # route as a last resort, then still require real draft entries.
        print(">>> 可见菜单未打开草稿箱，尝试直达草稿箱路由...")
        await page.goto(WECHAT_DRAFT_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        return await _wait_draft_list_ready(page)
    except Exception as e:
        print(f">>> 打开草稿箱失败: {e}")
        return False


async def _draft_list_entries(page):
    """Read the currently visible draft entries from the Wujie shadow DOM."""
    try:
        return await page.evaluate(
            """() => {
                const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                return Array.from(root.querySelectorAll('.post-draft-item')).map((item, index) => ({
                    index,
                    title: item.querySelector('.post-title')?.innerText || item.innerText,
                    time: item.querySelector('.post-time')?.innerText || '',
                    text: item.innerText,
                }));
            }"""
        )
    except Exception:
        return []


async def _open_draft_entry(page, index):
    """Open one draft by its current list index and wait for the edit form."""
    try:
        clicked = await page.evaluate(
            """(index) => {
                const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                const item = Array.from(root.querySelectorAll('.post-draft-item'))[index];
                if (!item) return false;
                const button = item.querySelector('.opr-item');
                if (!button) return false;
                button.click();
                return true;
            }""",
            index,
        )
        if not clicked:
            print(f">>> 草稿条目 {index} 缺少编辑入口")
            return False
        for _ in range(30):
            if await page.locator(".vertical-cover-wrap").count() > 0:
                return True
            if await page.locator(".horizon-cover-wrap").count() > 0:
                return True
            if await page.locator('input[type="file"]').count() > 0:
                return True
            await asyncio.sleep(1)
        print(">>> 草稿编辑页未打开")
        return False
    except Exception as e:
        print(f">>> 打开草稿编辑页失败: {e}")
        return False


async def _snapshot_drafts(page):
    """Capture visible draft signatures before creating a new upload."""
    if not await _open_draft_list(page):
        return None
    entries = await _draft_list_entries(page)
    return {_draft_signature(entry) for entry in entries}


async def _verify_draft_saved(
    page,
    title,
    desc=None,
    expected_covers=None,
    known_drafts=None,
    timeout=40,
    strict_covers=True,
):
    """Confirm a saved draft without treating a private draft's preview as fatal."""
    print(">>> 正在到草稿箱核验保存结果...")
    if not await _open_draft_list(page):
        print(">>> 无法打开草稿箱")
        return False
    expected_desc = _normalize_draft_text(desc or title)
    start = time.time()
    while time.time() - start < timeout:
        entries = await _draft_list_entries(page)
        candidates = _new_draft_entries(entries, expected_desc, known_drafts)
        if candidates:
            if len(candidates) != 1:
                print(f">>> 草稿箱匹配到 {len(candidates)} 条本次新增草稿，拒绝猜测")
                return False
            print(">>> 草稿箱已确认保存本次新增的视频")
            if not await _open_draft_entry(page, candidates[0]["index"]):
                print(">>> 保存后无法重新打开这条草稿")
                return False
            if expected_covers and strict_covers:
                if not await _wait_cover_editor_ready(page):
                    print(">>> 保存后的草稿封面编辑器未就绪")
                    return False
            if not await _verify_draft_form_fields(page, title, desc):
                return False
            if expected_covers:
                covers_ok = await _verify_wechat_cover_previews(
                    page, expected_covers
                )
                if not covers_ok:
                    if strict_covers:
                        return False
                    print(">>> 私发草稿已保存；封面回读未确认，请在发布前人工检查")
            return True
        await asyncio.sleep(2)
    print(f">>> 草稿箱未找到本次新增且描述精确匹配的草稿: {expected_desc[:80]}")
    return False


async def _read_form_text(page, selectors):
    """Read the first populated form field matching the selectors."""
    for selector in selectors:
        try:
            field = page.locator(selector).first
            if await field.count() == 0:
                continue
            value = await field.evaluate(
                "element => element.value ?? element.innerText ?? element.textContent ?? ''"
            )
            value = str(value or "").strip()
            if value:
                return value
        except Exception:
            continue
    return ""


async def _verify_draft_form_fields(page, title, desc):
    """Confirm the reopened draft still has the requested form title and description."""
    actual_title = await _read_form_text(page, TITLE_SELECTORS)
    actual_desc = await _read_form_text(page, DESC_SELECTORS)
    title_ok = _normalize_draft_text(actual_title) == _normalize_draft_text(title)
    desc_ok = not desc or _normalize_draft_text(actual_desc) == _normalize_draft_text(desc)
    if not title_ok:
        print(f">>> 草稿表单标题不匹配: 期望={title!r} 实际={actual_title!r}")
    if not desc_ok:
        print(f">>> 草稿表单描述不匹配: 期望={desc!r} 实际={actual_desc!r}")
    if title_ok and desc_ok:
        print(">>> 草稿表单标题与描述均已回读确认")
    return title_ok and desc_ok


async def _click_final_action(page, is_public):
    """Click only the action requested by the caller; never guess publish."""
    if is_public:
        selectors, action = PUBLISH_SELECTORS, "发布"
    else:
        selectors, action = SAVE_DRAFT_SELECTORS, "存草稿"
    btn = await _try_selectors(page, selectors, f"{action}按钮")
    if not btn:
        raise RuntimeError(f"找不到{action}按钮")
    print(f">>> 点击{action}按钮...")
    await btn.click()
    return action


async def _wait_publish_resources_ready(page, timeout=600):
    """Wait until generated covers/cards finish and the form has no title error."""
    print(f">>> 等待封面与分享卡片生成完成（最多 {timeout} 秒）...")
    start = time.time()
    last_generating = -1
    while time.time() - start < timeout:
        text = await _wujie_text(page)
        generating = text.count("生成中")
        if generating != last_generating:
            print(f">>> 待生成资源: {generating}")
            last_generating = generating
        if "标题超过16字限制" in text or "标题不能超过" in text:
            raise RuntimeError("微信短标题超过 16 字限制")
        if "标题包含特殊字符" in text:
            raise RuntimeError("微信短标题包含平台不支持的标点")
        if generating == 0:
            print(">>> 封面与分享卡片生成完成")
            return True
        await asyncio.sleep(2)
    print(">>> 封面或分享卡片生成超时")
    return False


async def _save_draft_after_resource_timeout(page, title, desc, known_drafts):
    """Preserve an already-uploaded video when generated resources are slow."""
    print(">>> 平台资源生成超时，先保存草稿保留已上传视频...")
    action = await _click_final_action(page, False)
    await asyncio.sleep(5)
    print(f">>> 超时恢复点击后页面: {page.url}")
    if not await _verify_draft_saved(
        page,
        title,
        desc,
        expected_covers=None,
        known_drafts=known_drafts,
    ):
        raise RuntimeError("平台资源生成超时后保存草稿失败")
    print(f">>> {action}已作为超时恢复保存，继续补齐封面")
    return True


async def _wait_cover_editor_ready(page, timeout=180):
    """Wait for WeChat's generated preview before opening a cover editor."""
    start = time.time()
    last_generating = -1
    last_processing = None
    ready_since = None
    while time.time() - start < timeout:
        text = await _wujie_text(page)
        cover_text = await _cover_editor_text(page)
        combined_text = f"{text}\n{cover_text}"
        generating = combined_text.count("生成中")
        processing = "视频处理中" in combined_text
        if generating != last_generating:
            print(f">>> 封面编辑器待生成资源: {generating}")
            last_generating = generating
        if processing != last_processing:
            if processing:
                print(">>> 封面编辑器仍在处理视频，等待可编辑状态...")
            last_processing = processing
        is_ready = (
            generating == 0
            and "预览图生成中" not in combined_text
            and "视频处理中" not in combined_text
        )
        if is_ready:
            ready_since = ready_since or time.time()
            # WeChat can briefly remove the badge before the popover is usable.
            # Require a stable ready state so an early text snapshot cannot pass.
            if time.time() - ready_since >= 3:
                print(">>> 封面编辑器已可编辑")
                return True
        else:
            ready_since = None
        await asyncio.sleep(1)
    print(">>> 封面编辑器资源生成超时")
    return False


async def _cover_editor_text(page):
    """Read cover-card text, including WeChat's hidden readiness tooltip."""
    try:
        return await page.evaluate(
            """() => {
                const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                return Array.from(
                    root.querySelectorAll('.vertical-cover-wrap, .horizon-cover-wrap')
                ).map((element) => element.textContent || '').join('\\n');
            }"""
        )
    except Exception:
        return ""


async def _click_visible_button(page, label, timeout=5000):
    buttons = page.locator("button", has_text=label)
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        count = await buttons.count()
        for index in range(count):
            button = buttons.nth(index)
            try:
                if await button.is_visible():
                    disabled = await button.get_attribute("disabled")
                    aria_disabled = await button.get_attribute("aria-disabled")
                    class_name = await button.get_attribute("class") or ""
                    if disabled or aria_disabled == "true" or "disabled" in class_name:
                        continue
                    await button.click()
                    return True
            except Exception:
                continue
        await asyncio.sleep(0.2)
    return False


async def _click_visible_text(page, label, timeout=5000):
    """Click a visible text control, including WeChat's non-button actions."""
    candidates = page.get_by_text(label, exact=True)
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        count = await candidates.count()
        for index in range(count):
            candidate = candidates.nth(index)
            try:
                if await candidate.is_visible():
                    await candidate.click()
                    return True
            except Exception:
                continue
        await asyncio.sleep(0.2)
    return False


async def _cover_wrap_images(page, wrap_selector):
    """Read the cover-card preview images inside the Wujie shadow DOM."""
    return await page.evaluate(
        """(selector) => {
            const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
            return Array.from(root.querySelectorAll(selector + ' img')).map((img) => ({
                src: img.src,
                naturalWidth: img.naturalWidth,
                naturalHeight: img.naturalHeight,
            }));
        }""",
        wrap_selector,
    )


def _cover_preview_matches_orientation(images, orientation):
    """Check whether a loaded preview has the expected card aspect ratio."""
    expected_ratio = 3 / 4 if orientation == "portrait" else 4 / 3
    for image in images or []:
        width = int(image.get("naturalWidth") or 0)
        height = int(image.get("naturalHeight") or 0)
        if width > 0 and height > 0 and abs(width / height - expected_ratio) <= 0.03:
            return True
    return False


def _matching_cover_preview(images, orientation):
    """Return the first loaded preview matching the requested card ratio."""
    expected_ratio = 3 / 4 if orientation == "portrait" else 4 / 3
    for image in images or []:
        width = int(image.get("naturalWidth") or 0)
        height = int(image.get("naturalHeight") or 0)
        if width and height and abs(width / height - expected_ratio) <= 0.03:
            return image
    return None


async def _current_cover_preview(page, wrap_selector, orientation):
    images = await _cover_wrap_images(page, wrap_selector)
    return _matching_cover_preview(images, orientation)


def _validate_cover_file(cover_path, orientation, expected_title):
    """Reject stale, wrong-shape, or wrong-title files before WeChat upload."""
    expected_size = (1080, 1440) if orientation == "portrait" else (1080, 810)
    try:
        with Image.open(cover_path) as image:
            if image.size != expected_size:
                raise ValueError(f"尺寸应为 {expected_size[0]}x{expected_size[1]}")
        if not cover_style_matches(cover_path, expected_title, orientation):
            raise ValueError("封面缓存标记与当前封面标题不一致")
    except Exception as e:
        print(f">>> 封面文件校验失败: {cover_path} ({e})")
        return False
    return True


async def _wait_cover_preview_changed(page, wrap_selector, orientation, before_sources, timeout=20):
    """Wait until the outer form card reflects the uploaded cover."""
    start = time.time()
    last_state = None
    while time.time() - start < timeout:
        images = await _cover_wrap_images(page, wrap_selector)
        state = tuple(
            (item.get("src"), item.get("naturalWidth"), item.get("naturalHeight"))
            for item in images
        )
        if state != last_state:
            print(f">>> 封面卡片预览状态: {state}")
            last_state = state
        loaded = [
            item for item in images
            if item.get("naturalWidth") and item.get("naturalHeight")
        ]
        changed = any(item.get("src") not in before_sources for item in loaded)
        if changed and _cover_preview_matches_orientation(loaded, orientation):
            print(">>> 封面卡片预览已切换为上传封面")
            return True
        await asyncio.sleep(0.5)
    print(">>> 封面卡片预览未切换为上传封面")
    return False


async def _upload_wechat_cover_card(page, cover_path, orientation, expected_title):
    """Upload one exact-ratio cover card in WeChat's cover editor."""
    wrap_selector = ".vertical-cover-wrap" if orientation == "portrait" else ".horizon-cover-wrap"
    edit_selector = f"{wrap_selector} .edit-btn"
    try:
        if not _validate_cover_file(cover_path, orientation, expected_title):
            return False
        wrap = page.locator(wrap_selector).first
        edit_btn = page.locator(edit_selector).first
        try:
            await wrap.wait_for(state="attached", timeout=20000)
            await edit_btn.wait_for(state="attached", timeout=20000)
        except Exception:
            print(f">>> 未找到{'个人主页' if orientation == 'portrait' else '分享'}封面编辑入口")
            return False
        if not await _wait_cover_editor_ready(page):
            return False
        before_images = await _cover_wrap_images(page, wrap_selector)
        before_sources = {item.get("src") for item in before_images}

        upload_tabs = page.get_by_text("上传封面", exact=True)
        direct_edit_tabs = page.get_by_text("直接编辑", exact=True)
        upload_tab = None
        for click_mode in ("normal", "dom"):
            try:
                if click_mode == "normal":
                    await wrap.hover()
                    await edit_btn.click(timeout=3000)
                else:
                    await edit_btn.evaluate("element => element.click()")
            except Exception as click_error:
                print(f">>> {click_mode} 点击封面编辑按钮失败: {click_error}")

            upload_deadline = time.time() + 8
            chose_direct_edit = False
            while time.time() < upload_deadline:
                if not chose_direct_edit:
                    direct_count = await direct_edit_tabs.count()
                    for index in range(direct_count):
                        direct_candidate = direct_edit_tabs.nth(index)
                        try:
                            if await direct_candidate.is_visible():
                                await direct_candidate.click()
                                chose_direct_edit = True
                                print(">>> 已选择直接编辑封面")
                                break
                        except Exception:
                            continue
                count = await upload_tabs.count()
                for index in range(count):
                    candidate = upload_tabs.nth(index)
                    try:
                        if await candidate.is_visible():
                            upload_tab = candidate
                            break
                    except Exception:
                        continue
                if upload_tab is not None:
                    break
                await asyncio.sleep(0.2)
            if upload_tab is not None:
                break
            print(f">>> {click_mode} 点击封面编辑按钮后未出现上传封面入口")
        if upload_tab is None:
            print(">>> 封面编辑弹层未显示可点击的上传封面入口")
            debug_path = os.path.join(SKILL_DIR, "assets", "cover_editor_failure.png")
            try:
                await page.screenshot(path=debug_path, full_page=True)
                print(f">>> 已保存封面编辑失败截图: {debug_path}")
            except Exception as screenshot_error:
                print(f">>> 保存封面编辑失败截图失败: {screenshot_error}")
            page_text = await _wujie_text(page)
            print(f">>> 封面编辑失败页面文本片段: {page_text[:1200]}")
            return False
        await upload_tab.click()

        image_input = page.locator(".single-cover-uploader-wrap input[type='file']").first
        if await image_input.count() == 0:
            image_input = page.locator('input[type="file"][accept*="image"]').first
        await image_input.wait_for(state="attached", timeout=10000)
        await image_input.set_input_files(cover_path)

        # WeChat renders a local preview before enabling the crop confirmation.
        dialog_ready = False
        start = time.time()
        while time.time() - start < 15:
            dialog_images = await page.evaluate(
                """() => {
                    const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                    return Array.from(root.querySelectorAll('.preview-image')).map((img) => ({
                        naturalWidth: img.naturalWidth,
                        naturalHeight: img.naturalHeight,
                    }));
                }"""
            )
            if _cover_preview_matches_orientation(dialog_images, orientation):
                dialog_ready = True
                break
            await asyncio.sleep(0.5)
        if not dialog_ready:
            print(">>> 封面编辑弹层未显示上传图片预览")
            return False

        confirmed = await _click_visible_button(page, "确认", timeout=8000)
        if not confirmed:
            confirmed = await _click_visible_button(page, "确定", timeout=5000)
        if not confirmed:
            print(">>> 未找到封面编辑弹层的确认按钮")
            return False

        if not await _wait_cover_preview_changed(
            page, wrap_selector, orientation, before_sources, timeout=20
        ):
            # Some flows show a second crop confirmation after the first save.
            if await _click_visible_button(page, "确认", timeout=2000):
                if not await _wait_cover_preview_changed(
                    page, wrap_selector, orientation, before_sources, timeout=15
                ):
                    return False
            else:
                return False
        preview = await _current_cover_preview(page, wrap_selector, orientation)
        if not preview:
            print(f">>> {'个人主页' if orientation == 'portrait' else '分享'}封面预览缺失")
            return False
        print(
            f">>> 已上传{'个人主页' if orientation == 'portrait' else '分享'}封面: "
            f"{preview.get('naturalWidth')}x{preview.get('naturalHeight')}"
        )
        # Let the "cover updated" toast clear before another cover editor opens.
        await asyncio.sleep(2)
        preview = dict(preview or {})
        preview["local_path"] = os.path.abspath(cover_path)
        return preview
    except Exception as e:
        print(f">>> 上传{'个人主页' if orientation == 'portrait' else '分享'}封面失败: {e}")
        return False


async def _set_wechat_cover(
    page, portrait_cover=None, share_cover=None, expected_title=None, finish=False
):
    """Set generated covers on the create form or an existing cover-edit page."""
    if not portrait_cover and not share_cover:
        return False
    previews = {}
    if portrait_cover:
        preview = await _upload_wechat_cover_card(
            page, portrait_cover, "portrait", expected_title
        )
        if not preview:
            return False
        previews["portrait"] = preview
    if share_cover:
        preview = await _upload_wechat_cover_card(
            page, share_cover, "share", expected_title
        )
        if not preview:
            return False
        previews["share"] = preview
    if finish and previews:
        if not await _click_visible_button(page, "完成", timeout=10000):
            print(">>> 未找到封面编辑页完成按钮")
            return False
        await asyncio.sleep(3)
    return previews


def _decode_data_url(data_url):
    try:
        metadata, encoded = str(data_url).split(",", 1)
        if "base64" not in metadata:
            return None
        return base64.b64decode(encoded, validate=False)
    except Exception:
        return None


async def _preview_image_bytes(page, src):
    """Read a preview image without relying on a stable CDN URL."""
    if not src:
        return None
    if str(src).startswith("data:"):
        return _decode_data_url(src)
    if str(src).startswith("blob:"):
        try:
            data_url = await page.evaluate(
                """async (src) => {
                    const blob = await fetch(src).blob();
                    return await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.onerror = reject;
                        reader.readAsDataURL(blob);
                    });
                }""",
                src,
            )
            return _decode_data_url(data_url)
        except Exception as e:
            print(f">>> 读取封面预览 blob 失败: {e}")
            return None
    try:
        response = await page.request.get(src)
        if response.ok:
            return await response.body()
        print(f">>> 读取封面预览失败: HTTP {response.status}")
    except Exception as e:
        print(f">>> 读取封面预览失败: {e}")
    return None


def _cover_preview_similarity(local_path, preview_bytes):
    """Return a normalized RMS pixel difference; lower means more similar."""
    try:
        with Image.open(local_path) as local_image, Image.open(io.BytesIO(preview_bytes)) as preview_image:
            local = ImageOps.fit(local_image.convert("RGB"), (128, 128))
            preview = ImageOps.fit(preview_image.convert("RGB"), (128, 128))
            difference = ImageChops.difference(local, preview)
            histogram = difference.histogram()
            total = 0
            sum_squares = 0
            for channel in range(3):
                channel_histogram = histogram[channel * 256:(channel + 1) * 256]
                total += sum(channel_histogram)
                sum_squares += sum(
                    value * value * count
                    for value, count in enumerate(channel_histogram)
                )
            return (sum_squares / total) ** 0.5
    except Exception as e:
        print(f">>> 比较封面预览失败: {e}")
        return None


async def _verify_cover_preview_matches(page, expected, orientation):
    if not expected or not expected.get("local_path"):
        return False
    wrap_selector = ".vertical-cover-wrap" if orientation == "portrait" else ".horizon-cover-wrap"
    current = await _current_cover_preview(page, wrap_selector, orientation)
    if not current:
        print(f">>> 回读时{'个人主页' if orientation == 'portrait' else '分享'}封面预览缺失")
        return False
    if current.get("src") and current.get("src") == expected.get("src"):
        print(f">>> 回读{'个人主页' if orientation == 'portrait' else '分享'}封面 URL 一致")
        return True

    preview_bytes = await _preview_image_bytes(page, current.get("src"))
    if not preview_bytes:
        return False
    score = _cover_preview_similarity(expected.get("local_path"), preview_bytes)
    if score is None:
        return False
    print(
        f">>> 回读{'个人主页' if orientation == 'portrait' else '分享'}封面相似度: "
        f"{score:.2f} (阈值 20.00)"
    )
    return score <= 20.0


async def _verify_wechat_cover_previews(page, expected_previews):
    """Confirm both saved cover cards still contain the uploaded images."""
    if not expected_previews:
        return False
    for orientation, label in (("portrait", "个人主页"), ("share", "分享")):
        expected = expected_previews.get(orientation)
        if not expected:
            print(f">>> 缺少{label}封面回读基准")
            return False
        if not await _verify_cover_preview_matches(page, expected, orientation):
            print(f">>> {label}封面回读校验失败")
            return False
    print(">>> 横竖双封面均已回读确认")
    return True


async def _open_draft_by_text(page, text):
    """Reopen the one saved draft whose full description matches exactly."""
    if not text:
        return False
    if not await _open_draft_list(page):
        return False
    needle = _normalize_draft_text(text)
    for _ in range(30):
        entries = await _draft_list_entries(page)
        candidates = [
            entry
            for entry in entries
            if _normalize_draft_text(entry.get("title")) == needle
        ]
        if len(candidates) > 1:
            print(f">>> 草稿箱匹配到 {len(candidates)} 条同描述草稿，拒绝猜测")
            return False
        if len(candidates) == 1:
            return await _open_draft_entry(page, candidates[0]["index"])
        await asyncio.sleep(1)
    print(">>> 草稿箱未找到描述精确匹配的草稿")
    return False


async def _set_wechat_covers_same_page(
    page, portrait_cover, share_cover, expected_title, max_attempts=2
):
    """Set both cover cards on the create form before saving one draft."""
    previews = {}
    for orientation, cover_path in (
        ("share", share_cover),
        ("portrait", portrait_cover),
    ):
        preview = None
        for attempt in range(1, max_attempts + 1):
            preview = await _upload_wechat_cover_card(
                page, cover_path, orientation, expected_title
            )
            if preview:
                previews[orientation] = preview
                break
            if attempt < max_attempts:
                print(
                    f">>> {'分享' if orientation == 'share' else '个人主页'}"
                    f"封面设置失败，准备重试 ({attempt}/{max_attempts})"
                )
        if not preview:
            print(
                f">>> {'分享' if orientation == 'share' else '个人主页'}"
                "封面未能确认设置"
            )
    return previews or False


async def _set_wechat_covers_for_upload(
    page, portrait_cover, share_cover, expected_title, require_both=True
):
    """Set covers; private drafts may retain an uploaded video when covers lag."""
    if not portrait_cover or not share_cover:
        raise RuntimeError("微信上传需要横竖两张封面，封面生成结果不完整")
    previews = await _set_wechat_covers_same_page(
        page, portrait_cover, share_cover, expected_title
    )
    incomplete = not previews or not previews.get("portrait") or not previews.get("share")
    if incomplete:
        if require_both:
            raise RuntimeError("微信横竖封面未完全设置成功，已停止保存草稿")
        print(">>> 私发草稿将保存已上传视频；封面未完全确认，请在发布前人工检查")
    return previews or {}


def _generate_wechat_covers(video_path, title, cover_title=None):
    """Generate 3:4 and 4:3 covers, reusing outputs when possible.

    ``cover_title`` is an explicit override for the text rendered on the cover
    images only. It intentionally does not change the WeChat form title.
    """
    if not _COVER_UTILS_AVAILABLE:
        print(">>> ⚠️ 封面生成模块不可用，本次不自动设置微信封面")
        return None, None
    effective_cover_title = str(cover_title or "").strip() or title
    try:
        portrait_path, share_path = generate_platform_covers(
            video_path,
            "wechat",
            effective_cover_title,
        )
        return portrait_path, share_path
    except Exception as e:
        print(f">>> ⚠️ 微信封面生成失败: {e}")
        return None, None


async def _wait_published(page, timeout=30):
    """等待发布完成（检测成功提示或页面跳转）。"""
    print(">>> 等待发布完成...")
    start = time.time()
    while time.time() - start < timeout:
        for indicator in ['text="发布成功"', 'text="已发布"', '[class*="success"]', '[class*="publish-success"]']:
            try:
                if await page.locator(indicator).count() > 0:
                    print(">>> 发布成功！")
                    return True
            except Exception:
                continue
        # 检测页面跳转
        if "/platform/post/manage" in page.url or "/platform/home" in page.url:
            print(">>> 页面已跳转，发布可能成功")
            return True
        await asyncio.sleep(1)
    print(">>> 未检测到明确的成功提示（可能已发布，建议人工确认）")
    return True  # 宽松返回，让批量流程继续


async def upload_one_video(video_path, title, desc=None, tags=None, is_public=False,
                           llm_provider="dashscope", llm_model="glm-5.2", headless=False,
                           max_retries=2, auto_title=True, cover_title=None, attribution=None):
    """上传单个视频到微信视频号。返回 True/False。"""
    if not os.path.exists(video_path):
        print(f">>> 视频文件不存在: {video_path}")
        return False
    if not _within_wechat_duration_limit(video_path):
        return False

    cached_metadata = _load_cached_upload_metadata(video_path)
    if desc is None:
        cached_description = str(cached_metadata.get("description") or "").strip()
        if cached_description:
            desc = cached_description
            print(">>> 描述来源：upload_metadata.json")
    cached_wechat_title = str(cached_metadata.get("wechat_title") or "").strip()
    if auto_title and cached_wechat_title:
        title = _normalize_wechat_title(cached_wechat_title)
        auto_title = False
        print(">>> 标题来源：upload_metadata.json")
    if not cover_title:
        cover_title = str(cached_metadata.get("cover_title") or "").strip()
        if cover_title:
            print(">>> 封面标题来源：upload_metadata.json")

    original_title = str(title or "").strip()
    if auto_title:
        try:
            title = build_title(
                video_path,
                original_title,
                llm_provider=llm_provider,
                llm_model=llm_model,
            )
        except SubtitleTitleError as e:
            print(f">>> ❌ 自动标题失败：{e}")
            print(">>> 已停止上传。请先生成/翻译字幕，或显式使用 --no_auto_title。")
            return False
    else:
        title = _normalize_wechat_title(original_title)
        if original_title != title:
            print(f">>> 标题已按微信短标题规则规范化: {title}")

    # 生成元数据
    desc = build_description(video_path, title, desc, llm_provider, llm_model)
    if not tags:
        sub_text, sub_src = _read_subtitle_text(video_path)
        if sub_text:
            gen_tags = _generate_tags_via_llm(sub_text, title, os.path.basename(sub_src), llm_provider, llm_model)
            if gen_tags:
                tags = gen_tags
                print(f">>> 标签来源：根据字幕生成（{os.path.basename(sub_src)}）")
    if not tags:
        tags = ["人工智能", "AI", "科技"]
        print(">>> 标签来源：默认标签")
    cover_title = str(cover_title or "").strip()
    if not cover_title:
        try:
            cover_title = build_cover_title(
                video_path,
                llm_provider=llm_provider,
                llm_model=llm_model,
                attribution=attribution,
            )
        except SubtitleTitleError as e:
            print(f">>> ❌ 自动封面标题失败：{e}")
            print(">>> 已停止上传。封面标题不能来自表单标题或文件名。")
            return False
    cover_headline = cover_title
    portrait_cover, share_cover = _generate_wechat_covers(
        video_path, title, cover_title=cover_title
    )
    if not portrait_cover or not share_cover:
        print(">>> ❌ 微信横竖封面生成失败，已停止上传，避免保存无标题封面")
        return False

    print(f"\n{'=' * 60}")
    print(f"📹 视频号上传: {title}")
    print(f"   文件: {video_path}")
    print(f"   描述: {desc[:60]}...")
    print(f"   标签: {', '.join(tags)}")
    print(f"   封面标题: {cover_headline}")
    print(f"   可见性: {'公开' if is_public else '仅自己可见'}")
    print(f"{'=' * 60}")

    from playwright.async_api import async_playwright
    last_error = None
    file_selected = False
    for attempt in range(1, max_retries + 1):
        try:
            async with async_playwright() as p:
                context, page = await _ensure_browser(p, headless=headless)
                try:
                    await page.goto(
                        WECHAT_HOME_URL,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await asyncio.sleep(2)
                    if not await _ensure_logged_in(page, allow_qr=not headless):
                        return False
                    await page.goto(
                        WECHAT_HOME_URL,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await asyncio.sleep(2)
                    known_drafts = await _snapshot_drafts(page)
                    if known_drafts is None:
                        print(">>> 草稿箱读取失败，先复查微信登录态...")
                        if not await _ensure_logged_in(page):
                            return False
                        await page.goto(
                            WECHAT_HOME_URL,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        await asyncio.sleep(2)
                        known_drafts = await _snapshot_drafts(page)
                    if known_drafts is None:
                        raise RuntimeError("上传前无法读取草稿箱，已停止以避免修改旧草稿")

                    def _log_wechat_response(response):
                        # Normal page polling returns many 200/201 responses.
                        # Keep the upload log readable and report failures only.
                        if response.status < 400:
                            return
                        if "channels.weixin.qq.com" not in response.url.lower():
                            return
                        print(
                            f">>> ⚠️ 微信接口异常: "
                            f"{response.status} {response.url[:220]}"
                        )
                    page.on("response", _log_wechat_response)

                    # 1. 直接打开发布页。首页按钮在 Wujie 微前端中加载时机不稳定，
                    # 直达 /post/create 后顶层文档即可看到 file input。
                    print(f">>> 打开发布页面 (尝试 {attempt}/{max_retries})...")
                    await page.goto(WECHAT_CREATE_URL, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)

                    # 登录态失效时会跳回登录页；已登录时发布页会加载 file input。
                    print(">>> 等待页面加载...")
                    file_input = None
                    for _ in range(20):
                        if "login" in page.url.lower():
                            break
                        try:
                            candidate = page.locator('input[type="file"]').first
                            if await candidate.count() > 0:
                                file_input = candidate
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(1)

                    if file_input is None and "login" in page.url.lower():
                        if headless:
                            print(">>> 需要扫码登录；无头模式不会弹出二维码窗口。")
                            print(f">>> 请先运行: {sys.executable} -u {os.path.abspath(__file__)} --login")
                            return False
                        print(">>> 需要扫码登录，请在弹出的浏览器中扫码...")
                        ok = await _wait_login(page, timeout=600)
                        if not ok:
                            return False
                        await asyncio.sleep(3)
                        await page.goto(WECHAT_CREATE_URL, wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(5)
                        for _ in range(20):
                            candidate = page.locator('input[type="file"]').first
                            if await candidate.count() > 0:
                                file_input = candidate
                                break
                            await asyncio.sleep(1)

                    if file_input is None:
                        error_path = os.path.join(SKILL_DIR, "assets", "page_error.html")
                        with open(error_path, "w", encoding="utf-8") as f:
                            f.write(await page.content())
                        raise RuntimeError("找不到视频上传入口，微信页面结构可能已更新")

                    # 2. 上传视频文件
                    print(f">>> 选择视频文件: {os.path.basename(video_path)}")
                    await file_input.set_input_files(video_path)
                    file_selected = True
                    print(">>> 视频文件已选择，等待上传...")

                    # 3. 等待上传完成
                    await asyncio.sleep(3)  # 等待上传开始
                    ok = await _wait_upload_complete(page, timeout=10000)
                    if not ok:
                        raise RuntimeError("视频上传超时或失败")

                    # 4. 填写标题
                    await asyncio.sleep(2)
                    await _set_text(page, TITLE_SELECTORS, title, "标题")

                    # 5. 填写描述
                    await _set_text(page, DESC_SELECTORS, desc, "描述")

                    # 6. 添加标签
                    await _add_tags(page, tags)

                    # 7. 设置可见性（公开 vs 仅自己可见）
                    resources_ready = await _wait_publish_resources_ready(page)
                    if not resources_ready:
                        if is_public:
                            raise RuntimeError("封面或分享卡片生成超时")
                        await _save_draft_after_resource_timeout(
                            page, title, desc, known_drafts
                        )
                        if not await _wait_cover_editor_ready(page, timeout=1800):
                            raise RuntimeError("超时恢复后的封面编辑器未就绪")
                    cover_previews = await _set_wechat_covers_for_upload(
                        page,
                        portrait_cover,
                        share_cover,
                        cover_headline,
                        require_both=is_public,
                    )

                    if is_public:
                        # 尝试找到"公开"选项
                        try:
                            public_opt = await _try_selectors(page, [
                                'text="公开"',
                                '[class*="public"]',
                                'label:has-text("公开")',
                            ], "公开选项")
                            if public_opt:
                                await public_opt.click()
                                print(">>> 已设置为公开")
                        except Exception:
                            print(">>> 未找到公开选项（默认可能已是公开）")

                    # 8. 发布或存草稿
                    await asyncio.sleep(1)
                    action = await _click_final_action(page, is_public)
                    await asyncio.sleep(5)
                    print(f">>> 点击后页面: {page.url}")
                    after_text = await _wujie_text(page)
                    print(f">>> 点击后页面文本片段: {after_text[:800]}")
                    debug_path = os.path.join(SKILL_DIR, "assets", "debug_after_save.png")
                    try:
                        await page.screenshot(path=debug_path, full_page=True)
                        print(f">>> 已保存调试截图: {debug_path}")
                    except Exception as e:
                        print(f">>> 保存调试截图失败: {e}")

                    # 9. 等待发布/保存结果。草稿必须回读草稿箱确认。
                    if is_public:
                        await _wait_published(page, timeout=30)
                    else:
                        ok = await _verify_draft_saved(
                            page,
                            title,
                            desc,
                            expected_covers=cover_previews,
                            known_drafts=known_drafts,
                            strict_covers=is_public,
                        )
                        if not ok:
                            raise RuntimeError("草稿箱核验失败，未确认保存草稿")

                    # 10. 标记完成
                    marker = os.path.join(os.path.dirname(video_path), ".wechat_uploaded")
                    with open(marker, "w") as f:
                        f.write("1")

                    print(f">>> {action}完成！")
                    return True

                finally:
                    await context.close()

        except Exception as e:
            last_error = e
            print(f">>> 尝试 {attempt} 失败: {e}")
            if file_selected:
                print(">>> 文件已提交给微信，为避免重复上传，本次不再自动重试")
                return False
            if attempt < max_retries:
                print(f">>> 等待 5 秒后重试...")
                await asyncio.sleep(5)

    print(f">>> 上传最终失败: {last_error}")
    return False


# ============================================================================ #
#  批量上传
# ============================================================================ #

def derive_title(video_path, project_title=None):
    """从视频路径推导标题。"""
    parent = os.path.basename(os.path.dirname(video_path))
    title = re.sub(r'\s*\[[a-zA-Z0-9_-]+\]\s*$', '', parent)
    title = re.sub(r'[_\s]+', ' ', title).strip()
    if not title:
        title = os.path.splitext(os.path.basename(video_path))[0].replace('_', ' ')
    if project_title:
        title = f"{project_title} - {title}"
    return title[:60]  # 视频号标题限制较短


def scan_videos(paths, pattern="*hardsub*.mp4"):
    """扫描路径下的视频文件。"""
    results = []
    for root_path in paths:
        if os.path.isfile(root_path) and root_path.lower().endswith((".mp4", ".mkv", ".webm")):
            results.append(os.path.abspath(root_path))
            continue
        if not os.path.isdir(root_path):
            print(f">>> 跳过不存在的路径: {root_path}")
            continue
        matched = glob.glob(os.path.join(root_path, "**", pattern), recursive=True)
        if not matched:
            matched = glob.glob(os.path.join(root_path, "**", "*.mp4"), recursive=True)
        results.extend(sorted(set(os.path.abspath(m) for m in matched)))
    return results


def load_failed_queue():
    if os.path.exists(QUEUE_PATH):
        try:
            with open(QUEUE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_failed_queue(queue):
    os.makedirs(os.path.dirname(QUEUE_PATH), exist_ok=True)
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)


async def run_batch(videos, args):
    total = len(videos)
    failed = []
    succeeded = []

    print(f"\n{'=' * 60}")
    print(f"📹 视频号批量上传: 共 {total} 个视频")
    print(f"{'=' * 60}\n")

    for i, vpath in enumerate(videos, 1):
        # 跳过已上传的
        marker = os.path.join(os.path.dirname(vpath), ".wechat_uploaded")
        if os.path.exists(marker):
            print(f"\n[{i}/{total}] 跳过已上传: {derive_title(vpath)}")
            succeeded.append(vpath)
            continue

        title = derive_title(vpath, args.project_title) if args.no_auto_title else "字幕自动标题"
        print(f"\n[{i}/{total}] 上传: {title}")

        try:
            ok = await upload_one_video(
                vpath, title,
                desc=args.desc,
                tags=[t.strip() for t in re.split(r'[,，、]', args.tags) if t.strip()] if args.tags else None,
                is_public=args.public,
                llm_provider=args.llm_provider,
                llm_model=args.llm_model,
                headless=args.headless,
                auto_title=not args.no_auto_title,
                attribution=args.attribution,
            )
            if ok:
                succeeded.append(vpath)
            else:
                failed.append({"path": vpath, "title": title, "error": "upload returned False"})
        except Exception as e:
            failed.append({"path": vpath, "title": title, "error": str(e)})

        if i < total:
            await asyncio.sleep(args.interval)

    if failed:
        save_failed_queue(failed)
        print(f"\n>>> {len(failed)} 个失败，已记录到 {QUEUE_PATH}")
        print(f">>> 补传: python wechat_upload.py --retry-failed")

    print(f"\n>>> 汇总: 成功 {len(succeeded)} / 失败 {len(failed)} / 总计 {total}")
    return len(failed) == 0


# ============================================================================ #
#  CLI
# ============================================================================ #

def main():
    parser = argparse.ArgumentParser(description="微信视频号自动上传工具")
    parser.add_argument("path", nargs="?", help="视频文件路径")
    parser.add_argument("title", nargs="?", help="视频标题；默认从字幕自动提炼")
    parser.add_argument("--desc", type=str, default=None, help="视频描述（留空自动生成）")
    parser.add_argument("--tags", type=str, default=None, help="标签（逗号分隔）")
    parser.add_argument(
        "--cover-title",
        default=None,
        help="单视频封面画面标题（留空则单独从字幕提炼；不影响表单标题）",
    )
    parser.add_argument(
        "--attribution",
        default=None,
        help="可选封面署名前缀，例如“Palantir CEO卡普”；不参与字幕事实校验",
    )
    parser.add_argument("--public", action="store_true", help="公开发布（默认存草稿/仅自己可见）")
    parser.add_argument("--login", action="store_true", help="首次扫码登录")
    parser.add_argument("--batch", type=str, default=None, help="批量上传目录")
    parser.add_argument("--pattern", default="*hardsub*.mp4", help="批量上传文件匹配模式")
    parser.add_argument("--project-title", default=None, help="项目标题前缀")
    parser.add_argument("--retry-failed", action="store_true", help="补传失败队列")
    parser.add_argument("--headless", action="store_true", help="无头模式（不显示浏览器窗口）")
    parser.add_argument("--no_auto_title", action="store_true", help="禁用字幕自动标题，使用传入/文件名标题")
    parser.add_argument("--interval", type=float, default=10.0, help="批量上传间隔秒数")
    _def_model, _def_prov = _autosub_llm_default()
    parser.add_argument("--llm-model", default=_def_model, help="LLM 模型")
    parser.add_argument("--llm-provider", default=_def_prov, help="LLM 通道")
    args = parser.parse_args()

    if args.login:
        ok = asyncio.run(_do_login())
        sys.exit(0 if ok else 1)

    if args.retry_failed:
        failed = load_failed_queue()
        if not failed:
            print("没有需要补传的失败记录。")
            return
        videos = [item["path"] for item in failed if os.path.exists(item.get("path", ""))]
        if not videos:
            print("失败队列中的文件已不存在。")
            return
        print(f">>> 补传模式: {len(videos)} 个视频")
        ok = asyncio.run(run_batch(videos, args))
        sys.exit(0 if ok else 1)

    if args.batch:
        if args.cover_title:
            parser.error("--cover-title only supports single-video uploads")
        videos = scan_videos([args.batch], args.pattern)
        if not videos:
            print("未找到视频文件。")
            return
        ok = asyncio.run(run_batch(videos, args))
        sys.exit(0 if ok else 1)

    if not args.path:
        parser.error("请提供视频文件路径，或使用 --login / --batch")
    if not args.title and args.no_auto_title:
        parser.error("使用 --no_auto_title 时必须提供视频标题")

    tags = [t.strip() for t in re.split(r'[,，、]', args.tags) if t.strip()] if args.tags else None
    ok = asyncio.run(upload_one_video(
        args.path, args.title or "",
        desc=args.desc, tags=tags,
        is_public=args.public,
        llm_provider=args.llm_provider, llm_model=args.llm_model,
        headless=args.headless,
        auto_title=not args.no_auto_title,
        cover_title=args.cover_title,
        attribution=args.attribution,
    ))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
