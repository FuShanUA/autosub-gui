import os
import sys

# Add common Mac Homebrew paths to PATH to ensure ffmpeg and other tools are found by child processes
for path in ["/opt/homebrew/bin", "/usr/local/bin"]:
    if os.path.exists(path) and path not in os.environ.get("PATH", ""):
        os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")

import re
import unicodedata
import time
import json
import math
import threading
import queue
import subprocess
import argparse
import shutil
import glob
import logging
import signal


def _venv_site_packages():
    """Absolute path to this project's venv site-packages, or None."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.join(here, ".venv")
    windows_layout = os.path.join(root, "Lib", "site-packages")
    if os.path.isdir(windows_layout):
        return windows_layout
    lib = os.path.join(root, "lib")
    if os.path.isdir(lib):
        for entry in sorted(os.listdir(lib)):
            candidate = os.path.join(lib, entry, "site-packages")
            if os.path.isdir(candidate):
                return candidate
    return None


def _import_psutil():
    """Import psutil, falling back to the project venv's site-packages.

    The macOS .app hands the engine a copied interpreter whose PYTHONPATH the
    launcher strips, so a plain import resolves against the system
    site-packages and psutil goes missing. Orphan cleanup, child cleanup, the
    pause kill and the single-instance check all test `psutil is None` or call
    `psutil.Process` inside a bare except, so the engine keeps running with
    every one of them silently disabled: a wedged upload is never killed, never
    cleaned up on restart, and holds its serial slot until someone intervenes.
    """
    try:
        import psutil
        return psutil
    except ModuleNotFoundError:
        pass
    site_packages = _venv_site_packages()
    if site_packages and site_packages not in sys.path:
        sys.path.append(site_packages)
        try:
            import psutil
            return psutil
        except ModuleNotFoundError:
            pass
    return None


psutil = _import_psutil()
try:
    import msvcrt
except ImportError:
    class _msvcrt_mock:
        def kbhit(self): return False
        def getch(self): return b''
        def putch(self, char): pass
    msvcrt = _msvcrt_mock()
import atexit
import ctypes

# Reasoning models can take longer than 30 seconds to generate upload metadata.
# Keep retries low, but allow the request itself enough time to finish.
os.environ.setdefault("LLM_REQUEST_TIMEOUT", "300")
os.environ.setdefault("LLM_MAX_RETRIES", "1")

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
import select
try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None

_old_termios_settings = None

def init_terminal():
    global _old_termios_settings
    if sys.platform != "win32" and termios and tty:
        try:
            fd = sys.stdin.fileno()
            _old_termios_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except Exception:
            pass

def restore_terminal():
    global _old_termios_settings
    if sys.platform != "win32" and _old_termios_settings and termios:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _old_termios_settings)
        except Exception:
            pass

atexit.register(restore_terminal)

def check_kbhit():
    if sys.platform == "win32":
        return msvcrt and msvcrt.kbhit()  # type: ignore
    else:
        try:
            fd = sys.stdin.fileno()
            dr, _, _ = select.select([fd], [], [], 0.02)
            return len(dr) > 0
        except Exception:
            return False

def read_char():
    """Read a single byte from stdin. Uses raw os.read to avoid text-mode buffering deadlocks on pipes."""
    if sys.platform == "win32":
        return msvcrt.getch() if msvcrt else b''  # type: ignore
    else:
        try:
            b = os.read(sys.stdin.fileno(), 1)
            return b if b else b''
        except Exception:
            return b''

def _read_escape_seq():
    """Try to read an ANSI escape sequence after ESC byte. Returns scroll direction or None.
    Handles both CSI (\x1b[...) and SS3 (\x1bO...) sequences safely."""
    try:
        fd = sys.stdin.fileno()
        # Wait briefly for the sequence introducer to arrive
        dr, _, _ = select.select([fd], [], [], 0.05)
        if not dr:
            return None
        ch2 = os.read(fd, 1)

        if ch2 == b'O':
            # SS3 sequence (F1-F4 on some terminals): \x1bOP, \x1bOQ, etc.
            # Consume the next byte (the function letter) and discard
            dr, _, _ = select.select([fd], [], [], 0.05)
            if dr:
                os.read(fd, 1)  # consume and discard
            return None

        if ch2 != b'[':
            # Not a recognized sequence start — just discard ch2
            return None

        # CSI sequence: \x1b[ followed by optional params and a final letter or '~'
        dr, _, _ = select.select([fd], [], [], 0.05)
        if not dr:
            return None
        ch3 = os.read(fd, 1)

        if ch3 == b'A':
            return 'up'
        elif ch3 == b'B':
            return 'down'

        # Consume remaining bytes of the sequence until a terminator (letter or ~)
        if ch3 in (b'~',):
            return None  # Already terminated (e.g., \x1b[~ edge case)

        # For sequences like \x1b[5~ (PgUp), \x1b[3~ (Delete), \x1b[1;5A (Ctrl+Up)
        # ch3 is a digit or intermediate char — keep reading until terminator
        while True:
            dr, _, _ = select.select([fd], [], [], 0.02)
            if not dr:
                break
            extra = os.read(fd, 1)
            if not extra:
                break
            # Terminators: alphabetic characters or '~'
            if extra.isalpha() or extra == b'~':
                break
        return None
    except Exception:
        return None

import contextlib
from datetime import datetime
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field, asdict

# UI & Tools
from rich.console import Console, Group
from rich.cells import cell_len
from rich.table import Table
from rich.live import Live
from rich.text import Text
from rich import box


# Set AppUserModelID for taskbar icon grouping
if sys.platform == "win32":
    windll = getattr(ctypes, 'windll', None)
    if windll:
        windll.shell32.SetCurrentProcessExplicitAppUserModelID("cc.autosub.batch.pro")

# --- 路径配置 ---
CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))
if getattr(sys, 'frozen', False):
    TOOLS_DIR = os.path.join(getattr(sys, '_MEIPASS', ''), "Library", "Tools")
else:
    TOOLS_DIR = os.environ.get("AUTOSUB_TOOLS_DIR") or os.path.dirname(CURRENT_DIR)
# Propagate to child processes (smart_translate, transcriber, etc.) so they
# resolve TOOLS_DIR the same way instead of relying on __file__ relative paths.
os.environ.setdefault("AUTOSUB_TOOLS_DIR", TOOLS_DIR)
PROJECT_ROOT = os.path.dirname(os.path.dirname(TOOLS_DIR))
DEFAULT_OUTPUT_ROOT = os.path.join(PROJECT_ROOT, "Projects")

# Reuse the canonical Bilibili upload API from the base autosub module.
sys.path.insert(0, CURRENT_DIR)
from autosub import (  # noqa: E402
    DEFAULTS,
    VENDOR_MAP,
    build_bili_cmd,
    build_wechat_cmd,
    get_bili_login_command,
    get_bili_login_skip_reason,
    get_wechat_duration_skip_reason,
    get_wechat_upload_skip_reason,
    get_upload_login_failure_hint,
    run_bili_cmd,
    run_wechat_cmd,
    prepare_bili_upload_metadata,
    prepare_wechat_upload_metadata,
    UPLOAD_STALL_TIMEOUT,
    UPLOAD_SILENCE_TIMEOUT,
)

BROWSER_COOKIE_NAMES = {"chrome", "firefox", "safari", "edge"}

# --- 核心工具指令 ---
VENV_DIR = os.path.join(CURRENT_DIR, ".venv")
if sys.platform == "win32":
    VENV_BIN = os.path.join(VENV_DIR, "Scripts")
    VENV_PYTHON = os.path.join(VENV_BIN, "python.exe")
else:
    VENV_BIN = os.path.join(VENV_DIR, "bin")
    VENV_PYTHON = os.path.join(VENV_BIN, "python")

PYTHON_EXE = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable

VDOWN_CMD = [PYTHON_EXE, os.path.join(TOOLS_DIR, "vdown", "download.py")]
TRANSCRIBER_CMD = [PYTHON_EXE, os.path.join(TOOLS_DIR, "transcriber", "transcribe_engine.py")]
SMART_TRANSLATE_CMD = [PYTHON_EXE, os.path.join(TOOLS_DIR, "autosub", "smart_translate.py")]
SUBTRANSLATOR_CMD = [PYTHON_EXE, os.path.join(TOOLS_DIR, "subtranslator", "subtranslator.py")]
SRT2ASS_CMD = [PYTHON_EXE, os.path.join(TOOLS_DIR, "hardsubber", "srt_to_ass.py")]
BURNSUB_CMD = [PYTHON_EXE, os.path.join(TOOLS_DIR, "hardsubber", "burn_engine.py")]
# Google Drive syncing is optional and requires an external helper script.
SYNC_SCRIPT = os.environ.get("AUTOSUB_GDRIVE_SYNC_SCRIPT", "")

# --- 日志配置 ---
LOG_DIR = os.path.join(DEFAULT_OUTPUT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(LOG_DIR, "autosub_batch_pro.log"),
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s',
    encoding='utf-8'
)
logger = logging.getLogger("AutoSubBatchPro")

# --- 工具查找 (FFmpeg 等) ---
def get_tool_path(tool_name="ffmpeg"):
    path = shutil.which(tool_name)
    if path: return path
    
    exe_ext = ".exe" if sys.platform == "win32" else ""
    
    # Mac specific paths
    if sys.platform == "darwin":
        mac_paths = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]
        for p in mac_paths:
            full_p = os.path.join(p, tool_name)
            if os.path.exists(full_p): return full_p

    user_home = os.path.expanduser("~")
    winget_base = os.path.join(user_home, "AppData", "Local", "Microsoft", "Winget", "Packages")
    if os.path.exists(winget_base):
        for d in os.listdir(winget_base):
            if tool_name in d:
                for bin_dir in glob.glob(os.path.join(winget_base, d, "**/bin"), recursive=True):
                    p = os.path.join(bin_dir, f"{tool_name}{exe_ext}")
                    if os.path.exists(p): return p
    
    fallbacks = [r"C:\ffmpeg\bin", r"D:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin"]
    for fb in fallbacks:
        p = os.path.join(fb, f"{tool_name}{exe_ext}")
        if os.path.exists(p): return p
    return tool_name

FFMPEG_EXE = get_tool_path("ffmpeg")
FFPROBE_EXE = get_tool_path("ffprobe")

def extract_vid(url):
    # Support more patterns (YouTube, X, etc.)
    yt_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', url)
    if yt_match: return yt_match.group(1)
    # X/Twitter: extract status ID as a fallback hint. NOTE: the status ID
    # in the URL is NOT the same as yt-dlp's video ID (%(id)s). The correct
    # video ID should come from yt-dlp metadata (entry.get('id')); this
    # function is only a last-resort fallback when metadata is unavailable.
    x_match = re.search(r'/status/(\d+)', url)
    if x_match: return x_match.group(1)
    return url.split('/')[-1].split('?')[0]


def clean_title_text(title, playlist_title=None):
    if not title: return ""
    title = title.strip()
    for suffix in [" - YouTube", " | YouTube"]:
        if title.endswith(suffix):
            title = title[:-len(suffix)].strip()

    if playlist_title and playlist_title.lower() != title.lower():
        escaped_pt = re.escape(playlist_title)
        marker = "<_MARK_>"
        temp_title = re.sub(f'(?i){escaped_pt}', marker, title)
        if temp_title != title:
            temp_title = re.sub(rf'\s*[-|:/]\s*{marker}\s*[-|:/]\s*', ' - ', temp_title)
            temp_title = re.sub(rf'^{marker}\s*[-|:/]\s*', '', temp_title)
            temp_title = re.sub(rf'\s*[-|:/]\s*{marker}$', '', temp_title)
            temp_title = temp_title.replace(marker, '').strip()
            if temp_title:
                title = temp_title

    return title

_ZERO_WIDTH_RE = re.compile(
    r'[\u200B-\u200F\u202A-\u202E\u2060-\u206F\uFE00-\uFE0F\u200D\u00AD]'
)

def sanitize_title(s: str) -> str:
    """Strip zero-width chars and replace ambiguous-width Unicode punctuation
    with ASCII equivalents so rich's cell_len matches terminal render width."""
    s = _ZERO_WIDTH_RE.sub('', str(s))
    for old, new in {'\u2014': '--', '\u2013': '-', '\u2019': "'",
                      '\u2018': "'", '\u201c': '"', '\u201d': '"',
                      '\u2026': '...'}.items():
        s = s.replace(old, new)
    return s

def cjk_display_width(s: str) -> int:
    """Calculate terminal display width using rich's cell_len.
    Ensures width calculation matches rich's internal table rendering."""
    return cell_len(s)


def cjk_truncate(s: str, max_width: int, ellipsis: str = '...') -> str:
    """Truncate a string to fit within max_width terminal display cells.
    Uses rich's cell_len for width calculation to match table rendering."""
    s = sanitize_title(str(s))
    if cell_len(s) <= max_width:
        return s
    ellipsis_w = cell_len(ellipsis)
    target = max_width - ellipsis_w
    if target <= 0:
        return ellipsis
    result = []
    w = 0
    for c in s:
        cw = cell_len(c)
        if cw == 0:
            result.append(c)
            continue
        if w + cw > target:
            break
        result.append(c)
        w += cw
    return ''.join(result) + ellipsis


def parse_duration_to_seconds(dur_str):
    if not dur_str: return 0
    if ":" in dur_str:
        try:
            parts = dur_str.split(":")
            if len(parts) == 2: # MM:SS
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3: # HH:MM:SS
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except: pass

    # Fallback: handle "1h 5m" or "5m 2s" format
    try:
        total = 0
        h_match = re.search(r'(\d+)h', dur_str)
        m_match = re.search(r'(\d+)m', dur_str)
        s_match = re.search(r'(\d+)s', dur_str)
        if h_match: total += int(h_match.group(1)) * 3600
        if m_match: total += int(m_match.group(1)) * 60
        if s_match: total += int(s_match.group(1))
        return total
    except: pass
    return 0

def parse_indices(input_str, total_count):
    """
    Parses user input for indices.
    Supports:
    - Exclusion: "1,3,5" (default)
    - Inclusion: "+2,4,6" or "i2,4,6"
    - Ranges: "1-5", "+1-5"
    Returns a set of indices to KEEP.
    """
    input_str = input_str.strip()
    if not input_str:
        return set(range(1, total_count + 1))

    # Robust inclusion detection: check if it starts with + or i (ignoring spaces)
    is_inclusion = input_str.startswith('+') or input_str.lower().startswith('i')

    # Strip all leading non-digits except '-' for ranges
    clean_str = re.sub(r'^[+iI\s]+', '', input_str)

    parts = [p.strip() for p in clean_str.split(',') if p.strip()]
    selected = set()

    for p in parts:
        if '-' in p:
            try:
                # Handle potential spaces around '-'
                s_e = [x.strip() for x in p.split('-') if x.strip()]
                if len(s_e) == 2:
                    start, end = int(s_e[0]), int(s_e[1])
                    selected.update(range(start, end + 1))
            except: pass
        else:
            # Extract digits from the part
            m = re.search(r'(\d+)', p)
            if m:
                selected.add(int(m.group(1)))

    if is_inclusion:
        # Only keep these
        res = {i for i in selected if 1 <= i <= total_count}
        return res
    else:
        # Exclude these
        return {i for i in range(1, total_count + 1) if i not in selected}

# --- 数据模型 ---
STAGE_ORDER = ("DL", "TR", "TL", "MR", "BR", "GD", "MG", "BI", "WX")
STAGE_MARKERS = {
    "BR": ".burn_complete",
    "GD": ".synced",
    "BI": ".bili_uploaded",
    "WX": ".wechat_uploaded",
}


@dataclass
class SubTask:
    uid: int
    url: str
    vid_id: Optional[str] = None
    title: str = "加载中..."
    duration: str = "未知"
    status: str = "排队中"  # QUEUED, ACTIVE (STAGE), DONE, FAILED, PAUSED
    pcts: Dict[str, float] = field(default_factory=lambda: {
        "DL": 0.0, "TR": 0.0, "TL": 0.0, "MR": 0.0, "BR": 0.0,
        "GD": 0.0, "MG": 0.0, "BI": 0.0, "WX": 0.0,
    })
    workdir: Optional[str] = None
    error: Optional[str] = None
    pid: Optional[int] = None
    is_paused: bool = False
    sub_dir: Optional[str] = None
    playlist_index: Optional[int] = None
    retries: int = 0
    wx_skip_reason: Optional[str] = None

    def to_dict(self):
        d = asdict(self)
        d['pid'] = None  # 不持久化 PID
        return d

    @classmethod
    def from_dict(cls, d):
        data = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        if isinstance(data.get("pcts"), dict):
            data["pcts"].setdefault("WX", 0.0)
            if "MG" not in data["pcts"]:
                # Old states did not track metadata generation separately. If a
                # platform upload already finished, metadata necessarily finished.
                data["pcts"]["MG"] = 100.0 if (
                    data.get("status") == "完成"
                    or data["pcts"].get("BI", 0.0) >= 100.0
                    or data["pcts"].get("WX", 0.0) >= 100.0
                ) else 0.0
        if (not data.get('vid_id')) and data.get('url'):
            data['vid_id'] = extract_vid(data['url'])
        # 强制清洗路径
        if data.get('workdir'):
            data['workdir'] = data['workdir'].strip().rstrip('. ')
        return cls(**data)


def is_wechat_over_limit_reason(reason: Optional[str]) -> bool:
    """Whether a WeChat upload stop reason is the platform duration limit."""
    value = str(reason or "")
    return "超过视频号" in value and "分钟限制" in value

# --- 子进程树控制 ---
# 上传子进程会再派生 chromium / ffmpeg，只杀父进程会留下占着浏览器 Profile 的
# 孤儿，下一次视频号上传因此卡死。psutil 缺失时也必须能杀干净，所以这里提供
# 一条不依赖 psutil 的兜底路径。
# 视频号上传脚本必须一并列出：它派生的 chromium 持有共享浏览器 Profile，
# 漏掉它会让重启后的第一个视频号上传卡在 Profile 锁上。
CHILD_ENGINE_MARKERS = (
    "transcribe_engine.py",
    "smart_translate.py",
    "burn_engine.py",
    "bili_upload.py",
    "wechat_upload.py",
    "sync.py",
)

def _descendant_pids(pid: int) -> List[int]:
    """Collect every descendant of pid, children first (deepest last)."""
    found: List[int] = []
    frontier = [pid]
    seen = {pid}
    while frontier:
        current = frontier.pop(0)
        try:
            if sys.platform == "win32":
                continue
            out = subprocess.run(
                ["pgrep", "-P", str(current)],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            continue
        for token in out.split():
            try:
                child = int(token)
            except ValueError:
                continue
            if child in seen:
                continue
            seen.add(child)
            found.append(child)
            frontier.append(child)
    return found


def kill_pid_tree(pid: Optional[int], terminate: bool = False) -> bool:
    """Signal a child process and all of its descendants. Returns True on success."""
    if not pid:
        return False
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False
        victims = proc.children(recursive=True)
        victims.append(proc)
        for victim in victims:
            try:
                victim.terminate() if terminate else victim.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return True

    sig = signal.SIGTERM if terminate else signal.SIGKILL
    victims = _descendant_pids(pid)
    victims.append(pid)
    signalled = False
    for victim in reversed(victims):
        try:
            os.kill(victim, sig)
            signalled = True
        except OSError:
            pass
    return signalled


def _pid_command(pid: int) -> str:
    """Best-effort command line for a pid, used when psutil is unavailable."""
    try:
        return subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


# --- 资源管理器 ---
class ResourceManager:
    def __init__(self):
        # 硬件探测
        import multiprocessing
        self.cpu_count = multiprocessing.cpu_count()
        self.has_cuda = False
        self.is_mac = sys.platform == "darwin"
        
        try:
            if sys.platform == "win32":
                windll = getattr(ctypes, 'windll', None)
                if windll:
                    self.has_cuda = bool(windll.kernel32.GetModuleHandleW("nvcuda.dll"))
        except: pass

        # 并发控制 (信号量)
        # On Mac, running two heavy local Whisper models concurrently will trigger kernel OOM killer (SIGKILL -9).
        # We enforce single-threaded transcription on macOS (tr_sem = 1) to guarantee absolute VRAM safety,
        # while keeping burnsub (br_sem) at default_heavy since it is CPU-heavy and doesn't trigger OOM.
        default_heavy = 2 if (self.has_cuda or self.is_mac) else 1
        tr_limit = 1 if self.is_mac else default_heavy
            
        self.tr_sem = threading.Semaphore(tr_limit)  # 转录 (计算密集)
        self.br_sem = threading.Semaphore(default_heavy)  # 压制 (计算密集)
        self.api_sem = threading.Semaphore(20) # LLM API (I/O密集)
        self.mr_sem = threading.Semaphore(20)  # 字幕合成 (极轻量)
        self.io_sem = threading.Semaphore(6)   # 下载/读取 (I/O密集)
        self.sync_sem = threading.Semaphore(4) # 同步 (I/O密集)
        self.bili_sem = threading.Semaphore(2) # B站上传 (I/O密集, 限2避免LLM元数据生成竞争B站API限流)
        self.wechat_sem = threading.Semaphore(1) # 视频号上传共用浏览器 Profile，必须串行
        # Total concurrency limiter: caps how many workers can be actively
        # running disk_truth / stage logic simultaneously (before stage-specific
        # semaphores kick in). Prevents thundering-herd on startup when dozens
        # of tasks all enter the worker loop at once.
        self.worker_sem = threading.Semaphore(4)

        self.lock = threading.Lock()
        self.global_pause = False

    def get_sem(self, stage: str):
        if stage == "TR": return self.tr_sem
        if stage == "BR": return self.br_sem
        if stage == "TL": return self.api_sem
        if stage == "MR": return self.mr_sem
        if stage == "GD": return self.sync_sem
        if stage == "BI": return self.bili_sem
        if stage == "WX": return self.wechat_sem
        return self.io_sem

# --- 核心引擎 ---
class BatchEngine:
    def __init__(self, output_dir=DEFAULT_OUTPUT_ROOT, sub_dir_name=None, state_file=None):
        self.output_dir = output_dir
        
        # --- Smart Project Discovery ---
        if not sub_dir_name and not state_file:
            # Try to find the most recently modified project folder with a batch_state.json
            latest_time = 0
            latest_project = None
            if os.path.exists(output_dir):
                for d in os.listdir(output_dir):
                    d_path = os.path.join(output_dir, d)
                    if os.path.isdir(d_path):
                        s_path = os.path.join(d_path, "batch_state.json")
                        if os.path.exists(s_path):
                            mtime = os.path.getmtime(s_path)
                            if mtime > latest_time:
                                latest_time = mtime
                                latest_project = d
            
            if latest_project:
                sub_dir_name = latest_project
                print(f"✨ Smart Resume: Auto-detected latest project [bold cyan]'{latest_project}'[/]")

        self.project_dir = os.path.join(output_dir, sub_dir_name) if sub_dir_name else output_dir
        self.state_file = state_file or (os.path.join(self.project_dir, "batch_state.json") if sub_dir_name else os.path.join(output_dir, "autosub_batch_pro_state.json"))
        os.makedirs(self.project_dir, exist_ok=True)

        self.res = ResourceManager()
        self.task_map: Dict[int, SubTask] = {}
        self.lock = threading.RLock()
        self.abort = False
        self.no_sequence = False

        # Initialize dashboard attributes before the single-instance lock check,
        # because Live Monitor Mode renders the Dashboard immediately.
        self.do_gdsync = False
        self.do_bili = False
        self.do_wechat = False
        self.wechat_public = False
        self.gdsync_id = os.environ.get("AUTOSUB_GDRIVE_FOLDER_ID", "")
        self.playlist_title = sub_dir_name or "AutoSub Batch Pro"
        self.quality = "high"
        self.active_workers: Set[int] = set()

        # Single instance lock for the project
        self.lock_file = os.path.join(self.project_dir, "batch_engine.lock")
        if os.path.exists(self.lock_file):
            try:
                with open(self.lock_file, "r") as f:
                    old_pid = int(f.read().strip())
                running = False
                if psutil is not None and psutil.pid_exists(old_pid):
                    try:
                        p = psutil.Process(old_pid)
                        cmdline = p.cmdline()
                        if any("autosub_batch_pro.py" in arg for arg in cmdline):
                            import time as _time
                            create_time = p.create_time()
                            if (_time.time() - create_time) < 86400:
                                running = True
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        running = False
                elif psutil is None and old_pid != os.getpid() and "autosub_batch_pro.py" in _pid_command(old_pid):
                    # Same check through `ps`. Without it a live engine is never
                    # detected, the read-only Live Monitor never engages, and two
                    # engines can drive one project at the same time.
                    running = True
                if running and old_pid != os.getpid():
                    print(f"✨ Active Batch Launcher instance detected (PID {old_pid}) running for this project.")
                    print("📊 Entering read-only Live Monitor Mode. Press Ctrl+C to exit without disturbing the running task.\n")
                    time.sleep(2.0)
                    
                    # Read-only Live loop
                    dash = Dashboard(self)
                    from rich.live import Live
                    try:
                        with Live(dash.render(), console=dash.console, screen=True, refresh_per_second=2) as live:
                            while True:
                                # Reload state from disk to see updates from the active background process
                                self.load_state_monitor()
                                live.update(dash.render())
                                time.sleep(0.5)
                    except KeyboardInterrupt:
                        pass
                    sys.exit(0)
            except Exception as e:
                import traceback
                print(f"Error starting Live Monitor: {e}")
                traceback.print_exc()
        
        try:
            with open(self.lock_file, "w") as f:
                f.write(str(os.getpid()))
            atexit.register(self._clean_engine_lock)
        except Exception as e:
            pass

        # Cookie Discovery
        self.cookie_path = self._discover_cookies()

        # Node Discovery
        self.node_exe = self._discover_node()

        # Clean up orphaned child processes belonging to this project folder
        try:
            self.cleanup_orphans()
        except Exception as e:
            logger.error(f"Error during orphaned process cleanup: {e}")

        # Start background pause monitor thread to immediately catch and kill paused sub-processes
        threading.Thread(target=self._pause_monitor, daemon=True).start()

    def cleanup_orphans(self):
        """Scan running processes and forcefully kill any orphaned child engines
        belonging to this project (transcribe_engine, smart_translate, burn_engine)
        to prevent resource leaks and concurrent execution of duplicate engines."""
        logger.info("🧹 Scanning for orphaned child engines from previous runs...")
        cleaned_count = 0
        current_pid = os.getpid()

        project_path_abs = os.path.abspath(self.project_dir)

        for pid, cmdline_str in self._iter_process_table():
            if pid == current_pid or not cmdline_str:
                continue
            if not any(x in cmdline_str for x in CHILD_ENGINE_MARKERS):
                continue
            # Check if the process belongs to this project
            # 1. By checking if the project folder is mentioned in the command line
            belongs_to_project = project_path_abs in os.path.abspath(cmdline_str)
            # 2. Or by checking if the process current working directory is inside our project folder
            if not belongs_to_project and self._pid_cwd_in_project(pid, project_path_abs):
                belongs_to_project = True
            if not belongs_to_project:
                continue
            logger.info(f"💀 Found orphaned child process PID {pid}: '{cmdline_str}'. Force killing...")
            if kill_pid_tree(pid):
                cleaned_count += 1
            else:
                logger.error(f"Failed to kill orphaned process {pid}")

        if cleaned_count > 0:
            logger.info(f"✨ Successfully cleaned up {cleaned_count} orphaned child engine(s).")
        else:
            logger.info("✅ No orphaned child engines found.")

    def _iter_process_table(self):
        """Yield (pid, command line) for every visible process.

        psutil is preferred. When it is missing the scan still has to run: a
        leftover wechat_upload.py from the previous session keeps the shared
        browser profile locked, and the next WeChat upload then wedges on that
        lock while holding the only WeChat slot, which parks every other task at
        "queued". `ps -axo` gives the same command lines without the dependency.
        """
        if psutil is not None:
            for proc in psutil.process_iter(['pid', 'cmdline']):
                try:
                    cmdline = proc.info.get('cmdline')
                    if cmdline:
                        yield proc.info['pid'], " ".join(cmdline)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            return
        logger.warning("psutil is unavailable; falling back to `ps` for the orphan scan")
        try:
            out = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                capture_output=True, text=True, timeout=20,
            ).stdout
        except Exception as e:
            logger.error(f"Process table unavailable; orphaned process cleanup skipped: {e}")
            return
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            pid_str, _, command = line.partition(" ")
            try:
                yield int(pid_str), command.strip()
            except ValueError:
                continue

    def _pid_cwd_in_project(self, pid: int, project_path_abs: str) -> bool:
        """psutil-only secondary belonging check: process cwd inside the project."""
        if psutil is None:
            return False
        try:
            return os.path.abspath(psutil.Process(pid).cwd()).startswith(project_path_abs)
        except Exception:
            return False

    def _pause_monitor(self):
        """Background thread that periodically scans tasks and forcefully kills
        sub-processes of paused tasks to guarantee immediate responsive pausing
        even if the worker thread is blocked on a pipe read."""
        while not self.abort:
            time.sleep(1.0)
            try:
                # Read state file to pick up any external pause changes from Launcher/GUI
                if os.path.exists(self.state_file):
                    try:
                        with open(self.state_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        tasks_source = data.get("tasks", data) if isinstance(data, dict) else {}
                        if isinstance(tasks_source, dict):
                            with self.lock:
                                for uid_str, d in tasks_source.items():
                                    # Skip non-task keys like "global_pause" that live alongside task entries
                                    # in the flat batch_state.json format
                                    if uid_str == "global_pause":
                                        continue
                                    if not isinstance(d, dict):
                                        continue
                                    try:
                                        uid = int(uid_str)
                                    except ValueError:
                                        continue
                                    if uid in self.task_map:
                                        self.task_map[uid].is_paused = d.get("is_paused", False)
                                # Also sync global pause — works for both nested and flat formats
                                global_p = data.get("global_pause", False)
                                if global_p != self.res.global_pause:
                                    self.res.global_pause = global_p
                    except:
                        pass

                # Kill any active processes that are marked as paused
                with self.lock:
                    for task in list(self.task_map.values()):
                        if (task.is_paused or self.res.global_pause) and task.pid is not None:
                            logger.info(f"[Pause Monitor] Force killing process {task.pid} for paused task {task.uid}")
                            kill_pid_tree(task.pid)
                            task.pid = None
            except Exception as e:
                logger.error(f"[Pause Monitor] Error in background monitor loop: {e}")

    def _discover_node(self):
        """Finds node for yt-dlp JS challenges."""
        path = shutil.which("node")
        if path: return path

        possible_paths = []
        if sys.platform == "win32":
            possible_paths = [
                os.path.join(TOOLS_DIR, "vdown", "node.exe"),
                r"C:\Program Files\nodejs\node.exe",
                r"D:\Program Files\nodejs\node.exe",
            ]
        elif sys.platform == "darwin":
            possible_paths = [
                "/opt/homebrew/bin/node",
                "/usr/local/bin/node",
                "/usr/bin/node",
            ]

        for p in possible_paths:
            if os.path.exists(p): return p
        return "node"

    def _discover_cookies(self):
        """Finds cookies.txt in prioritized locations."""
        # 1. Try settings.json
        try:
            settings_path = os.path.join(CURRENT_DIR, "settings.json")
            if os.path.exists(settings_path):
                with open(settings_path, "r", encoding="utf-8") as f:
                    settings = json.load(f)
                    cookie_val = settings.get("cookie_path") or settings.get("cookies")
                    if cookie_val:
                        if cookie_val in ["chrome", "firefox", "safari", "edge"]:
                            return cookie_val
                        if os.path.exists(cookie_val):
                            return cookie_val
        except: pass

        # 2. Fallback to browser on Mac (Prioritized to avoid invalid cookies.txt)
        if sys.platform == "darwin":
            return "chrome"

        # 3. Dynamic priority paths
        paths = [
            os.path.join(CURRENT_DIR, "cookies.txt"),
            os.path.join(TOOLS_DIR, "vdown", "cookies.txt")
        ]
        for p in paths:
            if os.path.exists(p): return p
        return ""

    def _load_global_settings(self) -> dict:
        """Loads model and style from settings.json or defaults.json."""
        s_path = os.path.join(CURRENT_DIR, "settings.json")
        d_path = os.path.join(CURRENT_DIR, "defaults.json")
        
        target = s_path if os.path.exists(s_path) else d_path
        if os.path.exists(target):
            try:
                with open(target, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except: pass
        return {
            "llm_model": DEFAULTS.get("llm_model", "glm-5.2"),
            "llm_vendor": DEFAULTS.get("llm_vendor", DEFAULTS.get("vendor", "")),
            "style": "auto",
        }

    def _get_upload_llm_settings(self):
        """Resolve the main AutoSub model/provider for upload metadata."""
        settings = self._load_global_settings()
        model = settings.get("llm_model") or DEFAULTS.get("llm_model") or "glm-5.2"
        vendor = (
            settings.get("llm_vendor")
            or settings.get("vendor")
            or DEFAULTS.get("llm_vendor")
            or DEFAULTS.get("vendor")
            or ""
        )
        provider = VENDOR_MAP.get(vendor, str(vendor).strip().lower())
        return model, provider

    @contextlib.contextmanager
    def use_temp_cookies(self, cookies_path):
        """Creates a temporary copy of cookies to prevent concurrent write corruption."""
        if not cookies_path or cookies_path in ["chrome", "firefox", "safari", "edge"] or not os.path.exists(cookies_path):
            yield None
            return
        import tempfile
        fd, temp_path = tempfile.mkstemp(suffix=".txt", prefix="yt_cookies_")
        os.close(fd)
        try:
            shutil.copy2(cookies_path, temp_path)
            yield temp_path
        finally:
            try:
                if os.path.exists(temp_path): os.remove(temp_path)
            except: pass
    def _clean_engine_lock(self):
        try:
            if hasattr(self, "lock_file") and os.path.exists(self.lock_file):
                os.remove(self.lock_file)
        except:
            pass

    def kill_children(self):
        """Kill all child subprocesses on exit to prevent orphaned processes."""
        if psutil is None:
            # Without psutil we cannot read a child's command line reliably, and
            # the engine is already on its way out: anything still parented to it
            # is ours. Kill the whole tree so chromium does not survive holding
            # the shared WeChat browser profile.
            logger.warning("psutil is unavailable; killing descendants via pgrep")
            for pid in reversed(_descendant_pids(os.getpid())):
                logger.info(f"Killing child on exit: PID {pid} ({_pid_command(pid)[:60]})")
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            return
        try:
            parent = psutil.Process(os.getpid())
            for child in parent.children(recursive=True):
                try:
                    cmdline = " ".join(child.cmdline())
                    if any(x in cmdline for x in CHILD_ENGINE_MARKERS):
                        logger.info(f"Killing child on exit: PID {child.pid} ({cmdline[:60]})")
                        # Tree kill: the uploader's chromium/ffmpeg grandchildren
                        # do not match the markers but must not survive either.
                        kill_pid_tree(child.pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception as e:
            logger.error(f"Error during child cleanup: {e}")

    def shutdown(self):
        """Stop children, normalize active task state, then persist it.

        Saving before killing children used to leave "running" statuses behind
        when the Launcher was closed manually. Those statuses then looked live
        after restart even though no process existed.
        """
        self.abort = True
        self.kill_children()

        deadline = time.time() + 2.0
        while self.active_workers and time.time() < deadline:
            time.sleep(0.05)

        with self.lock:
            for task in self.task_map.values():
                if not task.status.startswith("运行中"):
                    continue
                stage = task.status[3:].strip()
                if stage.startswith("(") and stage.endswith(")"):
                    stage = stage[1:-1]
                task.status = f"排队中 ({stage})" if stage else "排队中"
                task.pid = None
                task.error = None
        self.save_state()

    def save_state(self):
        with self.lock:
            # 兼容 Launcher 格式：如果是 dict 形式的 map
            tasks_data = {str(t.uid): t.to_dict() for t in self.task_map.values()}
            data = {"tasks": tasks_data, "time": time.time(), "global_pause": self.res.global_pause}
            cookie_state = {}
            if self.cookie_path in BROWSER_COOKIE_NAMES:
                cookie_state["browser"] = self.cookie_path
            elif self.cookie_path and os.path.exists(self.cookie_path):
                cookie_state["cookies"] = self.cookie_path
        try:
            # CRITICAL: Always save global_pause even in flat batch_state.json format,
            # so _pause_monitor can correctly sync global_pause from the state file.
            # For batch_state.json, embed global_pause alongside the flat task map.
            if "batch_state.json" in self.state_file:
                flat_data = {**tasks_data, "global_pause": self.res.global_pause,
                             "bili": self.do_bili, "gdsync": self.do_gdsync,
                             "wechat": self.do_wechat, "wechat_public": self.wechat_public,
                             **cookie_state}
                with open(self.state_file, "w", encoding="utf-8") as f:
                    json.dump(flat_data, f, ensure_ascii=False, indent=2)
            else:
                with open(self.state_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except: pass

    def _sync_marker_progress(self, task: SubTask):
        """Restore persisted progress from completion marker files."""
        if not task.workdir:
            return
        for stage, marker in STAGE_MARKERS.items():
            if os.path.exists(os.path.join(task.workdir, marker)):
                task.pcts[stage] = 100.0

    def _stage_is_complete(self, task: SubTask, stage: str) -> bool:
        if stage == "GD" and not self.do_gdsync:
            return True
        if stage == "MG" and not self.do_bili and not self.do_wechat:
            return True
        if stage == "BI" and not self.do_bili:
            return True
        if stage == "WX" and not self.do_wechat:
            return True

        marker = STAGE_MARKERS.get(stage)
        if marker and task.workdir and os.path.exists(os.path.join(task.workdir, marker)):
            return True

        if task.pcts.get(stage, 0.0) < 100.0:
            return False

        # A failed stage can retain a misleading 100% value. Keep it resettable
        # when the status explicitly names that stage as the failure.
        status_stage = ""
        if "(" in task.status:
            status_stage = task.status.split("(", 1)[1].split(")", 1)[0]
        if stage == status_stage and task.status.startswith(("失败", "报错")):
            return False
        return True

    def _first_unfinished_stage(self, task: SubTask) -> Optional[str]:
        """Derive the next unfinished stage from markers and progress."""
        self._sync_marker_progress(task)
        for stage in STAGE_ORDER:
            if not self._stage_is_complete(task, stage):
                return stage
        return None

    def reset_task(self, task: SubTask) -> Optional[str]:
        """Reset the next unfinished stage without discarding completed work."""
        with self.lock:
            stage = self._first_unfinished_stage(task)
            if stage is None:
                task.status = "完成"
                task.error = None
                task.retries = 0
                self.save_state()
                return None

            if stage in ("TR", "TL", "MR", "BR") and task.workdir and os.path.isdir(task.workdir):
                try:
                    files = os.listdir(task.workdir)
                    if stage == "TR":
                        for name in files:
                            if name.endswith(".srt") and not any(
                                ext in name for ext in (".zh", ".cn", ".bi")
                            ):
                                try: os.remove(os.path.join(task.workdir, name))
                                except OSError: pass
                    elif stage == "TL":
                        for name in files:
                            if ".zh.srt" in name or ".cn.srt" in name:
                                try: os.remove(os.path.join(task.workdir, name))
                                except OSError: pass
                    elif stage == "MR":
                        for name in files:
                            if ".bi.srt" in name:
                                try: os.remove(os.path.join(task.workdir, name))
                                except OSError: pass
                    elif stage == "BR":
                        for name in files:
                            if name.endswith(".ass") or name.endswith("_hardsub.mp4"):
                                try: os.remove(os.path.join(task.workdir, name))
                                except OSError: pass
                except OSError:
                    pass

            task.status = f"排队中 ({stage})"
            task.pcts[stage] = 0.0
            task.error = None
            task.retries = 0
            self.save_state()
            return stage

    def _refresh_wechat_skip(self, task: SubTask) -> Optional[str]:
        """Persist an early WeChat duration-limit decision from known duration."""
        if not self.do_wechat:
            return None

        duration_seconds = parse_duration_to_seconds(task.duration)
        reason = get_wechat_duration_skip_reason(duration_seconds)
        if reason:
            if task.wx_skip_reason != reason:
                task.wx_skip_reason = reason
                logger.info(
                    f"Task {task.uid} | WX | Early skip: {reason}"
                )
            return reason

        # A duration-limit decision goes stale once the cap is lifted or the
        # duration metadata is corrected. Drop it whenever the current duration no
        # longer produces one, otherwise the worker keeps re-marking the task 超限
        # and every manual retry dies before it starts. Other transient upload
        # failures are left untouched, and the worker re-derives the reason from
        # the real file before it decides to skip.
        if task.wx_skip_reason and is_wechat_over_limit_reason(task.wx_skip_reason):
            task.wx_skip_reason = None
        return task.wx_skip_reason

    def load_state(self):
        if not os.path.exists(self.state_file): return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 兼容两种格式：Launcher 的直接 map 和 Pro 的 data wrapper
            tasks_source = data.get("tasks", data) if isinstance(data, dict) else {}
            if not isinstance(tasks_source, dict): tasks_source = {}

            # Flat batch_state.json stores upload preferences alongside task IDs.
            # Restore them so a restart does not silently disable selected stages.
            if "batch_state.json" in os.path.basename(self.state_file):
                self.do_bili = bool(data.get("bili", self.do_bili))
                self.do_gdsync = bool(data.get("gdsync", self.do_gdsync))
                self.do_wechat = bool(data.get("wechat", self.do_wechat))
                self.wechat_public = bool(data.get("wechat_public", self.wechat_public))
                browser_cookie = data.get("browser")
                cookie_file = data.get("cookies") or data.get("cookie_path")
                if browser_cookie in BROWSER_COOKIE_NAMES:
                    self.cookie_path = browser_cookie
                elif cookie_file and os.path.exists(cookie_file):
                    self.cookie_path = cookie_file

            with self.lock:
                # 信号量物理重置：防止因之前崩溃导致的信号量泄露
                default_heavy = 2 if (self.res.has_cuda or self.res.is_mac) else 1
                tr_limit = 1 if self.res.is_mac else default_heavy
                self.res.tr_sem = threading.Semaphore(tr_limit)
                self.res.br_sem = threading.Semaphore(default_heavy)
                self.res.api_sem = threading.Semaphore(20)
                self.res.io_sem = threading.Semaphore(6)
                self.res.sync_sem = threading.Semaphore(4)

                for uid_str, d in tasks_source.items():
                    if not isinstance(d, dict): continue
                    t = SubTask.from_dict(d)
                    self.task_map[t.uid] = t

                    # Marker-file truth: if .synced/.bili_uploaded/.wechat_uploaded exist on disk,
                    # force pcts to 100% regardless of what the state file says.
                    # This fixes the pcts-vs-reality mismatch after batch_pro restarts.
                    if t.workdir:
                        if os.path.exists(os.path.join(t.workdir, ".synced")):
                            t.pcts["GD"] = 100.0
                        if os.path.exists(os.path.join(t.workdir, ".bili_uploaded")):
                            t.pcts["BI"] = 100.0
                        if os.path.exists(os.path.join(t.workdir, ".wechat_uploaded")):
                            t.pcts["WX"] = 100.0
                        if os.path.exists(os.path.join(t.workdir, ".burn_complete")):
                            t.pcts["BR"] = 100.0

                    if t.status.startswith("超限") and is_wechat_over_limit_reason(t.error):
                        t.wx_skip_reason = t.error
                    self._refresh_wechat_skip(t)

                    # Duration-limit skips are terminal, but they are not successes.
                    # Keep the persisted status/error so a restart does not requeue WX.
                    # Re-validate the limit before honoring it: the cap has been lifted,
                    # so a 超限 status persisted by an older run must fall through to the
                    # re-derivation below instead of blocking every later manual retry.
                    if (
                        t.status.startswith("超限")
                        and t.pcts.get("WX", 0.0) < 100.0
                        and get_wechat_duration_skip_reason(
                            parse_duration_to_seconds(t.duration)
                        )
                    ):
                        t.pid = None
                        continue

                    # 彻底自愈：不再解析可能损坏的 status 字符串，而是根据 pcts 数据重新推导
                    current_stage = "DL"
                    for s in STAGE_ORDER:
                        if t.pcts.get(s, 0.0) < 100.0:
                            current_stage = s
                            break
                    else:
                        current_stage = "完成"

                    if current_stage == "完成":
                        t.status = "完成"
                    elif t.is_paused:
                        t.status = f"暂停中 ({current_stage})"
                    else:
                        t.status = f"排队中 ({current_stage})"
                        t.error = None
                        t.retries = 0

                    t.pid = None # 启动时重置 PID
        except Exception as e:
            logger.error(f"Load state failed: {e}")

    def load_state_monitor(self):
        """Loads state directly from disk in read-only mode for monitor dashboard
        preserving actual live statuses (like '运行中') instead of resetting them."""
        if not os.path.exists(self.state_file): return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            tasks_source = data.get("tasks", data) if isinstance(data, dict) else {}
            if not isinstance(tasks_source, dict): tasks_source = {}
            with self.lock:
                for uid_str, d in tasks_source.items():
                    if not isinstance(d, dict): continue
                    t = SubTask.from_dict(d)
                    t.status = d.get("status", "未知")
                    self.task_map[t.uid] = t
        except Exception as e:
            pass

    def add_task(self, url, title=None, duration=None, uid=None, vid_id=None, sub_dir=None, playlist_index=None):
        with self.lock:
            new_uid = uid or (max(self.task_map.keys()) + 1 if self.task_map else 1)
            if any(t.url == url for t in self.task_map.values()): return

            task = SubTask(uid=new_uid, url=url, title=title or "未知视频", duration=duration or "未知", vid_id=vid_id, sub_dir=sub_dir, playlist_index=playlist_index)
            self._refresh_wechat_skip(task)
            # Set initial workdir if title is available
            if title:
                task.workdir = self.get_task_workdir(task)
            self.task_map[new_uid] = task

        # 异步获取元数据
        def async_meta():
            self.fetch_web_metadata(task)
            self.save_state()
            # 启动 worker
            self.worker(task)

        threading.Thread(target=async_meta, daemon=True).start()

    def get_task_workdir(self, task: SubTask):
        """Standardizes folder naming: '[xx] - Title [ID]'."""
        safe_title = re.sub(r'[\\/*?:"<>|]', '_', task.title).strip()[:80]
        vid_tag = task.vid_id if task.vid_id else "UnknownID"
        root = os.path.join(self.project_dir, task.sub_dir) if task.sub_dir else self.project_dir
        if not os.path.exists(root): os.makedirs(root, exist_ok=True)
        if getattr(self, "no_sequence", False):
            return os.path.join(root, f"{safe_title} [{vid_tag}]").strip().rstrip('. ')
        idx = task.playlist_index if task.playlist_index is not None else task.uid
        return os.path.join(root, f"[{idx:02d}] - {safe_title} [{vid_tag}]").strip().rstrip('. ')


    def get_duration(self, path):
        try:
            cmd = [FFPROBE_EXE, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
            out = subprocess.check_output(cmd, creationflags=CREATE_NO_WINDOW).decode().strip()
            s = int(float(out))
            if s <= 0: return "未知"
            return f"{s//60:02d}:{s%60:02d}"
        except: return "未知"

    def get_video_dimensions(self, path):
        """Returns (width, height) using ffprobe."""
        try:
            cmd = [FFPROBE_EXE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path]
            out = subprocess.check_output(cmd, creationflags=CREATE_NO_WINDOW).decode().strip()
            if "x" in out:
                w, h = out.split("x")
                return int(w), int(h)
        except: pass
        return 1920, 1080

    def fetch_web_metadata(self, task: SubTask):
        """Use download.py to fetch title and duration from the web."""
        try:
            # 1. Fetch Title
            if not task.title or task.title == "未知视频":
                cmd = list(VDOWN_CMD) + [task.url, self.cookie_path, "--get-title"]
                # download.py now handles JS runtime, but we ensure consistency here if needed
                out = subprocess.check_output(cmd, creationflags=CREATE_NO_WINDOW, timeout=30).decode('utf-8', errors='ignore').strip()
                if out and "Error" not in out:
                    task.title = clean_title_text(out, task.sub_dir)
                    # Update workdir after getting real title
                    task.workdir = self.get_task_workdir(task)

            # 2. Fetch Duration
            if not task.duration or task.duration == "未知":
                cmd = list(VDOWN_CMD) + [task.url, self.cookie_path, "--get-duration"]
                out = subprocess.check_output(cmd, creationflags=CREATE_NO_WINDOW, timeout=30).decode('utf-8', errors='ignore').strip()
                if out and out != "未知":
                    task.duration = out
                self._refresh_wechat_skip(task)

            # 3. Fetch vid_id (for X/Twitter, the URL status ID != yt-dlp video ID)
            # This is critical for folder lookup in disk_truth() to work correctly.
            if not task.vid_id or task.vid_id == "UnknownID":
                cmd = list(VDOWN_CMD) + [task.url, self.cookie_path, "", "--get-folder-name"]
                out = subprocess.check_output(cmd, creationflags=CREATE_NO_WINDOW, timeout=40).decode('utf-8', errors='ignore').strip()
                if out and "Error" not in out:
                    # Folder name format: "title [vid_id]" — extract the ID
                    lines = [l.strip() for l in out.splitlines() if l.strip()]
                    if lines:
                        folder_name = lines[-1]
                        if "[" in folder_name and "]" in folder_name:
                            real_vid_id = folder_name.split("[")[-1].rstrip("]")
                            if real_vid_id and real_vid_id != task.vid_id:
                                task.vid_id = real_vid_id
                                # Update workdir with correct vid_id
                                task.workdir = self.get_task_workdir(task)
                                logger.info(f"Task {task.uid} | Fetched real vid_id: {real_vid_id}")
        except Exception as e:
            logger.error(f"Task {task.uid} | fetch_web_metadata error: {e}")


    def disk_truth(self, task: SubTask):
        """物理文件扫描，决定任务当前该处于哪个环节。"""
        if task.status.startswith("超限"):
            return

        # 如果是暂停状态，保持文字，只做物理搜索
        is_p = task.is_paused or self.res.global_pause
        try:
            # 1. 判定当前 workdir 是否有效
            is_valid_dir = False
            if task.workdir and os.path.exists(task.workdir):
                try:
                    files_now = os.listdir(task.workdir)
                    if any(f.endswith((".mp4", ".mkv", ".webm", ".srt")) and not re.search(r'\.f\d+\.', f) and not f.endswith(".part") for f in files_now):
                        is_valid_dir = True
                except: pass

            # 2. 如果无效，则强制搜索更好的文件夹 (编号优先)
            if not is_valid_dir:
                search_pattern = f"[{task.uid:02d}]"
                vid_id = task.vid_id
                candidates = []
                if os.path.exists(self.project_dir):
                    for d in os.listdir(self.project_dir):
                        full_p = os.path.join(self.project_dir, d)
                        if not os.path.isdir(full_p): continue
                        if (vid_id and vid_id in d) or d.startswith(search_pattern):
                            try:
                                file_count = len(os.listdir(full_p))
                                candidates.append((full_p.strip().rstrip('. '), file_count))
                            except: pass
                if candidates:
                    candidates.sort(key=lambda x: x[1], reverse=True)
                    task.workdir = candidates[0][0]
                elif not task.workdir or "未知视频" in task.workdir:
                    task.workdir = self.get_task_workdir(task)

            if not task.workdir or not os.path.exists(task.workdir):
                task.status = "排队中 (DL)"
                logger.info(f"Finished syncing task {task.uid} (Dir not found)")
                return

            # 3. 补全时长 (阶梯策略: 原片 -> 成品 -> 元数据)
            # 只有当当前时长确实无效时，才进行扫描
            if not task.duration or task.duration in ["未知", "00:00"]:
                # A. 物理文件优先
                found_on_disk = False
                if os.path.exists(task.workdir):
                    files = os.listdir(task.workdir)
                    all_vids = [f for f in files if f.endswith((".mp4", ".mkv", ".webm")) and not re.search(r'\.f\d+\.', f) and not f.endswith(".part")]
                    if all_vids:
                        main_vids = [v for v in all_vids if "_hardsub" not in v]
                        target_vid = main_vids[0] if main_vids else all_vids[0]
                        dur = self.get_duration(os.path.join(task.workdir, target_vid))
                        if dur and dur != "未知":
                            task.duration = dur
                            found_on_disk = True
                            self._refresh_wechat_skip(task)

                # B. 物理文件未找到，尝试从 Web 获取
                if not found_on_disk:
                    # 避免在 disk_truth 中频繁调用网络，仅在空闲或必要时调用
                    # 我们在这里直接调用，因为 disk_truth 在 worker 中运行，是异步的
                    self.fetch_web_metadata(task)


            # 4. 判定环节状态
            files = os.listdir(task.workdir)
           
            # 前置检查：如果上传标记都已存在，直接判完成，无需检查中间产物
            has_bili_done = os.path.exists(os.path.join(task.workdir, ".bili_uploaded"))
            has_wechat_done = os.path.exists(os.path.join(task.workdir, ".wechat_uploaded"))
            has_synced = os.path.exists(os.path.join(task.workdir, ".synced"))
            has_hardsub = any("_hardsub" in f and f.endswith((".mp4", ".mkv", ".webm")) for f in files)
            has_burn_complete = os.path.exists(os.path.join(task.workdir, ".burn_complete"))
            
            # A task is complete when ALL enabled stages are done.
            # Base requirement: hardsub is complete.
            base_done = has_burn_complete
            
            # If upload stages are enabled, they must also be done.
            upload_done = (
                (not self.do_bili or has_bili_done)
                and (not self.do_gdsync or has_synced)
                and (not self.do_wechat or has_wechat_done)
            )
            
            if base_done and upload_done:
                task.status = "完成"
                for k in task.pcts: task.pcts[k] = 100.0
                task.error = None
                return

            # --- 优雅自愈与校验：如果存在 _hardsub 视频，校验其时长并维护 .burn_complete 状态 ---
            if has_hardsub:
                try:
                    hardsub_files = [f for f in files if "_hardsub" in f and f.endswith((".mp4", ".mkv", ".webm"))]
                    if hardsub_files:
                        hardsub_path = os.path.join(task.workdir, hardsub_files[0])
                        # Prefer the actual video file duration over task.duration
                        # (which comes from YouTube metadata and may be inaccurate)
                        orig_vids = [f for f in files if f.endswith((".mp4", ".mkv", ".webm")) and "_hardsub" not in f and not re.search(r'\.f\d+\.', f) and not f.endswith(".part")]
                        if orig_vids:
                            dur_orig = parse_duration_to_seconds(self.get_duration(os.path.join(task.workdir, orig_vids[0])))
                        else:
                            dur_orig = parse_duration_to_seconds(task.duration)
                        dur_hard = parse_duration_to_seconds(self.get_duration(hardsub_path))
                        is_complete = False
                        if dur_orig > 0 and dur_hard > 0:
                            if abs(dur_orig - dur_hard) <= 3:
                                is_complete = True
                        elif dur_hard > 0 and not any(f.endswith((".mp4", ".mkv", ".webm")) and "_hardsub" not in f for f in files):
                            # Original video is missing, but hardsub exists and has valid duration.
                            is_complete = True
                        
                        if is_complete and os.path.getsize(hardsub_path) > 1024 * 1024:
                            if not has_burn_complete:
                                with open(os.path.join(task.workdir, ".burn_complete"), "w", encoding="utf-8") as f:
                                    f.write("1")
                                has_burn_complete = True
                                logger.info(f"✨ Auto-repaired missing .burn_complete marker for task {task.uid} (found complete hardsub video)")
                        else:
                            # 毁尸灭迹：如果时长校验不通过，说明此前标记有误（可能是上次运行残留的假完成标记），必须强制删除以重新烧录
                            if has_burn_complete:
                                try:
                                    os.remove(os.path.join(task.workdir, ".burn_complete"))
                                except:
                                    pass
                                has_burn_complete = False
                                logger.info(f"🗑️ Destroyed invalid .burn_complete marker for task {task.uid} (duration mismatch)")
                except Exception as e:
                    logger.error(f"Error validating .burn_complete for task {task.uid}: {e}")

            if has_hardsub and has_burn_complete:
                need_gd = self.do_gdsync and not os.path.exists(os.path.join(task.workdir, ".synced"))
                need_bi = self.do_bili and not os.path.exists(os.path.join(task.workdir, ".bili_uploaded"))
                need_wx = self.do_wechat and not os.path.exists(os.path.join(task.workdir, ".wechat_uploaded"))
                pending_upload_stages = []
                if need_gd:
                    pending_upload_stages.append("GD")
                if need_bi or need_wx:
                    pending_upload_stages.append("MG")
                if pending_upload_stages:
                    task.status = f"排队中 ({'/'.join(pending_upload_stages)})"
                    _gd_pct = 100.0 if not need_gd else 0.0
                    _bi_pct = 100.0 if not need_bi else 0.0
                    _wx_pct = 100.0 if not need_wx else 0.0
                    _mg_pct = 100.0 if not (need_bi or need_wx) else 0.0
                    task.pcts.update({
                        "DL": 100, "TR": 100, "TL": 100, "MR": 100,
                        "BR": 100, "GD": _gd_pct, "MG": _mg_pct,
                        "BI": _bi_pct, "WX": _wx_pct,
                    })
                else:
                    task.status = "完成"
                    for k in task.pcts: task.pcts[k] = 100.0
                task.error = None
                return
            if any(".bi.srt" in f for f in files):
                task.status = "排队中 (BR)"
                task.pcts.update({"DL": 100, "TR": 100, "TL": 100, "MR": 100, "BR": 0.0, "GD": 0.0, "BI": 0.0})
                task.error = None
                return
            if any(re.search(r'(\.cn|\.zh)\.srt$', f) for f in files):
                task.status = "排队中 (MR)"
                task.pcts.update({"DL": 100, "TR": 100, "TL": 100, "MR": 0.0, "BR": 0.0, "GD": 0.0})
                task.error = None
                return
            if any(f.endswith(".srt") and not re.search(r'(\.zh|\.cn|\.bi)\.srt$', f) for f in files):
                task.status = "排队中 (TL)"
                task.pcts.update({"DL": 100, "TR": 100, "TL": 0.0, "MR": 0.0, "BR": 0.0, "GD": 0.0})
                task.error = None
                return
            if any(f.endswith((".mp4", ".mkv", ".webm")) and "_hardsub" not in f and not re.search(r'\.f\d+\.', f) and not f.endswith(".part") for f in files):
                task.status = "排队中 (TR)"
                
                # Check for transcription checkpoint
                ckpt = os.path.join(task.workdir, "transcribe_state.json")
                if os.path.exists(ckpt):
                    try:
                        with open(ckpt, "r", encoding="utf-8") as f:
                            state = json.load(f)
                            segments = state.get("segments", [])
                            if segments:
                                last_end = segments[-1].get("end", 0)
                                total_sec = parse_duration_to_seconds(task.duration)
                                if total_sec > 0:
                                    task.pcts["TR"] = round(min(99.9, (last_end / total_sec) * 100), 1)
                            else:
                                task.pcts["TR"] = 0.0
                    except:
                        task.pcts["TR"] = 0.0
                else:
                    task.pcts["TR"] = 0.0

                task.pcts.update({"DL": 100, "TL": 0.0, "MR": 0.0, "BR": 0.0, "GD": 0.0})
                task.error = None
                return

            if is_p:
                if "暂停中" not in task.status:
                    task.status = f"暂停中 ({task.status})"
                return

            task.status = "排队中 (DL)"
            logger.info(f"Finished syncing task {task.uid}")
        except Exception as e:
            logger.error(f"Task {task.uid} | disk_truth error: {e}")
            task.status = "状态异常"

    def full_physical_sync(self):
        """启动时全量同步物理真相。"""
        with self.lock:
            tasks = list(self.task_map.values())
        logger.info(f"Starting full physical sync for {len(tasks)} tasks...")
        # 使用多线程加速初始化
        threads = []
        for t in tasks:
            logger.info(f"Syncing task {t.uid} ({t.title})...")
            th = threading.Thread(target=self.disk_truth, args=(t,))
            th.start()
            threads.append(th)
        for th in threads: th.join()
        logger.info("Full physical sync completed.")

    def run_process(
        self,
        task: SubTask,
        stage: str,
        cmd: List[str],
        start_pct: float = 0.0,
        end_pct: float = 100.0,
        status_label: Optional[str] = None,
        stall_timeout: Optional[float] = None,
        silence_timeout: Optional[float] = None,
    ):
        """通用环节运行器，带进度抓取与多步进度映射。"""
        sem = self.res.get_sem(stage)
        has_sem = False
        output_lines = []
        display_label = status_label or stage
        try:
            # 1. 启动前的暂停检查 (不占坑)
            while (task.is_paused or self.res.global_pause) and not self.abort:
                with self.lock:
                    task.status = f"暂停中 ({display_label})"
                time.sleep(0.5)

            if self.abort: return False

            # 2. 占坑并启动
            # 上传槽位是串行的（视频号共用浏览器 Profile）。排队时每 5 分钟留一条
            # 日志，否则界面上只剩一个"排队中"，分不清是在等槽位还是已经卡死。
            _slot_wait_start = time.time()
            _next_slot_notice = _slot_wait_start + 300.0
            while not sem.acquire(timeout=5.0):
                if self.abort:
                    return False
                _now = time.time()
                if _now >= _next_slot_notice:
                    _next_slot_notice += 300.0
                    logger.info(
                        f"Task {task.uid} | {stage} | 等待上传槽位 "
                        f"{int(_now - _slot_wait_start)}s（同类上传串行执行中）"
                    )
            has_sem = True

            with self.lock:
                task.status = f"运行中 ({display_label})"

            logger.info(f"Task {task.uid} | {stage} | Start: {' '.join(cmd)}")
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
               cwd=task.workdir, creationflags=CREATE_NO_WINDOW
           )
            task.pid = p.pid

            # 看门狗状态。output=最后一次收到输出的时刻，advance=百分比最后一次
            # 前进的时刻。只有调用方显式传入超时才生效：BI/WX 会传，GD 的 sync.py
            # 输出不规律，不监控。
            _stall_note = ""

            if p.stdout:
                import queue as _queue
                import threading as _threading
                _line_q: _queue.Queue = _queue.Queue()
                _reader_stop = _threading.Event()

                def _stdout_reader():
                    try:
                        for line in p.stdout:
                            if _reader_stop.is_set(): break
                            _line_q.put(line)
                    except Exception:
                        pass
                    finally:
                        _line_q.put(None)

                _rt = _threading.Thread(target=_stdout_reader, daemon=True)
                _rt.start()
                _last_save = 0.0
                _watch = {"output": time.time(), "advance": time.time(), "pct": -1.0}

                def _consume(_line, allow_save=False):
                    nonlocal _last_save
                    _watch["output"] = time.time()
                    output_lines.append(_line)
                    m = re.search(r'Progress:\s*([\d\.]+)%', _line) or re.search(r'\[download\]\s*([\d\.]+)%', _line) or re.search(r'上传进度:\s*([\d\.]+)%', _line)
                    if not m:
                        return
                    val = float(m.group(1))
                    if val > _watch["pct"]:
                        _watch["pct"] = val
                        _watch["advance"] = time.time()
                    mapped_val = start_pct + (val / 100.0) * (end_pct - start_pct)
                    if mapped_val > task.pcts.get(stage, 0.0):
                        task.pcts[stage] = round(mapped_val, 1)
                        if allow_save:
                            _now = time.time()
                            if _now - _last_save > 2.0:
                                self.save_state()
                                _last_save = _now

                def _stall_reason():
                    now = time.time()
                    if silence_timeout and now - _watch["output"] > silence_timeout:
                        return f"{int(now - _watch['output'])}s 无任何输出"
                    if stall_timeout and now - _watch["advance"] > stall_timeout:
                        return f"进度停在 {max(_watch['pct'], 0.0):.1f}% 已 {int(now - _watch['advance'])}s"
                    return None

                while True:
                    if self.abort:
                        kill_pid_tree(p.pid); break
                    # 每轮都查，不只在队列空转时查：视频号上传会持续打印轮询日志
                    # 而百分比不动，只在队列超时时检查就永远不会触发。
                    if p.poll() is None:
                        reason = _stall_reason()
                        if reason:
                            _stall_note = reason
                            logger.error(
                                f"Task {task.uid} | {stage} | 看门狗触发：{reason}；"
                                f"杀掉整棵进程树，释放上传槽位后交给重试逻辑"
                            )
                            kill_pid_tree(p.pid)
                            _reader_stop.set()
                            break
                    try:
                        line = _line_q.get(timeout=1.0)
                    except Exception:
                        if p.poll() is not None:
                            while True:
                                try:
                                    line = _line_q.get(timeout=0.5)
                                except Exception:
                                    break
                                if line is None: break
                                _consume(line)
                            break
                        continue
                    if line is None: break
                    _consume(line, allow_save=True)

                    if task.is_paused or self.res.global_pause:
                        logger.info(f"Task {task.uid} | {stage} | Detected pause request, KILLING process to yield slot.")
                        kill_pid_tree(task.pid)
                        _reader_stop.set()
                        break

                # 先等进程退出，让读取线程自己读到 EOF，再关掉管道。不关的话
                # 每次上传都泄漏一个描述符，而引擎是长驻进程。
                p.wait()
                _reader_stop.set()
                _rt.join(timeout=2)
                try:
                    p.stdout.close()
                except Exception:
                    pass
            else:
                # 阻塞等待直到进程彻底退出
                p.wait()
            task.pid = None
            if p.returncode == 0:
                task.pcts[stage] = end_pct
                return True
            else:
                # 如果是因为暂停而退出的，不视为失败
                if task.is_paused or self.res.global_pause:
                    logger.info(f"Task {task.uid} | {stage} | Process exited due to pause.")
                    return False

                if stage in ("BI", "WX"):
                    label = (
                        "B站 Bilibili (BI)"
                        if stage == "BI"
                        else "视频号 WeChat (WX)"
                    )
                    login_reason = get_upload_login_failure_hint(
                        label, "".join(output_lines)
                    )
                    if login_reason:
                        task.status = f"失败 ({stage})"
                        task.error = login_reason
                        logger.error(
                            f"Task {task.uid} | {stage} | FAILED | {login_reason}"
                        )
                        return False

                # 清除脏 100% 数据（分片级假 100% 会残留），保留真实部分进度
                if task.pcts.get(stage, 0.0) >= 100.0:
                    task.pcts[stage] = 0.0
                task.status = f"失败 ({stage})"
                task.error = f"上传卡死（看门狗：{_stall_note}）" if _stall_note else f"Exit {p.returncode}"
                last_out = "".join(output_lines[-10:]).strip()
                logger.error(f"Task {task.uid} | {stage} | FAILED | Code: {p.returncode} | Last Out: {last_out}")
                return False
        except Exception as e:
            task.status = f"报错 ({stage})"
            task.error = str(e)
            logger.error(f"Task {task.uid} | {stage} | EXCEPTION: {e}")
            return False
        finally:
            if has_sem:
                sem.release()
                has_sem = False

    def worker(self, task: SubTask):
        """任务主循环：下载 -> 转录 -> 翻译 -> 合成 -> 压制 -> [同步]"""
        with self.lock:
            if task.uid in self.active_workers:
                return
            self.active_workers.add(task.uid)

        try:
            while not self.abort and task.status != "完成" and not task.status.startswith("失败") and not task.status.startswith("超限"):
                # 1. 暂停检查
                if task.is_paused or self.res.global_pause:
                    curr_stage = "排队中"
                    if "(" in task.status: curr_stage = task.status.split("(")[1].split(")")[0]
                    task.status = f"暂停中 ({curr_stage})"
                    while (task.is_paused or self.res.global_pause) and not self.abort:
                        time.sleep(0.5)

                # Acquire worker_sem to limit concurrent disk_truth + stage execution
                self.res.worker_sem.acquire()
                try:
                    # 2. 自动探测当前该干什么
                    self.disk_truth(task)
                finally:
                    self.res.worker_sem.release()
                if task.status == "完成": break

                stage = ""
                cmd = []
                start_pct = 0.0
                end_pct = 100.0

                workdir = task.workdir or ""
                if "DL" in task.status:
                    if workdir: os.makedirs(workdir, exist_ok=True)
                    stage = "DL"
                    # 清除脏 100% 数据（分片级假 100% 会残留），但保留真实的部分下载进度
                    if task.pcts.get("DL", 0.0) >= 100.0:
                        task.pcts["DL"] = 0.0
                    # Pre-check: skip download if a valid source video already exists
                    # in workdir (re-run or resume). Verify by comparing local video
                    # duration against remote duration with 5% tolerance.
                    dl_skip = False
                    if workdir and os.path.exists(workdir):
                        existing_vids = [f for f in os.listdir(workdir)
                                         if f.endswith(('.mp4', '.mkv', '.webm', '.mov', '.ts'))
                                         and not f.endswith('.part')
                                         and '_hardsub' not in f]
                        existing_vids.sort(key=lambda x: os.path.getsize(os.path.join(workdir, x)), reverse=True)
                        for cand in existing_vids:
                            candidate = os.path.join(workdir, cand)
                            local_dur = self.get_duration(candidate)
                            local_sec = parse_duration_to_seconds(local_dur)
                            if local_sec <= 0:
                                continue
                            if not task.duration or task.duration == "未知":
                                task.duration = local_dur
                            self._refresh_wechat_skip(task)
                            # Try remote duration; fall back to size check (>10MB)
                            remote_sec = parse_duration_to_seconds(task.duration) if task.duration and task.duration != "未知" else 0
                            if remote_sec > 0 and abs(local_sec - remote_sec) / remote_sec < 0.05:
                                logger.info(f"Task {task.uid} | DL skip: existing video matches duration ({local_sec:.0f}s vs {remote_sec:.0f}s)")
                                dl_skip = True
                                break
                            elif os.path.getsize(candidate) > 10 * 1024 * 1024:
                                logger.info(f"Task {task.uid} | DL skip: existing video ({local_sec:.0f}s, {os.path.getsize(candidate)/(1024*1024):.0f}MB)")
                                dl_skip = True
                                break
                    if dl_skip:
                        task.pcts["DL"] = 100.0
                        task.status = "排队中 (TR)"
                        continue
                    cmd = list(VDOWN_CMD) + [task.url, self.cookie_path, workdir, "--title", task.title]
                elif "TR" in task.status:
                    stage = "TR"
                    vids = [f for f in os.listdir(workdir) if f.lower().endswith((".mp4", ".mkv", ".webm", ".mov", ".ts")) and "_hardsub" not in f and not re.search(r'\.f\d+\.', f)]
                    cmd = list(TRANSCRIBER_CMD) + [os.path.join(workdir, vids[0]), "--output", workdir, "--no-gui"]
                elif "TL" in task.status:
                    stage = "TL"
                    srts = [f for f in os.listdir(workdir) if f.endswith(".srt") and not re.search(r'(\.zh|\.cn|\.bi)\.srt$', f)]
                    settings = self._load_global_settings()
                    model = settings.get("llm_model", DEFAULTS.get("llm_model", "glm-5.3"))
                    style = settings.get("style", "auto")
                    cmd = list(SMART_TRANSLATE_CMD) + [os.path.join(workdir, srts[0]), "--model", model, "--style", style]
                elif "MR" in task.status:
                    stage = "MR"
                    en = [f for f in os.listdir(workdir) if f.endswith(".srt") and not re.search(r'(\.zh|\.cn|\.bi)\.srt$', f)][0]
                    zh = [f for f in os.listdir(workdir) if re.search(r'(\.cn|\.zh)\.srt$', f)][0]
                    cmd = list(SUBTRANSLATOR_CMD) + ["merge", os.path.join(workdir, en), "--translated-file", os.path.join(workdir, zh)]
                elif "BR" in task.status:
                    stage = "BR"
                    bi = [f for f in os.listdir(workdir) if ".bi.srt" in f][0]
                    vid = [f for f in os.listdir(workdir) if f.lower().endswith((".mp4", ".mkv", ".webm", ".mov", ".ts")) and "_hardsub" not in f][0]
                    ass = bi.replace(".srt", ".ass")
                    out = os.path.splitext(vid)[0] + "_hardsub.mp4"
                    
                    # Load and apply global subtitle styles
                    settings = self._load_global_settings()
                    ass_cmd = list(SRT2ASS_CMD) + [os.path.join(workdir, bi), os.path.join(workdir, ass)]
                    if settings.get("layout"): ass_cmd += ["--layout", settings["layout"]]
                    if settings.get("main_lang"): ass_cmd += ["--main-lang", settings["main_lang"]]
                    if settings.get("cn_font"): ass_cmd += ["--cn-font", settings["cn_font"]]
                    if settings.get("en_font"): ass_cmd += ["--en-font", settings["en_font"]]
                    if settings.get("jp_font"): ass_cmd += ["--ja-font", settings["jp_font"]]
                    if settings.get("jp_size"): ass_cmd += ["--ja-size", settings["jp_size"]]
                    if settings.get("cn_size"): ass_cmd += ["--cn-size", settings["cn_size"]]
                    if settings.get("en_size"): ass_cmd += ["--en-size", settings["en_size"]]
                    if settings.get("cn_color"): ass_cmd += ["--cn-color", settings["cn_color"]]
                    if settings.get("en_color"): ass_cmd += ["--en-color", settings["en_color"]]
                    if settings.get("bg_box") is False: ass_cmd += ["--no-bg-box"]
                    # Pass video dimensions for proper font scaling (esp. vertical videos)
                    vw, vh = self.get_video_dimensions(os.path.join(workdir, vid))
                    # Upscale low-res sources so subtitles render crisp (avoids blurry
                    # subs on 360p/480p): if short edge < 720, scale up keeping aspect
                    # ratio (short edge -> 720) and use the larger canvas for both the
                    # ASS PlayRes/font-scaling AND the burn scale filter.
                    burn_extra = []
                    if min(vw, vh) < 720:
                        scale = 720.0 / min(vw, vh)
                        tw, th = int(round(vw * scale)), int(round(vh * scale))
                        tw -= tw % 2; th -= th % 2  # even dims (ffmpeg scale requires)
                        vw, vh = tw, th
                        burn_extra = ["--target-width", str(tw), "--target-height", str(th)]
                        print(f"📐 Source {vw}x{vh} < 720p — upscaling burn canvas to {tw}x{th} for crisp subtitles.")
                    ass_cmd += ["--width", str(vw), "--height", str(vh)]
                    
                    # Step 1: SRT to ASS (runs 0.0% to 2.0%)
                    if self.run_process(task, "BR", ass_cmd, start_pct=0.0, end_pct=2.0):
                        # Step 2: Burn Subtitles (runs 2.0% to 100.0%)
                        cmd = list(BURNSUB_CMD) + [os.path.join(workdir, vid), os.path.join(workdir, ass), os.path.join(workdir, out), "--headless", "--quality", getattr(self, "quality", "high")] + burn_extra
                        start_pct = 2.0
                        end_pct = 100.0
                    else: continue
                elif any(upload_stage in task.status for upload_stage in ("GD", "MG", "BI", "WX")):
                    stage = "GD"
                    need_gd = self.do_gdsync and not os.path.exists(os.path.join(workdir, ".synced"))
                    need_bi = self.do_bili and not os.path.exists(os.path.join(workdir, ".bili_uploaded"))
                    wx_skip_reason = self._refresh_wechat_skip(task)
                    if self.do_wechat and not wx_skip_reason and not os.path.exists(
                        os.path.join(workdir, ".wechat_uploaded")
                    ):
                        wx_skip_reason = get_wechat_upload_skip_reason(workdir)
                        if wx_skip_reason and is_wechat_over_limit_reason(wx_skip_reason):
                            task.wx_skip_reason = wx_skip_reason
                            self.save_state()
                    wx_over_limit = bool(
                        wx_skip_reason and is_wechat_over_limit_reason(wx_skip_reason)
                    )
                    need_wx = (
                        self.do_wechat
                        and not wx_over_limit
                        and not os.path.exists(os.path.join(workdir, ".wechat_uploaded"))
                    )
                    preflight_upload_failure_reasons = []
                    if need_bi:
                        bili_login_skip_reason = get_bili_login_skip_reason()
                        if bili_login_skip_reason:
                            reason = (
                                f"B站 Bilibili (BI) 登录态失效，请先执行: "
                                f"{get_bili_login_command()}（{bili_login_skip_reason}）"
                            )
                            preflight_upload_failure_reasons.append(reason)
                            need_bi = False
                            logger.error(f"Task {task.uid} | BI | upload stopped: {reason}")
                    upload_llm_model, upload_llm_provider = self._get_upload_llm_settings()

                    # Count how many uploads are needed
                    if need_gd and (not SYNC_SCRIPT or not self.gdsync_id):
                        reason = "Google Drive sync is not configured"
                        preflight_upload_failure_reasons.append(reason)
                        logger.error(f"Task {task.uid} | GD | upload stopped: {reason}")
                        need_gd = False

                    active = sum([bool(need_gd), bool(need_bi), bool(need_wx)])

                    if active == 0:
                        if preflight_upload_failure_reasons:
                            task.status = "失败 (上传)"
                            task.error = "；".join(preflight_upload_failure_reasons)
                            self.save_state()
                            break
                        if wx_over_limit:
                            task.status = "超限 (WX)"
                            task.error = f"视频号上传未启动：{wx_skip_reason}"
                            self.save_state()
                            break
                        cmd = []
                    elif active == 1 and not (need_bi or need_wx):
                        # Single upload — use the shared run_process block below
                        if need_gd:
                            cmd = [PYTHON_EXE, SYNC_SCRIPT, "--local-dir", workdir, "--remote-id", self.gdsync_id, "--wrap-folder", "--headless"]
                    else:
                        # === OVERLAPPING UPLOAD PIPELINE ===
                        # GD sync starts immediately. Bilibili metadata is
                        # prepared next; once it is complete, BI starts before
                        # WeChat's shorter title is derived. A WeChat metadata
                        # failure therefore cannot block a publishable BI task.
                        sync_cmd = [str(x) for x in [PYTHON_EXE, SYNC_SCRIPT, "--local-dir", workdir, "--remote-id", self.gdsync_id, "--wrap-folder", "--headless"] if x is not None] if need_gd else None

                        initial_upload_stages = []
                        if need_gd:
                            initial_upload_stages.append("GD")
                        if need_bi or need_wx:
                            initial_upload_stages.append("MG")
                        upload_status_label = "/".join(initial_upload_stages)
                        if upload_status_label:
                            with self.lock:
                                task.status = f"运行中 ({upload_status_label})"
                                if need_bi or need_wx:
                                    task.pcts["MG"] = max(
                                        task.pcts.get("MG", 0.0),
                                        1.0,
                                    )
                                self.save_state()

                        # Phase 1: kick off GD sync + Bilibili metadata concurrently
                        gd_result = [False]
                        bi_result = [False, None]
                        wx_result = [False, None]
                        bili_metadata = None
                        wechat_metadata = None
                        upload_threads = []

                        gd_thread = None
                        if need_gd and sync_cmd:
                            def _run_gd():
                                gd_result[0] = self.run_process(
                                    task,
                                    "GD",
                                    sync_cmd,
                                    start_pct=0.0,
                                    end_pct=100.0,
                                    status_label=upload_status_label,
                                )
                                if gd_result[0]:
                                    self._write_upload_marker(task, "GD")
                            gd_thread = threading.Thread(target=_run_gd, daemon=True)
                            gd_thread.start()

                        metadata_failed = False
                        upload_failure_reasons = list(preflight_upload_failure_reasons)
                        permanent_upload_failure = bool(preflight_upload_failure_reasons)
                        if wx_over_limit:
                            upload_failure_reasons.append(
                                f"视频号上传未启动：{wx_skip_reason}"
                            )

                        bili_cmd = None
                        if need_bi:
                            def _bili_metadata_log(message):
                                self._advance_mg_progress(task, message)
                                logger.info(f"Task {task.uid} | MG | {message}")

                            bili_metadata = prepare_bili_upload_metadata(
                                workdir,
                                llm_model=upload_llm_model,
                                llm_provider=upload_llm_provider,
                                on_log=_bili_metadata_log,
                                source_title=task.title,
                            )
                        if need_bi and bili_metadata:
                            with self.lock:
                                task.pcts["MG"] = max(
                                    task.pcts.get("MG", 0.0),
                                    100.0 if not need_wx else 60.0,
                                )
                                self.save_state()
                            bili_cmd = build_bili_cmd(
                                workdir,
                                title=bili_metadata.get("title"),
                                desc=bili_metadata.get("desc"),
                                cover_title=bili_metadata.get("cover_title"),
                                llm_model=upload_llm_model,
                                llm_provider=upload_llm_provider,
                                require_complete_metadata=True,
                            )
                            if not bili_cmd:
                                logger.error(
                                    f"Task {task.uid} | BI | upload stopped: "
                                    "complete pre-generated metadata is required"
                                )
                                upload_failure_reasons.append(
                                    "B站上传命令构建失败，未开始上传"
                                )
                                need_bi = False
                        elif need_bi:
                            logger.error(
                                f"Task {task.uid} | MG | Bilibili metadata "
                                "incomplete; BI upload stopped"
                            )
                            metadata_failed = True
                            upload_failure_reasons.append(
                                "B站元数据生成失败或不完整，BI上传未启动"
                            )
                            need_bi = False

                        # Start BI before deriving WeChat's 16-character title.
                        if need_bi and bili_cmd:
                            bili_thread = threading.Thread(
                                target=self._run_bili_upload,
                                args=(task, bili_cmd, bi_result),
                                daemon=True,
                            )
                            upload_threads.append(bili_thread)
                            bili_thread.start()

                        wechat_cmd = None
                        if need_wx:
                            def _wechat_metadata_log(message):
                                self._advance_mg_progress(task, message)
                                logger.info(f"Task {task.uid} | MG | {message}")

                            wechat_metadata = prepare_wechat_upload_metadata(
                                workdir,
                                llm_model=upload_llm_model,
                                llm_provider=upload_llm_provider,
                                on_log=_wechat_metadata_log,
                                source_title=task.title,
                            )
                            wx_skip_reason = get_wechat_upload_skip_reason(workdir)
                        if need_wx and not wechat_metadata:
                            logger.error(
                                f"Task {task.uid} | MG | WeChat metadata "
                                "incomplete; WX upload stopped"
                            )
                            metadata_failed = True
                            upload_failure_reasons.append(
                                "视频号元数据生成失败或不完整，WX上传未启动"
                            )
                            need_wx = False
                        elif need_wx:
                            with self.lock:
                                task.pcts["MG"] = 100.0
                                self.save_state()
                            wechat_cmd = build_wechat_cmd(
                                workdir,
                                title=wechat_metadata.get("wechat_title"),
                                desc=wechat_metadata.get("desc"),
                                cover_title=wechat_metadata.get("cover_title"),
                                public=self.wechat_public,
                                llm_model=upload_llm_model,
                                llm_provider=upload_llm_provider,
                                require_complete_metadata=True,
                            )
                            if not wechat_cmd:
                                logger.error(
                                    f"Task {task.uid} | WX | upload stopped: "
                                    "complete pre-generated metadata is required"
                                )
                                if wx_skip_reason:
                                    upload_failure_reasons.append(f"视频号上传未启动：{wx_skip_reason}")
                                    permanent_upload_failure = True
                                else:
                                    upload_failure_reasons.append("视频号上传命令构建失败，未开始上传")
                                need_wx = False

                        if need_wx and wechat_cmd:
                            wechat_thread = threading.Thread(
                                target=self._run_wechat_upload,
                                args=(task, wechat_cmd, wx_result),
                                daemon=True,
                            )
                            upload_threads.append(wechat_thread)
                            wechat_thread.start()

                        active_stages = []
                        if gd_thread and gd_thread.is_alive():
                            active_stages.append("GD")
                        if need_bi and bili_cmd:
                            active_stages.append("BI")
                        if need_wx and wechat_cmd:
                            active_stages.append("WX")
                        if active_stages:
                            with self.lock:
                                task.status = f"运行中 ({'/'.join(active_stages)})"
                                self.save_state()

                        # Wait for GD sync (may already be done)
                        if gd_thread:
                            gd_thread.join()
                        for t in upload_threads: t.join()

                        # Write markers
                        if gd_result[0]:
                            try:
                                with open(os.path.join(workdir, ".synced"), "w") as f: f.write("1")
                            except: pass
                            task.pcts["GD"] = 100.0
                        if bi_result[0]:
                            try:
                                with open(os.path.join(workdir, ".bili_uploaded"), "w") as f: f.write("1")
                            except: pass
                            task.pcts["BI"] = 100.0
                        if wx_result[0]:
                            try:
                                with open(os.path.join(workdir, ".wechat_uploaded"), "w", encoding="utf-8") as f: f.write("1")
                            except: pass
                            task.pcts["WX"] = 100.0
                        self.save_state()

                        if need_bi and not bi_result[0]:
                            upload_failure_reasons.append(
                                bi_result[1] if len(bi_result) > 1 and bi_result[1] else "BI upload failed"
                            )
                        if need_wx and not wx_result[0]:
                            upload_failure_reasons.append(
                                wx_result[1] if len(wx_result) > 1 and wx_result[1] else "WX upload failed"
                            )
                        permanent_upload_failure = permanent_upload_failure or any(
                            "登录态失效" in reason
                            for reason in upload_failure_reasons
                        ) or metadata_failed

                        all_ok = (
                            not metadata_failed
                            and not upload_failure_reasons
                            and all([
                            not need_gd or gd_result[0],
                            not need_bi or bi_result[0],
                            not need_wx or wx_result[0],
                            ])
                        )
                        if all_ok:
                            self.disk_truth(task)
                        else:
                            wechat_over_limit = any(
                                is_wechat_over_limit_reason(reason)
                                for reason in upload_failure_reasons
                            )
                            if (
                                wechat_over_limit
                                and not metadata_failed
                                and len(upload_failure_reasons) == 1
                                and (not need_gd or gd_result[0])
                                and (not need_bi or bi_result[0])
                            ):
                                task.status = "超限 (WX)"
                                task.error = "；".join(upload_failure_reasons)
                                self.save_state()
                                break

                            task.status = self._classify_upload_failure(
                                upload_failure_reasons,
                                metadata_failed,
                                need_gd,
                                need_bi,
                                need_wx,
                                gd_result,
                                bi_result,
                                wx_result,
                            )
                            if permanent_upload_failure:
                                task.error = "；".join(upload_failure_reasons)
                                self.save_state()
                                break
                            if task.retries < 3:
                                task.retries += 1
                                task.status = "排队中 (GD)"
                                time.sleep(5)
                                continue
                            task.error = "；".join(upload_failure_reasons) or "BI/WX上传未完成"
                            self.save_state()
                            break
                        cmd = []  # already handled, skip shared block

                if cmd:
                    cmd = [str(x) for x in cmd if x is not None]
                    # 只有 BI/WX 走看门狗：这两条上传会真的卡死（视频号尤其明显，
                    # 子进程一直打印轮询日志但百分比不动），而串行槽位只有一个，
                    # 卡住一个就把后面所有任务钉在"等待"。GD 的 sync.py 输出不规律，
                    # 不纳入监控。
                    _watchdog_kwargs = (
                        {"stall_timeout": UPLOAD_STALL_TIMEOUT,
                         "silence_timeout": UPLOAD_SILENCE_TIMEOUT}
                        if stage in ("BI", "WX") else {}
                    )
                    if self.run_process(task, stage, cmd, start_pct=start_pct, end_pct=end_pct, **_watchdog_kwargs):
                        if stage == "BR":
                            try:
                                with open(os.path.join(workdir, ".burn_complete"), "w", encoding="utf-8") as f:
                                    f.write("1")
                            except:
                                pass
                        if stage == "GD":
                            with open(os.path.join(workdir, ".synced"), "w") as f: f.write("1")
                        if stage == "BI":
                            with open(os.path.join(workdir, ".bili_uploaded"), "w") as f: f.write("1")
                        if stage == "WX":
                            with open(os.path.join(workdir, ".wechat_uploaded"), "w", encoding="utf-8") as f: f.write("1")
                        self.disk_truth(task)
                    else:
                        # 失败重试逻辑
                        if task.error and "登录态失效" in task.error:
                            task.status = f"失败 ({stage})"
                            self.save_state()
                            break
                        if task.retries < 3:
                            task.retries += 1
                            task.status = f"排队中 ({stage})"
                            time.sleep(5)
                            continue
                        break
                time.sleep(1)
        except Exception as e:
            import traceback
            task.status = "状态异常"
            task.error = f"{e.__class__.__name__}: {str(e)}"
            logger.error(f"Task {task.uid} | worker thread crashed: {e}\n{traceback.format_exc()}")
        finally:
            with self.lock:
                self.active_workers.discard(task.uid)

    @staticmethod
    def _classify_upload_failure(
        upload_failure_reasons,
        metadata_failed,
        need_gd,
        need_bi,
        need_wx,
        gd_result,
        bi_result,
        wx_result,
    ):
        """Map an upload-stage failure to the platform that actually failed."""
        has_bili_reason = any(
            "B站" in reason or "BI" in reason
            for reason in upload_failure_reasons
        )
        has_wechat_reason = any(
            "视频号" in reason or "WX" in reason
            for reason in upload_failure_reasons
        )
        if has_bili_reason != has_wechat_reason:
            return "失败 (BI)" if has_bili_reason else "失败 (WX)"
        if metadata_failed or upload_failure_reasons:
            return "失败 (上传)"
        if (need_bi and not bi_result[0]) or (need_wx and not wx_result[0]):
            return "失败 (上传)"
        if need_gd and not gd_result[0]:
            return "失败 (GD)"
        return "失败 (上传)"

    def _build_bili_cmd(self, workdir, llm_model=None, llm_provider=None):
        """Build Bilibili upload command via the canonical autosub.build_bili_cmd.

        A single remaining stage goes through here instead of the overlapping
        pipeline, so it must pass the same pre-generated metadata. Without it
        bili_upload.py falls back to its own LLM title and a retry publishes
        different metadata than the first attempt.
        """
        meta = prepare_bili_upload_metadata(
            workdir,
            llm_model=llm_model,
            llm_provider=llm_provider,
            on_log=lambda msg: logger.info(f"MG | {msg}"),
        )
        if not meta:
            return None
        cmd = build_bili_cmd(
            workdir,
            title=meta.get("title") if meta else None,
            desc=meta.get("desc") if meta else None,
            cover_title=meta.get("cover_title") if meta else None,
            llm_model=llm_model,
            llm_provider=llm_provider,
            require_complete_metadata=True,
        )
        if cmd is None:
            logger.warning(
                f"Bilibili cmd build failed for {workdir} "
                "(script/venv/hardsub missing or metadata incomplete)"
            )
        return cmd

    def _advance_mg_progress(self, task: SubTask, message: str):
        """Map metadata-generation log milestones to the MG progress bar."""
        value = str(message or "")
        if ">>> [MetaGen] 简介" in value:
            pct = 15.0
        elif ">>> [MetaGen] B站标题" in value:
            pct = 35.0
        elif ">>> [MetaGen] 封面标题" in value:
            pct = 50.0
        elif ">>> [MetaGen] 视频号标题" in value:
            pct = 70.0
        else:
            return
        should_save = False
        with self.lock:
            if pct > task.pcts.get("MG", 0.0):
                task.pcts["MG"] = pct
                should_save = True
        if should_save:
            self.save_state()

    def _build_wechat_cmd(self, workdir, llm_model=None, llm_provider=None):
        """Build WeChat Channels upload command via the canonical autosub API.

        Mirrors the overlapping pipeline: the form title comes from wechat_title
        and the command is refused when pre-generated metadata is incomplete.
        """
        meta = prepare_wechat_upload_metadata(
            workdir,
            llm_model=llm_model,
            llm_provider=llm_provider,
            on_log=lambda msg: logger.info(f"MG | {msg}"),
        )
        if not meta:
            return None
        cmd = build_wechat_cmd(
            workdir,
            title=meta.get("wechat_title") if meta else None,
            desc=meta.get("desc") if meta else None,
            cover_title=meta.get("cover_title") if meta else None,
            public=self.wechat_public,
            llm_model=llm_model,
            llm_provider=llm_provider,
            require_complete_metadata=True,
        )
        if cmd is None:
            logger.warning(
                f"WeChat cmd build failed for {workdir} "
                "(script/python/hardsub missing or metadata incomplete)"
            )
        return cmd

    def _write_upload_marker(self, task, stage):
        """Persist an upload's marker file the moment that upload succeeds.

        Parallel GD/BI/WX uploads used to write their markers only after every
        sibling thread joined. A fast BI upload therefore stayed unmarked while a
        slow WX upload ran, and a restart or retry in that window re-uploaded the
        already-published stage (duplicate Bilibili post).
        """
        markers = {"GD": ".synced", "BI": ".bili_uploaded", "WX": ".wechat_uploaded"}
        marker = markers.get(stage)
        workdir = task.workdir
        if not marker or not workdir:
            return
        try:
            with open(os.path.join(workdir, marker), "w", encoding="utf-8") as f:
                f.write("1")
        except OSError as e:
            logger.error(f"Task {task.uid} | {stage} | marker write failed: {e}")

    def _run_bili_upload(self, task, cmd, result_box):
        """Run Bilibili upload in a background thread (parallel with GD sync),
        delegating the subprocess to the canonical autosub.run_bili_cmd.
        Only updates task.pcts['BI']; the main worker thread owns task.status."""
        sem = self.res.get_sem("BI")
        has_sem = False
        try:
            if self.abort:
                return
            sem.acquire()
            has_sem = True
            with self.lock:
                task.pcts["BI"] = 0.0
            logger.info(f"Task {task.uid} | BI | Start (parallel): {' '.join(cmd)}")
            output_lines = []

            def on_progress(pct):
                if pct > task.pcts.get("BI", 0.0):
                    with self.lock:
                        task.pcts["BI"] = round(pct, 1)
                        self.save_state()

            def on_log(line):
                output_lines.append(str(line))
                logger.info(f"Task {task.uid} | BI | {line}")

            ok = run_bili_cmd(cmd, on_progress=on_progress, on_log=on_log, abort_flag=lambda: self.abort)
            output = "\n".join(output_lines)
            if ok:
                with self.lock:
                    task.pcts["BI"] = 100.0
                self._write_upload_marker(task, "BI")
                result_box[0] = True
                logger.info(f"Task {task.uid} | BI | Success")
            else:
                reason = get_upload_login_failure_hint("B站 Bilibili (BI)", output) or "BI upload failed"
                task.error = reason
                if len(result_box) > 1:
                    result_box[1] = reason
                logger.error(f"Task {task.uid} | BI | Failed: {reason}")
        except Exception as e:
            reason = str(e)
            task.error = reason
            if len(result_box) > 1:
                result_box[1] = reason
            logger.error(f"Task {task.uid} | BI | Exception: {e}")
        finally:
           if has_sem:
               sem.release()

    def _run_wechat_upload(self, task, cmd, result_box):
        """Run WeChat Channels upload in a background thread (parallel with GD/BI),
        delegating the subprocess to the canonical autosub.run_wechat_cmd."""
        sem = self.res.get_sem("WX")
        has_sem = False
        try:
            if self.abort:
                return
            sem.acquire()
            has_sem = True
            with self.lock:
                task.pcts["WX"] = 0.0
            logger.info(f"Task {task.uid} | WX | Start (parallel): {' '.join(cmd)}")
            output_lines = []

            def on_log(line):
                output_lines.append(str(line))
                logger.info(f"Task {task.uid} | WX | {line}")

            def on_progress(pct):
                if pct > task.pcts.get("WX", 0.0):
                    with self.lock:
                        task.pcts["WX"] = round(pct, 1)
                        self.save_state()

            ok = run_wechat_cmd(
                cmd,
                on_progress=on_progress,
                on_log=on_log,
                abort_flag=lambda: self.abort,
            )
            output = "\n".join(output_lines)
            if ok:
                with self.lock:
                    task.pcts["WX"] = 100.0
                self._write_upload_marker(task, "WX")
                result_box[0] = True
                logger.info(f"Task {task.uid} | WX | Success")
            else:
                reason = get_upload_login_failure_hint("视频号 WeChat (WX)", output) or "WX upload failed"
                task.error = reason
                if len(result_box) > 1:
                    result_box[1] = reason
                logger.error(f"Task {task.uid} | WX | Failed: {reason}")
        except Exception as e:
            reason = str(e)
            task.error = reason
            if len(result_box) > 1:
                result_box[1] = reason
            logger.error(f"Task {task.uid} | WX | Exception: {e}")
        finally:
            if has_sem:
                sem.release()

    def start(self):
        self.load_state()
        for t in self.task_map.values():
            threading.Thread(target=self.worker, args=(t,), daemon=True).start()

# --- UI 渲染 ---
class Dashboard:
    def __init__(self, engine: BatchEngine):
        self.engine = engine
        self.console = Console(force_terminal=True)
        self.cmd_buf = ""
        self.scroll_offset = 0
        self.page_size = 10  # 默认每页显示 10 个任务


    def make_bar(self, p, stage_active, row_color):
        # 动态缩减进度条宽度以适应小窗口
        term_width = self.console.width
        w = 10 if term_width >= 135 else 6

        # 只要有进度 (哪怕很小)，就至少显示一格，确保可视化反馈
        filled = math.ceil(p / 100 * w) if p > 0 else 0
        if p >= 100:
            bar_color = "green"
        elif stage_active:
            # Queued rows are grey, but an active stage still has live work.
            bar_color = "bright_blue" if row_color == "grey37" else row_color
        else:
            bar_color = "grey37"

        bar = "█" * filled + "░" * (w - filled)
        pct = f"{int(p)}%" if term_width < 100 else f"{p:>5.1f}%"
        # 紧凑布局：进度条与百分比
        return f"[{bar_color}]{bar}[/]\n[{bar_color}]{pct.center(w)}[/]"


    def render(self):
        # 计算动态页长 (根据终端高度)
        term_height = self.console.height
        term_width = self.console.width
        # Table 每一行约占用 2-3 行高度 (换行 + 线条)
        self.page_size = max(5, (term_height - 10) // 3)

        # Title bar: separate line above table, padded to full terminal width.
        # Avoids Rich's title-in-border rendering which misaligns with CJK/emoji.
        title_content = f"  {self.engine.playlist_title}  "
        title_content = sanitize_title(title_content)
        title_w = cjk_display_width(title_content)
        if title_w > term_width:
            title_content = cjk_truncate(title_content, term_width)
            title_w = cjk_display_width(title_content)
        t_pad_l = max(0, (term_width - title_w) // 2)
        t_pad_r = max(0, term_width - title_w - t_pad_l)
        title_bar = Text(' ' * t_pad_l + title_content + ' ' * t_pad_r, style='bold white on blue')

        table = Table(
            box=box.ROUNDED, expand=True, border_style="bright_blue", show_lines=True,
        )

        # Dynamic Responsive Layout Modes
        if term_width >= 135:
            # 1. Wide Mode: Show all columns with massive Title column (ratio=1)
            table.add_column("ID", width=3, justify="center", vertical="middle")
            table.add_column("Video Title", ratio=1, min_width=40, justify="left", vertical="middle", no_wrap=True, overflow="crop")
            table.add_column("Duration", width=8, justify="center", vertical="middle")
            table.add_column("Status", width=8, justify="center", vertical="middle")

            stages = ["DL", "TR", "TL", "MR", "BR", "GD"]
            if self.engine.do_bili or self.engine.do_wechat:
                stages.append("MG")
            if self.engine.do_bili: stages.append("BI")
            if self.engine.do_wechat: stages.append("WX")
            for s in stages:
                table.add_column(s, width=10, justify="center", vertical="middle")
            active_stages = list(stages)
            _metadata_stage_count = int(
                self.engine.do_bili or self.engine.do_wechat
            )
            _title_w = max(
                20,
                term_width - 107 - (
                    10 * (
                        _metadata_stage_count
                        + int(self.engine.do_bili)
                        + int(self.engine.do_wechat)
                    )
                ),
            )

        elif term_width >= 105:
            # 2. Medium Mode: Show all columns but optimize progress stages to be extremely compact (width=6)
            table.add_column("ID", width=3, justify="center", vertical="middle")
            table.add_column("Video Title", ratio=1, min_width=30, justify="left", vertical="middle", no_wrap=True, overflow="crop")
            table.add_column("Duration", width=8, justify="center", vertical="middle")
            table.add_column("Status", width=8, justify="center", vertical="middle")

            stages = ["DL", "TR", "TL", "MR", "BR", "GD"]
            if self.engine.do_bili or self.engine.do_wechat:
                stages.append("MG")
            if self.engine.do_bili: stages.append("BI")
            if self.engine.do_wechat: stages.append("WX")
            for s in stages:
                table.add_column(s, width=6, justify="center", vertical="middle")
            active_stages = list(stages)
            _metadata_stage_count = int(
                self.engine.do_bili or self.engine.do_wechat
            )
            _title_w = max(
                20,
                term_width - 83 - (
                    6 * (
                        _metadata_stage_count
                        + int(self.engine.do_bili)
                        + int(self.engine.do_wechat)
                    )
                ),
            )
            
        else:
            # 3. Narrow Mode: Completely hide stage progress columns to prioritize Title column
            table.add_column("ID", width=3, justify="center", vertical="middle")
            table.add_column("Video Title", ratio=1, min_width=25, justify="left", vertical="middle", no_wrap=True, overflow="crop")
            table.add_column("Duration", width=8, justify="center", vertical="middle")
            table.add_column("Status", width=8, justify="center", vertical="middle")
            active_stages = []
            _title_w = max(20, term_width - 32)

        with self.engine.lock:
            all_tasks = sorted(self.engine.task_map.values(), key=lambda x: x.uid)
            total_tasks = len(all_tasks)

            # 分页切片
            if self.scroll_offset >= total_tasks: self.scroll_offset = max(0, total_tasks - self.page_size)
            visible_tasks = all_tasks[self.scroll_offset : self.scroll_offset + self.page_size]

            for t in visible_tasks:
                # 确定行基础颜色
                is_done = t.status == "完成"
                is_failed = "失败" in t.status or "报错" in t.status
                is_over_limit = t.status.startswith("超限")
                is_paused = "暂停" in t.status
                is_running = "运行" in t.status
                is_queued = "排队" in t.status or "等待" in t.status or t.status.startswith("排队中") or t.status.startswith("等待中")

                row_color = "green" if is_done else \
                            "red" if is_failed else \
                            "bright_yellow" if is_over_limit else \
                            "bright_yellow" if is_paused else \
                            "bright_blue" if is_running else "grey37"

                row_style = f"bold {row_color}" if row_color != "grey37" else "grey37"

                # 判定哪个环节是活跃的
                active_stage = ""
                if "(" in t.status:
                    active_stage = t.status.split("(")[1].split(")")[0]

                # 状态格式化为英文 ASCII，规避 CJK 带来的排版错乱
                # 所有状态强制 2 行，保证 show_lines 行高一致，消除参差不齐
                disp_status = t.status
                if is_done:
                    disp_status = "DONE\n "
                elif is_failed:
                    disp_status = f"FAIL\n({active_stage})" if active_stage else "FAIL\n "
                elif is_over_limit:
                    disp_status = f"LIMIT\n({active_stage})" if active_stage else "LIMIT\n "
                elif is_paused:
                    disp_status = f"PAUSE\n({active_stage})" if active_stage else "PAUSE\n "
                elif is_running:
                    disp_status = f"RUN\n({active_stage})" if active_stage else "RUN\n "
                elif is_queued:
                    disp_status = f"WAIT\n({active_stage})" if active_stage else "WAIT\n "
                elif "异常" in t.status:
                    disp_status = f"ERR\n({active_stage})" if active_stage else "ERR\n "
                elif "重置" in t.status:
                    disp_status = f"RESET\n({active_stage})" if active_stage else "RESET\n "

                row: list = [
                    Text(str(t.uid), style=row_style),
                    Text(cjk_truncate(t.title, _title_w), style=row_style, no_wrap=True, overflow="crop"),
                    Text(t.duration if t.duration else "Unknown", style=row_style),
                    Text(disp_status, style=row_style, justify="center")
                ]
                for s in active_stages:
                    s_active = (s == active_stage) or (0 < t.pcts.get(s, 0.0) < 100.0)
                    row.append(self.make_bar(t.pcts[s], s_active, row_color))
                table.add_row(*row)

            if not visible_tasks:
                empty_row = [""] * len(table.columns)
                if len(empty_row) > 1:
                    empty_row[1] = Text("⚠️ Empty task list", style="bold red")
                table.add_row(*empty_row)

        # Caption bar: separate lines below table, padded to full terminal width.
        # ASCII-only (Up/Dn instead of arrows) to avoid ambiguous-width chars.
        page_info = f"Total Tasks: {total_tasks} | Showing: {self.scroll_offset+1}-{min(self.scroll_offset+self.page_size, total_tasks)} | Up/Dn Scroll"
        cmd_text = f"[bold cyan]Cmd: [/][white]{self.cmd_buf}[/]" if self.cmd_buf else "[dim]Waiting for command...[/]"
        help_msg = f"{cmd_text} | [bold yellow]P+A[/]:Pause/Resume All | [bold yellow]P+ID[/]:Pause/Resume One | [bold yellow]R+ID[/]:Reset | [bold red]Q[/]:Exit"

        cap1 = Text.from_markup(help_msg)
        cap2 = Text.from_markup(f"[dim]{page_info}[/]")
        def _center_pad_txt(txt: Text, width: int) -> Text:
            w = cell_len(txt.plain)
            pl = max(0, (width - w) // 2)
            pr = max(0, width - w - pl)
            r = Text()
            if pl: r.append(' ' * pl)
            r.append(txt)
            if pr: r.append(' ' * pr)
            return r

        return Group(title_bar, table, _center_pad_txt(cap1, term_width), _center_pad_txt(cap2, term_width))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--sub-dir-name", help="项目子目录名称")
    parser.add_argument("--gdsync", action="store_true")
    parser.add_argument("--bili-upload", action="store_true", help="Upload each hardsub video to Bilibili (auto-generates tid/tags/desc/season per video)")
    parser.add_argument("--wechat-upload", action="store_true", help="Upload each hardsub video to WeChat Channels as a draft")
    parser.add_argument("--wechat-public", action="store_true", help="Publish WeChat Channels uploads publicly instead of saving drafts")
    parser.add_argument("--urls", nargs="*")
    parser.add_argument("--file", help="包含 URL 列表的文本文件")
    parser.add_argument("--cookies", help="Path to cookies.txt (optional)")
    parser.add_argument("--cookies-from-browser", choices=sorted(BROWSER_COOKIE_NAMES), help="Read cookies from a browser profile")
    parser.add_argument("--exclude", help="要排除的视频序号 (1-based, 逗号分隔, 支持范围如 1-5)")
    parser.add_argument("--include", help="仅包含的视频序号 (1-based, 逗号分隔, 支持范围如 1-5)")
    parser.add_argument("--separate", action="store_true", help="使用播放列表名称作为子目录")
    parser.add_argument("--state-file", help="Path to state file")
    parser.add_argument("--quality", choices=["standard", "high", "lossless"], default="high", help="压制字幕画质 (standard, high, lossless)")
    parser.add_argument("--no-sequence", action="store_true", help="不要在视频文件夹名称前加序列号")
    args = parser.parse_args()

    engine = BatchEngine(output_dir=args.output, sub_dir_name=args.sub_dir_name, state_file=args.state_file)
    engine.quality = args.quality
    engine.do_gdsync = args.gdsync
    engine.do_bili = args.bili_upload
    engine.do_wechat = args.wechat_upload
    engine.wechat_public = args.wechat_public
    engine.no_sequence = args.no_sequence

    # 1. 加载既有状态
    engine.load_state()
    if args.cookies:
        engine.cookie_path = args.cookies
    elif args.cookies_from_browser:
        engine.cookie_path = args.cookies_from_browser

    # Unified index selection logic
    selection_str = ""
    if args.include:
        selection_str = "+" + args.include
    elif args.exclude:
        selection_str = args.exclude

    # CRITICAL: Apply manual filters (include/exclude) to existing state
    if selection_str and engine.task_map:
        logger.info(f"Applying selection filter: {selection_str}")
        with engine.lock:
            current_ids = list(engine.task_map.keys())
            total_for_filter = max(current_ids) if current_ids else 100
            to_keep = parse_indices(selection_str, total_for_filter)
            logger.info(f"Indices to keep: {to_keep}")

            filtered_map = {}
            for uid, task in engine.task_map.items():
                if uid in to_keep:
                    filtered_map[uid] = task
                else:
                    logger.info(f"Task {uid} excluded by filter.")

            engine.task_map = filtered_map
            logger.info(f"Final task count: {len(engine.task_map)}")

    # One shutdown hook must kill children before saving; otherwise active task
    # statuses are persisted as "running" after a manual Launcher exit.
    atexit.register(engine.shutdown)

    # 2. 物理真相全量同步 (启动瞬间完成)
    engine.full_physical_sync()

    # 3. 处理新传入的 URLs (支持播放列表展开)
    input_urls = []
    if args.urls: input_urls.extend(args.urls)
    if args.file and os.path.exists(args.file):
        with open(args.file, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"): continue
                match = re.search(r'(https?://[^\s,]+)', line)
                if match:
                    input_urls.append(match.group(1))
                else:
                    input_urls.append(line)

    # Selection logic already handled above for filtering loaded state

    if input_urls:
        # Note: We need a basic fetch_metadata for playlist expansion if run standalone
        # But if launched from launcher, state is already seeded, and URLs will be ignored by add_task (already exists)
        for url in input_urls:
            if "list=" in url or "playlist" in url:
                # Basic playlist expansion logic (simplified or use yt-dlp)
                try:
                    LOCAL_YTDLP = os.path.join(TOOLS_DIR, "vdown", "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")
                    YTDLP_EXE = LOCAL_YTDLP if os.path.exists(LOCAL_YTDLP) else os.path.join(VENV_BIN, "yt-dlp")

                    cmd = [YTDLP_EXE, "--flat-playlist", "--dump-single-json", "--quiet"]
                    # Fix: Bypass bot detection / PO Token blocks
                    cmd.extend(["--extractor-args", "youtube:player_client=web_safari"])

                    with engine.use_temp_cookies(engine.cookie_path) as temp_cookies:
                        if temp_cookies:
                            cmd.extend(["--cookies", temp_cookies])
                        elif engine.cookie_path in ["chrome", "firefox", "safari", "edge"]:
                            cmd.extend(["--cookies-from-browser", engine.cookie_path])
                        cmd.append(url)
                        out = subprocess.check_output(cmd, creationflags=CREATE_NO_WINDOW).decode('utf-8', errors='ignore')
                        data = json.loads(out)

                        playlist_title = data.get("title", "Playlist")
                        playlist_title = re.sub(r'[\\/*?:"<>|]', '_', playlist_title).strip()

                        entries = data.get("entries", [data])
                        indices_to_keep = parse_indices(selection_str, len(entries))
                        for idx, e in enumerate(entries, 1):
                            if idx not in indices_to_keep: continue
                            title = e.get("title", "Unknown")
                            # Skip private/deleted
                            if "[private video]" in title.lower() or "[deleted video]" in title.lower() or "private video" in title.lower():
                                continue

                            e_url = e.get("url") or e.get("webpage_url")
                            if e_url: engine.add_task(e_url, title=title, vid_id=e.get("id"), uid=None, sub_dir=playlist_title, playlist_index=idx)
                except Exception as e:
                    logger.error(f"Playlist expansion failed for {url}: {e}")
                    engine.add_task(url) # Fallback to single task
            else:
                engine.add_task(url)

    # 3. 为所有任务启动 worker
    for t in engine.task_map.values():
        threading.Thread(target=engine.worker, args=(t,), daemon=True).start()

    dash = Dashboard(engine)

    # 交互线程
    def input_loop():
        init_terminal()
        cmd_mode = None
        id_buf = ""
        while not engine.abort:
            if check_kbhit():
                # Read raw bytes to avoid decoding hang
                raw_ch = read_char()
                if not raw_ch: continue

                if raw_ch == b'\x1e':
                    buf = b''
                    fd = sys.stdin.fileno()
                    while not engine.abort:
                        try:
                            dr, _, _ = select.select([fd], [], [], 0.1)
                            if not dr:
                                break
                            b = os.read(fd, 1)
                            if not b or b == b'\n':
                                break
                            buf += b
                        except Exception:
                            break
                    line = buf.decode('utf-8', errors='ignore')
                    try:
                        h_str, w_str = line.split(",")
                        rows = int(h_str)
                        cols = int(w_str)
                        dash.console.width = cols
                        dash.console.height = rows
                    except Exception:
                        pass
                    continue

                ch = raw_ch.lower()
                if ch == b'q': engine.abort = True; break

                if sys.platform == "win32":
                    # 翻页支持
                    if ch in (b'\x00', b'\xe0'):
                        ch2 = read_char()
                        if ch2 == b'H': # Up
                            dash.scroll_offset = max(0, dash.scroll_offset - 1)
                        elif ch2 == b'P': # Down
                            with engine.lock:
                                max_offset = max(0, len(engine.task_map) - dash.page_size)
                            dash.scroll_offset = min(max_offset, dash.scroll_offset + 1)
                        continue
                else:
                    if ch == b'\x1b':
                        direction = _read_escape_seq()
                        if direction == 'up':
                            dash.scroll_offset = max(0, dash.scroll_offset - 1)
                        elif direction == 'down':
                            with engine.lock:
                                max_offset = max(0, len(engine.task_map) - dash.page_size)
                            dash.scroll_offset = min(max_offset, dash.scroll_offset + 1)
                        else:
                            # Bare ESC key — clear command buffer
                            cmd_mode, id_buf, dash.cmd_buf = None, "", ""
                        continue

                if ch == b'p':

                    dash.cmd_buf = "P (A/ID): "
                    cmd_mode = 'P'
                elif ch == b'r':
                    dash.cmd_buf = "R (ID): "
                    cmd_mode = 'R'
                elif ch.isdigit() and cmd_mode:
                    id_buf += ch.decode()
                    dash.cmd_buf += ch.decode()
                elif ch.lower() == b'a' and cmd_mode == 'P':
                    id_buf = "A"
                    dash.cmd_buf = "P (A/ID): A"
                elif ch in (b'\r', b'\n'):
                    if id_buf and cmd_mode:
                        if id_buf == "A" and cmd_mode == 'P':
                            new_global_pause = not engine.res.global_pause
                            engine.res.global_pause = new_global_pause
                            # 立即同步所有活跃进程的状态并更新 UI 状态
                            with engine.lock:
                                for t in engine.task_map.values():
                                    stage = "TR"
                                    if "(" in t.status: stage = t.status.split("(")[1].split(")")[0]

                                    if new_global_pause:
                                        # P+A 暂停所有：标记全局暂停，杀死所有活跃子进程
                                        t.status = f"暂停中 ({stage})"
                                        if t.pid:
                                            kill_pid_tree(t.pid, terminate=True)
                                    else:
                                        # P+A 恢复所有：清除全局暂停 AND 清除所有任务的 is_paused 标志
                                        # 关键修复：恢复时必须一并清除每个任务的 is_paused，否则
                                        # 之前被 P+数字 单独暂停的任务仍卡在暂停循环中
                                        t.is_paused = False
                                        t.status = f"排队中 ({stage})"
                                        t.error = None

                            engine.save_state()

                            # 恢复时：为所有未完成且没有活跃 worker 的任务重启线程
                            # 关键修复：暂停期间 worker 可能因进程被杀后重试耗尽等原因退出，
                            # 仅靠 worker 内部的 while 循环无法自动恢复已退出的线程
                            if not new_global_pause:
                                with engine.lock:
                                    for t in engine.task_map.values():
                                        if (
                                            t.status != "完成"
                                            and not t.status.startswith("失败")
                                            and not t.status.startswith("超限")
                                            and t.uid not in engine.active_workers
                                        ):
                                            threading.Thread(target=engine.worker, args=(t,), daemon=True).start()
                        else:
                            try:
                                uid = int(id_buf)
                                t = engine.task_map.get(uid)
                                if t:
                                    if cmd_mode == 'P':
                                        t.is_paused = not t.is_paused
                                        # 立即同步该进程状态并更新 UI
                                        with engine.lock:
                                            stage = "TR"
                                            if "(" in t.status: stage = t.status.split("(")[1].split(")")[0]

                                            if t.is_paused or engine.res.global_pause:
                                                t.status = f"暂停中 ({stage})"
                                            else:
                                                t.status = f"排队中 ({stage})"

                                            if t.pid:
                                                kill_pid_tree(t.pid)

                                            # 核心改进：如果是从“失败”状态点击 P 恢复，或者工人线程已死，则重新拉起线程
                                            if not t.is_paused and t.uid not in engine.active_workers:
                                                threading.Thread(target=engine.worker, args=(t,), daemon=True).start()
                                    elif cmd_mode == 'R':
                                        with engine.lock:
                                            if t.pid:
                                                kill_pid_tree(t.pid)
                                                t.pid = None

                                            reset_stage = engine.reset_task(t)
                                            # 仅在没有活跃工人时才重新拉起线程，防止“泥潭”效应
                                            if t.uid not in engine.active_workers:
                                                threading.Thread(target=engine.worker, args=(t,), daemon=True).start()
                                            else:
                                                logger.info(f"Task {t.uid} | Reset ({reset_stage or 'complete'}) | Worker thread already active, it will pick up the reset status.")
                            except: pass
                        cmd_mode, id_buf, dash.cmd_buf = None, "", ""
                elif ch == b'\x1b' and sys.platform == "win32": # ESC on Windows
                    cmd_mode, id_buf, dash.cmd_buf = None, "", ""
            time.sleep(0.05)

    threading.Thread(target=input_loop, daemon=True).start()

    with Live(dash.render(), console=dash.console, screen=True, refresh_per_second=2) as live:
        while not engine.abort:
            live.update(dash.render())
            engine.save_state()
            time.sleep(0.5)

if __name__ == "__main__":
    main()
