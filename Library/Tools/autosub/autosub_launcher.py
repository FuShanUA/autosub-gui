import os
import sys
import json
import subprocess
import re
import traceback
import time
import ctypes
import shutil
import tempfile
import contextlib
import unicodedata
from typing import Any, Dict, Optional, Set, List, Union

from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.panel import Panel
from rich import box
from rich.cells import cell_len

# Configuration
CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))
if getattr(sys, 'frozen', False):
    TOOLS_DIR = os.path.join(getattr(sys, '_MEIPASS', ''), "Library", "Tools")
else:
    TOOLS_DIR = os.environ.get("AUTOSUB_TOOLS_DIR") or os.path.dirname(CURRENT_DIR)
os.environ.setdefault("AUTOSUB_TOOLS_DIR", TOOLS_DIR)


def venv_site_packages():
    """Absolute path to this project's venv site-packages, or None."""
    root = os.path.join(CURRENT_DIR, ".venv")
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


# --- Robust Tool Discovery ---
def get_ytdlp_path():
    # 1. Check local tools folder
    local_ytdlp = os.path.join(TOOLS_DIR, "vdown", "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")
    if os.path.exists(local_ytdlp): return local_ytdlp
    # 2. Check current VENV bin
    venv_bin = os.path.join(CURRENT_DIR, ".venv", "bin")
    if sys.platform == "win32":
        venv_bin = os.path.join(CURRENT_DIR, ".venv", "Scripts")
    
    venv_ytdlp = os.path.join(venv_bin, "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")
    if os.path.exists(venv_ytdlp): return venv_ytdlp

    # 3. Check PATH
    path = shutil.which("yt-dlp")
    if path: return path
    return "yt-dlp"

YTDLP_EXE = get_ytdlp_path()

def discover_node():
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

NODE_EXE = discover_node()

BATCH_SCRIPT = os.path.join(CURRENT_DIR, "autosub_batch_pro.py")
SINGLE_SCRIPT = os.path.join(CURRENT_DIR, "autosub.py")
TOOLS_DIR = os.environ.get("AUTOSUB_TOOLS_DIR") or os.path.dirname(CURRENT_DIR)
PROJECT_ROOT = os.path.dirname(os.path.dirname(TOOLS_DIR))
DEFAULT_OUTPUT_ROOT = os.path.join(PROJECT_ROOT, "Projects")
PROJECT_REGISTRY_PATH = os.path.join(os.path.expanduser("~"), ".autosub", "projects.json")
LEGACY_PROJECT_SCAN_ROOTS = (
    os.path.join(os.path.expanduser("~"), "Downloads"),
    os.path.join(os.path.expanduser("~"), "Desktop"),
    os.path.join(os.path.expanduser("~"), "Documents"),
    os.path.join(os.path.expanduser("~"), "Movies"),
)
GDRIVE_FOLDER_ID = os.environ.get("AUTOSUB_GDRIVE_FOLDER_ID", "")


def discover_initial_cookies():
    """Finds cookies.txt in prioritized locations for the launcher."""
    paths = [
        os.path.join(CURRENT_DIR, "cookies.txt"),
        os.path.join(PROJECT_ROOT, "cookies.txt"),
    ]
    for p in paths:
        if os.path.exists(p): return p
    return None

INITIAL_COOKIES_PATH = discover_initial_cookies()

# CRITICAL: Use the exact same python executable that is running this launcher
PYTHON_EXE = sys.executable

console = Console()

# Set AppUserModelID for taskbar icon grouping
if sys.platform == "win32":
    windll = getattr(ctypes, 'windll', None)
    if windll:
        windll.shell32.SetCurrentProcessExplicitAppUserModelID("cc.autosub.batch.pro")

import re

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
    """Strip zero-width and formatting characters that break grid alignment."""
    return _ZERO_WIDTH_RE.sub('', str(s))

def cjk_display_width(s: str) -> int:
    """Calculate terminal display width using rich's cell_len.
    Ensures width calculation matches rich's internal table rendering."""
    return cell_len(s)


def cjk_truncate(s: str, max_width: int, ellipsis: str = '…') -> str:
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


def check_all_completed(state_data: Any) -> bool:
    """Helper to check if all tasks in the existing state file are completed."""
    if not isinstance(state_data, dict) or not state_data:
        return False
    # Handle both launcher direct mapping and Pro tasks wrapper
    tasks_map = state_data.get("tasks", state_data) if "tasks" in state_data else state_data
    if isinstance(tasks_map, dict) and tasks_map:
        for task_id, task_d in tasks_map.items():
            # Skip non-task metadata keys like "global_pause", "time" etc.
            if task_id in ("global_pause", "time") or not isinstance(task_d, dict):
                continue
            status = task_d.get("status", "")
            pcts = task_d.get("pcts", {})
            if status.startswith("超限"):
                # A platform duration limit is terminal, but it is not success.
                return False
            metadata_needed = bool(
                task_d.get("bili", state_data.get("bili", False))
                or task_d.get("wechat", state_data.get("wechat", False))
            )
            mg_done = (
                not metadata_needed
                or (isinstance(pcts, dict) and pcts.get("MG", 100.0) >= 100.0)
            )
            base_done = (status == "完成") or (
                isinstance(pcts, dict) and 
                pcts.get("BR", 0.0) >= 100.0 and 
                pcts.get("GD", 100.0) >= 100.0 and 
                pcts.get("BI", 100.0) >= 100.0
            )
            wechat_enabled = bool(task_d.get("wechat", state_data.get("wechat", False)))
            is_done = base_done and (
                not wechat_enabled
                or (isinstance(pcts, dict) and pcts.get("WX", 0.0) >= 100.0)
            ) and mg_done
            if not is_done:
                return False
        # Only return True if we found at least one actual task entry
        return any(isinstance(v, dict) and k not in ("global_pause", "time") for k, v in tasks_map.items())
    return False

import contextlib
import tempfile
import shutil

@contextlib.contextmanager
def use_temp_cookies(cookies_path):
    """Creates a temporary copy of cookies to prevent concurrent write corruption."""
    if not cookies_path or not os.path.exists(cookies_path):
        yield None
        return
    fd, temp_path = tempfile.mkstemp(suffix=".txt", prefix="yt_cookies_")
    os.close(fd)
    try:
        shutil.copy2(cookies_path, temp_path)
        yield temp_path
    finally:
        try:
            if os.path.exists(temp_path): os.remove(temp_path)
        except: pass

def fetch_metadata(url: str, cookies_path: Optional[str] = None, browser: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetches playlist/video metadata using yt-dlp."""
    label = "匿名"
    if cookies_path: label = f"Cookies: {os.path.basename(cookies_path)}"
    if browser: label = f"浏览器: {browser}"

    console.print(f"[bold blue]正在解析内容信息 ({label})，最长等待 90 秒...[/]")
    with console.status(f"[bold blue]正在解析内容信息 ({label})..."):
        cmd = [
            YTDLP_EXE,
            "--flat-playlist",
            "--dump-single-json",
            "--quiet",
            "--no-warnings"
        ]

        # Enhanced challenge solving for YouTube
        if "youtube.com" in url or "youtu.be" in url:
            cmd.extend(["--extractor-args", "youtube:player_client=web_safari"])
            cmd.extend(["--remote-components", "ejs:github"])
            if NODE_EXE != "node":
                cmd.extend(["--js-runtime", f"node:{NODE_EXE}"])
            else:
                cmd.extend(["--js-runtime", "node"])

        def run_cmd(full_cmd):
            try:
                if sys.platform == "win32":
                    process = subprocess.Popen(
                        full_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                    )
                else:
                    process = subprocess.Popen(
                        full_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                try:
                    stdout, stderr = process.communicate(timeout=90)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    return {"error": "解析内容信息超时（90 秒）。请检查网络或稍后重试。"}

                if process.returncode != 0:
                    err_msg = stderr.decode('utf-8', errors='ignore')
                    return {"error": err_msg}

                out = stdout.decode('utf-8', errors='ignore')
                if not out.strip():
                    return None
                return json.loads(out)
            except Exception as e:
                return {"error": str(e)}

        if browser:
            cmd.extend(["--cookies-from-browser", browser])
            cmd.append(url)
            return run_cmd(cmd)
        elif cookies_path:
            with use_temp_cookies(cookies_path) as temp_cookies:
                if temp_cookies:
                    cmd.extend(["--cookies", temp_cookies])
                cmd.append(url)
                return run_cmd(cmd)
        else:
            cmd.append(url)
            return run_cmd(cmd)

def format_duration(seconds):
    try:
        if seconds is None: return "未知"
        s = int(float(seconds))
        if s <= 0: return "未知"
        hrs, rem = divmod(s, 3600)
        mins, secs = divmod(rem, 60)
        if hrs > 0:
            return f"{hrs:02d}:{mins:02d}:{secs:02d}"
        return f"{mins:02d}:{secs:02d}"
    except:
        return "未知"

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

    is_inclusion = input_str.startswith('+') or input_str.lower().startswith('i')

    # Robust cleanup: remove leading +, i, I and any spaces
    clean_str = re.sub(r'^[+iI\s]+', '', input_str)

    parts = [p.strip() for p in clean_str.split(',') if p.strip()]
    selected = set()

    for p in parts:
        if '-' in p:
            try:
                s_e = [x.strip() for x in p.split('-') if x.strip()]
                if len(s_e) == 2:
                    start, end = int(s_e[0]), int(s_e[1])
                    selected.update(range(start, end + 1))
            except: pass
        else:
            m = re.search(r'(\d+)', p)
            if m:
                selected.add(int(m.group(1)))

    if is_inclusion:
        return {i for i in selected if 1 <= i <= total_count}
    else:
        return {i for i in range(1, total_count + 1) if i not in selected}

def is_cookie_error(err_msg):
    keywords = [
        "sign in", "cookies", "reloaded", "not a bot", "authentication",
        "logged in", "n challenge solving failed", "format is not available"
    ]
    err_lower = err_msg.lower()
    return any(k in err_lower for k in keywords)

def launch_engine(cmd):
    # Command Construction for Windows
    cmd_parts = []
    for i, arg in enumerate(cmd):
        s_arg = arg
        if any(c in s_arg for c in " &()+,-|:"):
            quoted_arg = f"'{s_arg}'"
            if i == 0:
                cmd_parts.append(f"& {quoted_arg}")
            else:
                cmd_parts.append(quoted_arg)
        else:
            cmd_parts.append(s_arg)

    ps_inner_cmd = " ".join(cmd_parts)
    escaped_inner = ps_inner_cmd.replace('"', '\"')
    ps_command = f'chcp 65001; & {{ {escaped_inner} }}'

    try:
        # The .app bundle execs a homebrew interpreter with PYTHONHOME set, so
        # clearing PYTHONPATH makes the engine resolve imports against
        # homebrew's site-packages, which has rich and playwright but no
        # psutil. Every process-management path in the engine then silently
        # no-ops: orphans survive a restart and keep the shared WeChat browser
        # profile locked, a wedged upload is never killed, and because the
        # WeChat slot is serial that one wedged upload parks every remaining
        # task at "queued" until the engine is restarted by hand. Point
        # PYTHONPATH at the project venv instead of dropping it.
        site_packages = venv_site_packages()
        if site_packages:
            os.environ["PYTHONPATH"] = os.pathsep.join([site_packages, CURRENT_DIR])
        elif "PYTHONPATH" in os.environ:
            # No venv to point at; keep the old behaviour rather than inherit a
            # PYTHONPATH from an unrelated environment.
            del os.environ["PYTHONPATH"]
        os.environ["PYTHONUTF8"] = "1"

        if sys.platform == "win32":
            final_cmd = f'start "AutoSub Engine" powershell -NoExit -Command "{ps_command}"'
            subprocess.Popen(final_cmd, shell=True)
            console.print("\n[bold blue]✅ 任务已移交。[/]")
        elif sys.platform == "darwin":
            console.print("\n[bold blue]🚀 正在启动 AutoSub 压制引擎，请稍候...[/]\n")
            # Replace this process so batch_pro inherits our stdout pipe
            # (terminal_gui captures output via subprocess.Popen).
            os.execv(cmd[0], cmd)
        else:
            subprocess.Popen(cmd)
            console.print("\n[bold blue]✅ 任务已在后台启动。[/]")
    except Exception as e:
        console.print(f"[bold red]启动失败: {e}[/]")
        input("\n按回车键退出...")

def is_project_running(project_path):
    lock_file = os.path.join(project_path, "batch_engine.lock")
    if os.path.exists(lock_file):
        try:
            with open(lock_file, "r") as f:
                pid = int(f.read().strip())
            import psutil
            if psutil.pid_exists(pid):
                try:
                    p = psutil.Process(pid)
                    cmdline = p.cmdline()
                    # Robust check: verify the process command line contains the script name
                    if any("autosub_batch_pro.py" in arg for arg in cmdline):
                        return True, pid
                except:
                    pass
        except:
            pass
    return False, None


# ---------------------------------------------------------------------------
# Project Pause Helper
# ---------------------------------------------------------------------------

def pause_project(project_path):
    """Mark all tasks in a project's batch_state.json as paused and set global_pause.
    If the project engine is still running, the _pause_monitor thread inside
    batch_pro will detect the state change within ~1 second and kill active
    sub-processes, releasing GPU/CPU resources immediately."""
    state_path = os.path.join(project_path, "batch_state.json")
    if not os.path.exists(state_path):
        return False
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Support both nested {"tasks": {...}} and flat {uid: {...}} formats
        if isinstance(data, dict) and "tasks" in data:
            tasks = data["tasks"]
        else:
            tasks = data
        if isinstance(tasks, dict):
            for key, val in tasks.items():
                if key in ("global_pause", "time", "bili", "gdsync"):
                    continue
                if isinstance(val, dict):
                    val["is_paused"] = True
        data["global_pause"] = True
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        console.print(f"[bold red]挂起项目时出错: {e}[/]")
        return False


def has_uncompleted_tasks(project_path):
    """Check if a project directory has a batch_state.json with uncompleted tasks."""
    state_path = os.path.join(project_path, "batch_state.json")
    if not os.path.exists(state_path):
        return False
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state_data = json.load(f)
        return not check_all_completed(state_data)
    except Exception:
        return False


def _load_project_registry() -> Set[str]:
    """Load project paths recorded by local-folder mode."""
    try:
        with open(PROJECT_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return set()

    if not isinstance(data, list):
        return set()
    return {
        os.path.abspath(str(path))
        for path in data
        if isinstance(path, str) and os.path.isdir(path)
    }


def register_project(project_path: str):
    """Persist a project location so restart can find it outside DEFAULT_OUTPUT_ROOT."""
    project_path = os.path.abspath(project_path)
    if not os.path.isdir(project_path):
        return

    registered = _load_project_registry()
    if project_path in registered:
        return
    registered.add(project_path)

    registry_dir = os.path.dirname(PROJECT_REGISTRY_PATH)
    os.makedirs(registry_dir, exist_ok=True)
    temp_path = PROJECT_REGISTRY_PATH + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(sorted(registered), f, ensure_ascii=False, indent=2)
        os.replace(temp_path, PROJECT_REGISTRY_PATH)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _candidate_project_directories() -> Set[str]:
    """Collect default, indexed, and legacy local project folders."""
    candidates: Set[str] = set()

    def add_children(root: str):
        try:
            entries = os.scandir(root)
        except OSError:
            return
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir():
                        candidates.add(os.path.abspath(entry.path))
                except OSError:
                    continue

    add_children(DEFAULT_OUTPUT_ROOT)
    for root in LEGACY_PROJECT_SCAN_ROOTS:
        add_children(root)
    candidates.update(_load_project_registry())
    return candidates


def find_uncompleted_projects() -> List[tuple]:
    """Find unfinished projects across the default root and local-folder locations."""
    projects = []
    for project_path in _candidate_project_directories():
        state_path = os.path.join(project_path, "batch_state.json")
        if not os.path.isfile(state_path):
            continue
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state_data = json.load(f)
            if not check_all_completed(state_data):
                projects.append((os.path.basename(project_path), project_path, state_data))
        except (OSError, json.JSONDecodeError):
            continue

    def state_mtime(project):
        state_path = os.path.join(project[1], "batch_state.json")
        try:
            return os.path.getmtime(state_path)
        except OSError:
            return 0.0

    return sorted(projects, key=state_mtime, reverse=True)


# ---------------------------------------------------------------------------
# Model Selection Onboarding
# ---------------------------------------------------------------------------

def _load_env_file() -> dict:
    """Parse the local .env for API key values (non-sensitive check)."""
    env_path = os.path.join(CURRENT_DIR, ".env")
    result = {}
    if not os.path.exists(env_path):
        return result
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                v = v.strip().strip('"').strip("'")
                if v:
                    result[k.strip()] = v
    except:
        pass
    return result


def _load_settings() -> dict:
    """Load settings.json (preferred) or defaults.json."""
    s_path = os.path.join(CURRENT_DIR, "settings.json")
    d_path = os.path.join(CURRENT_DIR, "defaults.json")
    target = s_path if os.path.exists(s_path) else d_path
    if os.path.exists(target):
        try:
            with open(target, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {"llm_model": "glm-5.3", "llm_vendor": "Alibaba (Bailian)", "style": "auto"}


def _save_settings(data: dict):
    """Persist updated settings to settings.json."""
    s_path = os.path.join(CURRENT_DIR, "settings.json")
    with open(s_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


_PROVIDER_CATALOG = [
    {
        "name": "Google Gemini / Vertex AI",
        "keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT",
                 "VERTEX_SA_KEY_PATH", "GOOGLE_APPLICATION_CREDENTIALS"],
        "models": [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
            "gemini-3.1-flash-preview",
            "gemini-3-flash-preview",
            "gemini-3.1-pro-preview",
        ],
        "default": "gemini-2.5-flash",
    },
    {
        "name": "OpenAI",
        "keys": ["OPENAI_API_KEY"],
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o4-mini"],
        "default": "gpt-4o",
    },
    {
        "name": "DeepSeek",
        "keys": ["DEEPSEEK_API_KEY"],
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default": "deepseek-chat",
    },
    {
        "name": "DashScope (Alibaba Cloud)",
        "keys": ["DASHSCOPE_API_KEY"],
        "models": ["deepseek-v4-pro", "qwen-plus", "qwen-max", "qwen-turbo"],
        "default": "deepseek-v4-pro",
    },
    {
        "name": "Moonshot (Kimi)",
        "keys": ["MOONSHOT_API_KEY"],
        "models": ["kimi-k2", "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "default": "kimi-k2",
    },
    {
        "name": "ZhipuAI (GLM)",
        "keys": ["ZHIPUAI_API_KEY", "ZHIPU_API_KEY"],
        "models": ["glm-4", "glm-4-flash", "glm-4-plus", "glm-4-long"],
        "default": "glm-4",
    },
    {
        "name": "SiliconFlow",
        "keys": ["SILICONFLOW_API_KEY"],
        "models": [
            "deepseek-ai/DeepSeek-V3",
            "deepseek-ai/DeepSeek-R1",
            "Qwen/Qwen2.5-72B-Instruct",
        ],
        "default": "deepseek-ai/DeepSeek-V3",
    },
]


def _test_model_connectivity(model_name: str) -> tuple:
    """Make a minimal test call to verify the model+provider is reachable.
    Returns (success: bool, message: str)."""
    common_dir = os.path.join(TOOLS_DIR, "common")
    if common_dir not in sys.path:
        sys.path.insert(0, common_dir)
    try:
        from llm_utils import LLMClient  # type: ignore
        client = LLMClient()
        provider = client._get_provider(model_name)
        ok, result = client.generate_content_with_provider(
            prompt="Reply with exactly one word: PONG",
            provider=provider,
            model_name=model_name,
            fallback=False,
        )
        if ok and result:
            return True, result.strip()[:120]
        return False, result or "(空响应)"
    except Exception as e:
        return False, str(e)


def onboard_model_selection():
    """Interactive model provider / model selection step.
    Called during launcher onboarding; defaults to no change."""
    settings = _load_settings()
    current_model = settings.get("llm_model", "glm-5.3")

    console.print(f"\n[dim]翻译模型: [bold cyan]{current_model}[/bold cyan][/dim]")
    if not Confirm.ask("是否修改模型设置?", default=False):
        return

    env_vars = _load_env_file()
    # Merge with live os.environ so cloud-injected keys are also visible
    combined = {**env_vars, **{k: v for k, v in os.environ.items() if v}}

    available = [
        p for p in _PROVIDER_CATALOG
        if any(combined.get(k) for k in p["keys"])
    ]

    if not available:
        console.print(
            Panel(
                "未在 [cyan].env[/] 中找到任何可用的 API 密钥。\n"
                "请先配置至少一个厂商的密钥后重试。",
                title="[bold yellow]⚠️  无可用厂商[/]",
                border_style="yellow",
            )
        )
        return

    while True:
        # ── Step 1: Choose provider ──────────────────────────────────────────
        console.print("\n[bold cyan]可用模型厂商：[/]")
        for i, p in enumerate(available, 1):
            console.print(f"  [bold]{i}.[/] {p['name']}")

        prov_choice = Prompt.ask(
            "请选择厂商编号",
            choices=[str(i) for i in range(1, len(available) + 1)],
            default="1",
        )
        selected = available[int(prov_choice) - 1]

        # ── Step 2: Choose model ─────────────────────────────────────────────
        models = selected["models"]
        default_model = selected["default"]

        console.print(f"\n[bold cyan]{selected['name']} 可选模型：[/]")
        for i, m in enumerate(models, 1):
            tag = " [dim green](推荐)[/dim green]" if m == default_model else ""
            console.print(f"  [bold]{i}.[/] {m}{tag}")
        console.print(f"  [bold]c.[/] 手动输入模型名称")

        model_raw = Prompt.ask("请选择模型编号或输入 c", default="1")

        if model_raw.strip().lower() == 'c':
            chosen_model = Prompt.ask("请输入完整模型名称").strip()
        else:
            try:
                chosen_model = models[int(model_raw.strip()) - 1]
            except (ValueError, IndexError):
                console.print("[red]无效选择，请重试。[/]")
                continue

        if not chosen_model:
            console.print("[red]模型名称不能为空，请重试。[/]")
            continue

        # ── Step 3: Connectivity test ────────────────────────────────────────
        with console.status(
            f"[bold blue]正在测试 {selected['name']} · {chosen_model} 连通性...[/]"
        ):
            ok, msg = _test_model_connectivity(chosen_model)

        if ok:
            console.print(f"[bold green]✅ 连接成功！[/] 响应: [dim]{msg}[/dim]")
            settings["llm_model"] = chosen_model
            settings["llm_vendor"] = selected["name"]
            _save_settings(settings)
            console.print(
                f"\n✨ 模型已更新为 [bold cyan]{chosen_model}[/bold cyan]，配置已保存。"
            )
            break
        else:
            console.print(
                Panel(
                    f"[bold red]❌ 连接测试失败[/bold red]\n\n{msg[:400]}",
                    title=f"{selected['name']} · {chosen_model}",
                    border_style="red",
                )
            )
            if not Confirm.ask("是否重新选择?", default=True):
                console.print("[dim]模型设置未变更。[/dim]")
                break


def confirm_wechat_upload(state: Optional[Dict[str, Any]] = None):
    """Ask for WeChat Channels upload settings with safe defaults."""
    # Resume path: reuse saved preference instead of re-asking every time
    if isinstance(state, dict) and "wechat" in state:
        enabled = bool(state.get("wechat", False))
        public = bool(state.get("wechat_public", False))
        return enabled, public

    # New project path: prompt the user
    choice = Prompt.ask(
        "\n微信视频号上传方式 (1: 不上传 / 2: 上传并保存草稿 / 3: 上传并公开发布)",
        choices=["1", "2", "3"],
        default="2",
    )
    enabled = choice in ("2", "3")
    public = choice == "3"

    if isinstance(state, dict):
        state["wechat"] = enabled
        state["wechat_public"] = public
    return enabled, public


def ask_wechat_cover_title():
    """Ask for an optional cover-only headline; blank keeps subtitle grounding."""
    return Prompt.ask(
        "\n微信封面标题 (留空自动按字幕提炼，仅影响封面画面)",
        default="",
    ).strip()


LOCAL_VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov", ".ts")


def is_local_source_video(filename: str) -> bool:
    """Whether a folder entry is an input video rather than a prior output."""
    name = filename.lower()
    return (
        name.endswith(LOCAL_VIDEO_EXTENSIONS)
        and "_hardsub" not in name
        and not name.endswith(".part")
        and not re.search(r"\.f\d+\.", name)
    )


def find_local_video_files(folder_path: str) -> List[str]:
    """Find source videos placed directly in a user-selected folder."""
    try:
        entries = os.listdir(folder_path)
    except OSError:
        return []

    videos = []
    for entry in entries:
        path = os.path.join(folder_path, entry)
        if os.path.isfile(path) and is_local_source_video(entry):
            videos.append(path)
    return sorted(videos, key=lambda path: os.path.basename(path).lower())


def safe_local_video_title(path: str) -> str:
    title = os.path.splitext(os.path.basename(path))[0].strip()
    title = re.sub(r'[\\/*?:"<>|]', "_", title)[:80].strip()
    return title or "Untitled Video"


def link_local_video(source_path: str, workdir: str) -> str:
    """Put a cheap filesystem link to the source video in a task workspace."""
    destination = os.path.join(workdir, os.path.basename(source_path))
    if os.path.lexists(destination):
        if os.path.isdir(destination) and not os.path.islink(destination):
            raise RuntimeError(f"Task video path is a directory: {destination}")
        os.remove(destination)

    try:
        os.link(source_path, destination)
    except OSError:
        os.symlink(source_path, destination)
    return destination


def build_local_video_tasks(folder_path: str, videos: List[str]) -> Dict[str, Any]:
    """Create or resume one isolated workspace for every local video."""
    state_path = os.path.join(folder_path, "batch_state.json")
    state: Dict[str, Any] = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, json.JSONDecodeError):
            state = {}

    existing_tasks = {
        key: value
        for key, value in state.items()
        if str(key).isdigit() and isinstance(value, dict)
    }
    next_uid = max((int(key) for key in existing_tasks), default=0)

    for index, video_path in enumerate(videos, 1):
        source_path = os.path.abspath(video_path)
        existing = None
        for value in existing_tasks.values():
            if os.path.abspath(str(value.get("local_video", ""))) == source_path:
                existing = value
                break

        safe_title = safe_local_video_title(source_path)
        if existing and existing.get("workdir"):
            workdir = os.path.abspath(str(existing["workdir"]))
        else:
            workdir = os.path.join(
                folder_path, f"[{index:02d}] - {safe_title} [local]"
            )

        os.makedirs(workdir, exist_ok=True)
        link_local_video(source_path, workdir)

        if existing:
            existing.update(
                {
                    "local_video": source_path,
                    "title": existing.get("title") or safe_title,
                    "workdir": workdir,
                }
            )
            continue

        next_uid += 1
        state[str(next_uid)] = {
            "uid": next_uid,
            "url": f"local:{source_path}",
            "vid_id": "local",
            "title": safe_title,
            "duration": "未知",
            "status": "排队中 (TR)",
                "pcts": {
                "DL": 100.0,
                "TR": 0.0,
                "TL": 0.0,
                "MR": 0.0,
                "BR": 0.0,
                "GD": 0.0,
                "MG": 0.0,
                "BI": 0.0,
                "WX": 0.0,
            },
            "workdir": workdir,
            "local_video": source_path,
            "error": None,
            "is_paused": False,
            "retries": 0,
        }

    return state


def process_local_folder_mode(folder_path: str, videos: List[str]):
    """Turn a folder of direct video files into normal AutoSub tasks."""
    folder_path = os.path.abspath(folder_path)
    proj_name = os.path.basename(folder_path)
    state = build_local_video_tasks(folder_path, videos)
    task_items = [
        (key, value)
        for key, value in state.items()
        if str(key).isdigit() and isinstance(value, dict)
    ]
    if not task_items:
        console.print("[bold red]❌ 未找到可处理的视频文件。[/]")
        return

    console.print(f"\n[bold cyan]📁 本地视频文件夹模式: {proj_name}[/]")
    console.print(f"[dim]发现 {len(task_items)} 个视频，将在独立任务目录中生成字幕。[/]\n")

    table = Table(box=box.SIMPLE_HEAD, expand=True, border_style="bright_blue")
    table.add_column("序号", width=6, style="bold cyan")
    table.add_column("视频名称", overflow="ellipsis", no_wrap=True)
    for i, (_, task) in enumerate(task_items, 1):
        display_title = cjk_truncate(
            str(task.get("title", "")), max(20, console.width - 6 - 6)
        )
        table.add_row(str(i), display_title)
    console.print(table)

    do_sync = Confirm.ask("\n是否同步到 Google Drive?", default=False)
    do_bili = Confirm.ask("\n是否上传到 Bilibili?", default=False)
    do_wechat, do_wechat_public = confirm_wechat_upload(state)
    quality = Prompt.ask(
        "\n选择压制字幕画质",
        choices=["standard", "high", "lossless"],
        default="high",
    )
    onboard_model_selection()

    state["bili"] = do_bili
    state["gdsync"] = do_sync
    state["wechat"] = do_wechat
    state["wechat_public"] = do_wechat_public
    state_path = os.path.join(folder_path, "batch_state.json")
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    register_project(folder_path)

    cmd = [
        PYTHON_EXE,
        BATCH_SCRIPT,
        "--output",
        os.path.dirname(folder_path),
        "--sub-dir-name",
        proj_name,
        "--quality",
        quality,
    ]
    if do_sync:
        cmd.append("--gdsync")
    if do_bili:
        cmd.append("--bili-upload")
    if do_wechat:
        cmd.append("--wechat-upload")
        if do_wechat_public:
            cmd.append("--wechat-public")

    console.print("\n[bold green]🚀 启动本地视频字幕引擎...[/]")
    launch_engine(cmd)


def process_folder_mode(folder_path: str):
    """Folder re-processing mode: skip download/transcription, re-run from translate/burn/upload."""
    folder_path = os.path.abspath(folder_path)
    proj_name = os.path.basename(folder_path)

    direct_videos = find_local_video_files(folder_path)
    if direct_videos:
        process_local_folder_mode(folder_path, direct_videos)
        return

    state_path = os.path.join(folder_path, "batch_state.json")
    existing_state = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                existing_state = json.load(f)
        except Exception:
            pass

    found_tasks = []
    for entry in sorted(os.listdir(folder_path)):
        sub_path = os.path.join(folder_path, entry)
        if not os.path.isdir(sub_path):
            continue
        try:
            files = os.listdir(sub_path)
        except Exception:
            continue
        videos = [f for f in files if f.lower().endswith(LOCAL_VIDEO_EXTENSIONS) and "_hardsub" not in f and not re.search(r'\.f\d+\.', f)]
        if not videos:
            continue
        srts = [f for f in files if f.endswith(".srt") and not re.search(r'(\.zh|\.cn|\.bi)\.srt$', f)]
        if not srts:
            continue
        task_meta = {}
        if isinstance(existing_state, dict):
            for k, v in existing_state.items():
                if isinstance(v, dict) and v.get("workdir") and os.path.abspath(v["workdir"]) == os.path.abspath(sub_path):
                    task_meta = v
                    break
        found_tasks.append((entry, sub_path, videos[0], srts[0], files, task_meta))

    if not found_tasks:
        console.print("[bold red]❌ 未找到包含视频和英文 SRT 的子目录。[/]")
        console.print("[dim]Folder 模式要求每个视频子目录已有原始视频和英文 SRT（转录产物）。[/]")
        input("\n按回车键退出...")
        return

    console.print(f"\n[bold cyan]📁 文件夹重处理模式: {proj_name}[/]")
    console.print(f"[dim]发现 {len(found_tasks)} 个已转录视频，将跳过下载和转录阶段。[/]\n")

    table = Table(box=box.SIMPLE_HEAD, expand=True, border_style="bright_blue")
    table.add_column("序号", width=6, style="bold cyan")
    table.add_column("时长", width=10, justify="right", style="green")
    table.add_column("视频名称", overflow="ellipsis", no_wrap=True)

    for i, (entry, sub_path, vid, srt, files, meta) in enumerate(found_tasks, 1):
        title = meta.get("title", entry)
        duration = meta.get("duration", "未知")
        display_title = cjk_truncate(title, max(20, console.width - 6 - 10 - 6))
        table.add_row(str(i), duration, display_title)
    console.print(table)

    console.print("\n[bold yellow]请选择重处理范围：[/]")
    console.print("  [bold]1.[/] 重新翻译+烧制+上传 [dim](删除翻译字幕、硬字幕视频、所有标记)[/]")
    console.print("  [bold]2.[/] 仅重新上传        [dim](保留硬字幕视频，重新上传)[/]")
    reset_choice = Prompt.ask("\n您的选择", choices=["1", "2"], default="1")
    full_reset = (reset_choice == "1")

    cleared = {"srt": 0, "ass": 0, "hardsub": 0, "markers": 0}
    for entry, sub_path, vid, srt, files, meta in found_tasks:
        all_files = os.listdir(sub_path)

        if full_reset:
            for f in all_files:
                if re.search(r'(\.zh|\.cn|\.bi)\.srt$', f) or f.endswith(".ass"):
                    try:
                        os.remove(os.path.join(sub_path, f))
                        cleared["srt" if f.endswith(".srt") else "ass"] += 1
                    except Exception:
                        pass

        if full_reset:
            for f in all_files:
                if "_hardsub" in f and f.endswith((".mp4", ".mkv", ".webm")):
                    try:
                        os.remove(os.path.join(sub_path, f))
                        cleared["hardsub"] += 1
                    except Exception:
                        pass

        markers_to_delete = [".synced", ".bili_uploaded", ".wechat_uploaded"]
        if full_reset:
            markers_to_delete.append(".burn_complete")
        for marker in markers_to_delete:
            marker_path = os.path.join(sub_path, marker)
            if os.path.exists(marker_path):
                try:
                    os.remove(marker_path)
                    cleared["markers"] += 1
                except Exception:
                    pass

    console.print(f"\n[bold green]✅ 清理完成: 删除 {cleared['srt']} 个翻译字幕, {cleared['ass']} 个 ASS, "
                  f"{cleared['hardsub']} 个硬字幕视频, {cleared['markers']} 个标记文件。[/]")

    do_sync = Confirm.ask("\n是否同步到 Google Drive?", default=False)
    do_bili = Confirm.ask("\n是否上传到 Bilibili?", default=False)
    do_wechat, do_wechat_public = confirm_wechat_upload(existing_state)
    quality = Prompt.ask("\n选择压制字幕画质", choices=["standard", "high", "lossless"], default="high")
    onboard_model_selection()

    if not os.path.exists(state_path):
        task_map = {}
        for i, (entry, sub_path, vid, srt, files, meta) in enumerate(found_tasks, 1):
            vid_id = meta.get("vid_id", "")
            task_map[str(i)] = {
                "uid": i,
                "url": meta.get("url", f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""),
                "vid_id": vid_id,
                "title": meta.get("title", entry),
                "duration": meta.get("duration", "未知"),
                "status": "等待中",
                "pcts": {"DL": 0.0, "TR": 0.0, "TL": 0.0, "MR": 0.0, "BR": 0.0, "GD": 0.0, "MG": 0.0, "BI": 0.0, "WX": 0.0},
                "workdir": sub_path,
                "error": None,
                "is_paused": False,
                "retries": 0,
            }
        task_map["bili"] = do_bili
        task_map["gdsync"] = do_sync
        task_map["wechat"] = do_wechat
        task_map["wechat_public"] = do_wechat_public
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(task_map, f, ensure_ascii=False, indent=2)

    cmd = [
        PYTHON_EXE, BATCH_SCRIPT,
        "--output", os.path.dirname(folder_path),
        "--sub-dir-name", proj_name,
        "--quality", quality,
    ]
    if do_sync:
        cmd.append("--gdsync")
    if do_bili:
        cmd.append("--bili-upload")
    if do_wechat:
        cmd.append("--wechat-upload")
        if do_wechat_public:
            cmd.append("--wechat-public")

    console.print(f"\n[bold green]🚀 启动文件夹重处理引擎...[/]")
    register_project(folder_path)
    launch_engine(cmd)


def main():
    console.print("[bold cyan]========================================================[/]")
    console.print("[bold cyan]              AutoSub 交互式任务启动器[/]")
    console.print("[dim]        引导式配置 · 精准环境锁定 · 语法安全模式[/]")
    console.print("[bold cyan]========================================================[/]")

    # Check for existing projects with uncompleted tasks
    uncompleted_projects = find_uncompleted_projects()

    if uncompleted_projects:
        # Check active status of uncompleted projects
        running_projects_status = {}
        for name, proj_path, _ in uncompleted_projects:
            is_running, pid = is_project_running(proj_path)
            running_projects_status[name] = (is_running, pid)

        console.print("[bold yellow]⚠️ 检测到未完成的项目：[/]")
        for i, (name, proj_path, _) in enumerate(uncompleted_projects, 1):
            is_run, pid = running_projects_status[name]
            status_suffix = f" [bold green](运行中 🟢 PID {pid})[/]" if is_run else ""
            console.print(f"  [{i}] {name}{status_suffix}")
        
        # Build options for all cases
        choices = [str(i) for i in range(1, len(uncompleted_projects) + 1)] + ["n", "q"]
        console.print("  [n] 开始全新项目（挂起未完成的项目）")
        console.print("  [q] 退出")
        choice = Prompt.ask("\n请选择您要继续的项目或输入操作", choices=choices, default="1")
        if choice == "q":
            return

        if choice == "n":
            # User wants to start a new project. Pause any running projects first.
            for name, proj_path, _ in uncompleted_projects:
                is_run, pid = running_projects_status[name]
                if is_run:
                    console.print(f"\n⏸️  [bold yellow]正在挂起运行中的项目 [cyan]{name}[/] (PID {pid})...[/]")
                    if pause_project(proj_path):
                        console.print(f"✅ [bold green]项目 [cyan]{name}[/] 已挂起，进度已保留，可随时恢复。[/]")
                        time.sleep(1.5)  # Give _pause_monitor a moment to kill sub-processes
                    else:
                        console.print(f"⚠️  [bold red]挂起项目 [cyan]{name}[/] 失败，请手动结束后重试。[/]")
                        return
            # Fall through to URL input below
        else:
            idx = int(choice) - 1
            proj_name, proj_path, state_data = uncompleted_projects[idx]
            is_run, pid = running_projects_status[proj_name]
            project_root = os.path.dirname(proj_path)

            if is_run:
                console.print(f"\n✨ [bold green]检测到项目 [cyan]{proj_name}[/] 正在后台运行 (PID {pid})。[/]")
                console.print("📈 [bold cyan]将自动为您唤醒【只读 Live 看板监控模式】，查看实时进度...[/]")
                time.sleep(2.0)
                cmd = [
                    PYTHON_EXE,
                    BATCH_SCRIPT,
                    "--output", project_root,
                    "--sub-dir-name", proj_name
                ]
                launch_engine(cmd)
                return

            quality = "high"
            cookies_path = None
            browser = None
            do_sync = False
            no_sequence = False

            if isinstance(state_data, dict):
                # Robust state check: support both nested "tasks" structure and flat task list
                tasks = state_data.get("tasks", state_data) if "tasks" in state_data else state_data
                if tasks:
                    # Find first actual task entry, skipping metadata keys like "global_pause", "time"
                    first_task = next((v for k, v in tasks.items() if isinstance(v, dict) and k not in ("global_pause", "time")), None)
                    quality = first_task.get("quality", "high") if isinstance(first_task, dict) else "high"
                    cookies_path = state_data.get("cookies", first_task.get("cookies") if isinstance(first_task, dict) else None)
                    browser = state_data.get("browser", first_task.get("browser") if isinstance(first_task, dict) else None)
                    do_sync = state_data.get("gdsync", first_task.get("gdsync", False) if isinstance(first_task, dict) else False)
                    # Auto deduct if previous project skipped sequence prefix
                    workdir = first_task.get("workdir", "")
                    if workdir:
                        folder_name_only = os.path.basename(workdir)
                        if not re.match(r'^\[\d+\]\s*-', folder_name_only):
                            no_sequence = True

            cmd = [
                PYTHON_EXE,
                BATCH_SCRIPT,
                "--output", project_root,
                "--sub-dir-name", proj_name,
                "--quality", quality
            ]
            if no_sequence:
                cmd.append("--no-sequence")
            if cookies_path and os.path.exists(cookies_path):
                cmd.extend(["--cookies", cookies_path])
            elif browser:
                cmd.extend(["--cookies-from-browser", browser])
            do_bili = state_data.get("bili", False)
            do_wechat, do_wechat_public = confirm_wechat_upload(state_data)
            if do_sync:
                cmd.append("--gdsync")
            if do_bili:
                cmd.append("--bili-upload")
            if do_wechat:
                cmd.append("--wechat-upload")
                if do_wechat_public:
                    cmd.append("--wechat-public")

            launch_engine(cmd)
            return

    # 1. URL Input
    url_input = ""
    while not url_input:
        url_input = Prompt.ask("\n[bold yellow]请输入播放列表/视频 URL、.txt 文件路径、或已有项目文件夹路径[/]").strip().strip('"')

    # Folder re-processing mode: skip download/transcription, re-run from translate/burn/upload
    if os.path.isdir(url_input):
        process_folder_mode(url_input)
        return

    urls = []
    is_from_file = False
    if url_input.lower().endswith(".txt") and os.path.exists(url_input):
        is_from_file = True
        try:
            with open(url_input, "r", encoding="utf-8") as f:
                urls = []
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"): continue
                    # Extract the first URL found in the line
                    url_match = re.search(r'(https?://[^\s,]+)', line)
                    if url_match:
                        urls.append(url_match.group(1))
                    else:
                        # Fallback to whole line if no protocol found but looks like a URL
                        urls.append(line)
        except Exception as e:
            console.print(f"[bold red]❌ 无法读取文件: {e}[/]")
            return
        if not urls:
            console.print("[bold red]❌ 错误：[/]文件内容为空。")
            return
        console.print(f"📖 [cyan]已从文件加载 {len(urls)} 个 URL[/]")
    else:
        urls = [url_input]

    # Metadata Fetch Loop
    current_cookies = INITIAL_COOKIES_PATH if (INITIAL_COOKIES_PATH and os.path.exists(INITIAL_COOKIES_PATH)) else None
    # On macOS, default to Chrome cookies (aligned with batch_pro._discover_cookies)
    if current_cookies is None and sys.platform == "darwin":
        current_browser = "chrome"
    else:
        current_browser = None
    all_entries: List[Dict[str, Any]] = []
    final_title = None
    data: Optional[Dict[str, Any]] = None

    for i, url in enumerate(urls):
        data = None
        while True:
            data = fetch_metadata(url, cookies_path=current_cookies, browser=current_browser)
            if data and "error" not in data: break

            err_detail = data.get("error", "未知错误") if data else "空返回"
            if is_cookie_error(err_detail):
                console.print(Panel(f"[bold red]❌ 权限验证失败 (URL #{i+1})[/]\n\n{err_detail.strip()}", title="身份验证", border_style="yellow"))
                console.print("  [bold]1.[/] 指定新的 [cyan]cookies.txt[/] 路径\n  [bold]2.[/] 尝试从 [cyan]Chrome[/] 读取 Cookies\n  [bold]3.[/] 忽略 Cookies，尝试匿名访问\n  [bold]q.[/] 退出程序")
                sub_choice = Prompt.ask("\n您的选择", choices=["1", "2", "3", "q"], default="1")
                if sub_choice == "1":
                    new_path = Prompt.ask("\n请输入 cookies.txt 的完整路径").strip().strip('"')
                    if os.path.exists(new_path): current_cookies, current_browser = new_path, None
                elif sub_choice == "2": current_browser, current_cookies = "chrome", None
                elif sub_choice == "3": current_cookies, current_browser = None, None
                else: return
            else:
                console.print(Panel(f"[bold red]❌ 抓取失败 (URL #{i+1})[/]\n\n{err_detail}", title="错误"))
                if is_from_file:
                    if Confirm.ask("跳过此 URL 继续?", default=True):
                        data = None
                        break
                    else: return
                else:
                    if not Confirm.ask("\n是否要更换 URL 重试?"): return
                    url = Prompt.ask("\n[bold yellow]请输入新的 URL[/]").strip()

        if not data: continue

        # Determine if this specific URL is a playlist
        is_p = 'entries' in data and data['entries'] is not None and len(data.get('entries', [])) > 0
        if is_p:
            all_entries.extend([e for e in data['entries'] if e])
            if not final_title: final_title = data.get('title')
        else:
            all_entries.append(data)
            if not final_title: final_title = data.get('title')

    if not all_entries:
        console.print("[bold red]❌ 错误：[/]未找到任何有效的视频信息。")
        input("\n按回车键退出..."); return

    # Construct virtual data object
    if is_from_file:
        data = {
            'title': final_title or os.path.splitext(os.path.basename(url_input))[0],
            'entries': all_entries
        }
    else:
        # For single URL input, ensure entries is populated for consistent downstream logic
        if not isinstance(data, dict):
            data = {}
        data['entries'] = all_entries

    # Normalize entries
    # Improved playlist detection: check for 'entries' key AND ensure it's not a single entry that is also a playlist
    is_playlist = 'entries' in data and data['entries'] is not None and len(data.get('entries', [])) > 0

    # Extra safety: if it's a playlist but it only has 1 entry and its ID matches the playlist ID, treat as single video
    if is_playlist and len(data['entries']) == 1:
        if data['entries'][0].get('id') == data.get('id'):
            is_playlist = False

    entries = [e for e in data.get('entries', []) if e] if is_playlist else [data]

    if not entries:
        console.print("[bold red]❌ 错误：[/]未找到有效的视频条目。")
        input("\n按回车键退出..."); return

    is_single_video = len(entries) == 1 and not is_from_file
    playlist_title = data.get('title') or data.get('playlist_title') or entries[0].get('title') or 'New_Project'
    clean_title = re.sub(r'[\\/*?:"<>|_]', ' ', playlist_title).strip()

    # 2. Show Summary Table
    term_w = console.width
    idx_w = 6
    dur_w = 10
    # padding: 3 cols * 2 = 6 cells
    title_w = max(20, term_w - idx_w - dur_w - 6)

    # Print title separately to avoid Rich's CJK title-width bug
    console.print()
    title_content = f"内容预览: {playlist_title}"
    title_content = sanitize_title(title_content)
    title_display_w = cjk_display_width(title_content)
    pad_left = max(0, (term_w - title_display_w) // 2)
    console.print(' ' * pad_left + f"[bold cyan]内容预览:[/] {sanitize_title(playlist_title)}")

    table = Table(
        box=box.SIMPLE_HEAD,
        expand=True,
        border_style="bright_blue"
    )
    table.add_column("序号", width=idx_w, style="bold cyan")
    table.add_column("时长", width=dur_w, justify="right", style="green")
    table.add_column("视频名称", width=title_w, overflow="ellipsis", no_wrap=True)

    durations = []
    for i, entry in enumerate(entries, 1):
        d = entry.get('duration')
        durations.append((i, d if d is not None else 0))
        display_title = clean_title_text(entry.get('title', '未知视频'), playlist_title)
        # CJK-aware truncation to prevent column overflow
        display_title = cjk_truncate(display_title, title_w)
        table.add_row(str(i), format_duration(d), display_title)
    console.print(table)

    # 3. Suggestions & Folder
    use_sequence = True
    exclude_input = ""
    if not is_single_video:
        exclude_suggestion = ""
        if len(durations) > 5:
            valid_durs = [d for d in durations if d[1] > 0]
            if valid_durs:
                shortest, longest = min(valid_durs, key=lambda x: x[1]), max(valid_durs, key=lambda x: x[1])
                exclude_suggestion = f"{shortest[0]},{longest[0]}"
                console.print(f"\n💡 [bold green]智能建议：[/]建议排除最短视频 [bold]#{shortest[0]}[/] 和最长视频 [bold]#{longest[0]}[/]")
        exclude_input = Prompt.ask("\n视频选择 (如 1,3,5 排除; [bold green]+2,4,6[/] 仅做; 1-5 范围)", default="")
        use_sequence = Confirm.ask("\n是否在视频文件夹前添加序列号 (原频道列表推荐 Yes，个人定制推荐 No)?", default=True)

    folder_name = Prompt.ask("\n项目保存文件夹名称", default=clean_title)

    # Check for name collision with existing uncompleted projects
    new_project_dir = os.path.join(DEFAULT_OUTPUT_ROOT, folder_name)
    if has_uncompleted_tasks(new_project_dir):
        console.print(f"\n[bold red]⚠️  警告：文件夹 [cyan]{folder_name}[/] 已存在且包含未完成的任务！[/]")
        console.print("继续使用同名将触发断点恢复流程（旧的未完成任务状态会被保留并可恢复）。")
        if not Confirm.ask("确定要使用这个文件夹名称吗？", default=False):
            console.print("[dim]请重新输入一个不同的文件夹名称。[/]")
            folder_name = Prompt.ask("\n项目保存文件夹名称", default=clean_title + "_new")

    do_sync = Confirm.ask("\n是否同步到 Google Drive?", default=False)
    do_bili = Confirm.ask("\n是否上传到 Bilibili? (分区/标签/简介/合集按视频自动生成)", default=False)
    do_wechat, do_wechat_public = confirm_wechat_upload()
    wechat_cover_title = ask_wechat_cover_title() if do_wechat and is_single_video else ""
    quality = Prompt.ask("\n选择压制字幕画质 (standard: 标准质量, high: 高清原画, lossless: 无损超清)", choices=["standard", "high", "lossless"], default="high")

    # 3.5 Model Selection (optional, default: no change)
    onboard_model_selection()

    # 4. Engine Assignment
    if is_single_video:
        target_script = SINGLE_SCRIPT
        cmd = [ PYTHON_EXE, target_script, url, "--output-dir", DEFAULT_OUTPUT_ROOT, "--project-name", folder_name, "--headless", "--quality", quality ]
        if current_cookies: cmd.extend(["--cookies", current_cookies])
        elif current_browser: cmd.extend(["--cookies-from-browser", current_browser])
        if do_sync: cmd.extend(["--sync-gdrive", GDRIVE_FOLDER_ID])
        if do_bili: cmd.append("--bili-upload")
        if do_wechat:
            cmd.append("--wechat-upload")
            if wechat_cover_title:
                cmd.extend(["--wechat-cover-title", wechat_cover_title])
            if do_wechat_public:
                cmd.append("--wechat-public")
    else:
        # Pre-seed the batch_state.json to avoid redundant yt-dlp fetch in batch_v4
        project_dir = os.path.join(DEFAULT_OUTPUT_ROOT, folder_name)
        os.makedirs(project_dir, exist_ok=True)
        state_path = os.path.join(project_dir, "batch_state.json")

        # CRITICAL FIX: Overwrite if the state file doesn't exist OR if a specific selection was provided
        # This ensures that "+2,4,6" actually updates the task list even if the project folder exists.
        should_write = True
        if os.path.exists(state_path):
            try:
                is_all_completed = False
                existing_data = None
                try:
                    with open(state_path, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                    is_all_completed = check_all_completed(existing_data)
                except Exception as e:
                    console.print(f"⚠️  解析现有任务进度时出错: {e}")

                if is_all_completed:
                    import datetime
                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    backup_path = f"{state_path}.bak_{timestamp}"
                    shutil.copy2(state_path, backup_path)
                    console.print(f"\n✨ [bold green]检测到该项目的先前任务已全部完成！[/bold green]")
                    console.print(f"📦 已自动将旧状态归档至: [cyan]{os.path.basename(backup_path)}[/cyan]")
                    console.print("🚀 开始创建全新进度的任务...")
                    should_write = True
                else:
                    console.print(f"\n[bold yellow]⚠️  检测到项目目录中已存在未完成的先前任务状态！[/bold yellow]")
                    resume = Confirm.ask("\n是否从上一次的断点恢复 (Resume) 任务进度？", default=True)
                    if resume:
                        if existing_data:
                            should_write = False
                            console.print("✨ [bold cyan]已选择从断点恢复上一次的任务进度。[/bold cyan]")
                        else:
                            console.print("⚠️  [bold yellow]现有任务状态文件为空或已损坏，无法从断点恢复！[/bold yellow]")
                            console.print("🚀 开始创建全新进度的任务...")
                    else:
                        import datetime
                        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                        backup_path = f"{state_path}.bak_{timestamp}"
                        shutil.copy2(state_path, backup_path)
                        console.print(f"📦 [bold green]已将未完成的状态备份至: {os.path.basename(backup_path)}[/bold green]")
                        console.print("🚀 开始创建全新进度的任务...")
            except Exception as e:
                console.print(f"⚠️  处理状态文件时出错: {e}")

        if should_write:
            # Build the task map for batch_v4
            task_map = {}
            indices_to_keep = parse_indices(exclude_input, len(entries))

            for idx, entry in enumerate(entries, 1):
                original_title = entry.get('title', 'Unknown')
                title = clean_title_text(original_title, playlist_title)
                # Skip private or deleted videos
                if "[private video]" in title.lower() or "[deleted video]" in title.lower():
                    console.print(f"  [dim]跳过第 {idx} 项: {title} (视频不可用)[/]")
                    continue

                if idx not in indices_to_keep:
                    console.print(f"  [dim]忽略第 {idx} 项: {title} (未选中)[/]")
                    continue

                vid_id = entry.get('id') or 'Unknown'
                safe_title = re.sub(r'[\\/*?:"<>|]', '_', title).strip()[:80]
                if use_sequence:
                    workdir = os.path.join(project_dir, f"[{idx:02d}] - {safe_title} [{vid_id}]")
                else:
                    workdir = os.path.join(project_dir, f"{safe_title} [{vid_id}]")

                task_map[str(idx)] = {
                    "url": entry.get('webpage_url') or entry.get('url'),
                    "title": title, "vid_id": vid_id, "uid": idx, "workdir": workdir,
                    "duration": format_duration(entry.get('duration')),
                    "status": "等待中", "error": None, "is_paused": False,
                    "pcts": {"DL": 0.0, "TR": 0.0, "TL": 0.0, "MR": 0.0, "BR": 0.0, "GD": 0.0, "MG": 0.0, "BI": 0.0, "WX": 0.0},
                    "queued_stages": ["DL"]
                }

            # Persist global flags so resume can restore them
            task_map["bili"] = do_bili
            task_map["gdsync"] = do_sync
            task_map["wechat"] = do_wechat
            task_map["wechat_public"] = do_wechat_public
            task_map["cookies"] = current_cookies
            task_map["browser"] = current_browser
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(task_map, f, ensure_ascii=False, indent=2)

        target_script = BATCH_SCRIPT
        # If we seeded the state, we don't need to pass --urls to avoid re-expansion conflicts
        cmd = [ PYTHON_EXE, target_script, "--output", DEFAULT_OUTPUT_ROOT, "--sub-dir-name", folder_name, "--quality", quality ]
        if not use_sequence:
            cmd.append("--no-sequence")
        if current_cookies: cmd.extend(["--cookies", current_cookies])
        elif current_browser: cmd.extend(["--cookies-from-browser", current_browser])
        sel = exclude_input.strip()
        if sel:
            if sel.startswith('+'):
                cmd.extend(["--include", sel[1:].strip()])
            elif sel.lower().startswith('i'):
                cmd.extend(["--include", sel[1:].strip()])
            else:
                cmd.extend(["--exclude", sel])
        if do_sync: cmd.append("--gdsync")
        if do_bili: cmd.append("--bili-upload")
        if do_wechat:
            cmd.append("--wechat-upload")
            if do_wechat_public:
                cmd.append("--wechat-public")

    console.print(f"\n[bold green]🚀 启动 {'单视频处理' if is_single_video else '批处理'} 引擎...[/]")
    launch_engine(cmd)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt: console.print("\n[yellow]已取消。[/]")
    except Exception as e:
        console.print(Panel(f"[bold red]程序运行发生错误：[/]\n{e}\n\n[dim]{traceback.format_exc()}[/]", title="崩溃保护"))
        input("\n按回车键退出...")
