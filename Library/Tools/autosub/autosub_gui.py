
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess
import threading
import sys
import os
import json
import re
import psutil
from datetime import datetime

class ProcessTree:
    """Manages cross-platform process suspension and termination."""
    @staticmethod
    def kill(pid):
        if not pid: return
        try:
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True): child.kill()
            parent.kill()
        except: pass

    @staticmethod
    def suspend(pid):
        if not pid: return False
        try:
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True): child.suspend()
            parent.suspend()
            return True
        except Exception:
            return False

    @staticmethod
    def resume(pid):
        if not pid: return False
        try:
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True): child.resume()
            parent.resume()
            return True
        except Exception:
            return False

# --- Paths ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.join(os.path.dirname(CURRENT_DIR), "common")
if COMMON_DIR not in sys.path:
    sys.path.append(COMMON_DIR)

AUTOSUB_SCRIPT = os.path.join(CURRENT_DIR, "autosub.py")
SETTINGS_FILE = os.path.join(CURRENT_DIR, "settings.json")
DEFAULTS_FILE = os.path.join(CURRENT_DIR, "defaults.json")
DEFAULTS = {}
if os.path.exists(DEFAULTS_FILE):
    try:
        with open(DEFAULTS_FILE, "r", encoding="utf-8") as _defaults_file:
            DEFAULTS.update(json.load(_defaults_file))
    except Exception:
        pass

# --- Base Output Directory (mirrors autosub.py logic) ---
_tools_dir = os.path.dirname(CURRENT_DIR)
PROJECT_ROOT = os.path.dirname(os.path.dirname(_tools_dir))
BASE_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "Projects")
# Video extensions that hardsub files can have (matches download_video in autosub.py)
VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.mov', '.ts')


def _read_history_source(workdir):
    """Read the original URL/path recorded at the start of workflow.log."""
    log_path = os.path.join(workdir, "workflow.log")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(8192)
        match = re.search(r"^--- 任务启动: (.+?) ---$", head, re.MULTILINE)
        return match.group(1).strip() if match else ""
    except OSError:
        return ""


def _history_video_name(workdir):
    """Pick the source video name, preferring the original over hardsub."""
    original = ""
    hardsub = ""
    try:
        names = os.listdir(workdir)
    except OSError:
        return ""
    for name in names:
        root, ext = os.path.splitext(name)
        if ext.lower() not in VIDEO_EXTS:
            continue
        if "hardsub" in root.lower():
            if not hardsub:
                hardsub = root
        elif not original:
            original = root
    return original or hardsub or os.path.basename(workdir)


def _history_marker(workdir, marker):
    return os.path.exists(os.path.join(workdir, marker))


def _history_has_subtitle(workdir):
    try:
        names = os.listdir(workdir)
    except OSError:
        return False
    return any(name.lower().endswith((".srt", ".ass")) for name in names)


def _history_has_hardsub(workdir):
    try:
        names = os.listdir(workdir)
    except OSError:
        return False
    return any(
        "hardsub" in os.path.splitext(name)[0].lower()
        and os.path.splitext(name)[1].lower() in VIDEO_EXTS
        for name in names
    )


def _history_timestamp(workdir):
    paths = [os.path.join(workdir, name) for name in ("workflow.log",)]
    try:
        names = os.listdir(workdir)
    except OSError:
        names = []
    paths.extend(
        os.path.join(workdir, name)
        for name in names
        if name in {
            ".burn_complete", ".synced", ".bili_uploaded", ".wechat_uploaded"
        } or os.path.splitext(name)[1].lower() in VIDEO_EXTS
    )
    latest = 0.0
    for path in paths:
        try:
            latest = max(latest, os.path.getmtime(path))
        except OSError:
            continue
    if not latest:
        return ""
    return datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M")


def scan_task_history(base_dir, limit=300):
    """Build a compact processing history from project folders."""
    base_dir = os.path.abspath(str(base_dir or ""))
    candidates = []
    if _history_has_hardsub(base_dir):
        candidates.append(base_dir)
    try:
        candidates.extend(
            os.path.join(base_dir, name)
            for name in os.listdir(base_dir)
            if os.path.isdir(os.path.join(base_dir, name))
        )
    except OSError:
        return []

    records = []
    for workdir in candidates:
        if not os.path.exists(os.path.join(workdir, "workflow.log")):
            if not _history_has_hardsub(workdir):
                continue
        completed = []
        missing = []
        if _history_has_subtitle(workdir):
            completed.append("字幕")
        else:
            missing.append("字幕")
        if _history_has_hardsub(workdir):
            completed.append("压制")
        else:
            missing.append("压制")
        for label, marker in (
            ("GDrive", ".synced"),
            ("B站", ".bili_uploaded"),
            ("微信", ".wechat_uploaded"),
        ):
            if _history_marker(workdir, marker):
                completed.append(label)
            else:
                missing.append(label)
        if os.path.isdir(os.path.join(workdir, "noads")):
            completed.append("去广告")

        if not os.path.exists(os.path.join(workdir, "workflow.log")):
            status = "有产物，无处理日志"
        elif missing[:2] == ["字幕", "压制"]:
            status = "未生成字幕和压制视频"
        elif "字幕" in missing:
            status = "字幕未完成"
        elif "压制" in missing:
            status = "字幕完成，压制未完成"
        elif missing:
            status = "本地完成，上传未完成"
        else:
            status = "全部完成"

        records.append(
            {
                "workdir": os.path.abspath(workdir),
                "name": _history_video_name(workdir),
                "source": _read_history_source(workdir),
                "updated_at": _history_timestamp(workdir),
                "status": status,
                "completed": completed,
                "missing": missing,
            }
        )
    records.sort(key=lambda item: item["updated_at"], reverse=True)
    return records[:limit]


def find_history_record_for_input(records, input_value):
    """Match a saved input to its project record."""
    value = str(input_value or "").strip()
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return next((item for item in records if item["source"] == value), None)

    input_path = os.path.abspath(value)
    for item in records:
        source_path = os.path.abspath(item["source"]) if item["source"] else ""
        workdir = item["workdir"]
        if input_path == source_path or input_path == workdir:
            return item
        if input_path.startswith(workdir + os.sep):
            return item
    return None


def is_project_complete(record, requirements):
    """Check core outputs plus only the uploads enabled for this run."""
    if not record:
        return False
    completed = set(record["completed"])
    if not {"字幕", "压制"}.issubset(completed):
        return False
    return all(stage in completed for stage in requirements)


def select_startup_input(records, saved_input, requirements):
    """Keep a completed task out of the input box, but retain unfinished work."""
    saved_input = str(saved_input or "").strip()
    if saved_input:
        record = find_history_record_for_input(records, saved_input)
        if record and is_project_complete(record, requirements):
            return ""
        return saved_input

    unfinished = next(
        (item for item in records if not is_project_complete(item, requirements)),
        None,
    )
    if unfinished:
        return unfinished["source"] or unfinished["workdir"]
    return ""

class AutoSubGUI:
    input_var: tk.StringVar
    output_dir_var: tk.StringVar
    layout_var: tk.StringVar
    main_lang_var: tk.StringVar
    cn_font_var: tk.StringVar
    jp_font_var: tk.StringVar
    en_font_var: tk.StringVar
    cn_size_var: tk.StringVar
    jp_size_var: tk.StringVar
    en_size_var: tk.StringVar
    cn_color_var: tk.StringVar
    en_color_var: tk.StringVar
    bili_enabled_var: tk.BooleanVar
    bili_tid_var: tk.StringVar
    bili_tags_var: tk.StringVar
    bili_season_var: tk.StringVar
    wechat_enabled_var: tk.BooleanVar
    wechat_public_var: tk.BooleanVar

    def __init__(self, root):
        self.root = root
        self.root.title("AutoSub Pro - 自动视频字幕生成工具")
        self.root.geometry("1020x500")

        # Set Window Icon
        icon_name = "autosub_gui_neon.png" if sys.platform == "darwin" else "autosub.ico"
        icon_path = os.path.join(CURRENT_DIR, icon_name)
        if os.path.exists(icon_path):
            try:
                if sys.platform == "darwin":
                    self.icon_img = tk.PhotoImage(file=icon_path)
                    self.root.tk.call('wm', 'iconphoto', self.root._w, '-default', self.icon_img)
                else:
                    self.root.iconbitmap(icon_path)
            except: pass

        self.ui_font = ("PingFang SC", 12) if sys.platform == "darwin" else ("Microsoft YaHei", 10)
        self.log_font = ("Menlo", 11) if sys.platform == "darwin" else ("Consolas", 10)

        # Vendor Map to LLMProvider Enum values
        self.vendor_map = {
            "Google Vertex AI": "vertex",
            "Google Gemini": "gemini",
            "OpenAI": "openai",
            "DeepSeek": "deepseek",
            "Silicon Flow": "siliconflow",
            "Zhipu (GLM)": "zhipu",
            "Moonshot (Kimi)": "moonshot",
            "Alibaba (Bailian)": "dashscope",
            "MiniMax": "minimax"
        }

        # Vendor Env Keys Mapping
        self.vendor_env_keys = {
            "Google Vertex AI": "VERTEX_SA_KEY_PATH",
            "Google Gemini": "GEMINI_API_KEY",
            "OpenAI": "OPENAI_API_KEY",
            "DeepSeek": "DEEPSEEK_API_KEY",
            "Silicon Flow": "SILICONFLOW_API_KEY",
            "Zhipu (GLM)": "ZHIPUAI_API_KEY",
            "Moonshot (Kimi)": "MOONSHOT_API_KEY",
            "Alibaba (Bailian)": "DASHSCOPE_API_KEY",
            "MiniMax": "MINIMAX_API_KEY"
        }

        self.settings = self.load_settings()
        self.vendor_default_models = self.settings.get("vendor_default_models", {})
        self.key_editor_vendor = self.settings.get(
            "llm_vendor", self.settings.get("vendor", "Google Vertex AI")
        )
        self.key_editor_env_key = self.vendor_env_keys.get(self.key_editor_vendor, "VERTEX_SA_KEY_PATH")
        self.current_processes = []
        self._process_lock = threading.Lock()
        self._upload_abort = threading.Event()
        self.last_was_progress = False
        self.is_paused = False
        self.task_history = []

        self.setup_ui()
        self.restore_pending_input()

        if sys.platform == "darwin":
            self.root.update_idletasks()
            self.root.lift()
            self.root.focus_force()

        # Initial async load
        self.root.after(1000, lambda: self.on_vendor_change(None))

    def load_settings(self):
        # Default fallback values
        defaults = {
            "input": "", "cookies": "chrome", "output_dir": "",
            "llm_vendor": "Alibaba (Bailian)",
            "llm_model": DEFAULTS.get("llm_model", "glm-5.2"),
            "api_key": "", "model": "large-v2", "style": "auto",
            "trans_mode": "balanced", "quick": False, "remove_ads": False,
            "ad_detector": "auto", "ad_cut_mode": "auto", "quality": "high",
            "layout": "bilingual", "main_lang": "cn",
            "cn_font": "STKaiti", "en_font": "Calibri",
            "jp_font": "Hiragino Maru Gothic ProN", "jp_size": "36",
            "cn_size": "60", "en_size": "36",
            "cn_color": "Gold", "en_color": "White",
            "bg_box": True,
            "sync_gdrive": "", "gdrive_enabled": False
        }

        # Try loading from existing settings
        for fpath in [DEFAULTS_FILE, SETTINGS_FILE]:
            if os.path.exists(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8") as fs:
                        data = json.load(fs)
                        # [COMPATIBILITY] Handle key mapping differences
                        if "remote_id" in data: data["sync_gdrive"] = data["remote_id"]
                        if "gdrive_sync" in data: data["gdrive_enabled"] = data["gdrive_sync"]
                        # Skip empty cookies settings to preserve the 'chrome' default
                        if "cookies" in data and not data["cookies"]:
                            del data["cookies"]
                        defaults.update(data)
                except: pass
        return defaults

    def setup_ui(self):
        self.main_container = ttk.Frame(self.root, padding=10)
        self.main_container.pack(fill="both", expand=True)

        # Create Notebook (Tabbed Interface) for compact and adaptive layout
        self.notebook = ttk.Notebook(self.main_container)
        self.notebook.pack(fill="both", expand=True, pady=5)

        # Create Tab Frames
        self.tab1 = ttk.Frame(self.notebook, padding=10)
        self.tab2 = ttk.Frame(self.notebook, padding=10)
        self.tab3 = ttk.Frame(self.notebook, padding=10)
        self.tab4 = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(self.tab1, text=" 任务配置 ")
        self.notebook.add(self.tab2, text=" 字幕样式 ")
        self.notebook.add(self.tab3, text=" 执行日志 ")
        self.notebook.add(self.tab4, text=" 处理历史 ")

        # 1. Input Section (Parent: tab1)
        f1 = ttk.LabelFrame(self.tab1, text=" 输入源 ", padding=10)
        f1.pack(fill="x", pady=5)
        self.create_entry_row(f1, "视频/URL:", "input_var", 0, self.browse_video)
        
        # Cookies Row (Dropdown + Entry)
        ttk.Label(f1, text="Cookies:").grid(row=1, column=0, sticky="w", pady=2)
        self.cookies_var = tk.StringVar(value=str(self.settings.get("cookies", "")))
        self.cookies_combo = ttk.Combobox(f1, textvariable=self.cookies_var, width=63)
        self.cookies_combo['values'] = ("None", "chrome", "safari", "firefox", "edge")
        self.cookies_combo.grid(row=1, column=1, padx=5)
        ttk.Button(f1, text="选择...", command=self.browse_cookies).grid(row=1, column=2)

        self.create_entry_row(f1, "项目根目录:", "output_dir_var", 2, self.browse_folder)

        # 2. Basic Settings (Parent: tab1)
        f2 = ttk.LabelFrame(self.tab1, text=" 基础设置 ", padding=10)
        f2.pack(fill="x", pady=5)

        ttk.Label(f2, text="模型厂商:").grid(row=0, column=0, sticky="w")
        current_vendor = str(
            self.settings.get("llm_vendor", self.settings.get("vendor", ""))
        )
        vendors = list(self.vendor_map.keys())
        if current_vendor not in vendors:
            current_vendor = "Alibaba (Bailian)"
            
        self.vendor_var = tk.StringVar(value=current_vendor)
        self.vendor_combo = ttk.Combobox(f2, textvariable=self.vendor_var, values=vendors, state="readonly", width=18)
        self.vendor_combo.grid(row=0, column=1, sticky="w", padx=5)
        self.vendor_combo.bind("<<ComboboxSelected>>", self.on_vendor_change)

        ttk.Label(f2, text="选择模型:").grid(row=0, column=2, sticky="w", padx=10)
        self.llm_model_var = tk.StringVar(value=str(self.settings.get("llm_model", "")))
        self.llm_combo = ttk.Combobox(f2, textvariable=self.llm_model_var, width=30)
        self.llm_combo.grid(row=0, column=3, sticky="w")
        self.llm_combo.bind("<<ComboboxSelected>>", self.on_model_select)
        ttk.Button(f2, text="设为默认", width=8, command=self.set_llm_model_default).grid(row=0, column=4, padx=5)

        self.api_key_label = ttk.Label(f2, text="API Key:")
        self.api_key_label.grid(row=1, column=0, sticky="w", pady=5)
        self.api_key_var = tk.StringVar()
        self.api_key_entry = ttk.Entry(f2, textvariable=self.api_key_var, show="*", width=35)
        self.api_key_entry.grid(row=1, column=1, columnspan=2, sticky="w", padx=5)
        self.api_key_entry.bind("<FocusOut>", lambda e: self.on_vendor_change(None))
        ttk.Button(f2, text="保存该密钥", width=10, command=self.save_api_key).grid(row=1, column=3, sticky="w")
        ttk.Button(f2, text="测试连接", width=10, command=self.test_connection).grid(row=1, column=4, padx=5)

        ttk.Label(f2, text="转录模型:").grid(row=2, column=0, sticky="w")
        self.whisper_var = tk.StringVar(value=str(self.settings.get("model", "")))
        ttk.Combobox(f2, textvariable=self.whisper_var, values=["large-v3", "large-v2", "medium", "small"], width=18).grid(row=2, column=1, sticky="w", padx=5)

        ttk.Label(f2, text="内容语气:").grid(row=2, column=2, sticky="w", padx=10)
        self.style_var = tk.StringVar(value=str(self.settings.get("style", "auto")))
        ttk.Combobox(f2, textvariable=self.style_var, values=["auto", "casual", "formal", "edgy"], width=22).grid(row=2, column=3, sticky="w")

        ttk.Label(f2, text="翻译策略:").grid(row=3, column=0, sticky="w", pady=5)
        self.trans_mode_var = tk.StringVar(value=str(self.settings.get("trans_mode", "balanced")))
        ttk.Combobox(f2, textvariable=self.trans_mode_var, values=["balanced", "paraphrase"], width=18).grid(row=3, column=1, sticky="w", padx=5)

        ttk.Label(f2, text="压制画质:").grid(row=3, column=2, sticky="w", padx=10)
        self.quality_var = tk.StringVar(value=str(self.settings.get("quality", "high")))
        ttk.Combobox(f2, textvariable=self.quality_var, values=["standard", "high", "lossless"], width=22).grid(row=3, column=3, sticky="w")

        self.quick_var = tk.BooleanVar(value=bool(self.settings.get("quick", False)))
        ttk.Checkbutton(f2, text="快捷转录（大模型/网页视频实验性）", variable=self.quick_var).grid(row=4, column=0, columnspan=2, sticky="w", pady=5)
        self.remove_ads_var = tk.BooleanVar(value=bool(self.settings.get("remove_ads", False)))
        ttk.Checkbutton(f2, text="剪除广告片段", variable=self.remove_ads_var).grid(row=4, column=2, columnspan=2, sticky="w", pady=5)

        ttk.Label(f2, text="广告检测:").grid(row=5, column=0, sticky="w", pady=5)
        self.ad_detector_var = tk.StringVar(value=str(self.settings.get("ad_detector", "auto")))
        ttk.Combobox(
            f2, textvariable=self.ad_detector_var,
            values=["auto", "visual", "llm"], state="readonly", width=18,
        ).grid(row=5, column=1, sticky="w", padx=5)
        ttk.Label(f2, text="剪切方式:").grid(row=5, column=2, sticky="w", padx=10)
        self.ad_cut_mode_var = tk.StringVar(value=str(self.settings.get("ad_cut_mode", "auto")))
        ttk.Combobox(
            f2, textvariable=self.ad_cut_mode_var,
            values=["auto", "precise", "legacy"], state="readonly", width=18,
        ).grid(row=5, column=3, sticky="w")

        self.multilang_var = tk.BooleanVar(value=bool(self.settings.get("multilang", False)))
        ttk.Checkbutton(f2, text="多语言检测（混合语言视频自动分段检测）", variable=self.multilang_var).grid(row=6, column=0, columnspan=4, sticky="w", pady=5)

        # 3. Subtitle Style (Parent: tab2)
        f3 = ttk.LabelFrame(self.tab2, text=" 字幕样式 ", padding=10)
        f3.pack(fill="x", pady=5)
        self.create_style_row(f3, "布局模式:", "layout_var", ["bilingual", "cn", "en"], "首选语言:", "main_lang_var", ["cn", "en"], 0)
        self.create_style_row(f3, "中文样式:", "cn_font_var", ["STKaiti", "PingFang SC"], "字号:", "cn_size_var", None, 1)
        self.create_style_row(f3, "英文样式:", "en_font_var", ["Arial", "Calibri"], "字号:", "en_size_var", None, 2)
        self.create_style_row(f3, "日文样式:", "jp_font_var", ["Hiragino Maru Gothic ProN", "YuMincho", "STKaiti"], "字号:", "jp_size_var", None, 3)
        self.create_style_row(f3, "中文颜色:", "cn_color_var", ["Gold", "White", "Yellow"], "原语言颜色:", "en_color_var", ["White", "Gold", "Gray"], 4)

        self.bg_box_var = tk.BooleanVar(value=bool(self.settings.get("bg_box", True)))
        ttk.Checkbutton(f3, text="启用背景框", variable=self.bg_box_var).grid(row=5, column=0, columnspan=2, sticky="w", pady=5)

        # 4. Cloud Sync (Parent: tab2)
        f4 = ttk.LabelFrame(self.tab2, text=" 云端同步 ", padding=10)
        f4.pack(fill="x", pady=5)
        self.gdrive_enabled_var = tk.BooleanVar(value=bool(self.settings.get("gdrive_enabled", False)))
        ttk.Checkbutton(f4, text="自动同步到 Google Drive", variable=self.gdrive_enabled_var).grid(row=0, column=0, sticky="w")
        ttk.Label(f4, text="文件夹 ID:").grid(row=0, column=1, padx=10)
        self.sync_gdrive_var = tk.StringVar(value=str(self.settings.get("sync_gdrive", "")))
        ttk.Entry(f4, textvariable=self.sync_gdrive_var, width=35).grid(row=0, column=2, padx=5)

        # 4.5 Bilibili Sync (Parent: tab2)
        f4_5 = ttk.LabelFrame(self.tab2, text=" Bilibili 同步 ", padding=10)
        f4_5.pack(fill="x", pady=5)
        self.bili_enabled_var = tk.BooleanVar(value=bool(self.settings.get("bili_enabled", False)))
        ttk.Checkbutton(f4_5, text="默认上传到 Bilibili", variable=self.bili_enabled_var).grid(row=0, column=0, sticky="w", pady=2)
        
        ttk.Label(f4_5, text="分区:").grid(row=0, column=1, padx=10, pady=2, sticky="e")
        self.bili_tid_var = tk.StringVar(value=str(self.settings.get("bili_tid", "智能识别 (Auto)")))
        bili_partitions = [
            "智能识别 (Auto)",
            "科技-计算机技术 (231)",
            "科技-软件应用 (230)",
            "科技-数码 (95)",
            "知识-科学科普 (201)",
            "知识-财经商业 (207)",
            "知识-职业职场 (209)",
            "知识-社科·法律·心理 (124)",
            "影视-AI影像 (259)",
            "音乐-AI音乐 (265)",
            "资讯-热点 (212)",
            "生活-日常 (21)"
        ]
        ttk.Combobox(f4_5, textvariable=self.bili_tid_var, values=bili_partitions, width=18).grid(row=0, column=2, padx=5, pady=2, sticky="w")
        
        ttk.Label(f4_5, text="标签 (逗号/顿号分隔):").grid(row=1, column=0, padx=10, pady=2, sticky="e")
        self.bili_tags_var = tk.StringVar(value="")  # Tags: dynamically generated per upload, not persisted
        ttk.Entry(f4_5, textvariable=self.bili_tags_var, width=30).grid(row=1, column=1, columnspan=2, padx=5, pady=2, sticky="w")
        
        ttk.Label(f4_5, text="合集名称 (新建/选择):").grid(row=2, column=0, padx=10, pady=2, sticky="e")
        self.bili_season_var = tk.StringVar(value=str(self.settings.get("bili_season", "")))
        ttk.Entry(f4_5, textvariable=self.bili_season_var, width=30).grid(row=2, column=1, columnspan=2, padx=5, pady=2, sticky="w")

        # 4.6 WeChat Channels Sync (Parent: tab2)
        f4_6 = ttk.LabelFrame(self.tab2, text=" 微信视频号同步 ", padding=10)
        f4_6.pack(fill="x", pady=5)
        self.wechat_enabled_var = tk.BooleanVar(value=bool(self.settings.get("wechat_enabled", False)))
        ttk.Checkbutton(f4_6, text="上传到微信视频号", variable=self.wechat_enabled_var).grid(row=0, column=0, sticky="w", pady=2)
        self.wechat_public_var = tk.BooleanVar(value=bool(self.settings.get("wechat_public", False)))
        ttk.Checkbutton(f4_6, text="公开发布（不勾选则保存草稿）", variable=self.wechat_public_var).grid(row=0, column=1, padx=15, sticky="w", pady=2)
        ttk.Label(f4_6, text="封面标题 (留空自动):").grid(row=1, column=0, padx=10, pady=2, sticky="e")
        self.wechat_cover_title_var = tk.StringVar(value="")
        ttk.Entry(f4_6, textvariable=self.wechat_cover_title_var, width=64).grid(
            row=1, column=1, columnspan=2, padx=5, pady=2, sticky="ew"
        )
        f4_6.columnconfigure(1, weight=1)
        f4_6.columnconfigure(2, weight=1)

        # 5. Log Area (Parent: tab3)
        f5 = ttk.LabelFrame(self.tab3, text=" 日志与进度 ", padding=10)
        f5.pack(fill="both", expand=True, pady=5)
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(f5, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", pady=5)
        self.status_label = ttk.Label(f5, text="等待任务...")
        self.status_label.pack(pady=2)
        log_frame = ttk.Frame(f5)
        log_frame.pack(fill="both", expand=True)
        self.log_scrollbar = ttk.Scrollbar(log_frame, command=lambda *a: self.log_text.yview(*a))
        self.log_scrollbar.pack(side="right", fill="y")
        self.log_text = tk.Text(log_frame, height=10, state="disabled", font=self.log_font, bg="#ffffff", padx=5, pady=5, yscrollcommand=lambda *a: self.log_scrollbar.set(*a))
        self.log_text.pack(side="left", fill="both", expand=True)

        # 5.5 Task History (Parent: tab4)
        history_frame = ttk.Frame(self.tab4)
        history_frame.pack(fill="both", expand=True)
        history_toolbar = ttk.Frame(history_frame)
        history_toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Button(
            history_toolbar,
            text="刷新",
            command=self.refresh_history,
        ).pack(side="left", padx=(0, 5))
        ttk.Button(
            history_toolbar,
            text="载入选中输入",
            command=self.load_selected_history_input,
        ).pack(side="left")

        history_columns = ("time", "name", "source", "status", "completed", "missing")
        self.history_tree = ttk.Treeview(
            history_frame,
            columns=history_columns,
            show="headings",
            selectmode="browse",
        )
        headings = (
            ("time", "最近更新", 125),
            ("name", "视频名称", 300),
            ("source", "视频URL/输入", 330),
            ("status", "处理结果", 165),
            ("completed", "已完成输出/上传", 220),
            ("missing", "未完成", 180),
        )
        for column, text, width in headings:
            self.history_tree.heading(column, text=text)
            self.history_tree.column(
                column,
                width=width,
                minwidth=90,
                stretch=True,
                anchor="w",
            )
        history_scroll_y = ttk.Scrollbar(
            history_frame,
            orient="vertical",
            command=self.history_tree.yview,
        )
        history_scroll_x = ttk.Scrollbar(
            history_frame,
            orient="horizontal",
            command=self.history_tree.xview,
        )
        self.history_tree.configure(
            yscrollcommand=history_scroll_y.set,
            xscrollcommand=history_scroll_x.set,
        )
        self.history_tree.grid(row=1, column=0, sticky="nsew")
        history_scroll_y.grid(row=1, column=1, sticky="ns")
        history_scroll_x.grid(row=2, column=0, sticky="ew")
        history_frame.rowconfigure(1, weight=1)
        history_frame.columnconfigure(0, weight=1)
        self.history_tree.bind("<Double-1>", lambda _event: self.load_selected_history_input())
        self.refresh_history()

        # 6. Controls (Parent: root)
        f6 = ttk.Frame(self.root, padding=10)
        f6.pack(fill="x", side="bottom")
        ttk.Button(f6, text="退出", command=self.root.destroy).pack(side="left", padx=5)
        ttk.Button(f6, text="保存设置", command=self.save_settings).pack(side="left", padx=5)
        ttk.Button(f6, text="剪切广告", command=self.cut_ads_now).pack(side="left", padx=5)
        self.stop_btn = ttk.Button(f6, text="中止", state="disabled", command=self.stop_process)
        self.stop_btn.pack(side="right", padx=5)
        self.pause_btn = ttk.Button(f6, text="暂停", state="disabled", command=self.toggle_pause)
        self.pause_btn.pack(side="right", padx=5)
        self.start_btn = ttk.Button(f6, text="开始处理", command=self.start_process)
        self.start_btn.pack(side="right", padx=5)
        self.upload_btn = ttk.Button(f6, text="只上传", command=self.start_upload_only)
        self.upload_btn.pack(side="right", padx=5)

        # Adjust window height dynamically based on the tallest tab content
        self.root.update_idletasks()
        req_height = self.root.winfo_reqheight()
        self.root.geometry(f"1020x{req_height + 10}")

    def create_entry_row(self, p, l, v, r, c):
        ttk.Label(p, text=l).grid(row=r, column=0, sticky="w", pady=2)
        var = tk.StringVar(value=str(self.settings.get(v.replace("_var", ""), "")))
        setattr(self, v, var)
        ttk.Entry(p, textvariable=var, width=65).grid(row=r, column=1, padx=5)
        ttk.Button(p, text="选择...", command=c).grid(row=r, column=2)

    def create_style_row(self, p, l1, v1, val1, l2, v2, val2, r):
        ttk.Label(p, text=l1).grid(row=r, column=0, sticky="w")
        var1 = tk.StringVar(value=str(self.settings.get(v1.replace("_var", ""), "")))
        setattr(self, v1, var1)
        if val1: ttk.Combobox(p, textvariable=var1, values=val1, width=22).grid(row=r, column=1, sticky="w", padx=5)
        else: ttk.Entry(p, textvariable=var1, width=24).grid(row=r, column=1, sticky="w", padx=5)
        ttk.Label(p, text=l2).grid(row=r, column=2, sticky="w", padx=10)
        var2 = tk.StringVar(value=str(self.settings.get(v2.replace("_var", ""), "")))
        setattr(self, v2, var2)
        if val2: ttk.Combobox(p, textvariable=var2, values=val2, width=22).grid(row=r, column=3, sticky="w", padx=5)
        else: ttk.Entry(p, textvariable=var2, width=24).grid(row=r, column=3, sticky="w", padx=5)

    def _history_requirements(self):
        requirements = []
        if self.gdrive_enabled_var.get():
            requirements.append("GDrive")
        if self.bili_enabled_var.get():
            requirements.append("B站")
        if self.wechat_enabled_var.get():
            requirements.append("微信")
        return requirements

    def _persist_input_value(self, value):
        """Persist only the input field, without showing a dialog."""
        data = {}
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except Exception:
                data = {}
        data["input"] = value
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=4, ensure_ascii=False)
        except Exception:
            pass

    def restore_pending_input(self):
        """Leave the input box empty when the saved task is fully complete."""
        saved_input = self.input_var.get().strip()
        startup_input = select_startup_input(
            self.task_history,
            saved_input,
            self._history_requirements(),
        )
        if startup_input != self.input_var.get():
            self.input_var.set(startup_input)
        self._persist_input_value(startup_input)

    def _update_input_after_run(self, input_video):
        """Clear a finished task, or retain it for the next upload-only retry."""
        current_input = self.input_var.get().strip()
        if current_input and current_input != str(input_video):
            return
        record = find_history_record_for_input(self.task_history, input_video)
        if record and is_project_complete(record, self._history_requirements()):
            self.input_var.set("")
            self._persist_input_value("")
        else:
            self._persist_input_value(str(input_video))

    def refresh_history(self):
        base_dirs = [BASE_OUTPUT_DIR]
        custom_output = self.output_dir_var.get().strip()
        if custom_output:
            custom_output = os.path.abspath(custom_output)
            if custom_output != BASE_OUTPUT_DIR:
                base_dirs.append(custom_output)

        records_by_workdir = {}
        for base_dir in base_dirs:
            for record in scan_task_history(base_dir):
                records_by_workdir[record["workdir"]] = record
        self.task_history = sorted(
            records_by_workdir.values(),
            key=lambda item: item["updated_at"],
            reverse=True,
        )

        self.history_tree.delete(*self.history_tree.get_children())
        for record in self.task_history:
            self.history_tree.insert(
                "",
                "end",
                values=(
                    record["updated_at"],
                    record["name"],
                    record["source"] or record["workdir"],
                    record["status"],
                    "、".join(record["completed"]) or "无",
                    "、".join(record["missing"]) or "无",
                ),
            )

    def load_selected_history_input(self):
        selection = self.history_tree.selection()
        if not selection:
            return
        index = self.history_tree.index(selection[0])
        if index >= len(self.task_history):
            return
        record = self.task_history[index]
        value = record["source"] or record["workdir"]
        self.input_var.set(value)
        self._persist_input_value(value)
        self.notebook.select(self.tab1)

    def browse_video(self): f = filedialog.askopenfilename(); self.input_var.set(f) if f else None
    def browse_cookies(self): f = filedialog.askopenfilename(); self.cookies_var.set(f) if f else None
    def browse_folder(self): f = filedialog.askdirectory(); self.output_dir_var.set(f) if f else None

    def get_project_env_path(self):
        return os.path.join(PROJECT_ROOT, ".env")

    def load_api_key(self, vendor, env_key=None):
        if not env_key:
            env_key = self.vendor_env_keys.get(vendor)
        if not env_key:
            return ""

        # 1. Try reading from project .env
        env_path = self.get_project_env_path()
        if os.path.exists(env_path):
            try:
                with open(env_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                match = re.search(rf'^{env_key}=(.*)', content, re.M)
                if match:
                    val = match.group(1).strip().strip("'").strip('"')
                    if val: return val
            except:
                pass

        return os.environ.get(env_key, "")

    def update_api_key_ui(self):
        key_val = self.load_api_key(self.key_editor_vendor, self.key_editor_env_key)
        if key_val:
            self.api_key_var.set("*" * 16)
        else:
            self.api_key_var.set("")

    def update_key_editor(self, vendor_name, env_key):
        self.key_editor_vendor = vendor_name
        self.key_editor_env_key = env_key
        self.api_key_label.config(text=f"API Key ({vendor_name}):")
        self.update_api_key_ui()

    def save_api_key(self):
        vendor = self.key_editor_vendor
        env_key = self.key_editor_env_key
        if not env_key:
            messagebox.showerror("错误", f"未找到厂商 {vendor} 对应的环境变量配置")
            return

        new_val = self.api_key_var.get().strip()
        if not new_val:
            messagebox.showwarning("警告", "请输入有效的 API Key")
            return

        if re.match(r'^\*+$', new_val):
            messagebox.showinfo("信息", "API Key 未改变，无需保存")
            return

        env_path = self.get_project_env_path()
        try:
            content = ""
            if os.path.exists(env_path):
                with open(env_path, 'r', encoding='utf-8') as f:
                    content = f.read()

            def set_env_val(key, val, current):
                regex = re.compile(rf'^{key}=.*', re.M)
                if regex.search(current):
                    return regex.sub(f'{key}={val}', current)
                prefix = "\n" if current and not current.endswith("\n") else ""
                return current + f'{prefix}{key}={val}'

            new_content = set_env_val(env_key, new_val, content)
            
            os.makedirs(os.path.dirname(env_path), exist_ok=True)
            with open(env_path, 'w', encoding='utf-8') as f:
                f.write(new_content.strip() + '\n')
            
            os.environ[env_key] = new_val

            messagebox.showinfo("成功", f"{vendor} 的 API Key 已保存并同步至统一 .env 文件")
            self.api_key_var.set("*" * 16)
        except Exception as e:
            messagebox.showerror("错误", f"保存 API Key 失败: {e}")

    def set_llm_model_default(self):
        vendor = self.vendor_var.get()
        model = self.llm_model_var.get()
        if not model or model in ["正在获取...", "正在获取模型列表...", "未发现可用模型", "无模型", "错误", "获取失败"]:
            messagebox.showerror("错误", "请先选择一个有效的模型")
            return
        self.vendor_default_models[vendor] = model
        self.save_settings()
        messagebox.showinfo("成功", f"已将 {model} 设为 {vendor} 的默认模型")

    def on_vendor_change(self, event=None):
        v_name = self.vendor_var.get()
        threading.Thread(target=self.fetch_models_async, args=(v_name,), daemon=True).start()
        env_key = self.vendor_env_keys.get(v_name, "GEMINI_API_KEY")
        self.update_key_editor(v_name, env_key)

    def on_model_select(self, event=None):
        """Prevent selecting separator rows in the model dropdown."""
        selected = self.llm_model_var.get()
        if selected.startswith("─"):
            prev = getattr(self, "_prev_model", "")
            self.llm_model_var.set(prev)
        else:
            self._prev_model = selected

    def fetch_models_async(self, v_name):
        try:
            from llm_utils import LLMProvider, LLMClient
            v_id_str = self.vendor_map.get(v_name, "gemini")
            provider = LLMProvider(v_id_str)

            # Show loading state
            self.root.after(0, lambda: self.llm_combo.config(values=["正在获取模型列表..."]))
            self.root.after(0, lambda: self.llm_model_var.set("正在获取..."))

            client = LLMClient()
            # Pass the API key from the GUI if it exists, but ONLY if not masked asterisks!
            gui_key = self.api_key_var.get().strip()
            if gui_key and not re.match(r'^\*+$', gui_key):
                client.set_provider_key(provider, gui_key)

            models = client.list_models_by_provider(provider)
            if models:
                self.root.after(0, lambda: self.llm_combo.config(values=models))
                
                # Check default model linkage
                default_model = self.vendor_default_models.get(v_name)
                if default_model and default_model in models:
                    self.root.after(0, lambda: self.llm_model_var.set(default_model))
                elif self.llm_model_var.get() not in models:
                    self.root.after(0, lambda: self.llm_model_var.set(models[0]))
            else:
                self.root.after(0, lambda: self.llm_combo.config(values=["未发现可用模型"]))
                self.root.after(0, lambda: self.llm_model_var.set("无模型"))
                self.root.after(0, lambda: self.log(f"⚠️ 厂商 {v_name} 未返回任何模型，请检查 API Key 或网络"))
        except Exception as e:
            import traceback
            err_msg = f"获取模型失败 ({v_name}): {str(e)}"
            self.root.after(0, lambda: self.log(f"❌ {err_msg}"))
            self.root.after(0, lambda: self.llm_combo.config(values=["获取失败"]))
            self.root.after(0, lambda: self.llm_model_var.set("错误"))
            print(traceback.format_exc())

    def test_connection(self):
        v_name = self.vendor_var.get()
        m_name = self.llm_model_var.get()
        api_key = self.api_key_var.get().strip()

        self.log(f"🧪 正在测试 {v_name} ({m_name}) 的连接...")
        threading.Thread(target=self.run_connection_test, args=(v_name, m_name, api_key), daemon=True).start()

    def run_connection_test(self, v_name, m_name, api_key):
        try:
            from llm_utils import LLMProvider, LLMClient
            v_id_str = self.vendor_map.get(v_name, "gemini")
            provider = LLMProvider(v_id_str)

            client = LLMClient()
            if api_key and not re.match(r'^\*+$', api_key):
                client.set_provider_key(provider, api_key)

            # Match the updated LLMClient.generate_content_with_provider API
            success, response = client.generate_content_with_provider(
                "Ping",
                provider=provider,
                model_name=m_name,
                fallback=False
            )

            if success:
                # Capture current values for the lambda
                final_resp = response
                self.root.after(0, lambda r=final_resp: self.log(f"✅ 连接成功! 响应: {r[:50]}..."))
                self.root.after(0, lambda r=final_resp: messagebox.showinfo("测试成功", f"与 {v_name} 的连接正常！\n\n响应内容:\n{r[:100]}..."))
            else:
                final_err = response
                self.root.after(0, lambda r=final_err: self.log(f"❌ 连接失败: {r}"))
                self.root.after(0, lambda r=final_err: messagebox.showerror("连接失败", f"无法连接到 {v_name}。\n\n详情:\n{r}"))
        except Exception as e:
            err_str = str(e)
            self.root.after(0, lambda es=err_str: self.log(f"❌ 测试过程出错: {es}"))

    def log(self, msg):
        self.log_text.config(state="normal")
        is_p = "Progress:" in msg or "%" in msg
        if is_p and self.last_was_progress: self.log_text.delete("end-2l", "end-1c")
        # Smart scroll: only jump to bottom if user is already near the bottom.
        at_bottom = self.log_text.yview()[1] > 0.95
        self.log_text.insert("end", msg + "\n")
        if at_bottom:
            self.log_text.see("end")
        self.log_text.config(state="disabled")
        self.last_was_progress = is_p
        m = re.search(r"(\d+(\.\d+)?)%", msg)
        if m: self.progress_var.set(float(m.group(1)))
        if any(e in msg for e in ["🎬", "🎙️", "🌍", "🔥"]): self.status_label.config(text=msg.strip())

    def _register_process(self, proc):
        with self._process_lock:
            self.current_processes.append(proc)

    def _unregister_process(self, proc):
        with self._process_lock:
            if proc in self.current_processes:
                self.current_processes.remove(proc)

    def _running_processes(self):
        with self._process_lock:
            return [proc for proc in self.current_processes if proc.poll() is None]

    def _prune_finished_processes(self):
        with self._process_lock:
            self.current_processes = [
                proc for proc in self.current_processes if proc.poll() is None
            ]

    def start_process(self):
        v = self.input_var.get().strip()
        if not v: return messagebox.showwarning("警告", "请输入视频 URL 或文件")
        
        # Switch to the Execution Logs tab automatically
        try:
            self.notebook.select(self.tab3)
        except Exception:
            pass

        # Reset UI state
        self.start_btn.config(state="disabled"); self.stop_btn.config(state="normal")
        self.progress_var.set(0)
        self.status_label.config(text="🚀 正在启动任务...")
        self.log_text.config(state="normal"); self.log_text.delete(1.0, tk.END); self.log_text.config(state="disabled")
        
        # Ensure any previous process is truly gone
        for proc in self._running_processes():
            try: ProcessTree.kill(proc.pid)
            except Exception: pass
        self._upload_abort.clear()

        # Robust Mapping for Localized Values
        trans_mode_map = {"平衡模式 (Balanced)": "balanced", "意译模式 (Paraphrase)": "paraphrase", "balanced": "balanced", "paraphrase": "paraphrase"}
        style_map = {"智能检测 (Auto)": "auto", "普通 (Casual)": "casual", "正式 (Formal)": "formal", "犀利 (Edgy)": "edgy", "auto": "auto", "casual": "casual", "formal": "formal", "edgy": "edgy"}
        
        t_mode = trans_mode_map.get(self.trans_mode_var.get(), "balanced")
        s_style = style_map.get(self.style_var.get(), "auto")

        py_exe = sys.executable
        venv_py = os.path.join(
            CURRENT_DIR,
            ".venv",
            "Scripts" if sys.platform == "win32" else "bin",
            "python.exe" if sys.platform == "win32" else "python",
        )
        if os.path.exists(venv_py):
            py_exe = venv_py
        cmd = [py_exe, AUTOSUB_SCRIPT, v, "--headless"]
        cmd.extend(["--model", self.whisper_var.get(), "--llm-model", self.llm_model_var.get()])
        v_id_str = self.vendor_map.get(self.vendor_var.get(), "")
        if v_id_str:
            cmd.extend(["--llm-provider", v_id_str])
        
        cmd.extend(["--style", s_style, "--trans-mode", t_mode, "--quality", self.quality_var.get()])
        cmd.extend(["--layout", self.layout_var.get(), "--main-lang", self.main_lang_var.get()])
        cmd.extend(["--cn-font", self.cn_font_var.get(), "--en-font", self.en_font_var.get()])
        cmd.extend(["--ja-font", self.jp_font_var.get()])
        cmd.extend(["--cn-size", self.cn_size_var.get(), "--en-size", self.en_size_var.get()])
        cmd.extend(["--ja-size", self.jp_size_var.get()])
        if not self.bg_box_var.get(): cmd.append("--no-bg-box")
        if self.quick_var.get(): cmd.append("--quick")
        if self.multilang_var.get(): cmd.append("--multilang")
        c_val = self.cookies_var.get().strip()
        if c_val:
            cmd.extend(["--cookies", c_val])
        if self.output_dir_var.get(): cmd.extend(["--output-dir", self.output_dir_var.get()])
        # GDrive sync runs in parallel with Bilibili upload after the pipeline
        # finishes (see run_worker), so we no longer pass --sync-gdrive here.
        gdrive_folder = self.sync_gdrive_var.get().strip() if self.gdrive_enabled_var.get() else ""

        print(f"🚀 [GUI] Launching: {' '.join(cmd)}")
        self._persist_input_value(v)
        threading.Thread(target=self.run_worker, args=(cmd, v, self.output_dir_var.get(), gdrive_folder, py_exe), daemon=True).start()

    def start_upload_only(self):
        """Upload-only mode: skip the pipeline and just upload existing hardsub video."""
        v = self.input_var.get().strip()
        if not v: return messagebox.showwarning("警告", "请输入视频 URL 或文件路径（用于定位项目目录）")

        try:
            self.notebook.select(self.tab3)
        except Exception:
            pass

        self.start_btn.config(state="disabled")
        self.upload_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.progress_var.set(0)
        self.status_label.config(text="📤 正在准备上传...")
        self.log_text.config(state="normal"); self.log_text.delete(1.0, tk.END); self.log_text.config(state="disabled")
        self._upload_abort.clear()

        py_exe = sys.executable
        venv_py = os.path.join(
            CURRENT_DIR,
            ".venv",
            "Scripts" if sys.platform == "win32" else "bin",
            "python.exe" if sys.platform == "win32" else "python",
        )
        if os.path.exists(venv_py):
            py_exe = venv_py

        cmd = [py_exe, AUTOSUB_SCRIPT, v, "--headless", "--upload-only"]
        v_id_str = self.vendor_map.get(self.vendor_var.get(), "")
        if v_id_str:
            cmd.extend(["--llm-provider", v_id_str])
        cmd.extend(["--llm-model", self.llm_model_var.get()])
        # Platform uploads stay in run_worker so upload-only follows the same
        # order as a full run: cut ads first, then GD(full)/BI/WX(ad-cut).
        gdrive_folder = self.sync_gdrive_var.get().strip() if self.gdrive_enabled_var.get() else ""

        print(f"📤 [GUI] Upload-only: {' '.join(cmd)}")
        self._persist_input_value(v)
        threading.Thread(target=self.run_worker, args=(cmd, v, self.output_dir_var.get(), gdrive_folder, py_exe), daemon=True).start()

    def run_worker(self, cmd, input_video, output_dir, gdrive_folder="", py_exe=None):
        import glob
        # Post-pipeline upload logs are opened after the real project workdir is
        # resolved. A local input may live in Downloads while autosub.py copies
        # and burns the video under Projects.
        _log_path = None
        _log_file = None

        def _persist(msg):
            """Write a message to both the GUI log panel and workflow.log."""
            self.root.after(0, self.log, msg)
            if _log_file:
                try:
                    _log_file.write(msg + "\n")
                    _log_file.flush()
                except Exception:
                    pass

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1)
            self._register_process(proc)
            self.root.after(0, lambda: self.pause_btn.config(state="normal"))
            if proc.stdout:
                for line in proc.stdout: self.root.after(0, self.log, line.strip())
            ret_code = proc.wait()
            # Keep the pause/stop controls alive for GD/BI/WX post-processing.
            self._unregister_process(proc)
            if ret_code != 0:
                self.root.after(0, lambda r=ret_code: self.status_label.config(text="❌ 失败"))
            else:
                # --- Parallel post-processing uploads ---
                # GDrive sync and Bilibili upload only READ the finished hardsub
                # video, so running them concurrently is safe; launch both at once.
                from autosub import find_hardsub_video
                base_out = output_dir or BASE_OUTPUT_DIR
                # Resolve the job workdir: the folder actually holding the
                # hardsub output for THIS run. For local inputs the workdir is
                # the input file's directory when the input lives under base_out.
                # For HTTP/X inputs the pipeline derives the folder from the video
                # title (get_video_folder_name) which the GUI does not capture, so
                # fall back to the most recently modified subfolder of base_out
                # that contains a hardsub file (the pipeline just finished, so
                # this run's workdir is newest).
                workdir = None
                if not str(input_video).startswith("http"):
                    vid_dir = os.path.dirname(input_video)
                    if vid_dir and os.path.isdir(vid_dir):
                        workdir = vid_dir
                # If the local input dir has no hardsub, the pipeline copied the
                # video to a project folder under base_out and burned there.
                # Fall through to the subfolder search to find the real workdir.
                if workdir is not None and not find_hardsub_video(workdir):
                    workdir = None
                if workdir is None:
                    self.root.after(0, self.log, f"🔍 在目录中搜索 hardsub 视频: {base_out}")
                    candidate_dirs = []
                    try:
                        for item in os.listdir(base_out):
                            ip = os.path.join(base_out, item)
                            if os.path.isdir(ip):
                                candidate_dirs.append(ip)
                    except OSError:
                        pass
                    candidate_dirs.append(base_out)  # flat layout fallback
                    best = None
                    best_mtime = -1.0
                    for cd in candidate_dirs:
                        hv = find_hardsub_video(cd)
                        if hv:
                            try:
                                mt = os.path.getmtime(hv)
                            except OSError:
                                continue
                            if mt > best_mtime:
                                best_mtime = mt
                                best = cd
                    workdir = best
                # Locate the hardsub inside the resolved workdir.
                newest_video = find_hardsub_video(workdir) if workdir else None
                if newest_video:
                    self.root.after(0, self.log, f"📺 定位到本任务 hardsub: {os.path.basename(newest_video)}")
                else:
                    self.root.after(0, self.log, "⚠️ 未找到本任务对应的 hardsub 视频文件，跳过上传。")
                # sync_dir is the workdir holding the hardsub; passed to GDrive
                # sync and as the workdir arg to build_bili_cmd.
                sync_dir = workdir if newest_video else None
                _log_dir = sync_dir
                if _log_dir is None and not str(input_video).startswith("http"):
                    _log_dir = os.path.dirname(input_video)
                if _log_dir is None:
                    _log_dir = output_dir or BASE_OUTPUT_DIR
                try:
                    _log_path = os.path.join(_log_dir, "workflow.log")
                    _log_file = open(_log_path, "a", encoding="utf-8", errors="replace")
                    _log_file.write("\n--- 后续上传阶段 ---\n")
                    _log_file.flush()
                except Exception as log_error:
                    self.root.after(0, self.log, f"⚠️ 无法写入上传日志: {log_error}")

                # Cut ads before building upload jobs. BI/WX must not publish the
                # full version when the user asked for the ad-cut version.
                ad_removal_ok = True
                if getattr(self, "remove_ads_var", None) and self.remove_ads_var.get() and sync_dir:
                    _persist("📱 主流程完成，开始剪除广告片段...")
                    _ra_script = os.path.join(CURRENT_DIR, "remove_ads.py")
                    _ra_model = self.llm_model_var.get() or "glm-5.2"
                    _ra_prov = self.vendor_map.get(self.vendor_var.get(), "")
                    _ra_cmd = [py_exe, _ra_script, sync_dir, "--model", _ra_model]
                    if _ra_prov:
                        _ra_cmd += ["--provider", _ra_prov]
                    _ra_cmd += [
                        "--detector", self.ad_detector_var.get(),
                        "--cut-mode", self.ad_cut_mode_var.get(),
                    ]
                    _ra_proc = None
                    try:
                        _ra_proc = subprocess.Popen(_ra_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
                        self._register_process(_ra_proc)
                        if _ra_proc.stdout:
                            for _ra_line in _ra_proc.stdout:
                                _persist(f"📱 [Ads] {_ra_line.strip()}")
                        _ra_proc.wait()
                        ad_removal_ok = _ra_proc.returncode == 0
                    except Exception as _ra_e:
                        ad_removal_ok = False
                        _persist(f"❌ 广告剪除异常: {_ra_e}")
                    finally:
                        if _ra_proc is not None:
                            self._unregister_process(_ra_proc)
                    if not ad_removal_ok:
                        _persist("⚠️ 广告剪除失败，Bilibili/微信上传已停止；Google Drive 仍同步完整版。")

                upload_results = {}
                upload_threads = []
                upload_results_lock = threading.Lock()

                def run_upload(label, ucmd, marker):
                    _persist(f"{label} 开始上传...")
                    proc = None
                    upload_ok = False
                    output_lines = []

                    def on_log(line):
                        stripped = str(line).strip()
                        if not stripped:
                            return
                        output_lines.append(stripped)
                        if len(output_lines) > 300:
                            output_lines.pop(0)
                        _persist(f"{label} {stripped}")

                    def on_progress(pct):
                        self.root.after(
                            0,
                            lambda p=pct: self.status_label.config(
                                text=f"📤 {label} {p:.0f}%"
                            ),
                        )

                    def register_upload_process(child):
                        nonlocal proc
                        proc = child
                        self._register_process(child)

                    try:
                        if marker == ".bili_uploaded":
                            from autosub import run_bili_cmd
                            upload_ok = run_bili_cmd(
                                ucmd,
                                on_progress=on_progress,
                                on_log=on_log,
                                abort_flag=self._upload_abort.is_set,
                                on_process_start=register_upload_process,
                                on_process_end=self._unregister_process,
                            )
                        elif marker == ".wechat_uploaded":
                            from autosub import run_wechat_cmd
                            upload_ok = run_wechat_cmd(
                                ucmd,
                                on_progress=on_progress,
                                on_log=on_log,
                                abort_flag=self._upload_abort.is_set,
                                on_process_start=register_upload_process,
                                on_process_end=self._unregister_process,
                            )
                        else:
                            proc = subprocess.Popen(
                                ucmd,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                                bufsize=1,
                            )
                            self._register_process(proc)
                            if proc.stdout:
                                for line in proc.stdout:
                                    on_log(line)
                            proc.wait()
                            upload_ok = proc.returncode == 0

                        exit_code = proc.returncode if proc is not None else (0 if upload_ok else 1)
                        mark = "✅" if upload_ok else "⚠️"
                        _persist(f"{label} {mark} 上传结束 (code={exit_code})")
                        if not upload_ok:
                            from autosub import get_upload_login_failure_hint
                            login_hint = get_upload_login_failure_hint(
                                label,
                                "\n".join(output_lines),
                            )
                            if login_hint:
                                _persist(login_hint)
                        if upload_ok and marker and sync_dir:
                            try:
                                marker_path = os.path.join(sync_dir, marker)
                                with open(marker_path, "w", encoding="utf-8") as marker_file:
                                    marker_file.write("1")
                            except Exception as marker_error:
                                _persist(f"{label} ⚠️ 写入上传标记失败: {marker_error}")
                    except Exception as ue:
                        _persist(f"{label} ❌ 异常: {ue}")
                    finally:
                        if proc is not None:
                            self._unregister_process(proc)
                    with upload_results_lock:
                        upload_results[label] = upload_ok
                    return upload_ok

                def start_upload_job(label, ucmd, marker):
                    self.root.after(
                        0,
                        lambda: self.status_label.config(text=f"📤 {label} 上传中..."),
                    )
                    thread = threading.Thread(
                        target=run_upload,
                        args=(label, ucmd, marker),
                        daemon=True,
                    )
                    upload_threads.append(thread)
                    thread.start()

                bili_pending = self.bili_enabled_var.get() and not (
                    sync_dir and os.path.exists(os.path.join(sync_dir, ".bili_uploaded"))
                )
                wechat_pending = self.wechat_enabled_var.get() and not (
                    sync_dir and os.path.exists(os.path.join(sync_dir, ".wechat_uploaded"))
                )
                if not ad_removal_ok:
                    bili_pending = False
                    wechat_pending = False
                login_failures = []
                metadata_failures = []
                if bili_pending:
                    from autosub import (
                        check_bili_login,
                        get_bili_login_command,
                        get_bili_login_skip_reason,
                    )
                    login_reason = get_bili_login_skip_reason()
                    if login_reason:
                        _persist(f"📱 [Bilibili] 上传跳过：{login_reason}")
                        _persist(f"📱 [Bilibili] 请先执行: {get_bili_login_command()}")
                        bili_pending = False
                        login_failures.append("📱 [Bilibili]")
                    else:
                        login_ok, login_detail = check_bili_login()
                        if login_ok:
                            _persist("📱 [Bilibili] 登录态有效")
                        elif login_ok is False:
                            _persist(f"📱 [Bilibili] 上传跳过：{login_detail}")
                            _persist(f"📱 [Bilibili] 请先执行: {get_bili_login_command()}")
                            bili_pending = False
                            login_failures.append("📱 [Bilibili]")
                        elif login_detail:
                            _persist(f"⚠️ {login_detail}")
                if wechat_pending:
                    from autosub import (
                        get_wechat_login_command,
                        get_wechat_login_skip_reason,
                    )
                    login_reason = get_wechat_login_skip_reason()
                    if login_reason:
                        _persist(f"💬 [WeChat] 上传跳过：{login_reason}")
                        _persist(f"💬 [WeChat] 请先执行: {get_wechat_login_command()}")
                        wechat_pending = False
                        login_failures.append("💬 [WeChat]")
                    else:
                        _persist("💬 [WeChat] 已找到持久化浏览器配置，登录态将在上传时验证")
                bili_metadata = None
                if sync_dir and bili_pending:
                    from autosub import prepare_bili_upload_metadata
                    self.root.after(
                        0,
                        lambda: self.status_label.config(
                            text="🧠 [MG] 生成B站标题和摘要..."
                        ),
                    )
                    bili_metadata = prepare_bili_upload_metadata(
                        sync_dir,
                        explicit_cover_title=None,
                        llm_model=self.llm_model_var.get(),
                        llm_provider=self.vendor_map.get(self.vendor_var.get(), ""),
                        on_log=_persist,
                    )
                    if not bili_metadata:
                        _persist("⚠️ Bilibili上传跳过：B站元数据生成失败或不完整。")
                        bili_pending = False
                        metadata_failures.append("📱 [Bilibili]")
                # 1) Google Drive sync (mirrors autosub.py trigger_sync headless branch)
                if gdrive_folder and sync_dir:
                    if os.path.exists(os.path.join(sync_dir, ".synced")):
                        _persist("☁️ [GDrive] 已上传过 (跳过)")
                    else:
                        sync_script = os.environ.get("AUTOSUB_GDRIVE_SYNC_SCRIPT", "")
                        creds_path = os.environ.get("AUTOSUB_GDRIVE_CREDENTIALS", "")
                        token_path = os.environ.get("AUTOSUB_GDRIVE_TOKEN", "")
                        sync_py = py_exe or sys.executable
                        if sync_script and creds_path and os.path.exists(sync_script) and os.path.exists(creds_path):
                            gdrive_cmd = [
                                sync_py, sync_script,
                                "--local-dir", sync_dir,
                                "--remote-id", gdrive_folder,
                                "--credentials", creds_path,
                                "--token", token_path,
                                "--wrap-folder",
                                "--exclude", "noads",
                                "--headless",
                            ]
                            start_upload_job("☁️ [GDrive]", gdrive_cmd, ".synced")
                        else:
                            _persist("⚠️ Google Drive 同步未配置或脚本、凭证不存在，已跳过。")
                # 2) Bilibili upload (cmd built by the canonical autosub.build_bili_cmd,
                #    shared with the CLI and batch engine)
                if self.bili_enabled_var.get():
                    if sync_dir and os.path.exists(os.path.join(sync_dir, ".bili_uploaded")):
                        _persist("📱 [Bilibili] 已上传过 (跳过)")
                    elif not bili_pending:
                        pass
                    elif bili_pending and sync_dir:
                        bili_tid_str = self.bili_tid_var.get().strip()
                        bili_tid = bili_category = None
                        if bili_tid_str and bili_tid_str != "智能识别 (Auto)":
                            m = re.search(r'\((\d+)\)', bili_tid_str)
                            if m:
                                bili_tid = m.group(1)
                            else:
                                bili_category = bili_tid_str
                        from autosub import build_bili_cmd
                        bili_cmd = build_bili_cmd(
                            sync_dir,
                            tid=bili_tid,
                            category_name=bili_category,
                            tags=self.bili_tags_var.get().strip() or None,
                            season=self.bili_season_var.get().strip() or None,
                            title=bili_metadata["title"],
                            desc=bili_metadata["desc"],
                            cover_title=bili_metadata["cover_title"],
                            llm_model=self.llm_model_var.get(),
                            llm_provider=self.vendor_map.get(self.vendor_var.get(), ""),
                            require_complete_metadata=True,
                        )
                        if bili_cmd:
                            _persist(f"📱 找到上传视频: {os.path.basename(bili_cmd[2])}")
                            start_upload_job(
                                "📱 [Bilibili]",
                                bili_cmd,
                                ".bili_uploaded",
                            )
                        elif newest_video:
                            _persist("⚠️ Bilibili 上传跳过（未找到上传脚本或虚拟环境）。")
                        else:
                            _persist("⚠️ 未找到生成的 hardsub 视频文件，跳过 Bilibili 上传。")
                wechat_metadata = None
                if sync_dir and wechat_pending:
                    from autosub import prepare_wechat_upload_metadata
                    self.root.after(
                        0,
                        lambda: self.status_label.config(
                            text="🧠 [MG] 生成微信标题..."
                        ),
                    )
                    wechat_metadata = prepare_wechat_upload_metadata(
                        sync_dir,
                        explicit_cover_title=self.wechat_cover_title_var.get().strip() or None,
                        llm_model=self.llm_model_var.get(),
                        llm_provider=self.vendor_map.get(self.vendor_var.get(), ""),
                        on_log=_persist,
                    )
                    if not wechat_metadata:
                        _persist("⚠️ 微信视频号上传跳过：微信标题生成失败或不完整。")
                        wechat_pending = False
                        metadata_failures.append("💬 [WeChat]")
                # 3) WeChat Channels upload (default: draft; public requires explicit checkbox)
                if self.wechat_enabled_var.get():
                    if sync_dir and os.path.exists(os.path.join(sync_dir, ".wechat_uploaded")):
                        _persist("💬 [WeChat] 已上传过 (跳过)")
                    elif not wechat_pending:
                        pass
                    elif wechat_pending and sync_dir:
                        from autosub import build_wechat_cmd, get_wechat_upload_skip_reason
                        skip_reason = get_wechat_upload_skip_reason(sync_dir)
                        if skip_reason:
                            _persist(f"⚠️ 微信视频号上传跳过: {skip_reason}")
                        else:
                            wechat_cmd = build_wechat_cmd(
                                sync_dir,
                                public=self.wechat_public_var.get(),
                                title=wechat_metadata["wechat_title"],
                                desc=wechat_metadata["desc"],
                                cover_title=wechat_metadata["cover_title"],
                                llm_model=self.llm_model_var.get(),
                                llm_provider=self.vendor_map.get(self.vendor_var.get(), ""),
                                require_complete_metadata=True,
                            )
                            if wechat_cmd:
                                _persist(f"💬 找到上传视频: {os.path.basename(wechat_cmd[2])}")
                                start_upload_job(
                                    "💬 [WeChat]",
                                    wechat_cmd,
                                    ".wechat_uploaded",
                                )
                            elif newest_video:
                                _persist("⚠️ 微信视频号上传命令构建失败。")
                            else:
                                _persist("⚠️ 未找到生成的 hardsub 视频文件，跳过微信视频号上传。")
                if upload_threads:
                    for upload_thread in upload_threads:
                        upload_thread.join()
                else:
                    _persist("⚠️ 没有可执行的上传任务。")
                failed_uploads = login_failures + metadata_failures + [
                    label for label, upload_ok in upload_results.items() if not upload_ok
                ]
                if failed_uploads:
                    _persist("⚠️ 上传未全部完成: " + ", ".join(failed_uploads))
                    self.root.after(0, lambda: self.status_label.config(text="⚠️ 部分上传失败"))
                else:
                    _persist("✅ 全部完成！")
                    self.root.after(0, lambda: self.status_label.config(text="✅ 完成！"))
        except Exception as e:
            _persist(f"错误: {str(e)}")
            self.root.after(0, self.log, f"错误: {str(e)}")
        finally:
            if _log_file:
                try: _log_file.close()
                except: pass
            self._prune_finished_processes()
            self.root.after(0, lambda: self.start_btn.config(state="normal"))
            self.root.after(0, lambda: self.upload_btn.config(state="normal"))
            self.root.after(0, lambda: self.stop_btn.config(state="disabled"))
            self.root.after(0, lambda: self.pause_btn.config(state="disabled", text="暂停 (Pause)"))
            self.root.after(0, self.refresh_history)
            self.root.after(0, lambda: self._update_input_after_run(input_video))
            self._upload_abort.clear()
            self.is_paused = False

    def toggle_pause(self):
        processes = self._running_processes()
        if not processes:
            self.root.after(0, self.log, "⚠️ 没有正在运行的进程，无法暂停/继续")
            return
        if self.is_paused:
            resumed = [
                proc.pid for proc in processes if ProcessTree.resume(proc.pid)
            ]
            if resumed:
                self.is_paused = False
                self.root.after(0, lambda: self.pause_btn.config(text="暂停 (Pause)"))
                self.root.after(
                    0,
                    lambda count=len(resumed): self.log(f"▶️ 已继续 {count} 个进程"),
                )
            else:
                self.root.after(0, self.log, "⚠️ 继续失败，进程可能已结束")
        else:
            suspended = [
                proc.pid for proc in processes if ProcessTree.suspend(proc.pid)
            ]
            if suspended:
                self.is_paused = True
                self.root.after(0, lambda: self.pause_btn.config(text="继续 (Resume)"))
                self.root.after(
                    0,
                    lambda count=len(suspended): self.log(f"⏸️ 已暂停 {count} 个进程"),
                )
            else:
                self.root.after(0, self.log, "⚠️ 暂停失败，进程可能已结束")

    def stop_process(self):
        self._upload_abort.set()
        processes = self._running_processes()
        if processes:
            try:
                for proc in processes:
                    ProcessTree.kill(proc.pid)
            except Exception:
                pass
            self._prune_finished_processes()
            self.root.after(
                0,
                lambda count=len(processes): self.log(f"⛔ 已停止 {count} 个进程"),
            )
        self.root.after(0, lambda: self.status_label.config(text="已停止"))
        self.root.after(0, lambda: self.start_btn.config(state="normal"))
        self.root.after(0, lambda: self.upload_btn.config(state="normal"))
        self.root.after(0, lambda: self.stop_btn.config(state="disabled"))
        self.root.after(0, lambda: self.pause_btn.config(state="disabled", text="暂停 (Pause)"))
        self.is_paused = False


    def cut_ads_now(self):
        """Standalone button: run ad-removal on the project dir from the input field."""
        v = self.input_var.get().strip()
        if not v:
            return messagebox.showwarning("警告", "请在输入源填入项目目录或已完成视频的文件路径")
        if v.startswith("http"):
            return messagebox.showwarning("警告", "剪切广告需要本地项目目录。请先处理视频，再对已完成的项目剪切广告。")
        target = v if os.path.isdir(v) else os.path.dirname(v)
        if not target or not os.path.isdir(target):
            return messagebox.showwarning("警告", f"找不到项目目录: {target}")
        self.run_remove_ads(target)

    def run_remove_ads(self, target):
        """Launch remove_ads.py in a background thread, streaming logs to tab3."""
        try:
            self.notebook.select(self.tab3)
        except Exception:
            pass
        self.progress_var.set(0)
        self.status_label.config(text="📺 正在识别并剪除广告片段...")
        self.log_text.config(state="normal"); self.log_text.delete(1.0, tk.END); self.log_text.config(state="disabled")
        py_exe = sys.executable
        venv_py = os.path.join(
            CURRENT_DIR,
            ".venv",
            "Scripts" if sys.platform == "win32" else "bin",
            "python.exe" if sys.platform == "win32" else "python",
        )
        if os.path.exists(venv_py):
            py_exe = venv_py
        script = os.path.join(CURRENT_DIR, "remove_ads.py")
        model = self.llm_model_var.get() or "glm-5.2"
        provider_id = self.vendor_map.get(self.vendor_var.get(), "")
        cmd = [py_exe, script, target, "--model", model]
        if provider_id:
            cmd += ["--provider", provider_id]
        cmd += [
            "--detector", self.ad_detector_var.get(),
            "--cut-mode", self.ad_cut_mode_var.get(),
        ]
        print(f"📺 [GUI] Launching remove_ads: {' '.join(cmd)}")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        threading.Thread(target=self._remove_ads_worker, args=(cmd,), daemon=True).start()

    def _remove_ads_worker(self, cmd):
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1)
            self._register_process(proc)
            self.root.after(0, lambda: self.pause_btn.config(state="normal"))
            if proc.stdout:
                for line in proc.stdout:
                    self.root.after(0, self.log, line.strip())
            ret = proc.wait()
            if ret == 0:
                self.root.after(0, lambda: self.status_label.config(text="✅ 广告剪除完成"))
            else:
                self.root.after(0, lambda: self.status_label.config(text="❌ 广告剪除失败"))
        except Exception as e:
            self.root.after(0, lambda: self.log(f"❌ 广告剪除异常: {e}"))
        finally:
            if proc is not None:
                self._unregister_process(proc)
            self._prune_finished_processes()
            self.root.after(0, lambda: self.start_btn.config(state="normal"))
            self.root.after(0, lambda: self.stop_btn.config(state="disabled"))
            self.root.after(0, lambda: self.pause_btn.config(state="disabled", text="暂停 (Pause)"))

    def save_settings(self):
        data = {
            "input": self.input_var.get(),
            "output_dir": self.output_dir_var.get(),
            "vendor": self.vendor_var.get(),
            "llm_vendor": self.vendor_var.get(),
            "llm_model": self.llm_model_var.get(),
            "trans_mode": self.trans_mode_var.get(),
            "quality": self.quality_var.get(),
            "layout": self.layout_var.get(),
            "main_lang": self.main_lang_var.get(),
            "cn_font": self.cn_font_var.get(),
            "jp_font": self.jp_font_var.get(),
            "jp_size": self.jp_size_var.get(),
            "en_font": self.en_font_var.get(),
            "cn_size": self.cn_size_var.get(),
            "en_size": self.en_size_var.get(),
            "cn_color": self.cn_color_var.get(),
            "en_color": self.en_color_var.get(),
            "bg_box": self.bg_box_var.get(),
            "api_key": self.api_key_var.get(),
            "quick": self.quick_var.get(),
            "multilang": self.multilang_var.get(),
            "remove_ads": self.remove_ads_var.get(),
            "ad_detector": self.ad_detector_var.get(),
            "ad_cut_mode": self.ad_cut_mode_var.get(),
            "sync_gdrive": self.sync_gdrive_var.get(),
            "gdrive_enabled": self.gdrive_enabled_var.get(),
            "wechat_enabled": self.wechat_enabled_var.get(),
            "wechat_public": self.wechat_public_var.get(),
            "vendor_default_models": self.vendor_default_models
        }
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            messagebox.showinfo("成功", "当前配置已保存为默认设置。")
        except Exception as e:
            messagebox.showerror("错误", f"保存失败: {str(e)}")

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    root = tk.Tk()
    # Set app icon early (before any widgets) so Cmd+Tab switcher picks it up
    try:
        from mac_icon import set_macos_app_icon
        _icon_name = "autosub_gui_neon.png" if sys.platform == "darwin" else "autosub.ico"
        set_macos_app_icon(root, os.path.join(CURRENT_DIR, _icon_name))
    except Exception:
        pass
    app = AutoSubGUI(root)
    root.mainloop()
