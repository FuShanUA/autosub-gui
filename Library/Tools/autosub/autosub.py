import os
import sys
import subprocess
import json
import time
import re
import glob
import shutil
import argparse
import io
import signal
import inspect

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# --- Global Window Suppression Patch for Windows ---
if sys.platform == "win32":
    _original_popen = subprocess.Popen
    def _patched_popen(*args, **kwargs):
        cflags = kwargs.get('creationflags', 0)
        kwargs['creationflags'] = cflags | CREATE_NO_WINDOW
        return _original_popen(*args, **kwargs)
    subprocess.Popen = _patched_popen

if sys.platform == "win32":
    import msvcrt
else:
    import select
    import termios
    import tty

    def _getch():
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return None
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch.encode('utf-8')

    def _kbhit():
        if not os.isatty(sys.stdin.fileno()):
            return False
        return select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], [])

    class _msvcrt_mock:
        @staticmethod
        def kbhit(): return _kbhit()
        @staticmethod
        def getch(): return _getch()
    
    msvcrt = _msvcrt_mock()

from datetime import datetime

# System and Third-party imports

# Force UTF-8 for stdout/stderr to handle emojis in logs on Windows
if sys.platform == "win32":
    try:
        if isinstance(sys.stdout, io.TextIOWrapper):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if isinstance(sys.stderr, io.TextIOWrapper):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, io.UnsupportedOperation):
        pass

# --- Logging Helper ---
class Logger:
    def __init__(self, log_path):
        self.terminal = sys.stdout
        self.log_file = open(log_path, "a", encoding="utf-8", errors="replace")
        
    def write(self, message):
        self.terminal.write(message)
        self.log_file.write(message)
        self.terminal.flush()
        self.log_file.flush()
        
    def flush(self):
        self.terminal.flush()
        self.log_file.flush()

    def close(self):
        if self.log_file:
            self.log_file.close()
            self.log_file = None  # type: ignore

# --- Configuration ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULTS_FILE = os.path.join(CURRENT_DIR, "defaults.json")
SETTINGS_FILE = os.path.join(CURRENT_DIR, "settings.json")
DEFAULTS = {}
if os.path.exists(DEFAULTS_FILE):
    try:
        with open(DEFAULTS_FILE, "r", encoding="utf-8") as f:
            DEFAULTS.update(json.load(f))
    except: pass
if os.path.exists(SETTINGS_FILE):
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            DEFAULTS = json.load(f)
    except Exception as e:
        print(f"Warning: Failed to load settings.json: {e}")

VENDOR_MAP = {
    "Google (Vertex AI)": "vertex",
    "Google (Gemini API)": "gemini",
    "OpenAI": "openai",
    "Anthropic": "anthropic",
    "DeepSeek": "deepseek",
    "Alibaba (Bailian)": "dashscope",
    "DashScope (Alibaba Cloud)": "dashscope",
    "Zhipu AI": "zhipu",
    "Moonshot (Kimi)": "moonshot",
    "SiliconFlow": "siliconflow",
    "MiniMax": "minimax"
}

import multiprocessing
multiprocessing.freeze_support()

# --- Environment Setup (Isolation Logic) ---
if getattr(sys, 'frozen', False):
    # Packaged / Installer Mode
    BUNDLE_DIR = getattr(sys, '_MEIPASS', '')
    TOOLS_DIR = os.path.join(BUNDLE_DIR, "Library", "Tools")
    USER_DATA_DIR = os.path.join(os.path.expanduser("~"), "Documents", "AutoSub")
    PROJECT_ROOT = USER_DATA_DIR
    BASE_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "Projects")
    env_path = os.path.join(PROJECT_ROOT, ".env")
    
    # In bundled mode, common is in BUNDLE_DIR/Library/Tools/common
    sys.path.append(os.path.join(TOOLS_DIR, "common"))
else:
    # Repository mode: root/Library/Tools/autosub
    CURRENT_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    TOOLS_DIR = os.environ.get("AUTOSUB_TOOLS_DIR") or os.path.dirname(CURRENT_SCRIPT_DIR)
    PROJECT_ROOT = os.path.dirname(os.path.dirname(TOOLS_DIR))

    BASE_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "Projects")
    env_path = os.path.join(PROJECT_ROOT, ".env")

    sys.path.append(os.path.join(TOOLS_DIR, "common"))
    print(f"🏠 [DEV MODE] Root: {PROJECT_ROOT}")

# Load srt_utils after path setup
try:
    import srt_utils
except ImportError:
    srt_utils = None

def get_python_exe():
    """Returns python.exe even if running under pythonw.exe to ensure console output stability."""
    exe = sys.executable
    if sys.platform == "win32" and exe.lower().endswith("pythonw.exe"):
        py_exe = exe.lower().replace("pythonw.exe", "python.exe")
        if os.path.exists(py_exe): return py_exe
        
    # Prefer virtual environment python if it exists
    curr_dir = os.path.dirname(os.path.abspath(__file__))
    venv_py = os.path.join(
        curr_dir,
        ".venv",
        "Scripts" if sys.platform == "win32" else "bin",
        "python.exe" if sys.platform == "win32" else "python",
    )
    if os.path.exists(venv_py):
        return venv_py
        
    return exe

def trigger_sync(local_dir, remote_id, headless=False):
    """Launches a single-run Google Drive sync."""
    sync_script = os.environ.get("AUTOSUB_GDRIVE_SYNC_SCRIPT", "")
    creds_path = os.environ.get("AUTOSUB_GDRIVE_CREDENTIALS", os.path.join(PROJECT_ROOT, "credentials.json"))
    token_path = os.environ.get("AUTOSUB_GDRIVE_TOKEN", os.path.join(PROJECT_ROOT, "token.json"))

    if not sync_script or not remote_id:
        print("⚠️  [Cloud Sync] Skipped: no sync script or Google Drive folder ID configured")
        return False
    
    # Use console-stable python executable
    py_exe = get_python_exe()
    
    if headless:
        print(f"☁️  [Cloud Sync] Starting integrated GDrive sync (HEADLESS) for: {os.path.basename(local_dir)}")
        sync_cmd = [
            py_exe, sync_script,
            "--local-dir", local_dir,
            "--remote-id", remote_id,
            "--credentials", creds_path,
            "--token", token_path,
            "--wrap-folder",
            "--exclude", "noads",
            "--headless",
        ]
        try:
            process = subprocess.Popen(sync_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, creationflags=CREATE_NO_WINDOW)
            for line in process.stdout or []:
                msg = line.strip()
                if "Progress:" in msg:
                    sys.stdout.write(f"\r   {msg}    ")
                    sys.stdout.flush()
                else:
                    print(f"   [Sync] {msg}", flush=True)
            process.wait()
            print(f"\n✅ [Cloud Sync] Completed for {os.path.basename(local_dir)}")
            return process.returncode == 0
        except Exception as e:
            print(f"⚠️  [Cloud Sync] Integrated sync failed: {e}")
            return False
    else:
        if sys.platform == "win32":
            final_cmd = (
                f"& '{py_exe}' '{sync_script}' --local-dir '{local_dir}' --remote-id '{remote_id}' "
                f"--credentials '{creds_path}' --token '{token_path}' --wrap-folder; "
                f"Write-Host '--- ✅ Sync task completed. Window will close in 5s ---' -ForegroundColor Green; "
                f"Start-Sleep 5"
            )
            ps_cmd = f"Start-Process powershell -ArgumentList '-NoExit', '-Command', \"{final_cmd}\""
            print(f"☁️  [Cloud Sync] Launching GDrive sync in a new window for: {os.path.basename(local_dir)}")
            try:
                subprocess.Popen(["powershell", "-Command", ps_cmd], creationflags=CREATE_NO_WINDOW)
            except Exception as e:
                print(f"⚠️  [Cloud Sync] Failed to launch sync window: {e}")
        elif sys.platform == "darwin":
            print(f"☁️  [Cloud Sync] Launching GDrive sync in a new Terminal window for: {os.path.basename(local_dir)}")
            mac_cmd = (
                f"'{py_exe}' '{sync_script}' --local-dir '{local_dir}' --remote-id '{remote_id}' "
                f"--credentials '{creds_path}' --token '{token_path}' --wrap-folder; "
                f"echo '--- ✅ Sync task completed. Window will close in 5s ---'; "
                f"sleep 5; exit"
            )
            osascript_cmd = f'tell application "Terminal" to do script "{mac_cmd}"'
            try:
                subprocess.Popen(["osascript", "-e", osascript_cmd])
            except Exception as e:
                print(f"⚠️  [Cloud Sync] Failed to launch sync terminal: {e}")
        else:
            print(f"☁️  [Cloud Sync] Running sync in background (not supported in new window on this platform)")
            trigger_sync(local_dir, remote_id, headless=True)

# --- Bilibili Upload (canonical implementation; batch engine + GUI reuse this) ---
BILI_SCRIPT = os.environ.get("BILI_UPLOAD_SCRIPT", "")
BILI_PYTHON = os.environ.get("BILI_UPLOAD_PYTHON", "")
BILI_LOGIN_SCRIPT = os.path.join(os.path.dirname(BILI_SCRIPT), "login.py") if BILI_SCRIPT else ""
BILI_COOKIES = (
    os.path.join(os.path.dirname(os.path.dirname(BILI_SCRIPT)), "assets", "cookies.json")
    if BILI_SCRIPT
    else ""
)
BILI_VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.mov', '.ts')
WECHAT_SCRIPT = os.path.join(
    CURRENT_DIR, "wechat-channel-uploader", "scripts", "wechat_upload.py"
)
# Login state is intentionally kept inside the repository and excluded by Git.
WECHAT_BROWSER_DATA_DIR = os.environ.get(
    "WECHAT_BROWSER_DATA_DIR",
    os.path.join(CURRENT_DIR, "wechat-channel-uploader", "assets", "browser_data"),
)
os.environ["WECHAT_BROWSER_DATA_DIR"] = WECHAT_BROWSER_DATA_DIR
SHARED_UPLOAD_TITLE_MAX_CHARS = 16
BILI_UPLOAD_TITLE_MAX_CHARS = 48
BILI_UPLOAD_TITLE_HARD_MAX_CHARS = 80
BILI_UPLOAD_TITLE_TARGET_MIN_CHARS = 18
_wechat_venv_python = os.path.join(
    CURRENT_DIR,
    ".venv",
    "Scripts" if sys.platform == "win32" else "bin",
    "python.exe" if sys.platform == "win32" else "python",
)
WECHAT_PYTHON = _wechat_venv_python if os.path.exists(_wechat_venv_python) else sys.executable

def get_bili_login_command():
    """Return the visible Bilibili QR-login command shown to the user."""
    return f"{BILI_PYTHON} -u {BILI_LOGIN_SCRIPT}"

def get_wechat_login_command():
    """Return the visible WeChat Channels QR-login command."""
    return f"{WECHAT_PYTHON} -u {WECHAT_SCRIPT} --login"

def get_bili_login_skip_reason():
    """Check the local Bilibili cookie file before starting an upload."""
    if not os.path.isfile(BILI_COOKIES):
        return f"Bilibili cookies not found: {BILI_COOKIES}"
    try:
        with open(BILI_COOKIES, "r", encoding="utf-8") as handle:
            cookies = json.load(handle)
    except (OSError, ValueError) as exc:
        return f"cannot read Bilibili cookies: {exc}"
    required = ("SESSDATA", "bili_jct", "buvid3")
    missing = [name for name in required if not str(cookies.get(name) or "").strip()]
    if missing:
        return "Bilibili cookies missing: " + ", ".join(missing)
    return None

def get_wechat_login_skip_reason():
    """Check that the persistent WeChat browser profile exists."""
    if not os.path.isdir(WECHAT_BROWSER_DATA_DIR):
        return f"WeChat browser profile not found: {WECHAT_BROWSER_DATA_DIR}"
    return None

def _bili_output_says_not_logged_in(output):
    text = str(output)
    return any(
        marker in text
        for marker in (
            "账号未登录",
            "code=-101",
            "'code': -101",
            '"code": -101',
            "CredentialNoSessdataException",
            "未提供 sessdata",
        )
    )

def check_bili_login(timeout=20):
    """Verify Bilibili cookies with the account's own lightweight API.

    Returns (True, None) for a valid login, (False, reason) for a definite
    login failure, and (None, reason) when the check is inconclusive. An
    inconclusive network error must not block the upload attempt itself.
    """
    skip_reason = get_bili_login_skip_reason()
    if skip_reason:
        return False, skip_reason
    if not os.path.isfile(BILI_PYTHON):
        return None, "Bilibili login check unavailable: upload Python not found"

    check_code = f"""
import asyncio, json
from bilibili_api import Credential
from bilibili_api.user import get_self_info

async def main():
    with open({BILI_COOKIES!r}, "r", encoding="utf-8") as handle:
        cookies = json.load(handle)
    credential = Credential(
        sessdata=cookies["SESSDATA"],
        bili_jct=cookies["bili_jct"],
        buvid3=cookies["buvid3"],
        buvid4=cookies.get("buvid4"),
    )
    await get_self_info(credential=credential)

asyncio.run(main())
"""
    try:
        result = subprocess.run(
            [BILI_PYTHON, "-u", "-c", check_code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "Bilibili login check timed out; upload will still be attempted"
    except OSError as exc:
        return None, f"Bilibili login check failed; upload will still be attempted: {exc}"

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0:
        return True, None
    if _bili_output_says_not_logged_in(output):
        return False, "Bilibili login is invalid (code -101)"
    brief = " ".join(output.split())[:240]
    return None, f"Bilibili login check failed; upload will still be attempted: {brief}"

def get_upload_login_failure_hint(label, output):
    """Turn uploader login errors into the visible recovery command."""
    text = str(output)
    if "Bilibili" in label and _bili_output_says_not_logged_in(text):
        return f"{label} 登录态失效，请先执行: {get_bili_login_command()}"
    if "WeChat" in label and any(
        marker in text
        for marker in ("登录态不可用", "登录态已失效", "需要扫码登录", "请先执行一次可见登录命令")
    ):
        return f"{label} 登录态失效，请先执行: {get_wechat_login_command()}"
    return None

def find_hardsub_video(workdir):
    """Return the most recently modified *_hardsub* video in workdir, or None."""
    try:
        files = os.listdir(workdir)
    except Exception:
        return None
    candidates = [f for f in files if "_hardsub" in f and f.endswith(BILI_VIDEO_EXTS)]
    if not candidates:
        return None
    return os.path.join(workdir, max(candidates, key=lambda fn: os.path.getmtime(os.path.join(workdir, fn))))

def find_noads_hardsub_video(workdir):
    """Return the newest ad-cut hardsub video in workdir/noads, or None."""
    noads_dir = os.path.join(workdir, "noads")
    try:
        files = os.listdir(noads_dir)
    except Exception:
        return None
    candidates = [
        fn for fn in files
        if "_hardsub" in fn and fn.endswith(BILI_VIDEO_EXTS)
    ]
    if not candidates:
        return None
    return os.path.join(
        noads_dir,
        max(candidates, key=lambda fn: os.path.getmtime(os.path.join(noads_dir, fn))),
    )

def find_upload_hardsub_video(workdir):
    """BI/WX upload the ad-cut version when available, otherwise the full video."""
    return find_noads_hardsub_video(workdir) or find_hardsub_video(workdir)

def build_bili_title(hardsub_path):
    """Derive a Bilibili title from a hardsub filename, capped at 80 chars."""
    title = os.path.basename(hardsub_path).replace("_hardsub", "")
    title = os.path.splitext(title)[0]
    title = re.sub(r' \[[a-zA-Z0-9_-]+\](\s*\[1080p\])?$', '', title).strip()
    if len(title) > 80:
        cut = title.rfind(' ', 0, 77)
        if cut == -1:
            cut = 77
        title = title[:cut].rstrip().rstrip('.') + "..."
    return title


def sanitize_wechat_title(title, max_chars=16):
    """Clean a title to WeChat's short-title punctuation rules and length limit."""
    cleaned = str(title).replace(",", " ").replace("，", " ")
    cleaned = re.sub(
        r"[^0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff《》“”‘’：:+?%℃ ]",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return (cleaned or "未命名视频")[:max_chars]


def normalize_shared_upload_title(title, max_chars=SHARED_UPLOAD_TITLE_MAX_CHARS):
    """Clean a shared title without silently truncating its meaning."""
    value = str(title or "").strip()
    if not value:
        return ""
    # Sanitize with the original length first so an over-limit title is
    # detected instead of being cut to 16 characters.
    cleaned = sanitize_wechat_title(value, max_chars=max(len(value), 1))
    if (
        not cleaned
        or cleaned == "未命名视频"
        or len(cleaned) > max_chars
        or _shared_title_has_quality_issue(cleaned)
    ):
        return ""
    return cleaned

def normalize_bili_upload_title(
    title,
    max_chars=BILI_UPLOAD_TITLE_HARD_MAX_CHARS,
):
    """Clean a Bilibili title without applying WeChat's punctuation rules."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    value = value.rstrip(" 。.！!？?；;，,")
    if (
        not value
        or len(value) > max_chars
        or _shared_title_has_quality_issue(value)
    ):
        return ""
    return value

def build_bili_cmd(
    workdir,
    title=None,
    tid=None,
    category_name=None,
    tags=None,
    season=None,
    desc=None,
    public=False,
    cover_title=None,
    llm_model=None,
    llm_provider=None,
    require_complete_metadata=False,
):
    """Build the Bilibili upload command for the hardsub video in workdir.
    Metadata (tid/tags/desc/season) is auto-generated per-video by bili_upload.py's
    built-in LLM logic unless explicitly overridden. Returns the cmd list or None."""
    if not os.path.exists(BILI_SCRIPT) or not os.path.exists(BILI_PYTHON):
        return None
    hardsub_path = find_upload_hardsub_video(workdir)
    if not hardsub_path:
        return None
    metadata_values = (title, desc, cover_title)
    if require_complete_metadata and not all(
        str(value or "").strip() for value in metadata_values
    ):
        return None
    # When title/desc/cover_title are all provided, the caller has pre-generated
    # metadata; tell bili_upload.py to skip its own LLM title generation.
    _pre_generated = title is not None and desc is not None and cover_title is not None
    if title is None:
        title = build_bili_title(hardsub_path)
    cmd = [BILI_PYTHON, "-u", BILI_SCRIPT, hardsub_path, title]
    if tid:
        cmd.extend(["--tid", str(tid)])
    if category_name:
        cmd.extend(["--category_name", str(category_name)])
    if tags:
        cmd.extend(["--tags", str(tags)])
    if season:
        cmd.extend(["--season_name", str(season)])
    if desc:
        cmd.extend(["--desc", str(desc)])
    if cover_title is not None and str(cover_title).strip():
        cmd.extend(["--cover-title", str(cover_title).strip()])
    if _pre_generated:
        cmd.append("--no_auto_title")
    if llm_model:
        cmd.extend(["--llm-model", str(llm_model)])
    if llm_provider:
        cmd.extend(["--llm-provider", str(llm_provider)])
    if public:
        cmd.append("--public")
    return cmd

# Upload watchdog bounds. A healthy hour-long 1080p upload reports a new
# percentage every 30s and finishes in 20-40 minutes, so both limits sit well
# above normal behaviour and only fire on a genuinely wedged child.
UPLOAD_STALL_TIMEOUT = 1800
UPLOAD_SILENCE_TIMEOUT = 1200


def _descendant_pids(pid):
    """Every descendant of pid, shallowest first. Empty on Windows."""
    found = []
    frontier = [pid]
    seen = {pid}
    while frontier:
        current = frontier.pop(0)
        try:
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


def kill_proc_tree(proc, terminate=False):
    """Signal a child process together with everything it spawned.

    The uploaders launch chromium and ffmpeg. Killing only the Python wrapper
    leaves those alive and holding the shared WeChat browser profile, so the
    next upload in the serial queue wedges on the locked profile directory.
    """
    if proc is None:
        return
    sig = signal.SIGTERM if terminate else signal.SIGKILL
    for pid in reversed(_descendant_pids(proc.pid)):
        try:
            os.kill(pid, sig)
        except OSError:
            pass
    try:
        proc.terminate() if terminate else proc.kill()
    except Exception:
        pass


def _run_upload_cmd(
    cmd,
    label,
    progress_pattern,
    on_progress=None,
    on_log=None,
    abort_flag=None,
    on_process_start=None,
    on_process_end=None,
    stall_timeout=UPLOAD_STALL_TIMEOUT,
    silence_timeout=UPLOAD_SILENCE_TIMEOUT,
):
    """Shared stdout pump for the Bilibili and WeChat Channels uploaders.

    Both children can wedge instead of exiting. The WeChat one keeps polling a
    helper endpoint and prints a line every ten seconds while the transfer
    itself never advances, so an output-based check alone never fires. Because
    the WeChat slot is serial, one wedged upload parks every remaining task at
    "queued" until the engine is restarted. Two independent timers bound that:
    silence_timeout covers a child that stops printing, stall_timeout covers a
    child that prints without moving the percentage. Either one kills the whole
    process tree and returns False so the caller's existing retry path runs.
    """
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        if on_log:
            on_log(f"❌ Failed to start {label} upload: {e}")
        return False

    if on_process_start:
        try:
            on_process_start(p)
        except Exception as e:
            if on_log:
                on_log(f"⚠️ Failed to register {label} process: {e}")

    try:
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

            started = time.time()
            state = {"output": started, "advance": started, "pct": -1.0}

            def _handle(line):
                state["output"] = time.time()
                stripped = line.strip()
                if stripped and on_log:
                    on_log(stripped)
                m = re.search(progress_pattern, line)
                if not m:
                    return
                pct = float(m.group(1))
                if pct > state["pct"]:
                    state["pct"] = pct
                    state["advance"] = time.time()
                if on_progress:
                    on_progress(pct)

            def _stall_reason():
                now = time.time()
                if silence_timeout and now - state["output"] > silence_timeout:
                    return f"no output for {int(now - state['output'])}s"
                if stall_timeout and now - state["advance"] > stall_timeout:
                    return f"stuck at {max(state['pct'], 0.0):.1f}% for {int(now - state['advance'])}s"
                return None

            while True:
                if abort_flag and abort_flag():
                    kill_proc_tree(p)
                    _reader_stop.set()
                    break
                # Checked every iteration, not only when the queue runs dry: the
                # WeChat uploader can print a polling line continuously while the
                # percentage never moves, and a queue-timeout-only check would then
                # never fire.
                if p.poll() is None:
                    reason = _stall_reason()
                    if reason:
                        if on_log:
                            on_log(f"⏱️ Watchdog: {label} upload {reason}; killing process tree and retrying")
                        kill_proc_tree(p)
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
                            _handle(line)
                        break
                    continue
                if line is None: break
                _handle(line)

            # Reap first so the reader thread reaches EOF on its own, then release
            # the pipe. Leaving it open leaks one descriptor per upload for the
            # whole life of the engine.
            p.wait()
            _reader_stop.set()
            _rt.join(timeout=2)
            try:
                p.stdout.close()
            except Exception:
                pass
        else:
            p.wait()
        return p.returncode == 0
    finally:
        if on_process_end:
            try:
                on_process_end(p)
            except Exception:
                pass


def run_bili_cmd(cmd, on_progress=None, on_log=None, abort_flag=None,
                 on_process_start=None, on_process_end=None,
                 stall_timeout=UPLOAD_STALL_TIMEOUT,
                 silence_timeout=UPLOAD_SILENCE_TIMEOUT):
    """Run a pre-built Bilibili upload command. Returns True on success.
    on_progress(pct)/on_log(line)/abort_flag() are optional callbacks; final
    success/failure status is NOT logged here (callers decide how to report)."""
    return _run_upload_cmd(
        cmd,
        "Bilibili",
        r'Progress:\s*([\d.]+)%',
        on_progress=on_progress,
        on_log=on_log,
        abort_flag=abort_flag,
        on_process_start=on_process_start,
        on_process_end=on_process_end,
        stall_timeout=stall_timeout,
        silence_timeout=silence_timeout,
    )

def trigger_bili_upload(
    workdir,
    title=None,
    tid=None,
    category_name=None,
    tags=None,
    season=None,
    desc=None,
    public=False,
    cover_title=None,
    llm_model=None,
    llm_provider=None,
    on_progress=None,
    on_log=None,
    abort_flag=None,
):
    """Build and run a Bilibili upload for the hardsub video in workdir.
    Convenience wrapper: build_bili_cmd + run_bili_cmd. Returns True on success."""
    cmd = build_bili_cmd(
        workdir,
        title=title,
        tid=tid,
        category_name=category_name,
        tags=tags,
        season=season,
        desc=desc,
        public=public,
        cover_title=cover_title,
        llm_model=llm_model,
        llm_provider=llm_provider,
    )
    if not cmd:
        print("⚠️  [Bilibili] upload skipped (script/venv/hardsub not found)", flush=True)
        return False
    _log = on_log or (lambda msg: print(msg, flush=True))
    _log(f"📺 Starting Bilibili upload: {os.path.basename(cmd[2])}")
    ok = run_bili_cmd(
        cmd,
        on_progress=on_progress,
        on_log=_log,
        abort_flag=abort_flag,
    )
    _log(f"📺 {'✅ Success' if ok else '❌ Failed'}")
    return ok

# --- Shared Upload Metadata Generation ---
# BI and WX share one description and cover title. Their form titles are also
# generated from the same transcript: Bilibili gets the fuller editorial title
# first, then WeChat receives a 16-character title derived from it.
_COMMON_DIR = os.path.join(os.path.dirname(os.path.dirname(CURRENT_DIR)), "common")
if _COMMON_DIR not in sys.path:
    sys.path.insert(0, _COMMON_DIR)

def _load_llm_defaults():
    """Read the model/vendor selected by the main AutoSub configuration."""
    try:
        p = os.path.join(CURRENT_DIR, "settings.json")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                s = json.load(f)
            model = s.get("llm_model") or DEFAULTS.get("llm_model") or "glm-5.2"
            vendor = s.get("llm_vendor") or s.get("vendor") or ""
            provider = VENDOR_MAP.get(vendor, str(vendor).strip().lower() or "dashscope")
            return model, provider
    except Exception:
        pass
    return DEFAULTS.get("llm_model", "glm-5.2"), VENDOR_MAP.get(
        DEFAULTS.get("llm_vendor", ""), "dashscope"
    )


def _metadata_model_candidates(primary_model, provider, llm_client):
    """Return model candidates used only by upload metadata generation."""
    primary = str(primary_model or "").strip()
    provider_name = str(getattr(provider, "value", provider) or "").strip().lower()
    if not primary or provider_name != "dashscope" or llm_client is None:
        return [primary] if primary else []

    configured = os.environ.get("DASHSCOPE_METADATA_FALLBACK_MODELS")
    if configured is not None:
        if configured.strip().lower() in {"0", "false", "off", "none", "disabled"}:
            return [primary]
        fallback_models = re.split(r"[,;\s]+", configured.strip())
    else:
        fallback_models = list(
            getattr(llm_client, "DASHSCOPE_FALLBACK_CHAIN", [])
        )

    candidates = []
    seen = set()
    for model_name in [primary] + fallback_models:
        value = str(model_name or "").strip()
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        candidates.append(value)
    return candidates


def _generate_content_with_timeout(
    llm_client,
    prompt,
    model_name,
    provider=None,
    request_timeout=90,
    max_retries=1,
):
    """Call metadata LLMs with a bounded read timeout without changing translation."""
    kwargs = {
        "model_name": model_name,
        "provider": provider,
    }
    try:
        signature = inspect.signature(llm_client.generate_content)
        supports_timeout = "request_timeout" in signature.parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
    except (TypeError, ValueError):
        supports_timeout = True
    if supports_timeout:
        kwargs["request_timeout"] = request_timeout
    try:
        signature = inspect.signature(llm_client.generate_content)
        supports_max_retries = "max_retries" in signature.parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
    except (TypeError, ValueError):
        supports_max_retries = True
    if supports_max_retries:
        kwargs["max_retries"] = max_retries
    return llm_client.generate_content(prompt, **kwargs)


_SHARED_TITLE_BANNED_TERMS = (
    "我们", "它", "他们", "她们", "东西", "能力", "到底", "究竟",
    "为什么", "为何", "如何", "怎么", "怎样", "什么",
    "死活",
)
_SHARED_TITLE_ACTION_MARKERS = (
    "谈", "讲", "靠", "用", "做", "让", "成为", "押", "是", "培养",
    "招", "卖", "买", "建立", "打造", "解决", "守住", "聚焦", "变成", "保护",
    "整合", "引领", "炮轰", "反对", "支持", "梳理", "展示", "迭代", "改造",
    "收购", "讨论", "推掉", "陪", "失去", "离世", "拒绝", "直面", "守",
    "被裁", "烂掉", "回忆",
    "到", "成", "感觉", "上班",
)
_SHARED_TITLE_CONTEXT_MARKERS = (
    "访谈", "对话", "谈", "讲", "为", "帮", "给", "客户", "企业", "主题",
    "人才", "名校", "父母", "孩子", "新戏", "小报", "传言",
    "问题", "不做", "不是", "不招", "押注", "才是", "隐私", "一半",
    "被裁", "烂掉",
    "人生", "中年", "上班", "素材库",
)
_PUBLIC_TITLE_STYLE_EXAMPLES = (
    "Palantir才是引领应用层的公司，客户从没见过这种阵势",
    "ToB AI落地的正道何在？Palantir CEO Alex Karp炮轰模型大厂",
    "Alex Karp早年访谈：Palantir不做服务公司，为企业打造解决数据问题的产品",
)
_SHARED_TITLE_STANCE_MARKERS = (
    "不做", "不是", "没有", "拒绝", "不招", "不卖", "才是", "推掉",
)

_INTERVIEW_TITLE_MARKERS = (
    "访谈", "专访", "对话", "受访", "敞开谈", "interview",
)
_INTERVIEW_LIFE_STAGE_MARKERS = (
    "中年", "50岁", "54岁", "人生", "第三节", "苦短", "人一到50",
)
_INTERVIEW_REPRESENTATIVE_HOOK_MARKERS = (
    "上班", "素材库", "遗憾", "无所谓", "性生活", "杏生活", "第三节", "苦短",
)
_INTERVIEW_RUMOR_MARKERS = (
    "小报", "传言", "谣言", "FBI", "突袭", "烧没了",
)
_INTERVIEW_EPISODIC_GRIEF_MARKERS = (
    "父母", "离世", "相继", "丧亲",
)
_INTERVIEW_PERSON_ROLE_MARKERS = (
    "创始人", "联合创始人", "CEO", "研究员", "演员", "导演", "作者", "嘉宾",
)
_INTERVIEW_KNOWN_PEOPLE = (
    "Ben Affleck", "Matt Damon", "Alex Karp", "Peter Thiel",
    "Marc Andreessen", "René Girard", "Rene Girard",
    "Andrej Karpathy", "Elon Musk",
)

_PROPER_NOUN_TITLE_ALIASES = {
    "Ben Affleck": ("本·阿弗莱克", "本阿弗莱克", "大本"),
    "Matt Damon": ("马特·达蒙", "马特达蒙"),
    "Alex Karp": ("卡普",),
    "Peter Thiel": ("彼得·蒂尔", "蒂尔"),
    "Marc Andreessen": ("安德森",),
    "René Girard": ("吉拉尔",),
    "Rene Girard": ("吉拉尔",),
    "Andrej Karpathy": ("卡帕西",),
    "Elon Musk": ("马斯克",),
    "Los Angeles": ("洛杉矶",),
    "New York": ("纽约",),
    "San Francisco": ("旧金山",),
}

_INTERVIEW_PUBLIC_IDENTITY_LABELS = {
    "Ben Affleck": "好莱坞一线男星",
    "Alex Karp": "Palantir CEO",
    "Peter Thiel": "硅谷投资人",
    "Marc Andreessen": "硅谷投资人",
    "Andrej Karpathy": "AI研究员",
}


def _is_interview_title_context(*contexts):
    """Detect a person interview instead of a product or business explainer."""
    text = " ".join(str(context or "") for context in contexts).casefold()
    if not text:
        return False
    has_interview_form = any(
        marker.casefold() in text
        for marker in _INTERVIEW_TITLE_MARKERS
    )
    life_stage_count = sum(
        marker in text
        for marker in _INTERVIEW_LIFE_STAGE_MARKERS
    )
    hook_count = sum(
        marker in text
        for marker in _INTERVIEW_REPRESENTATIVE_HOOK_MARKERS
    )
    has_person = (
        any(person.casefold() in text for person in _INTERVIEW_KNOWN_PEOPLE)
        or any(
            alias in text
            for targets in _PROPER_NOUN_TITLE_ALIASES.values()
            for alias in targets
        )
        or any(
            label in text
            for label in _INTERVIEW_PUBLIC_IDENTITY_LABELS.values()
        )
        or any(marker in text for marker in _INTERVIEW_PERSON_ROLE_MARKERS)
    )
    return (
        (has_interview_form and has_person)
        or life_stage_count + hook_count >= 2
    )


def _interview_title_quality_score(title):
    """Prefer a person, life stage, and a representative spoken hook."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    if not value:
        return 0.0
    score = 0.0
    if any(marker in value for marker in ("访谈", "专访", "对话")):
        score += 8.0
    if re.search(r"^[^：:]{1,18}[：:]", value):
        score += 3.0
    score += 5.0 * sum(
        marker in value
        for marker in _INTERVIEW_LIFE_STAGE_MARKERS
    )
    score += 6.0 * sum(
        marker in value
        for marker in _INTERVIEW_REPRESENTATIVE_HOOK_MARKERS
    )
    if any(marker in value for marker in _INTERVIEW_RUMOR_MARKERS) and not any(
        marker in value
        for marker in (
            _INTERVIEW_LIFE_STAGE_MARKERS
            + _INTERVIEW_REPRESENTATIVE_HOOK_MARKERS
        )
    ):
        score -= 8.0
    if any(marker in value for marker in _INTERVIEW_EPISODIC_GRIEF_MARKERS) and not any(
        marker in value
        for marker in _INTERVIEW_REPRESENTATIVE_HOOK_MARKERS
    ):
        score -= 10.0
    return score


def _parse_llm_title_aliases(raw):
    """Parse established Chinese aliases returned with title candidates."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        payload = raw
    else:
        text = str(raw).strip()
        text = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return {}
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    if not isinstance(payload, dict):
        return {}

    raw_aliases = payload.get("subject_aliases", payload.get("aliases", {}))
    if not isinstance(raw_aliases, dict):
        return {}

    aliases = {}
    for source, targets in raw_aliases.items():
        source_value = re.sub(r"\s+", " ", str(source or "")).strip()
        if not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9'_-]*(?: [A-Za-z][A-Za-z0-9'_-]*)*",
            source_value,
        ):
            continue
        if isinstance(targets, str):
            targets = [targets]
        elif not isinstance(targets, (list, tuple)):
            continue
        values = tuple(
            re.sub(r"\s+", " ", str(target or "")).strip()
            for target in targets
            if re.sub(r"\s+", " ", str(target or "")).strip()
            and re.search(r"[\u3400-\u9fff]", str(target or ""))
        )
        if values:
            aliases[source_value] = values
    return aliases


def _canonicalize_title_proper_nouns(title, subtitle_text, aliases):
    """Map Chinese aliases back to the spelling used in the subtitle."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    if not aliases:
        return value
    normalized_subtitle = re.sub(r"\s+", " ", str(subtitle_text or "")).strip()
    subtitle_lower = normalized_subtitle.casefold()

    for source, targets in aliases.items():
        source_value = re.sub(r"\s+", " ", str(source or "")).strip()
        if not source_value or source_value.casefold() not in subtitle_lower:
            continue
        if isinstance(targets, str):
            targets = (targets,)
        for target in sorted(
            (re.sub(r"\s+", " ", str(target or "")).strip() for target in targets),
            key=len,
            reverse=True,
        ):
            if target and target != source_value and target in value:
                value = value.replace(target, source_value)
                break
    return value


def _validate_title_with_proper_noun_aliases(
    title,
    subtitle_text,
    evidence,
    min_chars,
    max_chars,
    aliases,
    validator,
    error_class,
):
    """Ground aliases without letting their length bypass the real budget."""
    normalized_title = re.sub(r"\s+", " ", str(title or "")).strip()
    if len(normalized_title) < min_chars or len(normalized_title) > max_chars:
        raise error_class(f"标题长度必须在 {min_chars}-{max_chars} 个字符之间")

    canonical_title = _canonicalize_title_proper_nouns(
        normalized_title,
        subtitle_text,
        aliases,
    )
    if canonical_title == normalized_title:
        return validator(
            canonical_title,
            subtitle_text,
            evidence,
            min_chars=min_chars,
            max_chars=max_chars,
        )

    # The displayed title is budgeted above. Validation checks the canonical
    # English spelling, so its temporary expansion must not fail the length gate.
    return validator(
        canonical_title,
        subtitle_text,
        evidence,
        min_chars=1,
        max_chars=max(max_chars, len(canonical_title)),
    )


_SHARED_TITLE_CONCRETE_OBJECTS = (
    "数据", "应用", "平台", "工具", "模型", "算法", "本体",
)
_SHARED_TITLE_BENEFICIARIES = (
    "企业", "政企", "政府", "客户", "机构",
)
_TITLE_NATURALNESS_BAD_PREFIXES = (
    "就", "光是", "你得", "而且", "但", "但是", "所以", "然后",
    "换句话说", "也就是说",
)
_TITLE_TRANSLATION_PATTERNS = (
    r"^(?:对于|关于|作为|通过|基于)",
    r"(?:不仅仅是|而不是|而非|与其说|不如说)",
    r"(?:进行|实现)了",
    r"被.{0,12}所",
    r"(?:支持|学习|成长|价值|能力)的$",
)
_TITLE_AI_SLOP_TERMS = (
    "赋能", "助力", "深度解析", "全面解析", "一文读懂",
    "底层逻辑", "认知升级", "新范式", "未来已来", "无限可能",
    "干货满满", "保姆级", "颠覆认知",
)
_COVER_TITLE_VAGUE_TERMS = (
    "有人", "一些人", "许多人", "很多人", "所有人", "大量",
    "各种", "一系列", "东西", "内容",
)
_COVER_TITLE_SUBJECT_MARKERS = (
    "医院", "病人", "医生", "警局", "警察", "新人", "员工", "团队",
    "公司", "客户", "企业", "工程师", "学生", "学校", "军队", "前线",
    "数据治理", "网络攻击", "透析", "脓毒症", "癌症", "战争", "武器",
    "中年", "50岁", "54岁", "人生", "第三节", "素材库", "性生活", "杏生活",
)
_COVER_TITLE_CONFLICT_MARKERS = (
    "被", "没有", "不", "无法", "做不了", "停摆", "攻击", "裁",
    "危机", "错误", "失败", "冲突", "对手", "对付", "杀手",
)
_COVER_TITLE_CONSEQUENCE_MARKERS = (
    "受影响", "停摆", "烂掉", "没了", "断了", "做不了", "救",
    "治", "上线", "采用", "落地", "结果", "后果", "反而", "却",
    "先", "头号", "杀手",
)
_COVER_TITLE_ABSTRACT_MARKERS = (
    "行业落地", "实际价值", "业务价值", "商业价值", "成果",
    "价值", "能力", "支持", "创新", "范式", "开创性", "最前线",
)
_COVER_TITLE_THESIS_ACTION_MARKERS = (
    "用", "整合", "识别", "救治", "优化", "合作", "布局",
    "推进", "建立", "采用", "穿针引线",
    "解决", "放大", "谈",
    "到", "成", "感觉", "觉得", "以为", "认识",
    "收购",
)
_COVER_TITLE_TRANSLATION_TERMS = (
    "内部模型",
)


def _title_has_naturalness_issue(title):
    """Reject headline Chinese that reads like translation or filler speech."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    if not value:
        return True
    if "..." in value or "……" in value or "。" in value:
        return True
    if re.match(r"^(?:有意思|现在|显然|其实|当然)", value):
        return True
    if re.search(r"一个是.{1,24}?一个是", value):
        return True
    if any(value.startswith(prefix) for prefix in _TITLE_NATURALNESS_BAD_PREFIXES):
        return True
    if re.search(r"[：:](?:但|但是|所以|然后|而且|就是)", value):
        return True
    if re.search(r"[你您]", value):
        return True
    if re.search(r"(?:大量|许多|很多)的?(?:支持|东西|内容|信息)", value):
        return True
    if re.search(r"(?:获得|得到).{0,6}(?:支持|东西|内容|信息)", value):
        return True
    if re.search(r"(?:学到|收获).{0,4}(?:大量|很多|许多)", value):
        return True
    if re.search(r"(?:完全|绝对)(?:不对|正确|错误)", value):
        return True
    if re.search(r"(?:思路|逻辑)(?:完全)?(?:不对|错误)", value):
        return True
    if value.count("，") + value.count(",") > 2:
        return True
    if len(value) >= 16 and value.count("的") >= 3:
        return True
    if any(term in value for term in _TITLE_AI_SLOP_TERMS):
        return True
    return any(
        re.search(pattern, value)
        for pattern in _TITLE_TRANSLATION_PATTERNS
    )


def _cover_title_information_units(title):
    """Count the concrete units a viewer can decode in three seconds."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    latin_subject = bool(
        re.search(
            r"(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9_-]{2,}(?![A-Za-z0-9_-])",
            value,
        )
        and value.lower() not in {"the", "and", "for"}
    )
    return (
        latin_subject
        + any(marker in value for marker in _COVER_TITLE_SUBJECT_MARKERS)
        + _cover_title_has_thesis_action(value)
        + any(marker in value for marker in _COVER_TITLE_CONFLICT_MARKERS)
        + any(marker in value for marker in _COVER_TITLE_CONSEQUENCE_MARKERS)
        + bool(
            re.search(
                r"(?:[0-9一二三四五六七八九十]+(?:周|年|次|倍|岁)"
                r"|第[0-9一二三四五六七八九十]+|一半)",
                value,
            )
        )
    )


def _cover_title_has_thesis_action(value):
    """Treat negated uses such as ``不用`` as no action."""
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not value:
        return False
    if re.search(r"(?<![不没])用", value):
        return True
    return any(
        marker in value
        for marker in _COVER_TITLE_THESIS_ACTION_MARKERS
        if marker != "用"
    )


def _cover_title_has_engagement_issue(title):
    """Reject hooks that are striking but cannot be decoded quickly."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    if not value:
        return True
    if any(term in value for term in _COVER_TITLE_VAGUE_TERMS):
        return True
    if _cover_title_information_units(value) < 2:
        return True
    if any(marker in value for marker in _COVER_TITLE_ABSTRACT_MARKERS) and not (
        any(marker in value for marker in _COVER_TITLE_CONFLICT_MARKERS)
        or any(marker in value for marker in _COVER_TITLE_CONSEQUENCE_MARKERS)
    ):
        return True
    if value.startswith("我们") and "就像" in value:
        return not any(
            marker in value
            for marker in _COVER_TITLE_CONSEQUENCE_MARKERS
        )
    return False


def _cover_title_context_anchor_count(title, *contexts):
    """Count substantive title tokens shared with whole-video context."""
    title_tokens = _title_scoring_tokens(title)
    if not title_tokens:
        return 0
    context_tokens = set()
    for context in contexts:
        context_tokens.update(_title_scoring_tokens(context))
    return len(title_tokens & context_tokens)


def _cover_title_has_context_issue(title, form_title="", source_title="", description=""):
    """Require a cover hook to stay anchored to the video's main subject."""
    contexts = tuple(
        str(context or "")
        for context in (form_title, source_title, description)
        if str(context or "").strip()
    )
    if not contexts:
        return False
    substantive_description = _contains_chinese_text(description, 8)
    required_anchors = 2 if substantive_description else 1
    return _cover_title_context_anchor_count(title, *contexts) < required_anchors


def _cover_title_subject_tokens(*contexts):
    """Extract whole-video subjects shared by the available contexts."""
    tokens = []
    for context in contexts:
        tokens.extend(
            token
            for token in re.findall(
                r"\b[A-Za-z][A-Za-z0-9_-]{2,}\b",
                str(context or ""),
            )
            if token.lower() not in _TITLE_LATIN_STOPWORDS
        )
    if not tokens:
        return set()
    threshold = 2 if len(contexts) > 1 else 1
    counts = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return {
        token for token, count in counts.items() if count >= threshold
    }


def _cover_title_has_thesis_issue(
    title,
    form_title="",
    source_title="",
    description="",
):
    """Require a strong hook to name the video subject and its main action."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    subjects = _cover_title_subject_tokens(
        form_title,
        source_title,
        description,
    )
    if not subjects or not _contains_chinese_text(description, 8):
        return False
    value_lower = value.casefold()
    has_subject = any(
        subject.casefold() in value_lower for subject in subjects
    )
    has_action = _cover_title_has_thesis_action(value)
    return not (has_subject and has_action)


def _shared_title_has_quality_issue(title):
    """Reject structural fragments; editorial preferences are scored later."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    if not value:
        return True
    if _title_has_naturalness_issue(value):
        return True
    if any(term in value for term in _SHARED_TITLE_BANNED_TERMS):
        return True
    if "把" in value or "押注解决" in value:
        return True
    if "这个视频" in value or "内容非常丰富" in value:
        return True
    if value.endswith("做成产品") and not any(
        term in value
        for term in _SHARED_TITLE_CONCRETE_OBJECTS
    ):
        return True
    # A form title needs both a predicate and enough context for a viewer who
    # has not watched the video. This is a grammar gate, not an editorial gate.
    if not any(marker in value for marker in _SHARED_TITLE_ACTION_MARKERS):
        return True
    return not any(marker in value for marker in _SHARED_TITLE_CONTEXT_MARKERS)


def _shared_title_score(title, max_chars=SHARED_UPLOAD_TITLE_MAX_CHARS):
    """Prefer complete titles that carry more than one substantive claim."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    score = 0.0
    score += 2.0 * sum(marker in value for marker in _SHARED_TITLE_ACTION_MARKERS)
    score += 2.0 * sum(marker in value for marker in _SHARED_TITLE_CONTEXT_MARKERS)
    score += 4.0 * sum(marker in value for marker in _SHARED_TITLE_CONCRETE_OBJECTS)
    score += 5.0 * sum(marker in value for marker in _SHARED_TITLE_STANCE_MARKERS)
    score += 2.0 * sum(marker in value for marker in _SHARED_TITLE_BENEFICIARIES)
    if max_chars >= BILI_UPLOAD_TITLE_MAX_CHARS:
        if 24 <= len(value) <= min(40, max_chars):
            score += 3.0
        elif len(value) < 20:
            score -= 2.0
    elif 14 <= len(value) <= 16:
        score += 1.0
    if value.endswith("产品") and not any(
        marker in value for marker in _SHARED_TITLE_CONCRETE_OBJECTS
    ):
        score -= 4.0
    if max_chars >= BILI_UPLOAD_TITLE_MAX_CHARS:
        if not any(marker in value for marker in _SHARED_TITLE_CONCRETE_OBJECTS):
            score -= 4.0
        for repeated_object in ("数据", "问题", "产品", "销售", "工程师"):
            occurrences = value.count(repeated_object)
            if occurrences > 1:
                score -= 3.0 * (occurrences - 1)
    return score


_TITLE_LATIN_STOPWORDS = {
    "a", "an", "and", "as", "at", "for", "in", "of", "on",
    "the", "to", "with",
}
_TITLE_CJK_FUNCTION_CHARS = set("的了着是在和与把被给对向就都很也又再")


def _title_scoring_tokens(value):
    """Extract stable reference tokens from a mixed Chinese/English title."""
    normalized = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    tokens = {
        token
        for token in re.findall(r"[a-z][a-z0-9_-]*", normalized)
        if token not in _TITLE_LATIN_STOPWORDS
    }
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        compact = "".join(
            char for char in run if char not in _TITLE_CJK_FUNCTION_CHARS
        )
        if len(compact) == 1:
            tokens.add(compact)
        tokens.update(
            compact[index:index + 2]
            for index in range(len(compact) - 1)
        )
    # A Chinese public nickname must count as coverage of the English name in
    # the source description. Otherwise good titles such as "大本" are rejected
    # for having no lexical overlap with "Ben Affleck".
    for source, targets in _PROPER_NOUN_TITLE_ALIASES.items():
        source_value = str(source or "").strip().casefold()
        if not source_value or source_value not in normalized:
            continue
        for target in targets or ():
            target_runs = re.findall(
                r"[\u3400-\u9fff]+",
                str(target or ""),
            )
            for run in target_runs:
                compact = "".join(
                    char
                    for char in run
                    if char not in _TITLE_CJK_FUNCTION_CHARS
                )
                if len(compact) == 1:
                    tokens.add(compact)
                tokens.update(
                    compact[index:index + 2]
                    for index in range(len(compact) - 1)
                )
    return tokens


def _title_reference_coverage(title, reference):
    """Measure how much of a candidate is covered by a reference text."""
    title_tokens = _title_scoring_tokens(title)
    if not title_tokens:
        return 0.0
    reference_tokens = _title_scoring_tokens(reference)
    if not reference_tokens:
        return 0.0
    return len(title_tokens & reference_tokens) / len(title_tokens)


def _title_candidate_score(
    title,
    max_chars,
    central_claim="",
    description="",
    source_title="",
):
    """Combine editorial structure with semantic alignment to the video."""
    score = _shared_title_score(title, max_chars=max_chars)
    if _is_interview_title_context(
        central_claim,
        description,
        source_title,
    ):
        score += _interview_title_quality_score(title)
    if central_claim:
        score += 6.0 * _title_reference_coverage(title, central_claim)
    if description:
        first_sentence = re.split(
            r"[。！？\n]",
            str(description).strip(),
            maxsplit=1,
        )[0]
        score += 5.0 * _title_reference_coverage(title, first_sentence)
        score += 2.0 * _title_reference_coverage(title, description)
    if source_title:
        # The publisher title anchors the topic, but cannot outweigh the
        # transcript-derived description and central claim.
        score += 1.5 * _title_reference_coverage(title, source_title)
    return score


def _cover_title_repeats_form_title(title, form_title):
    """Reject generated covers that merely restate the form title."""
    normalized_title = _comparison_title(title)
    normalized_form = _comparison_title(form_title)
    if not normalized_title or not normalized_form:
        return False
    if normalized_title == normalized_form:
        return True
    title_tokens = _title_scoring_tokens(title)
    if not title_tokens:
        return False
    overlap = len(title_tokens & _title_scoring_tokens(form_title))
    return overlap / len(title_tokens) >= 0.75


def _contains_chinese_text(value, min_chars=2):
    """Return whether a metadata field carries meaningful Chinese content."""
    return len(re.findall(r"[\u3400-\u9fff]", str(value or ""))) >= min_chars


def _comparison_title(value):
    """Normalize mixed-language titles for duplicate and source-copy checks."""
    return re.sub(
        r"[^0-9A-Za-z\u3400-\u9fff]",
        "",
        str(value or ""),
    ).casefold()


def _metadata_title_is_source_copy(title, source_title):
    """Detect an untranslated publisher title, including its truncations."""
    normalized_title = _comparison_title(title)
    normalized_source = _comparison_title(source_title)
    if not normalized_title or not normalized_source:
        return False
    return (
        normalized_title == normalized_source
        or normalized_source.startswith(normalized_title)
    )


def _metadata_title_has_whole_video_issue(title, source_title="", description=""):
    """Reject cached titles that describe one transcript moment, not the video."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    summary = str(description or "").strip()
    if not value or not _contains_chinese_text(summary, 8):
        return False
    if re.match(
        r"^(?:因为|如果|我|我们|我是|我想|我觉得|我的|再说一次|再强调一次|换句话说)",
        value,
    ):
        return True
    references = [summary, str(source_title or "").strip()]
    if not any(references):
        return False
    coverage = max(
        _title_reference_coverage(value, reference)
        for reference in references
    )
    return coverage < 0.15


def _metadata_title_lacks_mainline_coverage(title, description):
    """Require a form title to cover the whole-video summary."""
    sentences = [
        sentence.strip()
        for sentence in re.split(r"[。！？\n]", str(description or ""))
        if sentence.strip()
    ]
    if len(sentences) < 2:
        return False
    if _is_interview_title_context(title, description):
        # Editorial interview titles compress a whole-person arc into a short
        # judgment. Requiring 55% token coverage pushes them toward keyword
        # concatenation instead of a readable sentence.
        coverage = _title_reference_coverage(title, description)
        return not (
            coverage >= 0.12
            and _interview_title_quality_score(title) >= 8.0
        )
    strongest_sentence_coverage = max(
        _title_reference_coverage(title, sentence)
        for sentence in sentences
    )
    whole_description_coverage = _title_reference_coverage(title, description)
    return not (
        strongest_sentence_coverage >= 0.45
        and whole_description_coverage >= 0.55
    )


def _metadata_title_has_quality_issue(
    title,
    source_title,
    require_chinese,
    description="",
):
    """Validate facts and language, not subjective editorial quality."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    if not value:
        return True
    if require_chinese and not _contains_chinese_text(value):
        return True
    if _metadata_title_is_source_copy(value, source_title):
        return True
    if _metadata_title_has_whole_video_issue(
        value,
        source_title=source_title,
        description=description,
    ):
        return True
    if (
        _is_interview_title_context(value, description, source_title)
        and _interview_title_quality_score(value) < 0
    ):
        return True
    return bool(_shared_title_has_quality_issue(value))


def _metadata_cover_title_has_quality_issue(
    cover_title,
    form_title,
    wechat_title,
    source_title,
    description,
    require_chinese,
):
    """Validate a persisted cover independently from subjective hook scoring."""
    value = re.sub(r"\s+", " ", str(cover_title or "")).strip()
    if not value:
        return True
    if not require_chinese:
        return False
    if require_chinese and not _contains_chinese_text(value):
        return True
    if _metadata_title_is_source_copy(value, source_title):
        return True
    normalized_cover = _comparison_title(value)
    if normalized_cover in {
        _comparison_title(form_title),
        _comparison_title(wechat_title),
    }:
        return True
    if _cover_title_repeats_form_title(value, form_title):
        return True
    if (
        _is_interview_title_context(value, description, source_title)
        and _interview_title_quality_score(value) < 0
    ):
        return True
    return bool(_cover_title_has_quality_issue(value))


def _invalidate_cached_upload_metadata(meta, source_title, chinese_subtitle):
    """Clear cached fields that contain unambiguous errors."""
    require_chinese = bool(chinese_subtitle)
    invalid_fields = []

    title_invalid = _metadata_title_has_quality_issue(
        meta.get("title"),
        source_title,
        require_chinese,
        meta.get("description"),
    )
    if title_invalid:
        if meta.get("title"):
            invalid_fields.append("title")
        if meta.get("wechat_title"):
            invalid_fields.append("wechat_title")
    if _metadata_title_has_quality_issue(
        meta.get("wechat_title"),
        source_title,
        require_chinese,
        meta.get("description"),
    ):
        if meta.get("wechat_title"):
            invalid_fields.append("wechat_title")

    if _metadata_cover_title_has_quality_issue(
        meta.get("cover_title"),
        meta.get("title"),
        meta.get("wechat_title"),
        source_title,
        meta.get("description"),
        require_chinese,
    ):
        if meta.get("cover_title"):
            invalid_fields.append("cover_title")

    if (
        require_chinese
        and meta.get("description")
        and not _contains_chinese_text(meta.get("description"), 8)
    ):
        invalid_fields.append("description")

    for field in dict.fromkeys(invalid_fields):
        if meta.get(field):
            meta[field] = ""
    return list(dict.fromkeys(invalid_fields))


def _cover_title_has_quality_issue(title):
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    if not value:
        return True
    if _title_has_naturalness_issue(value):
        return True
    if "这个视频" in value or "内容非常丰富" in value:
        return True
    if any(term in value for term in _COVER_TITLE_TRANSLATION_TERMS):
        return True
    predicate_markers = (
        "是", "像", "就像", "不是", "没有", "开", "做", "看", "说", "谈",
        "解决", "变成", "成为", "却", "并", "让", "给", "在",
        "对付", "阻止", "提供", "押注", "坚持", "拒绝", "被", "受影响",
        "没了", "断了", "烂掉",
    ) + _COVER_TITLE_THESIS_ACTION_MARKERS
    return not (
        any(marker in value for marker in predicate_markers)
        or _cover_title_has_thesis_action(value)
    )


def _normalize_cover_simile_title(title, full_subtitle_text):
    """Restore the simile marker when a model over-compresses a scene hook."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    simile_match = re.search(
        r"就有点像(.*)",
        str(full_subtitle_text or ""),
    )
    if not simile_match or not value.startswith("我们"):
        return value

    source_clause = simile_match.group(1)
    body = value[len("我们"):]
    body = re.sub(r"^(?:就像|像)", "", body)
    if re.search(r"带着(?:一帮)?工程师", source_clause):
        body = re.sub(r"带着(?:一帮)?工程师", "", body)
    if "正中央" in source_clause and "正中央" not in body:
        body = re.sub(
            r"(在[^，。 ]{1,16})(开)",
            r"\1正中央\2",
            body,
        )

    source_chars = set(re.findall(r"[\u3400-\u9fff]", source_clause))
    body_chars = set(re.findall(r"[\u3400-\u9fff]", body))
    if len(source_chars & body_chars) < 2:
        return value
    return "我们就像" + body


def _generate_title_from_description(
    description,
    subtitle_text,
    full_subtitle_text,
    llm_client,
    model_name,
    max_chars,
    provider=None,
    source_title=None,
):
    """Summarize the whole interview into a fuller Bilibili form title."""
    from subtitle_title import (
        parse_llm_title_candidates,
        SubtitleTitleError,
        validate_title_from_subtitle,
    )

    if not description or not subtitle_text or not full_subtitle_text or llm_client is None:
        return None

    min_chars = min(BILI_UPLOAD_TITLE_TARGET_MIN_CHARS, max_chars)
    source_title_reference = ""
    if source_title:
        source_title_reference = (
            "视频原标题是作者或平台给出的题材参考，不是唯一事实来源。"
            "先用它判断视频场景和受益者，再通读字幕确认实际主线。"
            "如果字幕反复聚焦另一个主体，标题应优先反映字幕主线，"
            "同时保留原标题中有用的场景或受益者。"
            "不要逐字翻译原标题，也不要只抽原标题里的一个词。"
            f"\n视频原标题：\n{source_title}\n\n"
        )
    interview_guidance = ""
    if _is_interview_title_context(
        source_title,
        description,
        subtitle_text,
    ):
        interview_guidance = (
            "本视频是人物访谈或人物特写，不要套用企业产品标题模板。"
            "先给人物一个公众可识别的中文称呼，优先用通行译名或熟知绰号，"
            "例如 Ben Affleck 用「大本」；无法确认时才保留英文全名。"
            "标题优先写成「人物/访谈：公众身份或人生阶段+代表性判断」，"
            "后半句可以来自全片最有代表性的原话或最小复述。"
            "表单标题仍要概括全片：选择贯穿访谈的中年处境、家庭、职业选择"
            "和情绪主线，不要把父母离世、谣言或单一新闻事件放到主位。"
            "至少 3 个候选使用原话钩子，至少 3 个候选用编辑总结；"
            "反复比较哪一句最能代表整场访谈，直到你对候选感到骄傲。"
            "只学下面结构，不复制事实："
            "「大本访谈：好莱坞一线男星的中年感悟」；"
            "「大本访谈：人生苦短，每天上班的感觉好极了」。"
            "反例是「54岁的大本谈中年心态、每天上班与人生素材库」，"
            "这种顿号并列关键词不是自然编辑标题。"
            "频道或栏目名是可选信号：有公众辨识度才写，没有就不硬凑。"
            "人物访谈不适用上文的核心业务、产品和受益者优先级；"
            "完整人物身份、人生阶段和代表性金句就是实质信息点。"
        )
    base_prompt = (
        "基于完整中文字幕和视频简介，生成B站表单标题。"
        "这不是普通摘要任务。请像准备首页推荐的资深编辑一样处理："
        "先通读全片，找出贯穿首尾的主线，再用大量思考反复改写候选，"
        "淘汰第一版和平庸版本，直到你对结果感到骄傲才输出。"
        f"标题长度必须为 {min_chars}-{max_chars} 个字符，英文字母逐字符计数。"
        "优先 20-40 个字符，只有信息不足时才低于 20 个字符。"
        "输出前必须逐字重数一遍；超过上限的候选直接作废。"
        "输出前再按中文编辑标准默读一遍：标题必须是现代汉语自然短句，"
        "不得有翻译腔、口语填充、机械拼接或AI套话。"
        "不得以「就」「光是」「你得」等口语强调开头，不得使用第二人称，"
        "不得输出「大量的支持」「学到大量东西」「思路完全不对」这类生硬表达。"
        "B站标题没有微信的 16 字限制，要表达得更充分："
        "通常覆盖 3 个实质信息点，例如访谈主题、核心业务/做法、"
        "明确立场或结果；不要为了短而删成分类标签。"
        "表单标题负责概括整场访谈，不得只放一个钩子；"
        "收购金额、发布事件或单句金句只能作为一个信息点，"
        "不得成为标题主体。"
        "表单标题禁用「把」字句；"
        "用「主体+为+受益者+动作+对象」或「主体+解决+对象+问题」表达完整谓语。"
        "先在内部完成五步：列出全片 5-8 个主要内容；"
        "判断哪条线索贯穿全片；写出 10 个候选；"
        "逐个检查事实、主体、谓语、字数和自然度；"
        "淘汰弱候选后重写一轮，只保留最强版本。"
        "多嘉宾、多段访谈或合集视频不强制压缩成单一 central_claim："
        "先用共同主题和代表性案例概括全片，标题可保留场次名称，"
        "但必须中文化并覆盖字幕中的实际场景。"
        "参考下面公开B站标题的结构，只学句式，不复制事实或短语："
        + "；".join(_PUBLIC_TITLE_STYLE_EXAMPLES)
        + "。"
        "标题必须概括谈话主题、被访者核心做法或结论，"
        "优先覆盖谈话主题、被访者核心做法和明确立场/结果；"
        "其中至少一个必须是核心业务、产品或具体做法。"
        "组织理念只能作为第二信息点，不能替代核心业务。"
        "如果某个信息点会让句子变啰嗦，保留核心业务和最有力的立场/结果。"
        "优先使用「主体+字幕中真实存在的动作或判断+具体对象+受益者/问题/结果」这类自然句式。"
        "核心业务优先写成具体动作和对象，例如「整合数据」；"
        "不要只写「解决数据隐私问题」这类结果而漏掉核心做法。"
        "候选必须能独立读成一句完整陈述：读者要知道谁做了什么、为谁或解决什么，"
        "或者知道主体明确坚持/反对什么。"
        "禁止只有「主体+动作+对象」的压缩片段，"
        "例如不得输出「Palantir把数据整合成产品」这类没有受益者、问题或立场的标题。"
        "动作和宾语按中文习惯排列，不要用「把」字句，也不要名词化成短语拼接。"
        "不要为了句式新增字幕没有的动词，包括「谈」。"
        "如果字幕没有出现人名，就用公司、产品或字幕中的具体主体开头。"
        "不要输出名词短语拼接，不要机械堆关键词，也不要只抽一个短语。"
        "不要为了凑长度新增字幕没有的连接短语。"
        "如果信息过多，优先保留主体、主线对象、核心做法和结论，删除修饰词。"
        "英文人名、地名可用中文通行译名或公众熟知的简称、绰号，"
        "只要能节省字符且不造成歧义；公司名和产品名通常保留公众拼写。"
        "不得生造译名或绰号，无法确认时保留英文原名。"
        "把使用的映射写入 subject_aliases，来源拼写必须逐字出现在字幕中。"
        "不要把全片主体缩小成单一客户群体；若同时服务企业和政府，"
        "用「客户」或自然的并列表达，不要用「大型企业」这类限定词缩窄范围。"
        "不要把动词硬放在核心对象前面造成翻译腔。"
        "多个分句不要重复同一个核心宾语或谓语，"
        "例如不要同时写「解决重要问题」和「解决问题」。"
        "「押注」后面接自然宾语，不要输出「押注解决」这类生硬连用。"
        "不要疑问句、悬念句、代词、泛词或营销口吻。"
        "不要市井口语和情绪副词，例如「死活」「简直」。"
        "标题中的事实和术语必须来自字幕。"
        "生成 10 个候选，按编辑质量从高到低排列。"
        "每个候选给出 1-2 条 evidence，必须逐字复制字幕原文，不得改写或加省略号。"
        "只输出 JSON："
        '{"central_claim":"...","subject_aliases":'
        '{"Ben Affleck":["本·阿弗莱克","大本"]},"candidates":['
        '{"title":"...","evidence":["..."]}]}\n\n'
        f"{source_title_reference}"
        f"视频简介：\n{description}\n\n"
        f"完整中文字幕：\n{subtitle_text}"
    )
    base_prompt = interview_guidance + base_prompt
    last_error = None
    validation_feedback = ""
    subject_aliases = dict(_PROPER_NOUN_TITLE_ALIASES)
    for _ in range(2):
        prompt = base_prompt
        if validation_feedback:
            prompt += (
                f"\n上一轮标题未通过字幕溯源校验：{validation_feedback}\n"
                "请只修正标题措辞和 evidence，保持全片概括不变。"
            )
        try:
            raw = _generate_content_with_timeout(
                llm_client,
                prompt,
                model_name,
                provider=provider,
            )
            _central_claim, candidates = parse_llm_title_candidates(raw)
            subject_aliases = _merge_proper_noun_aliases(
                _PROPER_NOUN_TITLE_ALIASES,
                _parse_llm_title_aliases(raw),
            )
            candidate_errors = []
            valid_candidates = []
            for title, evidence in candidates:
                title = re.sub(r"\s+", " ", str(title or "")).strip()
                if len(title) < min_chars:
                    candidate_errors.append(f"{title}: 标题少于 {min_chars} 字")
                    continue
                if len(title) > max_chars:
                    candidate_errors.append(f"{title}: 标题超过 {max_chars} 字")
                    continue
                if _shared_title_has_quality_issue(title):
                    candidate_errors.append(f"{title}: 标题不是自然完整短句")
                    continue
                try:
                    _validate_title_with_proper_noun_aliases(
                        title,
                        subtitle_text,
                        evidence,
                        min_chars=min_chars,
                        max_chars=max_chars,
                        aliases=subject_aliases,
                        validator=validate_title_from_subtitle,
                        error_class=SubtitleTitleError,
                    )
                    valid_candidates.append((title, evidence))
                except SubtitleTitleError as exc:
                    candidate_errors.append(f"{title}: {exc}")
            if valid_candidates:
                return max(
                    valid_candidates,
                    key=lambda item: _title_candidate_score(
                        item[0],
                        max_chars,
                        central_claim=_central_claim,
                        description=description,
                        source_title=source_title,
                    ),
                )[0]
            if candidate_errors:
                raise SubtitleTitleError("；".join(candidate_errors[:3]))
            raise SubtitleTitleError("未返回可校验标题候选")
        except SubtitleTitleError as exc:
            last_error = exc
            validation_feedback = str(exc)
    structured_title = _generate_structured_title_from_description(
        description,
        subtitle_text,
        llm_client,
        model_name,
        max_chars,
        source_title=source_title,
    )
    if structured_title:
        return structured_title
    if last_error is not None:
        raise last_error
    raise SubtitleTitleError("简介压缩标题生成失败")


def _generate_structured_title_from_description(
    description,
    subtitle_text,
    llm_client,
    model_name,
    max_chars,
    source_title=None,
):
    """Build a budgeted Bilibili title from semantic fields."""
    from subtitle_title import (
        SubtitleTitleError,
        validate_title_from_subtitle,
    )

    if not description or not subtitle_text or llm_client is None:
        return None

    min_chars = min(BILI_UPLOAD_TITLE_TARGET_MIN_CHARS, max_chars)
    source_title_reference = ""
    if source_title:
        source_title_reference = (
            "视频原标题只作题材和场景参考，不是唯一事实来源。"
            "若字幕主线与原标题侧重点不同，以字幕主线为主。"
            f"\n视频原标题：\n{source_title}\n\n"
        )
    interview_guidance = ""
    if _is_interview_title_context(
        source_title,
        description,
        subtitle_text,
    ):
        interview_guidance = (
            "本视频是人物访谈。subject 写人物通行中文名或熟知绰号，"
            "beneficiary 可为空；object 可写人生、中年、家庭或职业选择；"
            "second_claim 优先用全片代表性原话或最小复述。"
            "不要强行套用企业客户、核心业务或产品对象。"
            "候选必须是自然短句，不得用顿号并列「中年、上班、素材库」"
            "这类关键词。"
        )
    base_prompt = (
        "把视频简介和完整中文字幕概括成B站表单标题。"
        "输出5个结构化候选，按全片概括强度排序。"
        "字段：subject、beneficiary、action、object、second_claim、evidence。"
        "subject 来自字幕或简介；beneficiary 尽量2字；action 2字；object 2-4字。"
        "英文人名、地名的 subject 可用中文通行译名或公众熟知的简称、绰号；"
        "公司名和产品名通常保留公众拼写，不得生造译名或绰号。"
        "若使用中文主体，必须在顶层 subject_aliases 写明来源英文拼写。"
        "second_claim 是可选的明确立场或结果，尽量4-8字；"
        "只有字幕能支撑时才填写。"
        "只要字幕或简介能识别受益者，beneficiary 不得为空；"
        "若存在更具体受益者，不得用「客户」泛化。"
        "本地会拼接为「subject+为+beneficiary+action+object」，"
        "有 second_claim 时追加「，second_claim」。"
        f"完整标题必须为 {min_chars}-{max_chars} 个字符，英文字母逐字符计数。"
        "第一候选必须覆盖核心业务、产品或具体做法，"
        "object 必须是具体核心对象，例如数据、应用、平台或工具；"
        "不得用「产品」「价值」「问题」这类泛词充当 object。"
        "不要只选组织理念、单个案例或一句钩子。"
        "多嘉宾、多段访谈或合集视频不强制单一主线："
        "用共同主题和代表性案例概括全片，标题可保留场次名称。"
        "不要把全片主体缩小成单一客户群体；若同时服务企业和政府，"
        "beneficiary 用「客户」或自然的并列表达。"
        "不要市井口语和情绪副词，例如「死活」「简直」。"
        "参考公开B站标题的结构，只学句式，不复制事实。"
        "每个候选给1-2条 evidence，必须逐字复制中文字幕原文。"
        "只输出 JSON："
        '{"subject_aliases":{"Ben Affleck":["大本"]},'
        '"candidates":[{"subject":"...","beneficiary":"...",'
        '"action":"...","object":"...","second_claim":"...",'
        '"evidence":["..."]}]}\n\n'
        f"{source_title_reference}"
        f"视频简介：\n{description}\n\n"
        f"完整中文字幕：\n{subtitle_text}"
    )
    base_prompt = interview_guidance + base_prompt

    validation_feedback = ""
    subject_aliases = dict(_PROPER_NOUN_TITLE_ALIASES)
    for _ in range(2):
        prompt = base_prompt
        if validation_feedback:
            prompt += (
                f"\n上一轮候选未通过校验：{validation_feedback}\n"
                "请删除修饰语或第二信息点，不要换成同义长词。"
            )
        try:
            raw = _generate_content_with_timeout(
                llm_client,
                prompt,
                model_name,
                request_timeout=90,
            )
            text = str(raw or "").strip()
            text = re.sub(
                r"^```(?:json)?\s*|\s*```$",
                "",
                text,
                flags=re.IGNORECASE,
            ).strip()
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if not match:
                raise SubtitleTitleError("结构化标题输出不是 JSON 对象")
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise SubtitleTitleError("结构化标题 JSON 解析失败") from exc
            if not isinstance(payload, dict):
                raise SubtitleTitleError("结构化标题 JSON 结构不正确")
            subject_aliases = _merge_proper_noun_aliases(
                _PROPER_NOUN_TITLE_ALIASES,
                _parse_llm_title_aliases(payload),
            )

            raw_candidates = payload.get("candidates")
            if not isinstance(raw_candidates, list) or not raw_candidates:
                raise SubtitleTitleError("结构化标题未返回候选")

            candidate_errors = []
            valid_candidates = []
            for item in raw_candidates:
                if not isinstance(item, dict):
                    continue
                subject = re.sub(
                    r"\s+", "", str(item.get("subject", ""))
                )
                beneficiary = re.sub(
                    r"\s+", "", str(item.get("beneficiary", ""))
                )
                action = re.sub(r"\s+", "", str(item.get("action", "")))
                object_value = re.sub(
                    r"\s+", "", str(item.get("object", ""))
                )
                second_claim = re.sub(
                    r"\s+", "", str(item.get("second_claim", ""))
                )
                raw_evidence = item.get("evidence", [])
                if isinstance(raw_evidence, str):
                    raw_evidence = [raw_evidence]
                elif not isinstance(raw_evidence, list):
                    raw_evidence = []
                evidence = tuple(
                    str(value).strip()
                    for value in raw_evidence
                    if str(value).strip()
                )
                if not subject or not action or not object_value or not evidence:
                    continue

                title = subject
                if beneficiary:
                    title += "为" + beneficiary
                title += action + object_value
                if second_claim:
                    title += "，" + second_claim
                title = re.sub(r"\s+", " ", title).strip()
                if len(title) < min_chars:
                    candidate_errors.append(f"{title}: 标题少于 {min_chars} 字")
                    continue
                if len(title) > max_chars:
                    candidate_errors.append(f"{title}: 标题超过 {max_chars} 字")
                    continue
                if _shared_title_has_quality_issue(title):
                    candidate_errors.append(f"{title}: 标题不是自然完整短句")
                    continue
                try:
                    _validate_title_with_proper_noun_aliases(
                        title,
                        subtitle_text,
                        evidence,
                        min_chars=min_chars,
                        max_chars=max_chars,
                        aliases=subject_aliases,
                        validator=validate_title_from_subtitle,
                        error_class=SubtitleTitleError,
                    )
                    valid_candidates.append(title)
                except SubtitleTitleError as exc:
                    candidate_errors.append(f"{title}: {exc}")
            if valid_candidates:
                return max(
                    valid_candidates,
                    key=lambda title: _title_candidate_score(
                        title,
                        max_chars,
                        description=description,
                        source_title=source_title,
                    ),
                )
            if candidate_errors:
                raise SubtitleTitleError("；".join(candidate_errors[:3]))
            raise SubtitleTitleError("未返回可校验结构化标题候选")
        except SubtitleTitleError as exc:
            validation_feedback = str(exc)
        except Exception as exc:
            validation_feedback = str(exc)
    raise SubtitleTitleError("结构化共享标题生成失败")


def _extract_bili_title_subject(bili_title):
    """Return the leading subject that a WeChat title should preserve."""
    value = re.sub(r"\s+", " ", str(bili_title or "")).strip()
    if not value:
        return ""

    if "：" in value or ":" in value:
        prefix = re.split(r"[：:]", value, maxsplit=1)[0].strip()
        if "访谈" in prefix:
            before, after = prefix.split("访谈", 1)
            subject = after.strip() or before.strip()
            if subject:
                return subject

    markers = (
        _SHARED_TITLE_ACTION_MARKERS
        + _SHARED_TITLE_STANCE_MARKERS
        + ("为", "访谈", "对话", "没有")
    )
    positions = [
        position
        for marker in markers
        if marker and marker in value
        for position in (value.find(marker),)
        if position > 0
    ]
    if not positions:
        return ""

    subject = value[:min(positions)]
    subject = subject.strip(" ：:，,。.？?；;！!")
    return subject


def _merge_proper_noun_aliases(*alias_maps):
    """Combine built-in and model-provided aliases without losing either."""
    merged = {}
    for alias_map in alias_maps:
        if not isinstance(alias_map, dict):
            continue
        for source, targets in alias_map.items():
            source_value = str(source or "").strip()
            if not source_value:
                continue
            if isinstance(targets, str):
                targets = (targets,)
            values = tuple(
                str(target or "").strip()
                for target in targets or ()
                if str(target or "").strip()
            )
            if not values:
                continue
            merged.setdefault(source_value, tuple())
            existing = merged[source_value]
            merged[source_value] = tuple(dict.fromkeys(existing + values))
    return merged


def _preferred_title_subject(subject, max_chars, aliases=None):
    """Return the shortest unambiguous public spelling for a title subject."""
    value = str(subject or "").strip()
    if not value:
        return value
    candidates = [value]
    for source, targets in (aliases or {}).items():
        if source.strip().casefold() != value.casefold():
            continue
        candidates.extend(str(target or "").strip() for target in targets)
    valid = [
        candidate
        for candidate in candidates
        if candidate and len(candidate) <= max_chars
    ]
    return min(valid, key=len) if valid else value


def _apply_preferred_title_aliases(value, max_chars, aliases=None):
    """Use shorter public aliases for known English proper nouns."""
    result = re.sub(r"\s+", " ", str(value or "")).strip()
    alias_map = _merge_proper_noun_aliases(
        _PROPER_NOUN_TITLE_ALIASES,
        aliases,
    )
    for source, targets in alias_map.items():
        source_value = str(source or "").strip()
        if not source_value:
            continue
        preferred = _preferred_title_subject(
            source_value,
            max_chars,
            alias_map,
        )
        if preferred == source_value:
            continue
        result = re.sub(
            rf"(?<![A-Za-z0-9_-]){re.escape(source_value)}(?![A-Za-z0-9_-])",
            preferred,
            result,
        )
    return result


def _wechat_title_preserves_bili_subject(
    title,
    bili_title,
    max_chars,
    subject_aliases=None,
):
    """Check whether a compressed title keeps the Bilibili title's subject."""
    subject = _extract_bili_title_subject(bili_title)
    if not subject or len(subject) > max_chars - 4:
        return True
    value = str(title or "")
    if subject in value:
        return True
    aliases = (subject_aliases or {}).get(subject, ())
    return any(alias in value for alias in aliases)


def _compress_bili_title_for_wechat(
    bili_title,
    max_chars,
    description="",
    subject_aliases=None,
):
    """Locally shorten a validated Bilibili title when model counting fails."""
    value = re.sub(r"\s+", " ", str(bili_title or "")).strip()
    value = _apply_preferred_title_aliases(
        value,
        max_chars,
        subject_aliases,
    )
    subject = _extract_bili_title_subject(value)
    subject = _preferred_title_subject(
        subject,
        max_chars,
        _merge_proper_noun_aliases(
            _PROPER_NOUN_TITLE_ALIASES,
            subject_aliases,
        ),
    )
    clauses = [clause.strip() for clause in re.split(r"[，,；;。]", value) if clause.strip()]

    source_text = value + "\n" + str(description or "")
    action = next(
        (
            marker
            for marker in ("整合", "保护", "解决", "打造", "建立")
            if marker in source_text
        ),
        "",
    )
    concrete_object = next(
        (
            marker
            for marker in _SHARED_TITLE_CONCRETE_OBJECTS
            if marker in source_text
        ),
        "",
    )
    stance_match = re.search(
        r"(不招销售|不做服务|没有销售团队|不招市场|没有市场部门)",
        source_text,
    )
    if subject and action and concrete_object and stance_match:
        combined = subject + action + concrete_object + stance_match.group(1)
        combined = sanitize_wechat_title(
            combined,
            max_chars=max(len(combined), 1),
        )
        if (
            len(combined) <= max_chars
            and not _shared_title_has_quality_issue(combined)
            and _wechat_title_preserves_bili_subject(
                combined,
                value,
                max_chars,
                subject_aliases=subject_aliases,
            )
        ):
            return combined

    candidates = []
    for clause in clauses:
        body = clause[len(subject):] if subject and clause.startswith(subject) else clause
        without_modifiers = body
        for modifier in ("死活", "依然", "尽量", "所谓", "基本上", "散落", "各类", "底层"):
            without_modifiers = without_modifiers.replace(modifier, "")
        without_conjoined_object = re.sub(
            r"(?:与|和)[\u3400-\u9fff]{1,4}(?=团队|业务|问题|公司)",
            "",
            body,
        )
        without_beneficiary = re.sub(
            r"^为.{1,10}?(?=解决|整合|保护|打造|建立)",
            "",
            body,
        )
        without_bet_prefix = re.sub(
            r"^押注.{1,8}?(?=不招|不做|没有)",
            "",
            body,
        )
        compressed = re.sub(
            r"(?:与|和)[\u3400-\u9fff]{1,4}(?=团队|业务|问题|公司)",
            "",
            without_modifiers,
        )
        for candidate_body in (
            body,
            without_modifiers,
            without_conjoined_object,
            without_beneficiary,
            without_bet_prefix,
            compressed,
            compressed.replace("核心", ""),
            compressed.replace("团队", ""),
        ):
            candidate = subject + candidate_body if subject else candidate_body
            candidate = sanitize_wechat_title(
                candidate,
                max_chars=max(len(candidate), 1),
            )
            if (
                len(candidate) <= max_chars
                and not _shared_title_has_quality_issue(candidate)
                and _wechat_title_preserves_bili_subject(
                    candidate,
                    value,
                    max_chars,
                    subject_aliases=subject_aliases,
                )
            ):
                candidates.append(candidate)
    if not candidates:
        return None

    def _local_wechat_score(candidate):
        score = _shared_title_score(candidate, max_chars=max_chars)
        score += 6.0 * sum(
            marker in candidate
            for marker in _SHARED_TITLE_STANCE_MARKERS
        )
        score += 4.0 * sum(
            marker in candidate
            for marker in ("被裁", "烂掉", "没了", "断了", "受影响")
        )
        if 14 <= len(candidate) <= max_chars:
            score += 1.0
        return score

    return max(candidates, key=_local_wechat_score)


def _derive_wechat_title_from_bili_title(
    bili_title,
    description,
    subtitle_text,
    llm_client,
    model_name,
    max_chars=SHARED_UPLOAD_TITLE_MAX_CHARS,
    provider=None,
    source_title=None,
):
    """Choose a transcript-grounded hook within the Bilibili title's topic."""
    from subtitle_title import (
        parse_llm_title_candidates,
        SubtitleTitleError,
        validate_title_from_subtitle,
    )

    if not bili_title or not description or not subtitle_text or llm_client is None:
        return None

    min_chars = min(12, max_chars)
    source_title_reference = ""
    if source_title:
        source_title_reference = (
            "视频原标题只用于校验题材和场景，不得取代B站标题的中心主张。"
            f"\n视频原标题：\n{source_title}\n\n"
        )
    interview_guidance = ""
    if _is_interview_title_context(
        bili_title,
        description,
        subtitle_text,
        source_title,
    ):
        interview_guidance = (
            "本视频是人物访谈。必须保留人物主体，优先使用公众熟知绰号，"
            "例如「大本」，不要保留英文全名。"
            "16 字内优先写「人物+人生阶段+代表性金句」，"
            "例如「大本：人生到第三节」这类结构。"
            "也可以用「大本：人到中年没杏生活」这类完整短句。"
            "不要把单一八卦事件、父母离世或谣言当成视频号标题主体；"
            "这些只能作为次要信息，且通常放不下。"
        )
    base_prompt = (
        "为视频号生成更有吸引力的表单标题。"
        "请像资深短视频编辑一样处理：先确认B站标题的主题边界，"
        "再通读字幕寻找同一主线里的具体后果、反差和立场，"
        "内部生成 10 个候选并逐字计数，淘汰第一版和平庸版本后输出。"
        "B站标题只确定主题边界和必须保留的主体，不是逐字压缩模板。"
        "优先通读字幕，找出同一主线里最具体的冲突、后果、反差或人物立场；"
        "可以选字幕钩子，不得另起主题，也不得换成字幕外的概括。"
        "英文人名、地名如果节省字符且不造成歧义，优先使用中文通行译名"
        "或公众熟知的简称、绰号；公司名和产品名通常保留公众拼写。"
        "不得生造译名或绰号，无法确认时保留英文。"
        "例如 Ben Affleck 可用「本·阿弗莱克」「本阿弗莱克」或「大本」，"
        "并在 subject_aliases 里写明来源拼写。"
        f"标题长度必须为 {min_chars}-{max_chars} 个字符，英文字母逐字符计数。"
        "例如 Palantir 为 8 字符，中文最多还能放 8 个字。"
        "必须优先保留B站标题开头的主体，可用其中文通行名或公众熟知简称；"
        "例如 Palantir、卡普、OpenAI研究员或大本。"
        "只有主体过长导致无法保留核心谓语时才允许省略。"
        "先计算主体字符数，再用标题上限减去主体字符数得到剩余额度；"
        "每个候选的其余文字都必须落在这个额度内，超限候选直接作废。"
        "优先保留主体和最有力的后果或立场；"
        "长度允许时再保留核心业务/做法或受益者；"
        "不要只留「主体+核心对象」的单信息点标题。"
        "禁止只留「主体+对象」或「主体+动作+对象」的分类标签。"
        "16字允许时优先输出「主体+具体后果/反差」或"
        "「主体+核心动作+对象+最有力的立场」，"
        "例如主体后接「被裁后组织会烂掉」或「整合数据不招销售」。"
        "输出必须是现代汉语自然短句：不得以「就」「光是」开头，"
        "不得使用「你」「您」，不得保留字幕里的口语填充或直译腔。"
        "禁用「把」字句，输出必须能独立读成一句完整陈述。"
        "不要为了凑满 16 字新增字幕没有的连接短语。"
        "生成 10 个候选，按编辑质量从高到低排列。"
        "每个候选给出 1-2 条 evidence，必须逐字复制中文字幕原文。"
        "只输出 JSON："
        '{"central_claim":"...","subject_aliases":'
        '{"Ben Affleck":["本·阿弗莱克","大本"]},'
        '"candidates":['
        '{"title":"...","evidence":["..."]}]}\n\n'
        f"{source_title_reference}"
        f"B站表单标题：\n{bili_title}\n\n"
        f"视频简介：\n{description}\n\n"
        f"完整中文字幕：\n{subtitle_text}"
    )
    base_prompt = interview_guidance + base_prompt

    last_error = None
    validation_feedback = ""
    subject_aliases = dict(_PROPER_NOUN_TITLE_ALIASES)
    for _ in range(2):
        prompt = base_prompt
        if validation_feedback:
            prompt += (
                f"\n上一轮视频号标题未通过字幕溯源校验：{validation_feedback}\n"
                "请只修正措辞和 evidence，保持B站标题的核心主张不变。"
            )
        try:
            raw = _generate_content_with_timeout(
                llm_client,
                prompt,
                model_name,
                provider=provider,
            )
            _central_claim, candidates = parse_llm_title_candidates(raw)
            subject_aliases = _merge_proper_noun_aliases(
                _PROPER_NOUN_TITLE_ALIASES,
                _parse_llm_title_aliases(raw),
            )
            candidate_errors = []
            valid_candidates = []
            for title, evidence in candidates:
                title = sanitize_wechat_title(
                    title,
                    max_chars=max(len(str(title)), 1),
                )
                if len(title) < min_chars:
                    candidate_errors.append(f"{title}: 标题少于 {min_chars} 字")
                    continue
                if len(title) > max_chars:
                    candidate_errors.append(f"{title}: 标题超过 {max_chars} 字")
                    continue
                if _shared_title_has_quality_issue(title):
                    candidate_errors.append(f"{title}: 标题不是自然完整短句")
                    continue
                if not _wechat_title_preserves_bili_subject(
                    title,
                    bili_title,
                    max_chars,
                    subject_aliases=subject_aliases,
                ):
                    candidate_errors.append(
                        f"{title}: 未保留B站标题主体 {_extract_bili_title_subject(bili_title)}"
                    )
                    continue
                try:
                    _validate_title_with_proper_noun_aliases(
                        title,
                        subtitle_text,
                        evidence,
                        min_chars=min_chars,
                        max_chars=max_chars,
                        aliases=subject_aliases,
                        validator=validate_title_from_subtitle,
                        error_class=SubtitleTitleError,
                    )
                    valid_candidates.append((title, evidence))
                except SubtitleTitleError as exc:
                    candidate_errors.append(f"{title}: {exc}")
            if valid_candidates:
                subject = _extract_bili_title_subject(bili_title)
                aliases = subject_aliases.get(subject, ())
                return max(
                    valid_candidates,
                    key=lambda item: (
                        _shared_title_score(item[0], max_chars)
                        + (
                            6.0
                            if subject
                            and (subject in item[0] or any(
                                alias in item[0] for alias in aliases
                            ))
                            else 0.0
                        )
                        + 8.0 * _title_reference_coverage(item[0], bili_title)
                        + 1.5 * _title_reference_coverage(item[0], source_title)
                    ),
                )[0]
            if candidate_errors:
                raise SubtitleTitleError("；".join(candidate_errors[:3]))
            raise SubtitleTitleError("未返回可校验视频号标题候选")
        except SubtitleTitleError as exc:
            last_error = exc
            validation_feedback = str(exc)
    if last_error is not None:
        fallback_title = _compress_bili_title_for_wechat(
            bili_title,
            max_chars,
            description=description,
            subject_aliases=subject_aliases,
        )
        if fallback_title:
            return fallback_title
        raise last_error
    fallback_title = _compress_bili_title_for_wechat(
        bili_title,
        max_chars,
        description=description,
        subject_aliases=subject_aliases,
    )
    if fallback_title:
        return fallback_title
    raise SubtitleTitleError("视频号标题压缩失败")


def _generate_cover_title_from_full_subtitle(
    full_subtitle_text,
    llm_client,
    model_name,
    min_chars=12,
    max_chars=48,
    provider=None,
    form_title=None,
    source_title=None,
    description=None,
):
    """Generate a cover headline by scanning the complete subtitle transcript."""
    from subtitle_title import (
        parse_llm_title_candidates,
        SubtitleTitleError,
        validate_title_from_subtitle,
    )

    if not full_subtitle_text or llm_client is None:
        return None

    title_context = ""
    if form_title or source_title or description:
        title_context = (
            "封面标题不是表单标题的压缩版。表单标题只用于判断主题边界和兜底，"
            "不得作为封面标题的主要模板。请从完整字幕中选一个具体原话或克制复述，"
            "让它与表单标题呼应同一主题，但呈现更具体的钩子、画面或冲突。"
        )
        if form_title:
            title_context += f"\nB站表单标题：\n{form_title}\n"
        if source_title:
            title_context += (
                "\n视频原标题（题材参考，不是唯一事实来源）：\n"
                f"{source_title}\n"
            )
        if description:
            title_context += f"\n视频简介（全片主线参考）：\n{description}\n"
        title_context += "\n"
    if _is_interview_title_context(
        form_title,
        source_title,
        description,
    ):
        title_context += (
            "本视频是人物访谈。封面标题优先选人物亲口说出、"
            "且能代表全片情绪主线的金句；人生阶段、通勤上班、"
            "素材库、遗憾这类句子通常比单一谣言或八卦事件更强。"
            "钩子必须锚定人物，优先用公众熟知绰号，例如「大本」。"
            "先比较 8-12 个原话，再判断哪一句最能代表整场访谈，"
            "不要只按猎奇程度选择。"
            "只学结构，不复制事实："
            "「大本访谈：人一到50，就没性生活了」优于"
            "「大本访谈：活得够久，人生成了素材库」，"
            "因为前者是具体、可传播且贴住中年主线的原话。"
        )

    base_prompt = (
        "你是资深中文科技内容编辑。请通读下面的完整中文字幕，"
        "不要只看开头、结尾或采样片段。"
        "这不是普通摘句。先在内部列出 8-12 个有潜力的原话钩子，"
        "按主体明确、画面具体、后果清楚和对全片主题的代表性排序；"
        "再生成 10 个候选，逐个自检谁/哪里发生什么、有什么后果或反差，"
        "淘汰抽象、谜面和名词堆砌版本，重写一轮后只输出最强候选。"
        "多嘉宾、多段访谈或合集视频不要求一个钩子覆盖所有嘉宾："
        "从任一代表性段落选强钩子，但它要能体现全片的主题气质。"
        "参考高播放封面标题的结构，不复制事实、不学标题党："
        "观众三秒内要能读出「主体或场景、冲突、后果」中的至少两项；"
        "优先「主体+反常识做法+可见结果」「场景+突发事件+具体人群受影响」"
        "「身份+极端时间+具体任务」这类结构。"
        "钩子必须接回全片主旨：写出主体或场景，再接主线动作或可见后果，"
        "不得只留一个局部事实。"
        "人称视角按证据强度选择：亲历者讲已发生做法时，"
        "优先「我们+具体动作+了」的第一人称；需要交代全片主体、"
        "跨嘉宾主线或编辑判断时，用第三人称。不得为了统一风格"
        "抹掉「我们」或「了」，也不得把一般性陈述改成伪亲历。"
        "「internal model」写成「自建模型」或「自己的模型」，"
        "不得写成「内部模型」；只有同一证据链明确说本地部署时，"
        "才写「本地模型」，不得把别段的本地部署或本体拼进当前案例。"
        "标题可以留悬念，但不得留谜面；局部比喻必须补足主体或后果，"
        "否则宁可换成更直白的冲突句。"
        "封面标题必须是语义完整的句子，可以比表单标题更具体，不必刻意压短。"
        "主语、谓语和比喻/冲突对象要保留，目标形态例如"
        "「我们就像在帕罗奥图正中央开马戏团」。"
        "候选必须以具体主体或完整分句主语开头，"
        "不得以「就像」「在」「需要」「带着」这类无主语成分开头。"
        "标题长度优先 18-32 个字符；超过 32 字时先删除修饰性短语，"
        "保留主语、谓语、比喻核心和地点，不得保留「带着一帮工程师」这类可删修饰。"
        "「正中央」这类承担画面强度的定位词必须保留。"
        "这个例子只学结构，不得把示例事实带入当前字幕。"
        "优先选具体画面、比喻、冲突或人物立场强烈的原话；"
        "不要把普通商务反差或抽象结论当成最强钩子。"
        "前 3 个候选必须按编辑判断从强到弱排列，"
        "优先使用原话或最小语法整理；"
        "原句太长或翻译腔重时，后 3 个候选可以做克制复述，"
        "复述时只删除真正的修饰语，不得删除主语、谓语或比喻核心；"
        "原话含「就」「光是」「你」「大量的」这类口语或直译成分时，"
        "必须改写成自然标题，不能整句照抄。"
        "「我们」「的」「了」若承担语法或语境作用必须保留。"
        "字幕说「就有点像」时，标题必须压缩成「就像」，不得只写「像」。"
        "不得输出无语境的反常识短语或名词短语拼接。"
        "不得使用「有人」「所有人」「大量」这类模糊指代，"
        "受影响对象要写成具体人群、系统或业务。"
        "不得用「行业落地」「实际价值」「业务价值」这类抽象成果句充当封面钩子。"
        "每个候选生成后先自检：不知道视频内容的观众能否看出谁/哪里发生什么、"
        "为什么值得点开；看不出就作废。"
        "复述要保留原句的具体对象和语气，不得引入字幕外事实，"
        "不得使用市井口语、夸张营销或AI套话。"
        "英文人名、地名可用中文通行译名或公众熟知的简称、绰号，"
        "只要不造成歧义且让钩子更自然；公司名和产品名通常保留公众拼写。"
        "不得生造译名或绰号，无法确认时保留英文原名。"
        "把使用的映射写入 subject_aliases，来源拼写必须逐字出现在字幕中。"
        "标题长度 12-48 个字符，优先 18-32 个字符。"
        "每个候选给出 1-2 条 evidence，必须逐字复制字幕原文。"
        "只输出 JSON："
        '{"central_claim":"...","subject_aliases":'
        '{"Ben Affleck":["本·阿弗莱克","大本"]},"candidates":['
        '{"title":"...","evidence":["..."]}]}'
        f"\n{title_context}"
        f"\n\n完整字幕：\n{full_subtitle_text}"
    )

    last_error = None
    validation_feedback = ""
    subject_aliases = dict(_PROPER_NOUN_TITLE_ALIASES)
    for _ in range(2):
        prompt = base_prompt
        if validation_feedback:
            prompt += (
                f"\n上一轮候选未通过字幕溯源校验：{validation_feedback}\n"
                "请只修正标题措辞和 evidence，保持钩子不变。"
            )
        try:
            raw = _generate_content_with_timeout(
                llm_client,
                prompt,
                model_name,
                provider=provider,
            )
            _central_claim, candidates = parse_llm_title_candidates(raw)
            subject_aliases = _merge_proper_noun_aliases(
                _PROPER_NOUN_TITLE_ALIASES,
                _parse_llm_title_aliases(raw),
            )
            valid_candidates = []
            candidate_errors = []
            for title, evidence in candidates:
                try:
                    title = _normalize_cover_simile_title(title, full_subtitle_text)
                    if _cover_title_has_quality_issue(title):
                        raise SubtitleTitleError("封面标题不是完整短句")
                    if _cover_title_repeats_form_title(title, form_title):
                        raise SubtitleTitleError("封面标题复写表单标题")
                    _validate_title_with_proper_noun_aliases(
                        title,
                        full_subtitle_text,
                        evidence,
                        min_chars=min_chars,
                        max_chars=max_chars,
                        aliases=subject_aliases,
                        validator=validate_title_from_subtitle,
                        error_class=SubtitleTitleError,
                    )
                    valid_candidates.append((title, evidence))
                except SubtitleTitleError as exc:
                    candidate_errors.append(f"{title}: {exc}")
            if valid_candidates:
                compact_subtitle = re.sub(r"\s+", "", full_subtitle_text)
                def _cover_score(indexed_item):
                    index, item = indexed_item
                    title = item[0]
                    compact_title = re.sub(r"\s+", "", title)
                    score = 0.0
                    if _is_interview_title_context(
                        form_title,
                        source_title,
                        description,
                    ):
                        score += _interview_title_quality_score(title)
                    score += 2.0 * _cover_title_information_units(title)
                    score += 2.0 * _cover_title_context_anchor_count(
                        title,
                        form_title,
                        source_title,
                        description,
                    )
                    score += 0.5 * _title_reference_coverage(title, source_title)
                    # The model orders its strongest hooks first. Local scoring
                    # only corrects obvious filler and abstract-business bias.
                    score -= index * 0.25
                    if compact_title in compact_subtitle:
                        score += 2.0
                    if title.startswith("我们") and re.search(
                        r"(?:识别|建立|采用|上线|救治|整合|优化)了",
                        title,
                    ):
                        score += 1.5
                    if any(marker in title for marker in ("就像", "像")):
                        score -= 2.0
                    if any(marker in title for marker in _COVER_TITLE_CONSEQUENCE_MARKERS):
                        score += 4.0
                    if any(marker in title for marker in _COVER_TITLE_VAGUE_TERMS):
                        score -= 8.0
                    if 18 <= len(title) <= 32:
                        score += 3.0
                    elif 14 <= len(title) <= 36:
                        score += 1.0
                    # A concrete scene or metaphor is the cover's strongest
                    # hook. Ordinary business contrasts remain valid candidates
                    # but should not outrank a grounded visual sentence.
                    scene_markers = (
                        "马戏团", "帕罗奥图", "工程师", "西装", "被车撞",
                        "战争", "武器", "核弹", "恐怖", "袭击", "网络攻击",
                        "欺诈", "算法", "人脑", "大脑", "深水区", "警局",
                        "医院", "前线", "病房", "临床",
                    )
                    if any(marker in title for marker in scene_markers):
                        score += 6.0
                    business_markers = (
                        "销售团队", "商业模式", "收入", "增长", "估值",
                        "市场份额",
                    )
                    if any(marker in title for marker in business_markers):
                        score -= 5.0
                    return score
                return max(enumerate(valid_candidates), key=_cover_score)[1][0]
            last_error = "；".join(candidate_errors[:3])
            validation_feedback = last_error or "未返回可校验候选"
        except SubtitleTitleError as exc:
            last_error = exc
            validation_feedback = str(exc)
        except Exception as exc:
            validation_feedback = str(exc)
    if last_error is not None:
        if isinstance(last_error, SubtitleTitleError):
            raise last_error
        raise SubtitleTitleError(str(last_error))
    raise SubtitleTitleError("封面标题生成失败")


def _extract_chinese_subtitle_lines(full_subtitle_text):
    """Return every Chinese subtitle line, preserving transcript order."""
    lines = [
        line.strip()
        for line in str(full_subtitle_text or "").splitlines()
        if line.strip() and re.search(r"[\u3400-\u9fff]", line)
    ]
    return "\n".join(lines)


_PROPER_NOUN_ASR_CORRECTIONS = {
    "Ermann": "Ermon",
    "Walkwest": "Wahlquist",
}


def _correct_known_asr_proper_nouns(value):
    """Apply deterministic ASR corrections before metadata generation."""
    corrected = str(value or "")
    for wrong, right in _PROPER_NOUN_ASR_CORRECTIONS.items():
        corrected = re.sub(
            rf"(?<![A-Za-z]){re.escape(wrong)}(?![A-Za-z])",
            right,
            corrected,
        )
    return corrected


def _metadata_text_issues(text, subtitle_text, video_path):
    """Check generated metadata against subtitle and filename proper nouns."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ["description is empty"]
    if any(fragment in value for fragment in ("快么服务", "集成么服务", "么服务")):
        return ["description contains a truncated service fragment"]
    for wrong in _PROPER_NOUN_ASR_CORRECTIONS:
        if re.search(rf"(?<![A-Za-z]){re.escape(wrong)}(?![A-Za-z])", value):
            return [f"known ASR proper noun misspelling: {wrong}"]

    allowed_text = (
        re.sub(r"\s+", " ", str(subtitle_text or ""))
        + " "
        + os.path.basename(str(video_path or ""))
    ).casefold()
    allowed_text = re.sub(r"[^a-z0-9'\- ]+", " ", allowed_text)
    allowed_tokens = set(allowed_text.split())

    # A Chinese summary should only introduce English tokens that are proper
    # nouns. Common sentence words are ignored so an English fallback summary
    # is not rejected merely for grammar words.
    ignored = {"a", "an", "and", "as", "at", "for", "in", "of", "on", "the", "to", "with"}
    tokens = re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Za-z0-9'-]*(?![A-Za-z0-9])", value)
    unsupported = []
    for token in tokens:
        normalized = token.casefold().strip("'-")
        if not normalized or normalized in ignored:
            continue
        if normalized not in allowed_tokens:
            unsupported.append(token)
    return [f"unsupported proper noun: {token}" for token in unsupported]


def _extract_source_title(video_path):
    """Recover the publisher title from an info file or the video filename."""
    path = str(video_path or "")
    directory = os.path.dirname(path) or "."
    base = os.path.splitext(os.path.basename(path))[0]
    base = re.sub(r"_hardsub$", "", base)

    info_candidates = [os.path.join(directory, base + ".info.json")]
    info_candidates.extend(glob.glob(os.path.join(directory, "*.info.json")))
    for info_path in info_candidates:
        try:
            with open(info_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            title = str(data.get("title") or "").strip()
            normalized_title = re.sub(r"[\s_\-]+", " ", title).strip().casefold()
            if title and normalized_title not in {
                "video", "hardsub", "video hardsub", "output", "untitled",
            }:
                return title
        except (OSError, ValueError, json.JSONDecodeError):
            continue

    # Downloaded names normally end with the video id and quality labels.
    for _ in range(2):
        base = re.sub(r"\s*\[[^\]]+\]\s*$", "", base).strip()
    base = base.replace("_", " ")
    value = re.sub(r"\s+", " ", base).strip()
    if _is_usable_source_title(value):
        return value

    # Some downloaded filenames collapse to a playlist label such as "ENG 1".
    # Only then should the task folder, which preserves the publisher title,
    # be used as the source-title fallback.
    if re.fullmatch(
        r"(?:eng|en|ep|episode|part|p|video)\s*\d+",
        value.casefold(),
    ):
        folder_base = os.path.basename(os.path.normpath(directory))
        folder_value = re.sub(r"\s+", " ", folder_base.replace("_", " ")).strip()
        folder_value = re.sub(r"^\[\d+\]\s*-\s*", "", folder_value).strip()
        for _ in range(2):
            folder_value = re.sub(r"\s*\[[^\]]+\]\s*$", "", folder_value).strip()
        if _is_usable_source_title(folder_value):
            return folder_value
    return ""


_SOURCE_TITLE_TOPIC_TRANSLATIONS = (
    ("ontology", "本体论"),
    ("deep learning", "深度学习"),
    ("artificial intelligence", "AI"),
    ("machine learning", "机器学习"),
    ("robots", "机器人"),
)


def _source_title_topic_fallback(source_title, max_chars, description=""):
    """Turn a publisher topic into a readable Chinese last-resort title."""
    value = re.sub(r"\s+", " ", str(source_title or "")).strip()
    if not value:
        return ""
    value = re.sub(r"^\[[^]]+\]\s*-\s*", "", value).strip()
    lower_value = value.casefold()
    topics = [
        chinese
        for english, chinese in _SOURCE_TITLE_TOPIC_TRANSLATIONS
        if english in lower_value
    ]
    if not topics:
        return ""

    subject = _description_cover_subject(value, description)
    if not subject:
        subject = next(
            (
                token
                for token in re.findall(r"\b[A-Za-z][A-Za-z0-9_-]*\b", value)
                if token.casefold() not in _TITLE_LATIN_STOPWORDS
                and not re.fullmatch(
                    r"(?:eng|en|ep|episode|part|p|video)\d*",
                    token.casefold(),
                )
                and not any(
                    token.casefold() in english
                    for english, _ in _SOURCE_TITLE_TOPIC_TRANSLATIONS
                )
            ),
            "",
        )
    subject = _apply_preferred_title_aliases(subject, max_chars)

    primary_topic = topics[0]
    if subject:
        candidate = f"{subject}谈{primary_topic}"
        if "深度学习" in topics and primary_topic != "深度学习":
            candidate = f"{subject}谈{primary_topic}：深度学习新解读"
    else:
        candidate = f"用{'与'.join(topics[:2])}讲清主题"
    if len(candidate) > max_chars:
        candidate = f"{subject}谈{primary_topic}" if subject else candidate[:max_chars]
    return candidate


def _is_usable_source_title(value):
    """Reject placeholders and sequence labels that carry no video topic."""
    normalized = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    if normalized in {
        "video", "hardsub", "video hardsub", "output", "untitled",
        "unknown video", "loading", "noads",
    }:
        return False
    if re.fullmatch(
        r"(?:eng|en|ep|episode|part|p|video)\s*\d+",
        normalized,
    ):
        return False
    return len(re.sub(r"[^a-z0-9\u3400-\u9fff]", "", normalized)) >= 4


def _fallback_bili_title_from_source_title(source_title, max_chars, description=""):
    """Use the publisher title when model and subtitle titles both fail."""
    value = re.sub(r"\s+", " ", str(source_title or "")).strip()
    value = _apply_preferred_title_aliases(value, max_chars)
    if not value:
        return ""
    topic_title = _source_title_topic_fallback(
        source_title,
        max_chars,
        description=description,
    )
    if topic_title:
        normalized_topic_title = normalize_bili_upload_title(
            topic_title,
            max_chars=max_chars,
        )
        if normalized_topic_title:
            return normalized_topic_title
    interview_title = re.sub(
        r"^([^\s：:][^：:]{0,47}?)\s+on\s+(.+)$",
        r"\1谈\2",
        value,
    )
    if interview_title == value:
        interview_title = re.sub(
            r"^([^\s：:][^：:]{0,47}?)\s+-\s+(.+)$",
            r"\1谈\2",
            value,
        )
        dash_match = re.match(
            r"^([^\s：:][^：:]{0,47}?)\s+-\s+(.+)$",
            value,
        )
        if dash_match:
            topic_match = re.search(
                r"\b(AI|GPT|Europe|America|China|OpenAI|Google|Meta|Palantir|AfD)\b",
                dash_match.group(2),
                re.IGNORECASE,
            )
            if topic_match:
                interview_title = (
                    f"{dash_match.group(1).strip()}谈{topic_match.group(1)}"
                )
    if interview_title != value:
        value = interview_title
        summary = str(description or "")
        if "模仿理论" in summary and "模仿理论" not in value:
            value += "与模仿理论"
    if len(value) > max_chars:
        cut = value.rfind(" ", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = max_chars
        value = value[:cut].rstrip(" .,:;!?")
    return normalize_bili_upload_title(value, max_chars=max_chars)


def _fallback_wechat_title_from_source_title(source_title, max_chars):
    """Keep a word-boundary prefix of the publisher title for WeChat."""
    topic_title = _source_title_topic_fallback(source_title, max_chars)
    if topic_title:
        normalized_topic_title = normalize_shared_upload_title(
            topic_title,
            max_chars=max_chars,
        )
        if normalized_topic_title:
            return normalized_topic_title
    value = _fallback_bili_title_from_source_title(source_title, max_chars)
    # Interview-style publisher titles often contain a usable subject and topic
    # even when the transcript has no strong local hook.
    raw = _apply_preferred_title_aliases(source_title, max_chars)
    subject_match = re.match(r"^(.{2,48}?)\s*(?:[-:：]\s)", raw)
    topic_match = re.search(
        r"\b(AI|GPT|Europe|America|China|OpenAI|Google|Meta|Palantir|AfD)\b",
        raw,
        re.IGNORECASE,
    )
    if subject_match and topic_match:
        subject = subject_match.group(1).strip()
        if len(subject) > max_chars - 4:
            words = subject.split()
            subject = " ".join(words[-2:]) if len(words) >= 2 else subject
        candidate = f"{subject}谈{topic_match.group(1)}"
        normalized = normalize_shared_upload_title(candidate, max_chars=max_chars)
        if normalized:
            return normalized
    return normalize_shared_upload_title(value, max_chars=max_chars)


_SUBTITLE_HOOK_MARKERS = (
    "穿针引线", "结缔组织", "筋膜", "烂掉", "裁掉", "人格分裂",
    "深水区", "马戏团", "正中央", "没有销售", "不招销售", "不做服务",
    "一半", "断了", "没了",
)
_GENERIC_TITLE_MARKERS = (
    "帮助", "贡献", "展现价值", "时代", "生态", "可靠", "赋能",
    "打造", "构建", "保障", "创新", "从业者",
)


def _clean_subtitle_hook_line(line):
    """Turn one subtitle line into a headline-shaped local fallback."""
    value = re.sub(r"\s+", " ", str(line or "")).strip()
    value = value.strip("“”\"' 　")
    value = re.sub(r"^\[[^\]]+\]\s*", "", value)
    value = re.sub(r"^(?:嗯|哦|对|就是)[，,：: ]+", "", value)
    value = re.sub(
        r"^(?:哎呀|唉|然后|当然|说明一下|我是说|我是想说)[，,：: ]+",
        "",
        value,
    )
    value = re.sub(
        r"^(?:当然可以|没问题|好的|好|可以)[。.!！?？\s]+",
        "",
        value,
    )
    return value.rstrip(" 。.！!？?；;，,、")


def _clarify_cover_hook_line(line):
    """Make a grounded fallback hook readable without adding new facts."""
    value = _clean_subtitle_hook_line(line)
    replacements = (
        (
            "人一到50，那肯定就没性生活了",
            "人一到50，就没性生活了",
        ),
        (
            "人生都快到头了。我20岁的时候，觉得50岁的人跟我毫不相干",
            "人生快到头，50岁的人毫不相干",
        ),
        (
            "然后40岁、50岁，你会说，等等，怎么突然就打到人生第三节了",
            "40岁、50岁，突然打到人生第三节",
        ),
        (
            "我觉得大家要是知道你每天都来上班，会非常吃惊",
            "大家知道他每天都来上班，会非常吃惊",
        ),
        (
            "活得够久，人生本身就成了最丰富的素材库",
            "活得够久，人生成了素材库",
        ),
        ("医院被攻击，就有人做不了透析", "医院被攻击，透析先受影响"),
        ("医院被攻击，有人做不了透析", "医院被攻击，透析先受影响"),
        ("数据治理有一半的工作是在当穿针引线的人", "数据治理一半工作是在穿针引线"),
        (
            "脓毒症这个病，是全球医院病人的头号杀手",
            "我们用自建模型提前识别了脓毒症",
        ),
        ("一上来就被扔进深水区", "新人一入职就被扔进深水区"),
        ("刚入职就被扔进深水区", "新人入职就被扔进深水区"),
        ("所有人的目标都是别让飞机相撞", "目标是不让飞机相撞"),
    )
    for old, new in replacements:
        value = value.replace(old, new)
    value = re.sub(r"就有人做不了(.{2,10})", r"\1先受影响", value)
    return value


def _anchor_cover_hook_with_context(
    title,
    form_title="",
    source_title="",
    description="",
    max_chars=48,
):
    """Prefix a local hook with the whole-video subject when needed."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    contexts = (form_title, source_title, description)
    subjects = _cover_title_subject_tokens(*contexts)
    if subjects and any(
        subject.casefold() in value.casefold() for subject in subjects
    ):
        return value

    subject = ""
    context_text = " ".join(str(context or "") for context in contexts)
    for known_subject in _PROPER_NOUN_TITLE_ALIASES:
        if re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(known_subject)}(?![A-Za-z0-9_-])",
            context_text,
        ):
            subject = known_subject
            break
    if not subject:
        for context in contexts:
            for token in (
                token
                for token in re.findall(
                    r"\b[A-Za-z][A-Za-z0-9_-]{2,}\b",
                    str(context or ""),
                )
                if token.lower() not in _TITLE_LATIN_STOPWORDS
            ):
                if token:
                    subject = token
                    break
            if subject:
                break
        if not subject:
            for context in contexts:
                context_text = str(context or "")
                match = re.search(
                    r"\b([A-Z][A-Za-z0-9_-]{2,}"
                    r"(?:\s+[A-Z][A-Za-z0-9_-]{2,})+)\b",
                    context_text,
                )
                if match:
                    subject = match.group(1)
                    break
                match = re.search(
                    r"\b([A-Za-z][A-Za-z0-9_-]{2,})\s*(?:现场)?访谈",
                    context_text,
                )
                if match:
                    subject = match.group(1)
                    break
    if not subject:
        for context in contexts:
            tokens = re.findall(
                r"\b[A-Za-z][A-Za-z0-9_-]{2,}\b",
                str(context or ""),
            )
            if tokens:
                subject = tokens[0]
                break
    subject = str(subject or "").strip(" ：:，,。.！!？?；;")
    subject = _apply_preferred_title_aliases(subject, max_chars)
    if (
        not subject
        or subject.casefold() in value.casefold()
    ):
        return value

    context_text = " ".join(str(context or "") for context in contexts)
    if (
        any(
            marker in context_text
            for marker in ("访谈", "专访", "对话", "敞开谈")
        )
        and "访谈" not in subject
        and "访谈" not in value
    ):
        subject = f"{subject}访谈"
    if len(f"{subject}：{value}") > max_chars:
        return value
    return f"{subject}：{value}"


def _subtitle_sentence_candidates(subtitle_text):
    """Join subtitle fragments into complete Chinese sentence candidates."""
    buffer = ""
    candidates = []
    for raw_line in str(subtitle_text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or not re.search(r"[\u3400-\u9fff]", line):
            if buffer:
                candidates.append(buffer)
                buffer = ""
            continue

        starts_new_clause = re.match(r"^(?:我们|他们|她们|它|这|那|我)", line)
        if buffer and (
            starts_new_clause
            or (len(buffer) >= 12 and re.search(r"[，,、；;：:]", buffer))
        ):
            candidates.append(buffer)
            buffer = line
        else:
            buffer = line if not buffer else f"{buffer}{line}"
        if re.search(r"[。！？!?]$", line):
            candidates.append(buffer)
            buffer = ""
        elif len(buffer) > 72:
            candidates.append(buffer)
            buffer = ""
    if buffer:
        candidates.append(buffer)

    cleaned = []
    seen = set()
    for candidate in candidates:
        value = _clean_subtitle_hook_line(candidate)
        normalized = _comparison_title(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(value)
    return cleaned


def _subtitle_hook_score(value):
    """Prefer concrete images and consequences over generic value claims."""
    score = 0.0
    score += 12.0 * sum(
        marker in value
        for marker in _INTERVIEW_LIFE_STAGE_MARKERS
    )
    score += 14.0 * sum(
        marker in value
        for marker in _INTERVIEW_REPRESENTATIVE_HOOK_MARKERS
    )
    if any(marker in value for marker in _INTERVIEW_RUMOR_MARKERS) and not any(
        marker in value
        for marker in (
            _INTERVIEW_LIFE_STAGE_MARKERS
            + _INTERVIEW_REPRESENTATIVE_HOOK_MARKERS
        )
    ):
        score -= 16.0
    score += 16.0 * sum(
        marker in value
        for marker in ("结缔组织", "筋膜", "马戏团", "正中央", "深水区")
    )
    score += 8.0 * sum(
        marker in value
        for marker in _SUBTITLE_HOOK_MARKERS
    )
    if any(marker in value for marker in ("性生活", "杏生活")):
        score += 55.0
    score += 4.0 * sum(marker in value for marker in ("被", "没了", "断了"))
    score -= 10.0 * sum(marker in value for marker in _GENERIC_TITLE_MARKERS)
    if re.search(r"[?？]$", value):
        score -= 8.0
    if len(value) < 12:
        score -= 4.0
    return score


def _compress_subtitle_hook_for_wechat(line, max_chars):
    """Compress one grounded subtitle hook into WeChat's 16-character limit."""
    value = _clean_subtitle_hook_line(line)
    # Subtitles often break in the middle of an enumeration. The first clause
    # is the complete hook; the truncated remainder belongs to the next line.
    if "、" in value:
        value = value.split("、", 1)[0]
    replacements = (
        ("有一半的工作是在当", "一半是在"),
        ("有一半工作是", "一半是"),
        ("有一半的工作是", "一半是"),
        ("是在当", "是在"),
        ("的人", ""),
        ("的时候", ""),
    )
    for old, new in replacements:
        value = value.replace(old, new)
    value = value.rstrip("的了着")
    if not value or len(value) > max_chars:
        return ""
    candidate = sanitize_wechat_title(value, max_chars=max_chars)
    if (
        not candidate
        or candidate == "未命名视频"
        or _title_has_naturalness_issue(candidate)
        or any(marker in candidate for marker in _GENERIC_TITLE_MARKERS)
    ):
        return ""
    return candidate


def _fallback_consequence_wechat_title(subtitle_text, form_title, max_chars):
    """Turn a transcript cause and consequence into a short form title."""
    subject = _extract_bili_title_subject(form_title)
    subject = _preferred_title_subject(
        subject,
        max_chars,
        _PROPER_NOUN_TITLE_ALIASES,
    )
    compact_text = re.sub(r"\s+", "", str(subtitle_text or ""))
    if not subject:
        return ""
    if not re.search(r"被.{0,8}裁掉", compact_text) or "烂掉" not in compact_text:
        return ""
    candidate = f"{subject}被裁后组织会烂掉"
    candidate = sanitize_wechat_title(candidate, max_chars=max_chars)
    if (
        len(candidate) <= max_chars
        and not _shared_title_has_quality_issue(candidate)
        and subject in candidate
    ):
        return candidate
    return ""


def _fallback_consequence_cover_title(subtitle_text):
    """Combine a metaphor loss with its later consequence in the transcript."""
    compact_text = re.sub(r"\s+", "", str(subtitle_text or ""))
    match = re.search(r"没了([\u4e00-\u9fff]{1,6})，([^，。？！]{1,20})", compact_text)
    if not match or "烂掉" not in compact_text:
        return ""
    lost_object = match.group(1)
    affected = match.group(2).replace("、", "")
    candidate = f"没了{lost_object}，{affected}慢慢烂掉"
    if 12 <= len(candidate) <= 48 and not _cover_title_has_quality_issue(candidate):
        return candidate
    return ""


def _fallback_interview_wechat_title_from_subtitle(
    subtitle_text,
    form_title=None,
    max_chars=16,
):
    """Turn a representative interview quote into a short WeChat hook."""
    value = re.sub(r"\s+", " ", str(form_title or "")).strip()
    subject = _extract_bili_title_subject(value)
    full_name = re.search(
        r"\b[A-Z][A-Za-z0-9_-]{2,}(?:\s+[A-Z][A-Za-z0-9_-]{2,})+\b",
        value,
    )
    if full_name:
        subject = full_name.group(0)
    subject = re.sub(r"^[0-9]{2}岁的", "", subject)
    subject = _preferred_title_subject(
        subject,
        max_chars,
        _PROPER_NOUN_TITLE_ALIASES,
    )
    if not subject:
        return ""

    hook_candidates = []
    for cleaned in _subtitle_sentence_candidates(subtitle_text):
        cleaned = _clarify_cover_hook_line(cleaned)
        if not any(
            marker in cleaned
            for marker in (
                _INTERVIEW_LIFE_STAGE_MARKERS
                + _INTERVIEW_REPRESENTATIVE_HOOK_MARKERS
            )
        ):
            continue
        if "第三节" in cleaned:
            hook = "人生到第三节"
        elif "性生活" in cleaned or "杏生活" in cleaned:
            hook = "人到中年没杏生活"
        elif "快到头" in cleaned:
            hook = "人生快到头"
        elif "上班" in cleaned:
            hook = "每天上班"
        elif "素材库" in cleaned:
            hook = "人生成素材库"
        else:
            continue
        hook_score = 5.0 * sum(
            marker in hook
            for marker in _INTERVIEW_LIFE_STAGE_MARKERS
        )
        hook_score += 6.0 * sum(
            marker in hook
            for marker in _INTERVIEW_REPRESENTATIVE_HOOK_MARKERS
        )
        if "第三节" in hook:
            hook_score += 8.0
        if "杏生活" in hook or "性生活" in hook:
            hook_score += 20.0
        if "素材库" in hook:
            hook_score += 6.0
        if "快到头" in hook:
            hook_score += 3.0
        if "上班" in hook:
            hook_score += 2.0
        candidate = f"{subject}：{hook}"
        if len(candidate) > max_chars:
            continue
        normalized = normalize_shared_upload_title(
            candidate,
            max_chars=max_chars,
        )
        if normalized and not _shared_title_has_quality_issue(normalized):
            hook_candidates.append((hook_score, normalized))
    if not hook_candidates:
        return ""
    return max(hook_candidates, key=lambda item: (item[0], len(item[1])))[1]


def _fallback_wechat_title_from_subtitle(
    subtitle_text,
    max_chars=16,
    form_title=None,
):
    """Choose a concrete transcript hook when the title model times out."""
    candidates = []
    interview_title = _fallback_interview_wechat_title_from_subtitle(
        subtitle_text,
        form_title=form_title,
        max_chars=max_chars,
    )
    if interview_title:
        candidates.append((120.0, interview_title))
    consequence_title = _fallback_consequence_wechat_title(
        subtitle_text,
        form_title,
        max_chars,
    )
    if consequence_title:
        candidates.append((24.0, consequence_title))
    for line in str(subtitle_text or "").splitlines():
        cleaned = _clean_subtitle_hook_line(line)
        if not any(marker in cleaned for marker in _SUBTITLE_HOOK_MARKERS):
            continue
        candidate = _compress_subtitle_hook_for_wechat(cleaned, max_chars)
        if candidate:
            score = _subtitle_hook_score(candidate)
            if candidate.startswith("我们"):
                score -= 6.0
            if "数据治理" in candidate:
                score += 6.0
            candidates.append((score, candidate))
    if not candidates:
        return ""
    return max(
        candidates,
        key=lambda item: (
            not item[1].startswith("我们"),
            "数据治理" in item[1],
            item[0],
            len(item[1]),
        ),
    )[1]


def _fallback_interview_cover_title_from_description(
    description,
    form_title="",
    source_title="",
    max_chars=48,
):
    """Use a saved interview summary's closing judgment as a cover hook."""
    value = re.sub(r"\s+", " ", str(description or "")).strip()
    if not value or not _is_interview_title_context(
        form_title,
        source_title,
        value,
    ):
        return ""
    sentences = [
        sentence.strip()
        for sentence in re.split(r"[。！？\n]", value)
        if sentence.strip()
    ]
    if not sentences:
        return ""
    closing_sentence = sentences[-1]
    if "——" in closing_sentence:
        closing_sentence = closing_sentence.rsplit("——", 1)[-1]
    if "：" in closing_sentence:
        closing_sentence = closing_sentence.rsplit("：", 1)[-1]
    elif ":" in closing_sentence:
        closing_sentence = closing_sentence.rsplit(":", 1)[-1]
    candidate = _clarify_cover_hook_line(closing_sentence)
    if not 12 <= len(candidate) <= max_chars:
        return ""
    if not any(
        marker in candidate
        for marker in (
            _INTERVIEW_LIFE_STAGE_MARKERS
            + _INTERVIEW_REPRESENTATIVE_HOOK_MARKERS
        )
    ):
        return ""
    candidate = _anchor_cover_hook_with_context(
        candidate,
        form_title=form_title,
        source_title=source_title,
        description=value,
        max_chars=max_chars,
    )
    if (
        _cover_title_has_quality_issue(candidate)
        or _cover_title_has_engagement_issue(candidate)
        or _cover_title_has_context_issue(
            candidate,
            form_title=form_title,
            source_title=source_title,
            description=value,
        )
        or _cover_title_has_thesis_issue(
            candidate,
            form_title=form_title,
            source_title=source_title,
            description=value,
        )
    ):
        return ""
    return candidate


def _fallback_cover_title_from_subtitle(
    subtitle_text,
    form_title=None,
    description=None,
    source_title=None,
    max_chars=48,
):
    """Use any complete subtitle hook instead of repeating the form title."""
    candidates = []
    description_hook = _fallback_interview_cover_title_from_description(
        description,
        form_title=form_title,
        source_title=source_title,
        max_chars=max_chars,
    )
    if description_hook and not _cover_title_repeats_form_title(
        description_hook,
        form_title,
    ):
        # A saved summary hook is one candidate, not an override. A direct
        # transcript quote can be more specific and easier to spread.
        candidates.append(
            (_subtitle_hook_score(description_hook) + 12.0, description_hook)
        )
    consequence_cover = _fallback_consequence_cover_title(subtitle_text)
    if consequence_cover and not _cover_title_repeats_form_title(
        consequence_cover,
        form_title,
    ):
        candidates.append((100.0, consequence_cover))

    scene_markers = (
        "医院", "病房", "临床", "透析", "脓毒症", "前线", "战争", "武器",
        "攻击", "袭击", "网络攻击", "欺诈", "警局", "大脑", "人脑",
        "马戏团", "帕罗奥图", "深水区", "工程师",
        "50岁", "54岁", "人生", "第三节", "上班", "素材库",
    )
    conflict_markers = (
        "被", "没有", "不是", "不了", "头号", "杀手", "救", "烂掉",
        "断了", "没了", "永远", "必须", "标准",
        "没", "突然", "快到头",
    )
    opening_markers = (
        "大家好", "欢迎", "谢谢", "接下来", "今天我们", "本期视频",
    )

    for cleaned in _subtitle_sentence_candidates(subtitle_text):
        cleaned = _clarify_cover_hook_line(cleaned)
        if "、" in cleaned:
            first_clause = cleaned.split("、", 1)[0]
            if len(first_clause) >= 12:
                cleaned = first_clause
        if not 12 <= len(cleaned) <= max_chars:
            continue
        if not _contains_chinese_text(cleaned, 4):
            continue
        if any(marker in cleaned for marker in opening_markers):
            continue
        if _cover_title_has_quality_issue(cleaned):
            continue
        if _cover_title_has_engagement_issue(cleaned):
            continue
        if _cover_title_repeats_form_title(cleaned, form_title):
            continue
        cleaned = _anchor_cover_hook_with_context(
            cleaned,
            form_title=form_title,
            source_title=source_title,
            description=description,
            max_chars=max_chars,
        )
        if _cover_title_has_quality_issue(cleaned):
            continue
        if _cover_title_has_engagement_issue(cleaned):
            continue
        if _cover_title_has_context_issue(
            cleaned,
            form_title=form_title,
            source_title=source_title,
            description=description,
        ):
            continue
        if _cover_title_has_thesis_issue(
            cleaned,
            form_title=form_title,
            source_title=source_title,
            description=description,
        ):
            continue
        score = _subtitle_hook_score(cleaned)
        score += 3.0 * _cover_title_information_units(cleaned)
        score += 3.0 * _cover_title_context_anchor_count(
            cleaned,
            form_title,
            source_title,
            description,
        )
        if any(marker in cleaned for marker in _COVER_TITLE_CONSEQUENCE_MARKERS):
            score += 5.0
        if any(marker in cleaned for marker in _COVER_TITLE_VAGUE_TERMS):
            score -= 10.0
        score += 6.0 * sum(marker in cleaned for marker in scene_markers)
        score += 4.0 * sum(marker in cleaned for marker in conflict_markers)
        if 18 <= len(cleaned) <= 32:
            score += 3.0
        elif 14 <= len(cleaned) <= 36:
            score += 1.0
        if any(marker in cleaned for marker in _GENERIC_TITLE_MARKERS):
            score -= 8.0
        if (
            re.search(
                r"我们.{0,12}(?:识别|建立|采用|上线|救治|整合|优化)了",
                cleaned,
            )
            and any(marker in cleaned for marker in _COVER_TITLE_SUBJECT_MARKERS)
        ):
            score += 5.0
        candidates.append((score, cleaned))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: (item[0], len(item[1])))[1]


def _persist_upload_metadata(workdir, metadata):
    """Write generated upload metadata atomically for review and reuse."""
    if not metadata:
        return
    path = os.path.join(workdir, "upload_metadata.json")
    temp_path = f"{path}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _load_upload_metadata(workdir):
    """Load persisted metadata so retries do not regenerate completed fields."""
    path = os.path.join(workdir, "upload_metadata.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _description_title_fragments(description):
    """Extract conservative title building blocks from a saved summary."""
    value = re.sub(r"\s+", " ", str(description or "")).strip()
    if not value:
        return {}
    first_sentence = re.split(r"[。！？\n]", value, maxsplit=1)[0]
    subject_match = re.search(
        r"(?:专访|对话)\s*([A-Za-z][A-Za-z0-9&.-]*(?:\s+[A-Za-z][A-Za-z0-9&.-]*)?)",
        first_sentence,
    )
    subject = subject_match.group(1).strip() if subject_match else ""
    amount_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*亿美元", value)
    acquisition_match = re.search(
        r"收购(?:保险经纪公司\s*)?([A-Za-z][A-Za-z0-9&.-]*)",
        value,
    )
    compact_value = re.sub(r"\s+", "", value)
    return {
        "subject": subject,
        "short_subject": subject.split()[0] if subject else "",
        "amount": f"{amount_match.group(1)}亿美元" if amount_match else "",
        "acquisition_target": (
            acquisition_match.group(1) if acquisition_match else ""
        ),
        "ai_privatization": "AI私有化" in compact_value,
        "largest_ai_privatization": (
            "AI私有化" in compact_value and "规模最大" in compact_value
        ),
        "engineering_platform": (
            "工程平台" in compact_value or "Atlas" in value
        ),
        "incumbents": "在位企业" in compact_value,
        "ownership": "所有权" in value
        and ("改造" in value or "在位企业" in value),
        "agi_safety": "AGI" in value and "安全" in value,
        "output": "产出" in value,
    }


def _description_fallback_subject(description, prefer_company=False):
    """Extract a conservative interview subject from a saved summary."""
    value = re.sub(r"\s+", " ", str(description or "")).strip()
    if not value:
        return ""
    first_sentence = re.split(r"[。！？\n]", value, maxsplit=1)[0]

    company_match = re.search(
        r"([A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z][A-Za-z0-9_-]*)*)"
        r"\s*(?:联合创始人|创始人|CEO)",
        first_sentence,
    )
    if company_match:
        company = company_match.group(1).strip()
        if prefer_company or "创始人" in first_sentence:
            role = "创始人" if "创始人" in first_sentence else "CEO"
            return f"{company}{role}"

    named_role_match = re.search(
        r"(?:之一|请来)\s*([A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z][A-Za-z0-9_-]*)*)",
        first_sentence,
    )
    if named_role_match:
        return named_role_match.group(1).strip()

    fde_company_match = re.search(
        r"([A-Za-z][A-Za-z0-9_-]*)\s*的\s*FDE",
        first_sentence,
    )
    if fde_company_match:
        return f"{fde_company_match.group(1)} FDE"

    dialog_match = re.search(
        r"(?:对话|对谈|专访|回访)\s*"
        r"([A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z][A-Za-z0-9_-]*)*)",
        first_sentence,
    )
    if dialog_match:
        return dialog_match.group(1).strip()
    return ""


def _interview_public_identity_label(subject, description):
    """Use a stable public identity when the audience knows the person."""
    subject_value = str(subject or "").strip().casefold()
    description_value = str(description or "").casefold()
    for person, label in _INTERVIEW_PUBLIC_IDENTITY_LABELS.items():
        person_value = str(person or "").strip().casefold()
        if (
            person_value == subject_value
            or (
                person_value in description_value
                and subject_value in person_value
            )
        ):
            return label
    return ""


def _fallback_interview_bili_title_from_description(description, max_chars):
    """Write a readable person-interview summary, not a keyword list."""
    value = re.sub(r"\s+", " ", str(description or "")).strip()
    if not value or not _is_interview_title_context(value):
        return ""

    subject = ""
    age = ""
    age_subject_match = re.search(
        r"([0-9]{2})岁的\s*([A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z][A-Za-z0-9_-]*)*)"
        r"\s*(?:敞开谈|谈|聊|回忆)",
        value,
    )
    if age_subject_match:
        age = f"{age_subject_match.group(1)}岁"
        subject = age_subject_match.group(2).strip()
    else:
        dialog_match = re.search(
            r"(?:对话|专访|访谈)\s*"
            r"([A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z][A-Za-z0-9_-]*)*)",
            value,
        )
        if dialog_match:
            subject = dialog_match.group(1).strip()
    raw_subject = subject
    subject = _apply_preferred_title_aliases(subject, max_chars)
    if not subject:
        return ""

    compact_value = re.sub(r"\s+", "", value)
    judgment = ""
    if "中年" in compact_value:
        judgment = "中年感悟"
    elif "人生" in compact_value:
        judgment = "人生感悟"
    elif "复杂的一年" in compact_value:
        judgment = "复杂一年"
    if not judgment:
        return ""

    identity = _interview_public_identity_label(raw_subject, value)
    if identity:
        candidate = f"{subject}访谈：{identity}的{judgment}"
    else:
        candidate = f"{subject}访谈：{judgment}"
        if len(candidate) < 12 and age:
            candidate = f"{subject}访谈：{age}的{judgment}"
        elif len(candidate) < 12 and "复杂的一年" in compact_value:
            candidate = f"{subject}访谈：复杂一年的{judgment}"
    return normalize_bili_upload_title(candidate, max_chars=max_chars)


def _description_cover_subject(source_title, description):
    """Find a Latin subject shared by the publisher title and summary."""
    latin_token_pattern = (
        r"(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9_-]*(?![A-Za-z0-9_-])"
    )
    source_tokens = re.findall(
        latin_token_pattern,
        str(source_title or ""),
    )
    description_tokens = {
        token.casefold()
        for token in re.findall(
            latin_token_pattern,
            str(description or ""),
        )
    }
    for token in source_tokens:
        normalized = token.casefold()
        if (
            len(normalized) >= 3
            and normalized not in _TITLE_LATIN_STOPWORDS
            and normalized in description_tokens
        ):
            return token
    return ""


def _fallback_bili_title_from_subtitle(
    subtitle_text,
    source_title=None,
    max_chars=48,
):
    """Choose a Chinese summary sentence when description extraction fails."""
    summary_markers = (
        "主题", "核心", "聚焦", "关键", "本质", "标准", "实际", "落地",
    )
    weak_markers = (
        "大家好", "欢迎", "谢谢", "接下来", "今天我们", "我觉", "我想",
        "你知道吗", "举个例子",
    )
    candidates = []
    for cleaned in _subtitle_sentence_candidates(subtitle_text):
        cleaned = re.sub(
            r"^(?:我是说|我是想说|我的意思是)[，,：: ]+",
            "",
            cleaned,
        )
        cleaned = cleaned.replace("的主题就是", "的主题是")
        if not 12 <= len(cleaned) <= max_chars:
            continue
        if not _contains_chinese_text(cleaned, 4):
            continue
        if any(marker in cleaned for marker in weak_markers):
            continue
        if re.search(r"[?？]$", cleaned):
            continue
        title = normalize_bili_upload_title(cleaned, max_chars=max_chars)
        if not title or _metadata_title_is_source_copy(title, source_title):
            continue
        score = 0.0
        score += 5.0 * sum(marker in title for marker in summary_markers)
        score += 3.0 * _title_reference_coverage(title, source_title)
        if 24 <= len(title) <= 40:
            score += 3.0
        if title.startswith(("我", "我们")):
            score -= 6.0
        if any(marker in title for marker in _GENERIC_TITLE_MARKERS):
            score -= 5.0
        candidates.append((score, title))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: (item[0], len(item[1])))[1]


def _fallback_bili_title_from_description(description, max_chars):
    """Build a conservative shared title when title-model calls time out."""
    raw_value = re.sub(r"\s+", " ", str(description or "")).strip()
    value = _apply_preferred_title_aliases(raw_value, max_chars)
    if not value:
        return ""
    if not _contains_chinese_text(value, 8):
        return ""

    interview_title = _fallback_interview_bili_title_from_description(
        raw_value,
        max_chars,
    )
    if interview_title:
        return interview_title

    fragments = _description_title_fragments(value)
    candidates = []
    if fragments.get("subject") and fragments.get("ai_privatization"):
        mainline_title = f"{fragments['subject']}谈AI私有化"
        if fragments.get("ownership"):
            method = "用所有权"
            if fragments.get("engineering_platform"):
                method += "和工程平台"
            if fragments.get("incumbents"):
                mainline_title += f"：{method}改造在位企业"
            else:
                mainline_title += f"：{method}改造企业"
        candidates.append(mainline_title)
    if fragments.get("subject") and fragments.get("ownership"):
        ownership_title = f"{fragments['subject']}：用所有权改造在位企业"
        if fragments.get("amount"):
            ownership_title = (
                f"{fragments['subject']}：{fragments['amount']}收购后，"
                "用所有权改造在位企业"
            )
        candidates.append(ownership_title)
    if fragments.get("subject") and fragments.get("agi_safety"):
        safety_title = f"{fragments['subject']}研究员谈AGI安全"
        if fragments.get("output"):
            safety_title = f"{fragments['subject']}研究员谈AGI安全与产出"
        candidates.append(safety_title)

    compact_value = re.sub(r"\s+", "", value)
    subject = _description_fallback_subject(value)
    if subject and "FDE" in compact_value and "全程负责" in compact_value:
        candidates.append(f"{subject}谈FDE从发掘客户到部署全程负责")
    if subject and "前线传感器" in compact_value:
        candidates.append(f"{subject}谈FDE做客户组织前线传感器")
    if subject and "复合技能" in compact_value and "担责" in compact_value:
        candidates.append(f"{subject}谈FDE复合技能与担责")
    if (
        subject
        and "测试评估" in compact_value
        and ("智能体" in compact_value or "打磨系统" in compact_value)
    ):
        candidates.append(f"{subject} FDE用测试评估打磨企业智能体")
    for candidate in candidates:
        normalized = normalize_bili_upload_title(candidate, max_chars=max_chars)
        if normalized:
            return normalized

    first_sentence = re.split(r"[。！？\n]", value, maxsplit=1)[0]
    if "教你用好" in first_sentence:
        subject, object_value = first_sentence.split("教你用好", 1)
        teaching_title = f"{subject}讲透{object_value}"
        teaching_title = normalize_bili_upload_title(
            teaching_title,
            max_chars=max_chars,
        )
        if teaching_title:
            return teaching_title

    panel_match = re.search(r"^(?:本期视频为|这是)(.{2,32})，聚焦(.{4,28})", first_sentence)
    if panel_match:
        subject = panel_match.group(1).strip()
        subject = subject.replace("现场", "").replace("合集", "")
        focus = panel_match.group(2).strip().replace("的", "")
        panel_title = re.sub(r"\s+", " ", f"{subject}聚焦{focus}").strip()
        panel_title = re.sub(
            r"(?<=[\u3400-\u9fff])\s+(?=[A-Za-z0-9])"
            r"|(?<=[A-Za-z0-9])\s+(?=[\u3400-\u9fff])",
            "",
            panel_title,
        )
        if len(panel_title) <= max_chars:
            panel_title = normalize_bili_upload_title(
                panel_title,
                max_chars=max_chars,
            )
            if panel_title:
                return panel_title

    subject_match = re.search(r"((?:[A-Za-z][A-Za-z0-9'&.-]*\s*)?员工)", value)
    if subject_match and "：" in value:
        before_colon, after_colon = value.split("：", 1)
        if "被" in before_colon:
            hook = re.split(r"[。！？\n]", before_colon)[-1]
        else:
            hook = after_colon
        hook = re.split(r"[。！？\n]", hook, maxsplit=1)[0]
        hook = re.split(r"[，,；;]", hook, maxsplit=1)[0]
        hook = hook.replace("“", "").replace("”", "")
        value = f"{subject_match.group(1)}谈{hook}"
    else:
        first_sentence = re.split(r"[。！？\n]", value, maxsplit=1)[0]
        if "：" in first_sentence:
            prefix, remainder = first_sentence.split("：", 1)
            first_clause = re.split(r"[，,；;]", remainder, maxsplit=1)[0]
            value = f"{prefix}{first_clause}"
        else:
            value = re.split(r"[，,；;]", first_sentence, maxsplit=1)[0]

    value = value.replace("“", "").replace("”", "")
    value = value.replace("现身说法", "谈")
    if value.startswith("几位 "):
        value = value[len("几位 "):]

    if len(value) > max_chars:
        cut = value.rfind(" ", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        value = value[:cut].rstrip(" ：:，,。.！!？?；;")
    return normalize_bili_upload_title(value, max_chars=max_chars)


def _fallback_wechat_title_from_description(description, max_chars):
    """Build a natural short Panel title from the saved Chinese summary."""
    value = re.sub(r"\s+", " ", str(description or "")).strip()
    value = _apply_preferred_title_aliases(value, max_chars)
    if not value or not _contains_chinese_text(value, 8):
        return ""

    fragments = _description_title_fragments(value)
    candidates = []
    if fragments.get("short_subject") and fragments.get("ownership"):
        candidates.append(f"{fragments['short_subject']}用所有权改造企业")
    if fragments.get("short_subject") and fragments.get("agi_safety"):
        candidates.append(f"{fragments['short_subject']}谈AGI安全")
    for candidate in candidates:
        normalized = normalize_shared_upload_title(
            candidate,
            max_chars=max_chars,
        )
        if normalized:
            return normalized

    first_sentence = re.split(r"[。！？\n]", value, maxsplit=1)[0]
    if "教你用好" in first_sentence:
        subject, object_value = first_sentence.split("教你用好", 1)
        subject_words = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", subject)
        short_subject = subject_words[-1] if subject_words else subject
        compact_object = next(
            (
                marker
                for marker in ("LLM应用", "AI应用", "ChatGPT", "GPT")
                if marker in object_value
            ),
            "",
        )
        if compact_object:
            candidate = f"{short_subject}讲透{compact_object}"
            return normalize_shared_upload_title(candidate, max_chars=max_chars)

    panel_match = re.search(
        r"^(?:本期视频为|这是)(.{2,32})，聚焦(.{4,28})",
        first_sentence,
    )
    if not panel_match:
        return ""

    subject = panel_match.group(1).strip()
    latin_tokens = re.findall(r"[A-Za-z][A-Za-z0-9]*", subject)
    event = latin_tokens[-1] if latin_tokens else subject
    event = event.replace("现场", "").replace("合集", "").strip()
    if not event:
        return ""

    focus = panel_match.group(2).strip()
    if "行业落地" in focus:
        focus = "行业落地"
    else:
        focus = re.split(r"[，,；;、]", focus, maxsplit=1)[0]
        focus = re.sub(r"(?:的|成果|结果)$", "", focus)
    if not focus:
        return ""

    candidate = f"{event}访谈聚焦{focus}"
    if len(candidate) > max_chars:
        candidate = f"{event}聚焦{focus}"
    if len(candidate) > max_chars:
        return ""
    return normalize_shared_upload_title(candidate, max_chars=max_chars)


def _fallback_wechat_title_from_bili_title(bili_title, max_chars):
    """Compress the local Bilibili fallback without another model call."""
    value = re.sub(r"\s+", " ", str(bili_title or "")).strip()
    value = _apply_preferred_title_aliases(value, max_chars)
    compact_value = re.sub(r"\s+", "", value)

    if _is_interview_title_context(value):
        subject = _extract_bili_title_subject(value)
        subject = re.sub(r"^[0-9]{2}岁的", "", subject)
        hook = ""
        if "素材库" in compact_value:
            hook = "人生成素材库"
        elif "人生感悟" in compact_value:
            hook = "人到中年的感悟"
        elif "中年" in compact_value:
            hook = "人到中年的感悟" if "感悟" in compact_value else "人到中年"
        elif "上班" in compact_value:
            hook = "每天上班"
        if subject and hook:
            normalized = normalize_shared_upload_title(
                f"{subject}：{hook}",
                max_chars=max_chars,
            )
            if normalized:
                return normalized

    interview_match = re.match(
        r"^(.{2,32}?)"
        r"(?:在访谈中直言|在专访中直言|在访谈中说|在专访中说|在访谈中指出)"
        r"[:：]?(.+)$",
        value,
    )
    if interview_match:
        subject = interview_match.group(1).strip()
        tail = re.split(
            r"[，,。.；;：:]",
            interview_match.group(2),
            maxsplit=1,
        )[0]
        tail = tail.strip(" ：:，,。.！!？?；;")
        if subject and tail:
            candidate = f"{subject}谈{tail}"
            normalized = normalize_shared_upload_title(
                candidate,
                max_chars=max_chars,
            )
            if normalized:
                return normalized

    generic_candidates = []
    if "全程负责" in compact_value:
        subject = compact_value.split("谈", 1)[0]
        if subject.endswith("创始人"):
            subject = subject[: -len("创始人")]
        generic_candidates.append(f"{subject}谈FDE全程负责")
    if "前线传感器" in compact_value:
        generic_candidates.append("FDE是客户组织传感器")
    if "复合技能" in compact_value:
        generic_candidates.append("FDE为客户做AI担责")
    if "测试评估" in compact_value:
        generic_candidates.append("FDE用测试评估帮客户")
    if "老年用户" in compact_value:
        generic_candidates.append("Cresta为老人做智能体")
    if "新的麦肯锡" in compact_value:
        generic_candidates.append("FDE为客户解决AI交付")
    for candidate in generic_candidates:
        normalized = normalize_shared_upload_title(candidate, max_chars=max_chars)
        if normalized:
            return normalized

    marker = value.find("谈")
    if marker > 0:
        subject = re.sub(r"\s+", "", value[:marker])
        body = re.sub(r"\s+", "", value[marker + len("谈"):])
        body = re.sub(r"一上来就?", "", body)
        parts = re.split(r"(?:即被|就被|被|[，,；;：:])", body, maxsplit=1)
        prefix = parts[0]
        suffix = parts[1] if len(parts) > 1 else ""
        if suffix:
            suffix = re.sub(r"^(?:扔进|丢进|放进)", "", suffix)
            if len(prefix) + len(suffix) <= max_chars - len(subject) - len("谈"):
                body = prefix + suffix
            else:
                body = prefix
        else:
            body = prefix
        remaining = max_chars - len(subject) - len("谈")
        if remaining > 0:
            body = body[:remaining].rstrip("的了着")
            candidate = f"{subject}谈{body}"
            candidate = normalize_shared_upload_title(
                candidate,
                max_chars=max_chars,
            )
            if candidate:
                return candidate

    # Some grounded Bilibili fallbacks are complete spoken sentences. Remove
    # the conversational lead-in and turn the conditional into a claim.
    cleaned = re.sub(
        r"^(?:再说一次|再强调一次|我的意思是|换句话说)[，,]\s*",
        "",
        value,
    )
    cleaned = re.sub(r"^如果从?", "", cleaned)
    cleaned = cleaned.replace("说起，那就是", "是")
    cleaned = cleaned.replace("面临的核心问题", "核心问题")
    cleaned = re.sub(r"\s+", "", cleaned)
    if cleaned and len(cleaned) <= max_chars:
        return normalize_shared_upload_title(cleaned, max_chars=max_chars)
    return ""


def _fallback_wechat_hook_from_bili_title(bili_title, max_chars):
    """Build a short hook from the subject and stance in a Bilibili title."""
    value = _apply_preferred_title_aliases(
        re.sub(r"\s+", " ", str(bili_title or "")).strip(),
        max_chars,
    )
    compact = re.sub(r"\s+", "", value).strip()
    if not compact:
        return ""

    if "：" in value or ":" in value:
        prefix, body = re.split(r"[：:]", value, maxsplit=1)
        subject = _extract_bili_title_subject(value)
        body = re.sub(r"\s+", "", body)
        body = re.split(r"[，,。.；;]", body)[0]
        if subject and body:
            candidate = f"{subject}：{body}"
            normalized = normalize_shared_upload_title(
                candidate,
                max_chars=max_chars,
            )
            if not normalized:
                marker = next(
                    (
                        marker
                        for marker in (
                            _INTERVIEW_LIFE_STAGE_MARKERS
                            + _INTERVIEW_REPRESENTATIVE_HOOK_MARKERS
                        )
                        if marker in body
                    ),
                    "",
                )
                if marker:
                    if marker in ("中年", "50岁", "54岁"):
                        compact_marker = "人到中年"
                    elif marker in ("人生", "苦短"):
                        compact_marker = (
                            "人生苦短" if "苦短" in body else "谈人生"
                        )
                    elif marker == "第三节":
                        compact_marker = "人生到第三节"
                    elif marker == "素材库":
                        compact_marker = "人生成素材库"
                    elif marker == "上班":
                        compact_marker = "每天上班"
                    else:
                        compact_marker = marker
                    candidate = f"{subject}：{compact_marker}"
                normalized = normalize_shared_upload_title(
                    candidate,
                    max_chars=max_chars,
                )
                if normalized:
                    return normalized

    subject_match = re.search(
        r"(?<![A-Za-z0-9_-])("
        r"[A-Z][A-Za-z0-9_-]{2,}(?:\s+[A-Z][A-Za-z0-9_-]{2,})*"
        r")(?![A-Za-z0-9_-])",
        value,
    )
    if subject_match:
        subject = subject_match.group(1)
    else:
        subject = _extract_bili_title_subject(compact)
    if not subject:
        return ""

    clauses = re.split(r"[，,。.；;：:、]", compact)
    anchor_clause = next((clause for clause in clauses if subject in clause), "")
    if not anchor_clause:
        return ""
    tail = anchor_clause.split(subject, 1)[1]
    tail = re.sub(r"^(?:干脆|直接|选择|决定|开始|转而)", "", tail)
    tail = re.split(
        r"(?:其|并且|而且|以及|同时|却|但|不过)",
        tail,
        maxsplit=1,
    )[0]
    tail = re.sub(r"^(?:就是|真的|确实)", "", tail)

    candidates = []
    if "培养" in tail:
        object_value = "人才" if "人才" in tail else "学生"
        stance = ""
        if "名校" in compact and any(
            marker in compact
            for marker in ("不再只盯着", "不盯", "不看", "打破")
        ):
            stance = "不看名校"
        candidates.append(f"{subject}培养{object_value}{stance}")
        candidates.append(f"{subject}自己培养{object_value}")
    if tail:
        candidates.append(f"{subject}{tail}")

    for candidate in candidates:
        if len(candidate) > max_chars:
            continue
        normalized = normalize_shared_upload_title(
            candidate,
            max_chars=max_chars,
        )
        if normalized:
            return normalized
    return ""


def _fallback_cover_title_from_description(
    description,
    form_title="",
    max_chars=48,
    source_title="",
):
    """Turn a saved teaching summary and its metaphor into a cover headline."""
    value = re.sub(r"\s+", " ", str(description or "")).strip()
    if not value or not _contains_chinese_text(value, 8):
        return ""

    interview_hook = _fallback_interview_cover_title_from_description(
        value,
        form_title=form_title,
        source_title=source_title,
        max_chars=max_chars,
    )
    if interview_hook:
        return interview_hook

    fragments = _description_title_fragments(value)
    if (
        fragments.get("largest_ai_privatization")
        and fragments.get("amount")
        and fragments.get("acquisition_target")
    ):
        candidate = (
            f"AI私有化最大交易：{fragments['amount']}"
            f"收购{fragments['acquisition_target']}"
        )
        if (
            12 <= len(candidate) <= max_chars
            and not _cover_title_repeats_form_title(candidate, form_title)
            and not _cover_title_has_quality_issue(candidate)
            and not _cover_title_has_engagement_issue(candidate)
        ):
            return candidate

    compact_value = re.sub(r"\s+", "", value)
    generic_candidates = []
    if "FDE" in compact_value and "追责" in compact_value:
        generic_candidates.append("FDE解决企业客户追责问题")
    if "评测" in compact_value and "智能体" in compact_value:
        anchor = _description_cover_subject(source_title, value)
        if anchor and anchor != "FDE":
            generic_candidates.append(f"{anchor}用评测解决智能体问题")
        else:
            generic_candidates.append("FDE用测试评估解决智能体问题")
    if "不担责" in compact_value or "担不了责任" in compact_value:
        generic_candidates.append("AI不担责，FDE解决责任")
    if "交付的不是PPT" in compact_value and "智能体" in compact_value:
        generic_candidates.append("FDE用智能体解决客户交付")
    for candidate in generic_candidates:
        if not 12 <= len(candidate) <= max_chars:
            continue
        if _cover_title_repeats_form_title(candidate, form_title):
            continue
        if (
            _cover_title_has_quality_issue(candidate)
            or _cover_title_has_engagement_issue(candidate)
        ):
            continue
        return candidate

    first_sentence = re.split(r"[。！？\n]", value, maxsplit=1)[0]
    if "教你用好" not in first_sentence:
        return ""
    subject, _ = first_sentence.split("教你用好", 1)
    subject = subject.strip()
    if not subject:
        return ""

    for sentence in re.split(r"[。！？\n]", value):
        metaphor_match = re.search(
            r"把(.{1,16}?)讲成[“\"]([^”\"]{2,24})[”\"]",
            sentence,
        )
        if not metaphor_match:
            continue
        object_value = metaphor_match.group(1)
        metaphor = metaphor_match.group(2)
        candidate = f"{subject}用{metaphor}讲{object_value}"
        if not 12 <= len(candidate) <= max_chars:
            continue
        if _cover_title_repeats_form_title(candidate, form_title):
            continue
        if (
            _cover_title_has_quality_issue(candidate)
            or _cover_title_has_engagement_issue(candidate)
        ):
            continue
        return candidate
    return ""


def _fallback_cover_title(meta):
    """Reuse the shortest validated shared title as the cover headline."""
    for key in ("wechat_title", "title"):
        value = re.sub(r"\s+", " ", str(meta.get(key) or "")).strip()
        if 12 <= len(value) <= 48 and not _cover_title_has_quality_issue(value):
            return value
    return ""


def generate_upload_metadata(
    workdir,
    on_log=None,
    bili_title_max_chars=BILI_UPLOAD_TITLE_MAX_CHARS,
    wechat_title_max_chars=SHARED_UPLOAD_TITLE_MAX_CHARS,
    llm_model=None,
    llm_provider=None,
    include_wechat=True,
    source_title=None,
):
    """Generate Bilibili/WeChat form titles and shared upload metadata.

    Returns ``{title, wechat_title, cover_title, description}`` or ``None``.
    ``title`` is the fuller Bilibili title; ``wechat_title`` is derived from it.
    """
    _log = on_log or (lambda msg: print(msg, flush=True))
    try:
        from subtitle_title import (
            read_full_subtitle_text,
        )
    except Exception as e:
        _log(f"⚠️ [MetaGen] subtitle_title import failed: {e}")
        return None
    try:
        from llm_utils import get_client as _get_llm_client, LLMProvider
        llm_client = _get_llm_client()
    except Exception:
        llm_client = None
    default_model, default_provider = _load_llm_defaults()
    model = str(llm_model or default_model or "glm-5.2")
    provider_name = str(llm_provider or default_provider or "dashscope").strip().lower()
    provider = None
    if llm_client is not None:
        try:
            provider = LLMProvider(provider_name)
        except ValueError:
            provider = None
    metadata_models = _metadata_model_candidates(model, provider, llm_client)
    preferred_model = model
    hardsub_path = find_upload_hardsub_video(workdir)
    if not hardsub_path:
        _log("⚠️ [MetaGen] No hardsub video found")
        return None
    meta = _load_upload_metadata(workdir)
    sub_text = None
    full_sub_text = None
    explicit_source_title = re.sub(
        r"\s+",
        " ",
        str(source_title or ""),
    ).strip()
    if not _is_usable_source_title(explicit_source_title):
        explicit_source_title = ""
    source_title = explicit_source_title or _extract_source_title(hardsub_path)
    if source_title:
        meta["source_title"] = source_title

    try:
        full_sub_text, _full_sub_path = read_full_subtitle_text(hardsub_path)
        full_sub_text = _correct_known_asr_proper_nouns(full_sub_text)
        sub_text = _extract_chinese_subtitle_lines(full_sub_text)
    except Exception as e:
        _log(f"⚠️ [MetaGen] Full subtitle read failed: {e}")

    invalid_cached_fields = _invalidate_cached_upload_metadata(
        meta,
        source_title,
        sub_text,
    )
    if invalid_cached_fields:
        _log(
            "⚠️ [MetaGen] 缓存元数据质量不足，重新生成："
            + "、".join(invalid_cached_fields)
        )

    required_fields = ["title", "description", "cover_title"]
    if include_wechat:
        required_fields.append("wechat_title")
    if all(str(meta.get(field) or "").strip() for field in required_fields):
        return meta

    # Generate the description first. The form title is a compressed version of
    # this summary, so sharing its first sentence keeps the title tied to the
    # whole video instead of one striking transcript fragment.
    if not meta.get("description") and sub_text and llm_client:
        try:
            prompt = (
                "根据以下视频字幕，写一段100-200字的视频简介。简洁有力，概括核心内容，不要AI味。\n\n"
                "视频原标题只作题材和场景参考，不是唯一事实来源。"
                "简介应先判断字幕实际主线，再吸收原标题中的场景或受益者。"
                f"\n视频原标题：\n{source_title}\n\n"
                "所有数字、金额、时间和公司关系必须逐字来自字幕；"
                "不得换算数量级，也不得新增字幕没有的事实。\n"
                "英文人名、公司名、产品名必须逐字使用字幕或视频文件名中的拼写，"
                "不得改拼、音译回英文或使用相近姓氏。\n\n"
                f"{sub_text}\n\n只输出简介内容。"
            )
            for _attempt in range(2):
                desc = _generate_content_with_timeout(
                    llm_client,
                    prompt,
                    model,
                    provider=provider,
                    request_timeout=90,
                )
                desc = re.sub(r"\s+", " ", str(desc or "")).strip()
                issues = _metadata_text_issues(desc, full_sub_text, hardsub_path)
                if not issues:
                    meta["description"] = desc
                    _log(f">>> [MetaGen] 简介: {desc[:60]}...")
                    break
                _log(f"⚠️ [MetaGen] Description rejected: {'；'.join(issues)}")
                prompt = (
                    "上一轮简介未通过专有名词溯源校验："
                    f"{'；'.join(issues)}。\n"
                    "请保留原概括，只修正这些英文专名；"
                    "无法确认的英文专名改用中文字幕中的既有译名。\n\n"
                    f"{prompt}"
                )
        except Exception as e:
            _log(f"⚠️ [MetaGen] Description generation failed: {e}")

    # The whole-video description is the title source. If every model candidate
    # fails, use a conservative local summary only when it passes the same gate.
    if not meta.get("title") and meta.get("description") and sub_text and llm_client:
        for candidate_model in metadata_models:
            try:
                summary_title = _generate_title_from_description(
                    meta["description"],
                    sub_text,
                    full_sub_text,
                    llm_client,
                    candidate_model,
                    bili_title_max_chars,
                    provider,
                    source_title=source_title,
                )
                if summary_title and _metadata_title_has_quality_issue(
                    summary_title,
                    source_title,
                    bool(sub_text),
                    meta["description"],
                ):
                    _log(
                        "⚠️ [MetaGen] Bilibili title rejected "
                        f"({candidate_model}): {summary_title}"
                    )
                    continue
                if summary_title:
                    meta["title"] = summary_title
                    preferred_model = candidate_model
                    _log(f">>> [MetaGen] B站标题: {summary_title}")
                    break
            except Exception as e:
                _log(
                    "⚠️ [MetaGen] Bilibili title generation failed "
                    f"({candidate_model}): {e}"
                )

    if not meta.get("title"):
        local_title = _fallback_bili_title_from_description(
            meta.get("description"),
            bili_title_max_chars,
        )
        if local_title and _metadata_title_has_quality_issue(
            local_title,
            source_title,
            bool(sub_text),
            meta.get("description"),
        ):
            _log(f"⚠️ [MetaGen] Bilibili local fallback rejected: {local_title}")
        elif local_title:
            meta["title"] = local_title
            _log(f">>> [MetaGen] B站标题（本地兜底）: {local_title}")
        else:
            _log("⚠️ [MetaGen] Bilibili title generation failed; no local fallback")

    # 2. WeChat form title, derived from the Bilibili title rather than
    # regenerating a separate theme under a tighter character budget.
    if include_wechat and (
        not meta.get("wechat_title")
        and meta.get("title")
        and meta.get("description")
        and sub_text
        and llm_client
    ):
        wechat_models = _metadata_model_candidates(
            preferred_model,
            provider,
            llm_client,
        )
        for candidate_model in wechat_models:
            try:
                wechat_title = _derive_wechat_title_from_bili_title(
                    meta["title"],
                    meta["description"],
                    sub_text,
                    llm_client,
                    candidate_model,
                    max_chars=wechat_title_max_chars,
                    provider=provider,
                    source_title=source_title,
                )
                if wechat_title and _metadata_title_has_quality_issue(
                    wechat_title,
                    source_title,
                    bool(sub_text),
                    meta["description"],
                ):
                    _log(
                        "⚠️ [MetaGen] WeChat title rejected "
                        f"({candidate_model}): {wechat_title}"
                    )
                    continue
                if wechat_title:
                    meta["wechat_title"] = wechat_title
                    preferred_model = candidate_model
                    _log(f">>> [MetaGen] 视频号标题: {wechat_title}")
                    break
            except Exception as e:
                _log(
                    "⚠️ [MetaGen] WeChat title generation failed "
                    f"({candidate_model}): {e}"
                )

    if include_wechat and not meta.get("wechat_title"):
        local_wechat_title = _fallback_wechat_title_from_bili_title(
            meta.get("title"),
            wechat_title_max_chars,
        )
        if not local_wechat_title:
            local_wechat_title = _fallback_wechat_hook_from_bili_title(
                meta.get("title"),
                wechat_title_max_chars,
            )
        if not local_wechat_title:
            local_wechat_title = _fallback_wechat_title_from_description(
                meta.get("description"),
                wechat_title_max_chars,
            )
        if not local_wechat_title:
            local_wechat_title = _fallback_wechat_title_from_subtitle(
                sub_text,
                max_chars=wechat_title_max_chars,
                form_title=meta.get("title"),
            )
        if local_wechat_title and _metadata_title_has_quality_issue(
            local_wechat_title,
            source_title,
            bool(sub_text),
            meta.get("description"),
        ):
            _log(
                "⚠️ [MetaGen] WeChat local fallback rejected: "
                f"{local_wechat_title}"
            )
        elif local_wechat_title:
            meta["wechat_title"] = local_wechat_title
            _log(f">>> [MetaGen] 视频号标题（本地兜底）: {local_wechat_title}")
        else:
            _log("⚠️ [MetaGen] WeChat title generation failed; no local fallback")

    # 3. Cover title (editorial headline style)
    if not meta.get("cover_title") and sub_text and llm_client:
        cover_models = _metadata_model_candidates(
            preferred_model,
            provider,
            llm_client,
        )
        for candidate_model in cover_models:
            try:
                cover_title = _generate_cover_title_from_full_subtitle(
                    sub_text,
                    llm_client,
                    candidate_model,
                    min_chars=12,
                    max_chars=48,
                    provider=provider,
                    form_title=meta.get("title"),
                    source_title=source_title,
                    description=meta.get("description"),
                )
                if cover_title:
                    if _metadata_cover_title_has_quality_issue(
                        cover_title,
                        meta.get("title"),
                        meta.get("wechat_title"),
                        source_title,
                        meta.get("description"),
                        bool(sub_text),
                    ):
                        _log(
                            "⚠️ [MetaGen] Cover title rejected "
                            f"({candidate_model}): {cover_title}"
                        )
                        continue
                    meta["cover_title"] = cover_title
                    preferred_model = candidate_model
                    _log(f">>> [MetaGen] 封面标题: {cover_title}")
                    break
            except Exception as e:
                _log(
                    "⚠️ [MetaGen] Cover title generation failed "
                    f"({candidate_model}): {e}"
                )

    if not meta.get("cover_title"):
        local_cover_title = _fallback_cover_title_from_subtitle(
            sub_text,
            form_title=meta.get("title"),
            description=meta.get("description"),
            source_title=source_title,
        )
        if not local_cover_title:
            local_cover_title = _fallback_cover_title_from_description(
                meta.get("description"),
                form_title=meta.get("title"),
                source_title=source_title,
            )
        if local_cover_title and _metadata_cover_title_has_quality_issue(
            local_cover_title,
            meta.get("title"),
            meta.get("wechat_title"),
            source_title,
            meta.get("description"),
            bool(sub_text),
        ):
            _log(f"⚠️ [MetaGen] Cover local fallback rejected: {local_cover_title}")
        elif local_cover_title:
            meta["cover_title"] = local_cover_title
            _log(f">>> [MetaGen] 封面标题（本地兜底）: {local_cover_title}")
        else:
            _log("⚠️ [MetaGen] Cover title generation failed; no local fallback")

    # The publisher title is deliberately the last resort. It is weaker than a
    # transcript-grounded title, but a known-topic upload is more recoverable
    # than an upload permanently blocked by metadata generation.
    if not meta.get("description") and source_title:
        meta["description"] = source_title
        _log(">>> [MetaGen] 简介（原标题兜底）: " + source_title[:60])
    if not meta.get("title") and source_title:
        meta["title"] = _fallback_bili_title_from_source_title(
            source_title,
            bili_title_max_chars,
            description=meta.get("description"),
        )
        _log(f">>> [MetaGen] B站标题（原标题兜底）: {meta['title']}")
    if include_wechat and not meta.get("wechat_title") and source_title:
        meta["wechat_title"] = _fallback_wechat_title_from_source_title(
            source_title,
            wechat_title_max_chars,
        )
        if meta["wechat_title"]:
            _log(f">>> [MetaGen] 视频号标题（原标题兜底）: {meta['wechat_title']}")
    if not meta.get("cover_title") and meta.get("title"):
        meta["cover_title"] = _fallback_bili_title_from_source_title(
            meta["title"],
            48,
        )
        if meta["cover_title"]:
            _log(f">>> [MetaGen] 封面标题（兜底）: {meta['cover_title']}")

    if not meta.get("title"):
        _persist_upload_metadata(workdir, meta)
        return meta
    _persist_upload_metadata(workdir, meta)
    return meta if meta else None


def prepare_shared_upload_metadata(
    workdir,
    explicit_cover_title=None,
    llm_model=None,
    llm_provider=None,
    on_log=None,
    source_title=None,
):
    """Generate and validate the metadata shared by Bilibili and WeChat."""
    _log = on_log or (lambda msg: print(msg, flush=True))
    meta = generate_upload_metadata(
        workdir,
        bili_title_max_chars=BILI_UPLOAD_TITLE_MAX_CHARS,
        wechat_title_max_chars=SHARED_UPLOAD_TITLE_MAX_CHARS,
        llm_model=llm_model,
        llm_provider=llm_provider,
        on_log=on_log,
        source_title=source_title,
    )
    if not meta:
        _log("❌ [MetaGen] shared metadata generation failed; BI/WX uploads stopped")
        return None

    bili_title = normalize_bili_upload_title(
        meta.get("title"),
        max_chars=BILI_UPLOAD_TITLE_HARD_MAX_CHARS,
    )
    wechat_title = normalize_shared_upload_title(
        meta.get("wechat_title"),
        max_chars=SHARED_UPLOAD_TITLE_MAX_CHARS,
    )
    description = str(meta.get("description") or "").strip()
    cover_title = str(
        explicit_cover_title or meta.get("cover_title") or ""
    ).strip()
    if not bili_title or not wechat_title or not description or not cover_title:
        _log(
            "❌ [MetaGen] Bilibili/WeChat title, description, or cover title "
            "incomplete; BI/WX uploads stopped"
        )
        return None

    return {
        "title": bili_title,
        "wechat_title": wechat_title,
        "desc": description,
        "cover_title": cover_title,
    }


def prepare_bili_upload_metadata(
    workdir,
    explicit_cover_title=None,
    llm_model=None,
    llm_provider=None,
    on_log=None,
    source_title=None,
):
    """Generate only the metadata required to publish Bilibili."""
    _log = on_log or (lambda msg: print(msg, flush=True))
    meta = generate_upload_metadata(
        workdir,
        bili_title_max_chars=BILI_UPLOAD_TITLE_MAX_CHARS,
        llm_model=llm_model,
        llm_provider=llm_provider,
        on_log=on_log,
        include_wechat=False,
        source_title=source_title,
    )
    if not meta:
        _log("❌ [MetaGen] Bilibili metadata generation failed; BI upload stopped")
        return None

    bili_title = normalize_bili_upload_title(
        meta.get("title"),
        max_chars=BILI_UPLOAD_TITLE_HARD_MAX_CHARS,
    )
    description = str(meta.get("description") or "").strip()
    cover_title = str(
        explicit_cover_title or meta.get("cover_title") or ""
    ).strip()
    if not bili_title or not description or not cover_title:
        _log(
            "❌ [MetaGen] Bilibili title, description, or cover title "
            "incomplete; BI upload stopped"
        )
        return None

    return {
        "title": bili_title,
        "desc": description,
        "cover_title": cover_title,
    }


def prepare_wechat_upload_metadata(
    workdir,
    explicit_cover_title=None,
    llm_model=None,
    llm_provider=None,
    on_log=None,
    source_title=None,
):
    """Derive and validate the shorter WeChat form title after Bilibili."""
    _log = on_log or (lambda msg: print(msg, flush=True))
    meta = generate_upload_metadata(
        workdir,
        bili_title_max_chars=BILI_UPLOAD_TITLE_MAX_CHARS,
        wechat_title_max_chars=SHARED_UPLOAD_TITLE_MAX_CHARS,
        llm_model=llm_model,
        llm_provider=llm_provider,
        on_log=on_log,
        include_wechat=True,
        source_title=source_title,
    )
    if not meta:
        _log("❌ [MetaGen] WeChat metadata generation failed; WX upload stopped")
        return None

    wechat_title = normalize_shared_upload_title(
        meta.get("wechat_title"),
        max_chars=SHARED_UPLOAD_TITLE_MAX_CHARS,
    )
    description = str(meta.get("description") or "").strip()
    cover_title = str(
        explicit_cover_title or meta.get("cover_title") or ""
    ).strip()
    if not wechat_title or not description or not cover_title:
        _log(
            "❌ [MetaGen] WeChat title, description, or cover title "
            "incomplete; WX upload stopped"
        )
        return None

    return {
        "wechat_title": wechat_title,
        "desc": description,
        "cover_title": cover_title,
    }


# --- WeChat Channels Upload (canonical implementation; batch engine + GUI reuse this) ---
def build_wechat_title(hardsub_path):
    """Derive a WeChat Channels title, capped at the platform's short-title limit."""
    return sanitize_wechat_title(build_bili_title(hardsub_path))

def get_wechat_duration_skip_reason(duration):
    """WeChat uploads no longer have an application-side duration cap."""
    return None

def get_wechat_upload_skip_reason(workdir):
    """Return a user-facing reason when WeChat upload must not start."""
    if not os.path.exists(WECHAT_SCRIPT):
        return f"WeChat upload script not found: {WECHAT_SCRIPT}"
    if not os.path.exists(WECHAT_PYTHON):
        return f"WeChat upload Python not found: {WECHAT_PYTHON}"
    hardsub_path = find_upload_hardsub_video(workdir)
    if not hardsub_path:
        return "hardsub video not found"
    duration = get_video_duration(hardsub_path)
    if duration <= 0:
        return f"cannot read duration: {os.path.basename(hardsub_path)}"
    return get_wechat_duration_skip_reason(duration)

def build_wechat_cmd(
    workdir,
    title=None,
    desc=None,
    tags=None,
    public=False,
    cover_title=None,
    llm_model=None,
    llm_provider=None,
    require_complete_metadata=False,
):
    """Build the WeChat Channels upload command for the hardsub video in workdir.
    Description/tags are auto-generated by the skill unless explicitly overridden.
    The default action saves a draft; public publishing requires an explicit flag."""
    skip_reason = get_wechat_upload_skip_reason(workdir)
    if skip_reason:
        return None
    metadata_values = (title, desc, cover_title)
    if require_complete_metadata and not all(
        str(value or "").strip() for value in metadata_values
    ):
        return None
    hardsub_path = find_upload_hardsub_video(workdir)
    # Omit the title by default so wechat_upload.py generates it from the
    # subtitle. A caller can still pass a title for an explicit manual override.
    _pre_generated = title is not None and desc is not None
    cmd = [WECHAT_PYTHON, "-u", WECHAT_SCRIPT, hardsub_path]
    if title is not None:
        cmd.append(str(title))
    if desc:
        cmd.extend(["--desc", str(desc)])
    if tags:
        cmd.extend(["--tags", str(tags)])
    if cover_title is not None and str(cover_title).strip():
        cmd.extend(["--cover-title", str(cover_title).strip()])
    if _pre_generated:
        cmd.append("--no_auto_title")
    if llm_model:
        cmd.extend(["--llm-model", str(llm_model)])
    if llm_provider:
        cmd.extend(["--llm-provider", str(llm_provider)])
    if public:
        cmd.append("--public")
    # Login is a separate, visible one-time `--login` flow. Normal uploads use
    # the persistent profile without opening a Chrome window.
    cmd.append("--headless")
    return cmd

def run_wechat_cmd(cmd, on_progress=None, on_log=None, abort_flag=None,
                   on_process_start=None, on_process_end=None,
                   stall_timeout=UPLOAD_STALL_TIMEOUT,
                   silence_timeout=UPLOAD_SILENCE_TIMEOUT):
    """Run a pre-built WeChat Channels upload command. Returns True on success."""
    return _run_upload_cmd(
        cmd,
        "WeChat Channels",
        r'(?:Progress|上传进度):\s*([\d.]+)%',
        on_progress=on_progress,
        on_log=on_log,
        abort_flag=abort_flag,
        on_process_start=on_process_start,
        on_process_end=on_process_end,
        stall_timeout=stall_timeout,
        silence_timeout=silence_timeout,
    )

def trigger_wechat_upload(
    workdir,
    title=None,
    desc=None,
    tags=None,
    public=False,
    cover_title=None,
    llm_model=None,
    llm_provider=None,
    on_progress=None,
    on_log=None,
    abort_flag=None,
):
    """Build and run a WeChat Channels upload for the hardsub video in workdir."""
    _log = on_log or (lambda msg: print(msg, flush=True))
    skip_reason = get_wechat_upload_skip_reason(workdir)
    if skip_reason:
        _log(f"⚠️  [WeChat] upload skipped ({skip_reason})")
        return False
    cmd = build_wechat_cmd(
        workdir,
        title=title,
        desc=desc,
        tags=tags,
        public=public,
        cover_title=cover_title,
        llm_model=llm_model,
        llm_provider=llm_provider,
    )
    if not cmd:
        _log("⚠️  [WeChat] upload skipped (command could not be built)")
        return False
    _log(f"📹 Starting WeChat Channels upload: {os.path.basename(cmd[2])}")
    ok = run_wechat_cmd(cmd, on_progress=on_progress, on_log=_log, abort_flag=abort_flag)
    _log(f"📹 {'✅ Success' if ok else '❌ Failed'}")
    return ok


def _write_upload_marker(workdir, marker_name):
    try:
        with open(os.path.join(workdir, marker_name), "w", encoding="utf-8") as handle:
            handle.write("1")
    except Exception:
        pass


def run_post_processing_uploads(
    workdir,
    sync_gdrive=None,
    bili_upload=False,
    wechat_upload=False,
    headless=False,
    bili_cover_title=None,
    wechat_cover_title=None,
    wechat_public=False,
    llm_model=None,
    llm_provider=None,
):
    """Run GD/BI/WX uploads with platform-specific metadata preflight."""
    need_sync = bool(sync_gdrive) and not os.path.exists(os.path.join(workdir, ".synced"))
    need_bili = bool(bili_upload) and not os.path.exists(os.path.join(workdir, ".bili_uploaded"))
    need_wechat = bool(wechat_upload) and not os.path.exists(os.path.join(workdir, ".wechat_uploaded"))
    if not any((need_sync, need_bili, need_wechat)):
        return

    bili_kwargs = {}
    wechat_kwargs = {"public": wechat_public}
    if llm_model:
        bili_kwargs["llm_model"] = llm_model
        wechat_kwargs["llm_model"] = llm_model
    if llm_provider:
        bili_kwargs["llm_provider"] = llm_provider
        wechat_kwargs["llm_provider"] = llm_provider

    import threading
    sync_result = [False]
    bili_result = [False]
    wechat_result = [False]
    threads = []

    if need_sync:
        def _run_sync():
            sync_result[0] = trigger_sync(workdir, sync_gdrive, headless=headless) is True
        sync_thread = threading.Thread(target=_run_sync, daemon=True)
        threads.append(sync_thread)
        sync_thread.start()

    if need_bili:
        bili_metadata = prepare_bili_upload_metadata(
            workdir,
            explicit_cover_title=bili_cover_title,
            llm_model=llm_model,
            llm_provider=llm_provider,
        )
        if not bili_metadata:
            need_bili = False
        else:
            bili_kwargs.update({
                "title": bili_metadata["title"],
                "desc": bili_metadata["desc"],
                "cover_title": bili_metadata["cover_title"],
            })

            def _run_bili():
                bili_result[0] = trigger_bili_upload(workdir, **bili_kwargs)

            bili_thread = threading.Thread(target=_run_bili, daemon=True)
            threads.append(bili_thread)
            bili_thread.start()

    if need_wechat:
        wechat_metadata = prepare_wechat_upload_metadata(
            workdir,
            explicit_cover_title=wechat_cover_title,
            llm_model=llm_model,
            llm_provider=llm_provider,
        )
        if not wechat_metadata:
            need_wechat = False
        else:
            wechat_kwargs.update({
                "title": wechat_metadata["wechat_title"],
                "desc": wechat_metadata["desc"],
                "cover_title": wechat_metadata["cover_title"],
            })

            def _run_wechat():
                wechat_result[0] = trigger_wechat_upload(workdir, **wechat_kwargs)

            wechat_thread = threading.Thread(target=_run_wechat, daemon=True)
            threads.append(wechat_thread)
            wechat_thread.start()

    for thread in threads:
        thread.join()

    if sync_result[0]:
        _write_upload_marker(workdir, ".synced")
    if bili_result[0]:
        _write_upload_marker(workdir, ".bili_uploaded")
    if wechat_result[0]:
        _write_upload_marker(workdir, ".wechat_uploaded")


# --- Robust FFmpeg/ffprobe Detection ---
def find_tool(tool_name):
    """Finds a tool in PATH or common installation directories."""
    import shutil
    # 0. Check bundled internal path (if frozen)
    if getattr(sys, 'frozen', False):
        bundle_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        exe_ext = ".exe" if sys.platform == "win32" else ""
        internal_tool = os.path.join(bundle_dir, tool_name + exe_ext)
        if os.path.exists(internal_tool): return internal_tool
        # Also check root of exe
        root_tool = os.path.join(os.path.dirname(sys.executable), tool_name + exe_ext)
        if os.path.exists(root_tool): return root_tool

    # 1. Check PATH
    path = shutil.which(tool_name)
    if path: return path
    
    # 2. Check WinGet Gyan FFmpeg (User Specific)
    user_home = os.path.expanduser("~")
    winget_base = os.path.join(user_home, "AppData", "Local", "Microsoft", "Winget", "Packages")
    if os.path.exists(winget_base):
        # Search for Gyan FFmpeg
        for d in os.listdir(winget_base):
            if "Gyan.FFmpeg" in d:
                # Glob search for the bin folder
                for bin_dir in glob.glob(os.path.join(winget_base, d, "**/bin"), recursive=True):
                    exe_ext = ".exe" if sys.platform == "win32" else ""
                    tool_path = os.path.join(bin_dir, tool_name + exe_ext)
                    if os.path.exists(tool_path): return tool_path

    # 3. Check common hardcoded paths
    fallbacks = [
        r"C:\ffmpeg\bin",
        r"C:\Program Files\ffmpeg\bin",
    ]
    if sys.platform == "darwin":
        fallbacks.extend([
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/opt/local/bin",
        ])
    
    # Proactively check for CapCut installation to steal its FFmpeg
    # Better: check for "CapCut.exe" path and then look for ffmpeg nearby
    for drive in ["C:", "D:"]:
        capcut_root = os.path.join(drive, "Program Files", "CapCut")
        if os.path.exists(capcut_root):
            # Find latest version folder
            try:
                versions = [d for d in os.listdir(capcut_root) if os.path.isdir(os.path.join(capcut_root, d)) and "." in d]
                if versions:
                    latest = sorted(versions, key=lambda x: [int(v) for v in x.split('.') if v.isdigit()], reverse=True)[0]
                    fallbacks.append(os.path.join(capcut_root, latest))
            except: pass

    for fb in fallbacks:
        exe_ext = ".exe" if sys.platform == "win32" else ""
        tool_path = os.path.join(fb, tool_name + exe_ext)
        if os.path.exists(tool_path): return tool_path
        
    return tool_name # Fallback to original name and hope for the best

FFMPEG_EXE = find_tool("ffmpeg")
FFPROBE_EXE = find_tool("ffprobe")

if FFMPEG_EXE != "ffmpeg":
    print(f"📦 Found FFmpeg at: {FFMPEG_EXE}")
    # Add to path for sub-scripts
    os.environ["PATH"] += os.pathsep + os.path.dirname(FFMPEG_EXE)

VDOWN_CMD = [sys.executable, os.path.join(TOOLS_DIR, "vdown", "download.py")]
TRANSCRIBER_CMD = [sys.executable, os.path.join(TOOLS_DIR, "transcriber", "transcribe_engine.py")]
SMART_TRANSLATE_CMD = [sys.executable, os.path.join(TOOLS_DIR, "autosub", "smart_translate.py")]
SUBTRANSLATOR_CMD = [sys.executable, os.path.join(TOOLS_DIR, "subtranslator", "subtranslator.py")]
SRT2ASS_CMD = [sys.executable, os.path.join(TOOLS_DIR, "hardsubber", "srt_to_ass.py")]
BURNSUB_CMD = [sys.executable, os.path.join(TOOLS_DIR, "hardsubber", "burn_engine.py")]

if os.path.exists(env_path):
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f :
                if '=' in line and not line.strip().startswith('#'):
                    k, v = line.strip().split('=', 1)
                    if k and v: os.environ[k] = v.strip('"\'')
    except: pass

def get_workdir(input_val, base_output_dir):
    if input_val.startswith("http"):
        return os.path.join(base_output_dir, "temp_" + str(int(time.time())))
    else:
        abs_path = os.path.abspath(input_val)
        try:
            base = os.path.normpath(base_output_dir).lower()
            current = os.path.normpath(abs_path).lower()
            if current.startswith(base): return os.path.dirname(abs_path)
        except: pass
        return os.path.join(base_output_dir, os.path.splitext(os.path.basename(input_val))[0])

def get_video_title(url, cookies=None):
    """Fetches video title using download.py --get-title."""
    try:
        # Use --print title for less noisy output
        cmd = list(VDOWN_CMD) + [url, cookies or "", "", "--get-title"]
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            encoding='utf-8', 
            errors='replace',
            creationflags=CREATE_NO_WINDOW,
            timeout=40
        )
        if result.returncode == 0:
            title = result.stdout.strip()
            # If multiple lines, take the last non-empty one
            lines = [l.strip() for l in title.splitlines() if l.strip()]
            if lines:
                title = lines[-1]
                if "Error" not in title: return title
    except: pass
    return None

def get_video_folder_name(url, cookies=None):
    """Fetches stable folder name using download.py --get-folder-name."""
    try:
        cmd = list(VDOWN_CMD) + [url, cookies or "", "", "--get-folder-name"]
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            encoding='utf-8', 
            errors='replace',
            creationflags=CREATE_NO_WINDOW,
            timeout=40
        )
        if result.returncode == 0:
            folder_name = result.stdout.strip()
            # If multiple lines, take the last non-empty one
            lines = [l.strip() for l in folder_name.splitlines() if l.strip()]
            if lines:
                folder_name = lines[-1]
                if "Error" not in folder_name: return folder_name
    except: pass
    return None

def sanitize_filename(filename):
    """Cleans a string to be a safe filename."""
    clean = re.sub(r'[\\/*?:"<>|]', '_', filename)
    clean = clean.strip().strip('.') # Strip trailing spaces and dots
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean[:100] # Limit length

def rename_workdir_safely(old_workdir, new_workdir, current_video_path, logger_instance):
    """Safely closes the logger, renames the directory, updates paths, and reopens the logger."""
    import sys
    
    # 1. Close current logger to release file handles
    if logger_instance:
        try:
            # Restore stdout/stderr first
            sys.stdout = logger_instance.terminal
            sys.stderr = logger_instance.terminal
            logger_instance.close()
        except Exception as e:
            print(f"⚠️ Error closing logger for rename: {e}")
            
    # 2. Rename directory
    renamed = False
    try:
        if os.path.exists(old_workdir) and not os.path.exists(new_workdir):
            os.rename(old_workdir, new_workdir)
            renamed = True
            print(f"✅ Project folder renamed to: {os.path.basename(new_workdir)}")
    except Exception as e:
        print(f"⚠️ Failed to rename project folder: {e}")
        
    if renamed:
        resolved_workdir = new_workdir
        # Update video path
        video_filename = os.path.basename(current_video_path)
        resolved_video_path = os.path.join(new_workdir, video_filename)
    else:
        resolved_workdir = old_workdir
        resolved_video_path = current_video_path
        
    # 3. Re-initialize logger in the new/old path
    new_log_path = os.path.join(resolved_workdir, "workflow.log")
    try:
        new_logger = Logger(new_log_path)
        sys.stdout = new_logger
        sys.stderr = new_logger
    except Exception as e:
        print(f"⚠️ Failed to restart logger: {e}")
        new_logger = logger_instance # Fallback
        
    return resolved_workdir, resolved_video_path, new_logger

def get_remote_duration(url, cookies=None):
    """Fetch video duration (seconds) via yt-dlp --get-duration."""
    try:
        cmd = list(VDOWN_CMD) + [url, cookies or "", "", "--get-duration"]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW, timeout=40)
        if result.returncode == 0:
            raw = result.stdout.strip()
            # yt-dlp returns seconds for --get-duration (e.g. "2938")
            try:
                return float(raw)
            except ValueError:
                # Fallback: parse "HH:MM:SS" format
                parts = raw.split(':')
                if len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                return 0
    except Exception:
        pass
    return 0

def download_video(url, workdir, cookies=None, quick=False, start_time=None, end_time=None, title=None):
    print(f"🎬 Downloading {url}...", flush=True)
    if not os.path.exists(workdir): os.makedirs(workdir)
    # Skip download if a valid source video already exists in workdir (re-run
    # of the same URL). Verify by comparing local video duration against the
    # remote duration reported by yt-dlp, with a 5% tolerance. _hardsub outputs
    # are never treated as source inputs.
    if not start_time and not end_time:
        existing = [f for f in os.listdir(workdir)
                    if f.endswith(('.mp4', '.mkv', '.webm', '.mov', '.ts'))
                    and not f.endswith('.part')
                    and '_hardsub' not in f]
        existing.sort(key=lambda x: os.path.getsize(os.path.join(workdir, x)), reverse=True)
        for cand in existing:
            candidate = os.path.join(workdir, cand)
            local_dur = get_video_duration(candidate)
            if local_dur <= 0:
                continue
            remote_dur = get_remote_duration(url, cookies)
            if remote_dur > 0 and abs(local_dur - remote_dur) / remote_dur < 0.05:
                print(f"✅ Found existing video matching remote duration ({local_dur:.0f}s vs {remote_dur:.0f}s): {os.path.basename(candidate)} (Skipping download)")
                return candidate
            elif remote_dur <= 0:
                # yt-dlp could not fetch duration — fall back to size check
                # (>10 MB to exclude stubs / partial downloads).
                if os.path.getsize(candidate) > 10 * 1024 * 1024:
                    print(f"✅ Found existing video (remote duration unknown, local {local_dur:.0f}s, {os.path.getsize(candidate)/(1024*1024):.0f}MB): {os.path.basename(candidate)} (Skipping download)")
                    return candidate
    cmd = list(VDOWN_CMD) + [url, cookies or "", workdir]
    if title:
        cmd.extend(["--title", title])
    if quick:
        cmd.append("--write-subs")
    if start_time:
        cmd.extend(["--start", start_time])
    if end_time:
        cmd.extend(["--end", end_time])
    try: 
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, creationflags=CREATE_NO_WINDOW)
        for line in process.stdout or []:
            msg = line.strip()
            if "Progress:" in msg or ("[download]" in msg and "%" in msg):
                sys.stdout.write(f"\r   {msg}    ")
                sys.stdout.flush()
            else:
                print(msg, flush=True)
        process.wait()
        
        # --- Robustness Check with Transparency ---
        # _hardsub outputs are excluded — they are burn results, not sources.
        video_files = [f for f in os.listdir(workdir) if f.endswith(('.mp4', '.mkv', '.webm', '.mov', '.ts')) and not f.endswith('.part') and '_hardsub' not in f]
        if video_files:
            video_files.sort(key=lambda x: os.path.getsize(os.path.join(workdir, x)), reverse=True)
            candidate = os.path.join(workdir, video_files[0])
            if os.path.getsize(candidate) > 1024 * 1024:
                if process.returncode != 0:
                    print(f"⚠️  Download reported error ({process.returncode}), but a valid video file was found.")
                    print(f"   Proceeding with: {os.path.basename(candidate)}")
                return candidate

        if process.returncode != 0:
            print(f"❌ Download command failed with code {process.returncode}")
            return None
    except Exception as e:
        print(f"❌ Error during download: {e}")
        return None
    return None
def transcribe_video(video_path, workdir, model="large-v2", quick=False, multilang=False):
    print(f"🎙️ Transcribing {os.path.basename(video_path)}...", flush=True)
    cmd = list(TRANSCRIBER_CMD) + [video_path, "--model", model, "--output", workdir, "--no-gui"]
    if quick:
        cmd.append("--quick")
    if multilang:
        cmd.append("--multilang")
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, creationflags=CREATE_NO_WINDOW)
        for line in process.stdout or []:
            msg = line.strip()
            if "Progress:" in msg or ("[download]" in msg and "%" in msg):
                sys.stdout.write(f"\r   {msg}    ")
                sys.stdout.flush()
            else:
                print(msg, flush=True)
        process.wait()
        res = os.path.join(workdir, os.path.splitext(os.path.basename(video_path))[0] + ".srt")
        if os.path.exists(res):
            en_res = res.replace(".srt", ".en.srt")
            if not os.path.exists(en_res):
                shutil.copy2(res, en_res)
                print(f"✅ Created copy: {os.path.basename(en_res)}")
            return res
        return None
    except: return None

def merge_bilingual(src_srt, zh_srt, main_lang="cn", llm_model=DEFAULTS.get("llm_model", "glm-5.3")):
    print(f"🔀 Smart-Merging into bilingual SRT...")
    bi_path = src_srt[:-7] + ".bi.srt" if src_srt.lower().endswith(".en.srt") else src_srt.replace(".srt", ".bi.srt")
    
    # Check if a healthy bi_path already exists
    if os.path.exists(bi_path) and os.path.getsize(bi_path) > 100:
        with open(bi_path, 'r', encoding='utf-8', errors='ignore') as f:
            if "[UNTRANSLATED]" not in f.read(): 
                print(f"✅ Reusing existing bilingual file: {os.path.basename(bi_path)}")
                return bi_path
            
    cmd = list(SUBTRANSLATOR_CMD) + ["merge", src_srt, "--translated-file", zh_srt]
    env = os.environ.copy(); env["GEMINI_MODEL"] = llm_model
    
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env=env, creationflags=CREATE_NO_WINDOW)
        for line in process.stdout or []:
            msg = line.strip()
            if "Progress:" in msg:
                sys.stdout.write(f"\r   {msg}    ")
                sys.stdout.flush()
            else:
                print(f"   [Merge] {msg}", flush=True)
        sys.stdout.write("\n")
        process.wait()
        
        if process.returncode == 0 and os.path.exists(bi_path):
            return bi_path
        else:
            print(f"❌ Merge process failed with code {process.returncode}")
            return None
    except Exception as e:
        print(f"❌ Merge launch error: {e}")
        return None

def burn_subtitle(video_path, srt_path, layout, main_lang, cn_font, en_font, ja_font, cn_size, en_size, cn_color, en_color, bg_box=True, quality="high", ja_size=None):
    print("🔥 Burning subtitles...", flush=True)
    base_srt, _ = os.path.splitext(srt_path)
    ass_path = base_srt + ".ass"
     
    # --- Get Video Dimensions for Styling ---
    width, height = get_video_dimensions(video_path)
    print(f"📐 Video Resolution: {width}x{height}")
 
    if not os.path.exists(ass_path):
         cmd = list(SRT2ASS_CMD) + [srt_path, ass_path, "--layout", layout, "--main-lang", main_lang, "--cn-font", cn_font, "--en-font", en_font, "--ja-font", ja_font, "--cn-size", cn_size, "--en-size", en_size, "--cn-color", cn_color, "--en-color", en_color]
         if ja_size: cmd += ["--ja-size", str(ja_size)]
         cmd += ["--width", str(width), "--height", str(height)]
         if not bg_box: cmd.append("--no-bg-box")
         subprocess.run(cmd, check=True, creationflags=CREATE_NO_WINDOW)
        
    video_base = os.path.splitext(os.path.basename(video_path))[0]
    video_ext = os.path.splitext(video_path)[1]
    out_video = os.path.join(os.path.dirname(srt_path), video_base + "_hardsub" + video_ext)
    
    # --- Critical: Prevent File Locking issues ---
    if os.path.exists(out_video):
        try:
            os.remove(out_video)
        except PermissionError:
            print(f"❌ Error: Output file is LOCKED: {os.path.basename(out_video)}")
            print(f"   Please close any video players or run: Stop-Process -Name ffmpeg -Force")
            return None
        except Exception as e:
            print(f"⚠️ Warning: Could not remove existing output: {e}")

    cmd = list(BURNSUB_CMD) + [video_path, ass_path, out_video, "--headless", "--quality", quality]
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
        last_logs = []
        for line in process.stdout or []:
            msg = line.strip()
            if msg:
                if "Progress:" in msg:
                    sys.stdout.write(f"\r   {msg}    ")
                    sys.stdout.flush()
                else:
                    print(f"   [Burn] {msg}", flush=True)
                last_logs.append(msg)
                if len(last_logs) > 50: last_logs.pop(0)
        sys.stdout.write("\n")
        
        process.wait()
        if process.returncode != 0:
            print(f"❌ Burn failed with exit code {process.returncode}")
            # If logs didn't print or were short, show them again
            if not last_logs: print("   No log output captured.")
            raise subprocess.CalledProcessError(process.returncode, cmd)
        else:
            try:
                marker_path = os.path.join(os.path.dirname(out_video), ".burn_complete")
                with open(marker_path, "w", encoding="utf-8") as f:
                    f.write("1")
            except:
                pass
            
    except Exception as e:
        if isinstance(e, subprocess.CalledProcessError): raise e
        print(f"❌ Launch Error: {e}")
        return None
    return out_video if os.path.exists(out_video) else None

def get_video_duration(path):
    try:
        cmd = [FFPROBE_EXE, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
        return float(subprocess.check_output(cmd, creationflags=CREATE_NO_WINDOW).decode().strip())
    except Exception:
        return 0

def get_video_dimensions(path):
    """Returns (width, height) using ffprobe."""
    try:
        cmd = [FFPROBE_EXE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path]
        out = subprocess.check_output(cmd, creationflags=CREATE_NO_WINDOW).decode().strip()
        if "x" in out:
            w, h = out.split("x")
            return int(w), int(h)
    except: pass
    return 1920, 1080

def is_audio_only(path):
    """Returns True if the file is audio-only (no burnable video stream).

    Burning subtitles into an audio file is impossible: the container cannot
    hold a burned-in video track, and the only "video" stream, if any, is a
    single cover-art frame (attached pic). The burn stage is skipped for these
    inputs; SRT/ASS deliverables are already complete by this point.
    """
    AUDIO_EXTS = ('.mp3', '.m4a', '.wav', '.aac', '.flac', '.ogg', '.opus',
                  '.m4b', '.wma', '.aiff', '.aif')
    return os.path.splitext(path)[1].lower() in AUDIO_EXTS

def extract_cover_art(audio_path, out_path):
    """Extract embedded cover art (attached pic) from an audio file to out_path.
    Falls back to the bundled autosub logo if the file has no cover art.
    Returns out_path on success, None if neither cover nor fallback exists."""
    try:
        cmd = [FFMPEG_EXE, "-y", "-i", audio_path, "-map", "0:v", "-frames:v", "1", "-update", "1", out_path]
        subprocess.run(cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception:
        pass
    fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autosub.png")
    if os.path.exists(fallback):
        try:
            shutil.copy2(fallback, out_path)
            return out_path
        except Exception:
            pass
    return None


def burn_audio_subtitles(audio_path, srt_path, layout, main_lang, cn_font, en_font, ja_font, cn_size, en_size, cn_color, en_color, bg_box=True, quality="high", ja_size=None):
    """Burn subtitles into an audio-only file by looping the embedded cover art
    into a video stream and rendering the ASS subtitles onto it. Output is an MP4
    (so the result is uploadable to Bilibili etc.). Returns output path or None."""
    print("🔥 Burning subtitles into audio (cover-art video)...", flush=True)
    import uuid
    uid = uuid.uuid4().hex[:8]
    workdir = os.path.dirname(srt_path)
    base_srt, _ = os.path.splitext(srt_path)
    ass_path = base_srt + ".ass"
 
    width, height = get_video_dimensions(audio_path)  # cover-art dims (e.g. 500x500)
    print(f"📐 Cover Resolution: {width}x{height}")
 
    if not os.path.exists(ass_path):
         cmd = list(SRT2ASS_CMD) + [srt_path, ass_path, "--layout", layout, "--main-lang", main_lang,
                                    "--cn-font", cn_font, "--en-font", en_font, "--ja-font", ja_font,
                                    "--cn-size", cn_size, "--en-size", en_size,
                                    "--cn-color", cn_color, "--en-color", en_color,
                                    "--width", str(width), "--height", str(height)]
         if ja_size: cmd += ["--ja-size", str(ja_size)]
         if not bg_box: cmd.append("--no-bg-box")
         subprocess.run(cmd, check=True, creationflags=CREATE_NO_WINDOW)

    cover_path = os.path.join(workdir, f"tmp_cover_{uid}.jpg")
    if not extract_cover_art(audio_path, cover_path):
        print("❌ No cover art in audio and no fallback image available; cannot burn.")
        return None

    base = os.path.splitext(os.path.basename(audio_path))[0]
    out_video = os.path.join(workdir, base + "_hardsub.mp4")
    if os.path.exists(out_video):
        try: os.remove(out_video)
        except Exception: pass

    temp_ass_name = f"tmp_sub_{uid}.ass"
    temp_ass_path = os.path.join(workdir, temp_ass_name)
    try:
        shutil.copy2(ass_path, temp_ass_path)
    except Exception as e:
        print(f"❌ Error copying subtitle: {str(e)[:60]}")
        return None
    escaped_ass = temp_ass_name.replace('\\', '/').replace(':', '\\:')
    vf = f"ass=filename='{escaped_ass}'"

    crf = {"lossless": "12", "high": "20", "standard": "23"}.get(str(quality).lower(), "20")
    total_dur = get_video_duration(audio_path)

    cmd = [
        FFMPEG_EXE, "-y",
        "-loop", "1", "-i", os.path.abspath(cover_path),
        "-i", os.path.abspath(audio_path),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", crf,
        "-tune", "stillimage", "-r", "5", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        os.path.abspath(out_video),
    ]

    print(f"📦 Cover: {os.path.basename(cover_path)} | Subtitle: {os.path.basename(ass_path)} | Output: {os.path.basename(out_video)}")

    process = subprocess.Popen(cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, creationflags=CREATE_NO_WINDOW)
    last_logs = []
    for line in process.stdout or []:
        msg = line.strip()
        if not msg: continue
        if total_dur > 0:
            m = re.search(r'time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)', msg)
            if m:
                cur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                pct = min(100.0, cur / total_dur * 100.0)
                sys.stdout.write(f"\r   Progress: {pct:5.1f}% (Burning...)    ")
                sys.stdout.flush()
                continue
        print(f"   [Burn] {msg}", flush=True)
        last_logs.append(msg)
        if len(last_logs) > 50: last_logs.pop(0)
    sys.stdout.write("\n")
    process.wait()

    for p in (cover_path, temp_ass_path):
        try: os.remove(p)
        except Exception: pass

    if process.returncode != 0:
        print(f"❌ Burn failed with exit code {process.returncode}")
        if not last_logs: print("   No log output captured.")
        return None

    try:
        with open(os.path.join(workdir, ".burn_complete"), "w", encoding="utf-8") as f:
            f.write("1")
    except Exception:
        pass
    print(f"✅ Audio hardsub created: {os.path.basename(out_video)}")
    return out_video if os.path.exists(out_video) else None

def validate_cookies_file(path):
    """Checks if a cookie file exists and has a valid Netscape format header."""
    if not path or not os.path.exists(path): return False, "文件不存在"
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            first_line = f.readline()
            if "# Netscape HTTP Cookie File" in first_line or "# HTTP Cookie File" in first_line:
                return True, "有效的 Netscape 格式"
            content = f.read(1024)
            if "\t" in content and content.count("\t") > 5:
                return True, "疑似有效的格式 (缺少文件头)"
            return False, "非标准 Cookie 格式"
    except Exception as e:
        return False, f"读取失败: {e}"

def confirm_cookies(args):
    """Auto-detects and confirms cookies with the user (CLI)."""
    if getattr(args, 'cookies', None) and args.cookies.lower() == "none":
        args.cookies = "none"
        return "none"
    if getattr(args, 'cookies', None) and args.cookies.lower() in {"chrome", "firefox", "safari", "edge"}:
        return args.cookies
    search_paths = []
    if getattr(args, 'cookies', None): search_paths.append(args.cookies)
    
    # Priority defaults
    paths_to_check = [
        os.path.join(PROJECT_ROOT, "cookies.txt"),
        os.path.join(os.getcwd(), "cookies.txt"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    ]
    
    for p in paths_to_check:
        if p not in search_paths:
            search_paths.append(p)

    found_path = None
    for p in search_paths:
        if os.path.exists(p):
            found_path = p
            break
    
    if not found_path:
        # Don't prompt for local files
        is_url = args.input.startswith("http") if hasattr(args, 'input') else False
        if is_url:
            print("\n[bold yellow]⚠️  警告: 未找到 Cookie 文件。[/bold yellow]")
            print("   如果处理 YouTube 视频，可能会遇到机器人检测错误。")
            time.sleep(1)
        return None

    is_valid, reason = validate_cookies_file(found_path)
    mtime = datetime.fromtimestamp(os.path.getmtime(found_path)).strftime('%Y-%m-%d %H:%M:%S')
    size_kb = os.path.getsize(found_path) / 1024

    print(f"\n🍪 [Cookies Check] 检测到相关文件:")
    print(f"   路径: {found_path}")
    print(f"   时间: {mtime} ({size_kb:.1f} KB)")
    print(f"   状态: {reason}")

    # No prompt if it's not a URL (local file processing doesn't need cookies)
    is_url = args.input.startswith("http") if hasattr(args, 'input') else False
    if not is_url:
        args.cookies = found_path
        return found_path

    print("\n   [Confirm] 按回车键直接确认 (5s)，按 'N' 取消任务，或输入新路径:")
    
    start_wait = time.time()
    user_input = ""
    while time.time() - start_wait < 5:
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b'\r', b'\n'): break
            if ch.lower() == b'n':
                print("\n❌ 任务取消。")
                sys.exit(0)
            try:
                char = ch.decode('utf-8')
                if char == '\x08': user_input = user_input[:-1]
                else: user_input += char
                sys.stdout.write(f"\r   新路径: {user_input} ")
                sys.stdout.flush()
            except: pass
        time.sleep(0.01)
    
    if user_input.strip():
        new_path = user_input.strip().strip('"')
        if os.path.exists(new_path):
            args.cookies = new_path
            print(f"\n   -> 已切换: {new_path}")
        else:
            print(f"\n   -> 路径无效，默认使用: {found_path}")
            args.cookies = found_path
    else:
        args.cookies = found_path
        print("\n   -> 已确认继续。")
    
    return args.cookies

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?", default="", help="Input video URL or local file")
    
    # Batch mode arguments
    parser.add_argument("--batch-urls", nargs="+", help="Multiple URLs or path to a .txt/.md file containing URLs")
    parser.add_argument("--batch-dir", help="Directory containing multiple video project folders to process automatically")
    parser.add_argument("--workers", type=int, default=5, help="Number of concurrent workers max 10")
    parser.add_argument("--max-api-calls", type=int, default=20, help="Global concurrency limit for Translation APIs")

    parser.add_argument("--model", default=DEFAULTS.get("whisper_model", DEFAULTS.get("model", "large-v2")))
    default_llm_model, default_llm_provider = _load_llm_defaults()
    parser.add_argument("--llm-model", default=default_llm_model, help="LLM Model")
    parser.add_argument("--llm-provider", default=default_llm_provider, help="Explicit LLM Provider (e.g., dashscope, vertex)")
    parser.add_argument("--style", default=DEFAULTS.get("style", "auto"))
    parser.add_argument("--trans-mode", default=DEFAULTS.get("trans_mode", "balanced"), choices=["paraphrase", "balanced"])
    parser.add_argument("--cookies")
    parser.add_argument("--cookies-from-browser", choices=["chrome", "firefox", "safari", "edge"], help="Read cookies from a browser profile")
    parser.add_argument("--layout", default=DEFAULTS.get("layout", "bilingual"))
    parser.add_argument("--main-lang", default=DEFAULTS.get("main_lang", "cn"))
    parser.add_argument("--cn-font", default=DEFAULTS.get("cn_font", "KaiTi"))
    parser.add_argument("--en-font", default=DEFAULTS.get("en_font", "Arial"))
    parser.add_argument("--ja-font", default=DEFAULTS.get("ja_font", "Hiragino Sans"))
    parser.add_argument("--cn-size", default=DEFAULTS.get("cn_size", "60"))
    parser.add_argument("--en-size", default=DEFAULTS.get("en_size", "36"))
    parser.add_argument("--ja-size", default=DEFAULTS.get("ja_size", "36"), help="Japanese font size")
    parser.add_argument("--cn-color", default=DEFAULTS.get("cn_color", "Gold"))
    parser.add_argument("--en-color", default=DEFAULTS.get("en_color", "White"))
    parser.add_argument("--no-bg-box", action="store_true")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument("--output-dir", help="Project root directory for output files")
    parser.add_argument("--project-name", help="Dummy argument to prevent crashes")
    parser.add_argument("--sync-gdrive", help="Google Drive Folder ID to sync output files to")
    parser.add_argument("--bili-upload", action="store_true", help="Upload the hardsub video to Bilibili (auto-generates tid/tags/desc/season per video)")
    parser.add_argument("--bili-cover-title", help="Text rendered on Bilibili cover images (default: subtitle-grounded form title)")
    parser.add_argument("--wechat-upload", action="store_true", help="Upload the hardsub video to WeChat Channels as a draft (use --wechat-public to publish)")
    parser.add_argument("--wechat-public", action="store_true", help="Publish the WeChat Channels upload publicly instead of saving a draft")
    parser.add_argument("--wechat-cover-title", help="Text rendered on WeChat cover images (default: subtitle-grounded form title)")
    parser.add_argument("--quick", action="store_true", help="Experimental: Quick Transcribe (YouTube Subs + LLM)")
    parser.add_argument("--start", help="Start time (e.g. 00:01:30 or 90)")
    parser.add_argument("--end", help="End time (e.g. 00:03:00 or 180)")
    parser.add_argument("--quality", default="high", choices=["standard", "high", "lossless"], help="Burn quality (standard, high, lossless)")
    parser.add_argument("--upload-only", action="store_true", help="Skip transcription/translation/burn; only upload existing hardsub video to GDrive/Bilibili")
    parser.add_argument("--multilang", action="store_true", help="Enable per-segment language detection for mixed-language videos")
    args = parser.parse_args()

    if args.cookies_from_browser:
        args.cookies = args.cookies_from_browser
    
    # --- Intelligent Time Parsing ---
    if args.start and "-" in args.start and not args.end:
        parts = args.start.split("-")
        args.start = parts[0].strip()
        args.end = parts[1].strip()
        print(f"✂️  Parsed time range: {args.start} to {args.end}")

    # Cookie verification step for CLI
    confirm_cookies(args)

    # Determine if we should run batch mode
    if args.batch_urls or args.batch_dir:
        import autosub_batch  # type: ignore
        return autosub_batch.run_batch(args)
        
    if not args.input:
        return parser.print_help()

    # --- Directory Logic ---
    final_output_dir = args.output_dir or DEFAULTS.get("output_dir") or BASE_OUTPUT_DIR
    if not os.path.isabs(final_output_dir):
        final_output_dir = os.path.abspath(os.path.join(PROJECT_ROOT, final_output_dir))
    
    # Update global-like state for this run
    effective_base_output_dir = final_output_dir

    # 0. Intelligent Project Naming
    if args.input.startswith("http"):
        # Local, instantaneous extraction of YouTube Video ID
        video_id = None
        if "youtube.com" in args.input or "youtu.be" in args.input:
            match = re.search(r'(?:v=|\/|embed\/|shorts\/)([a-zA-Z0-9_-]{11})', args.input)
            if match:
                video_id = match.group(1)
        elif "x.com" in args.input or "twitter.com" in args.input:
            # X/Twitter status ID (numeric, variable length).
            # NOTE: This is the TWEET STATUS ID, not the video ID that yt-dlp
            # uses internally. yt-dlp's %(id)s is the X video ID, which differs
            # from the status ID in the URL. We use the status ID only as a
            # first-pass hint for folder lookup; if it fails, we fall through
            # to yt-dlp --get-folder-name which returns the correct video ID.
            match = re.search(r'/status/(\d+)', args.input)
            if match:
                video_id = match.group(1)
        
        # Check if a folder ending with [{video_id}] already exists locally
        existing_dir = None
        if video_id and os.path.exists(effective_base_output_dir):
            for item in os.listdir(effective_base_output_dir):
                item_path = os.path.join(effective_base_output_dir, item)
                if os.path.isdir(item_path) and item.endswith(f"[{video_id}]"):
                    existing_dir = item_path
                    break
        
        # For X/Twitter, the status ID from the URL != yt-dlp's video ID.
        # If the folder lookup failed, try yt-dlp --get-folder-name which
        # returns the correct video ID. This is slower (~15s for X) but
        # essential to avoid re-downloading already-completed videos.
        if not existing_dir and ("x.com" in args.input or "twitter.com" in args.input):
            print(f"🔍 Folder lookup by status ID failed. Fetching real video ID from yt-dlp (timeout 35s)...", flush=True)
            real_folder_name = get_video_folder_name(args.input, args.cookies)
            if real_folder_name:
                candidate_dir = os.path.join(effective_base_output_dir, real_folder_name)
                if os.path.exists(candidate_dir) and os.path.isdir(candidate_dir):
                    existing_dir = candidate_dir
                    video_id = real_folder_name.split("[")[-1].rstrip("]") if "[" in real_folder_name else video_id
        
        if existing_dir:
            workdir = existing_dir
            print(f"📁 Found existing Project Folder locally: {os.path.basename(workdir)}")
            title = get_video_title(args.input, args.cookies)
        else:
            print(f"🔍 Fetching video details (timeout in 35s)...", flush=True)
            folder_name = get_video_folder_name(args.input, args.cookies)
            title = get_video_title(args.input, args.cookies)
            if folder_name:
                workdir = os.path.join(effective_base_output_dir, folder_name)
                print(f"📁 Project Folder: {folder_name}")
            elif title:
                # Replicate stable restricted naming in Python fallback
                t_clean = title.replace(":", " - ").replace("|", " - ")
                t_clean = re.sub(r'\s+', '_', t_clean)
                t_clean = re.sub(r'[^a-zA-Z0-9_.-]', '', t_clean)
                t_clean = re.sub(r'_+', '_', t_clean).strip('_')
                
                if video_id:
                    folder_name_fallback = f"{t_clean} [{video_id}]"
                else:
                    folder_name_fallback = t_clean
                    
                workdir = os.path.join(effective_base_output_dir, folder_name_fallback)
                print(f"📁 Project Folder (fallback match): {folder_name_fallback}")
            else:
                workdir = get_workdir(args.input, effective_base_output_dir)
    else:
        workdir = get_workdir(args.input, effective_base_output_dir)

    if not os.path.exists(workdir): os.makedirs(workdir, exist_ok=True)
    
    # --- Initialize Workflow Logging ---
    log_path = os.path.join(workdir, "workflow.log")
    logger = Logger(log_path)
    sys.stdout = logger
    sys.stderr = logger

    # --- Upload-Only Mode ---
    # Skip the entire pipeline (transcribe, translate, burn) and jump straight
    # to GDrive sync / Bilibili upload. Use this when a previous run produced
    # the hardsub video but the upload step was skipped or failed.
    if getattr(args, 'upload_only', False):
        print(f"📤 Upload-only mode: {args.input}", flush=True)
        hardsub_path = None
        for f in os.listdir(workdir):
            if "hardsub" in f and f.endswith(('.mp4', '.mkv', '.webm', '.mov', '.ts')):
                hardsub_path = os.path.join(workdir, f)
                break
        if not hardsub_path:
            print(f"❌ No hardsub video found in {workdir}", flush=True)
            return
        print(f"✅ Found hardsub: {os.path.basename(hardsub_path)}", flush=True)
        run_post_processing_uploads(
            workdir,
            sync_gdrive=getattr(args, 'sync_gdrive', None),
            bili_upload=getattr(args, 'bili_upload', False),
            wechat_upload=getattr(args, 'wechat_upload', False),
            headless=args.headless,
            bili_cover_title=getattr(args, 'bili_cover_title', None),
            wechat_cover_title=getattr(args, 'wechat_cover_title', None),
            wechat_public=getattr(args, 'wechat_public', False),
            llm_model=getattr(args, 'llm_model', None),
            llm_provider=getattr(args, 'llm_provider', None),
        )
        print("✅ Upload-only complete!", flush=True)
        return

    print(f"--- 任务启动: {args.input} ---")
    print(f"🏠 [DEV MODE] Root: {PROJECT_ROOT}")
    print(f"📦 Found FFmpeg at: {FFMPEG_EXE}")

    if args.input.startswith("http"):
        # Always download full video first (more robust/fast than --download-sections for live streams)
        video_path = download_video(args.input, workdir, args.cookies, quick=args.quick, title=title)
        
        # Dynamic project folder renaming if title was not pre-fetched (temp fallback case)
        if video_path and os.path.exists(video_path) and not title:
            # Extract real video title from downloaded file
            actual_filename = os.path.basename(video_path)
            # Remove extension first
            name_without_ext = os.path.splitext(actual_filename)[0]
            # Keep the ID but strip only the last bracketed section (e.g. resolution "[1080p]")
            nice_title = re.sub(r'\s*\[[^\]]+\]\s*$', '', name_without_ext)
            nice_title = sanitize_filename(nice_title)
            
            if nice_title and not nice_title.startswith("temp_"):
                new_workdir = os.path.join(effective_base_output_dir, nice_title)
                # If target directory already exists, append a timestamp
                if os.path.exists(new_workdir) and new_workdir.lower() != workdir.lower():
                    new_workdir = new_workdir + "_" + str(int(time.time()))
                
                print(f"📁 Video downloaded. Dynamically upgrading project folder to: {os.path.basename(new_workdir)}", flush=True)
                workdir, video_path, logger = rename_workdir_safely(workdir, new_workdir, video_path, logger)
    else:
        # For local files, copy to project folder first
        src_path = os.path.abspath(args.input)
        dest_path = os.path.join(workdir, os.path.basename(src_path))
        if os.path.exists(src_path):
            if os.path.abspath(src_path).lower() != os.path.abspath(dest_path).lower():
                print(f"📂 Copying video to project folder...")
                shutil.copy2(src_path, dest_path)
            video_path = dest_path
        elif os.path.exists(dest_path):
            print(f"ℹ️ Original source missing, using video in project folder.")
            video_path = dest_path
        else:
            video_path = src_path

    # Post-acquisition / Clipping
    if (args.start or args.end) and video_path and os.path.exists(video_path):
        src_path = video_path
        ext = os.path.splitext(src_path)[1]
        s_clean = sanitize_filename(args.start or "0")
        e_clean = sanitize_filename(args.end or "end")
        clip_path = os.path.join(workdir, f"clip_{s_clean}_{e_clean}{ext}")
        
        # Skip if clip already exists
        if os.path.exists(clip_path) and os.path.getsize(clip_path) > 1024*1024:
            print(f"ℹ️  Clipped segment already exists, reusing: {os.path.basename(clip_path)}")
            video_path = clip_path
        else:
            print(f"✂️  Clipping segment: {args.start or '0'} -> {args.end or 'end'}")
            time.sleep(1) # Small delay to avoid file lock issues on Windows
            ss = ["-ss", args.start] if args.start else []
            to = ["-to", args.end] if args.end else []
            cmd = [FFMPEG_EXE, "-y"] + ss + ["-i", src_path] + to + ["-c", "copy", clip_path]
            try:
                subprocess.run(cmd, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
                video_path = clip_path
                print(f"✅ Segment clipped: {os.path.basename(clip_path)}")
            except Exception as e:
                print(f"⚠️  Clipping failed: {e}. Using full file instead.")
            
    if not video_path or not os.path.exists(video_path):
        print("❌ Invalid input")
        return 1

    vid_dur = get_video_duration(video_path)
    base = os.path.splitext(os.path.basename(video_path))[0]
    expected_srt = os.path.join(workdir, base + ".srt")
    expected_en = os.path.join(workdir, base + ".en.srt")
    expected_cn = os.path.join(workdir, base + ".cn.srt")

    # 1. Transcription (Sequential)
    src_srt = None
    
    # Priority: 1. .srt (Transcribed) 2. .en.srt (Downloaded)
    possible_sources = [expected_srt, expected_en]
    
    for candidate in possible_sources:
        if os.path.exists(candidate) and os.path.getsize(candidate) > 500:
            if srt_utils:
                if vid_dur > 0:
                    try:
                        srt_dur = srt_utils.get_srt_duration(candidate)
                        if srt_dur > vid_dur * 0.9:
                            print(f"✅ Found existing SRT: {os.path.basename(candidate)} (Duration matches)")
                            src_srt = candidate
                            break
                        else:
                            print(f"⚠️ {os.path.basename(candidate)} duration mismatch ({srt_dur:.1f}s vs {vid_dur:.1f}s).")
                    except Exception as e:
                        print(f"⚠️ Error checking {os.path.basename(candidate)}: {e}")
                else:
                    print(f"✅ Found existing SRT: {os.path.basename(candidate)} (Video duration unknown, skipping transcription)")
                    src_srt = candidate
                    break
            else:
                # If srt_utils is missing, we still trust the file if it's > 500 bytes
                print(f"✅ Found existing SRT: {os.path.basename(candidate)} (srt_utils missing, assuming OK)")
                src_srt = candidate
                break
    
    if not src_srt:
        src_srt = transcribe_video(video_path, workdir, args.model, quick=args.quick, multilang=args.multilang)
    
    if not src_srt:
        print("❌ Transcription failed")
        return 1

    # --- Integrated Acoustic Spellcheck Middleware ---
    has_translation = False
    for candidate_cn in [expected_cn, os.path.join(workdir, base + ".zh.srt")]:
        if os.path.exists(candidate_cn) and os.path.getsize(candidate_cn) > 500:
            if srt_utils:
                try:
                    src_count = len(srt_utils.parse_srt(src_srt))
                    zh_count = len(srt_utils.parse_srt(candidate_cn))
                    if zh_count >= src_count * 0.95:
                        has_translation = True
                        break
                except:
                    pass
            else:
                has_translation = True
                break

    if has_translation:
        print("✅ Translation file already exists, skipping Acoustic Spellcheck Preprocessing.")
    else:
        try:
            py_exe = get_python_exe()
            spellcheck_script = os.path.join(CURRENT_DIR, "spellcheck_srt.py")
            print("🎙️ Running Acoustic Spellcheck Preprocessing...")
            # Spellcheck is a deterministic correction task, not deep reasoning.
            # Use a fast non-reasoning model (spellcheck_model) instead of the
            # heavy llm_model (e.g. glm-5.2 reasoning -> 143s/chunk -> 90s timeout).
            spellcheck_model = DEFAULTS.get("spellcheck_model") or args.llm_model
            cmd = [py_exe, spellcheck_script, src_srt, "--model", spellcheck_model]
            if getattr(args, "llm_provider", None):
                cmd.extend(["--provider", args.llm_provider])
            # Stream output line-by-line into Logger (and workflow.log).
            # subprocess.run(capture_output=False) inherits the underlying fd,
            # bypassing Logger.write() — spellcheck logs vanished from workflow.log.
            sp = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=CREATE_NO_WINDOW)
            for line in sp.stdout or []:
                print(line.rstrip(), flush=True)
            sp.wait()
            if sp.returncode != 0:
                print(f"⚠️ Acoustic Spellcheck exited with code {sp.returncode}")
        except Exception as e:
            print(f"⚠️ Acoustic Spellcheck failed/skipped: {e}")

    # 2. Translation (Sequential)
    zh_srt = None
    expected_zh = os.path.join(workdir, base + ".zh.srt")
    
    for candidate_cn in [expected_cn, expected_zh]:
        if os.path.exists(candidate_cn) and os.path.getsize(candidate_cn) > 500:
            if srt_utils:
                try:
                    src_count = len(srt_utils.parse_srt(src_srt))
                    zh_count = len(srt_utils.parse_srt(candidate_cn))
                    if zh_count >= src_count * 0.95:
                        print(f"✅ Found existing translation: {os.path.basename(candidate_cn)}")
                        zh_srt = candidate_cn
                        break
                except Exception as e:
                    print(f"⚠️ Error parsing {os.path.basename(candidate_cn)}: {e}")
            else:
                zh_srt = candidate_cn
                break

    if not zh_srt:
        print("🌍 Smart-translating...")
        try:
            py_exe = get_python_exe()
            cmd = [py_exe, SMART_TRANSLATE_CMD[1], src_srt, "--style", args.style, "--model", args.llm_model, "--trans-mode", getattr(args, "trans_mode", "balanced")]
            if getattr(args, "llm_provider", None):
                cmd.extend(["--provider", args.llm_provider])
            # Stream output in real-time so the user sees progress
            # (translation can take several minutes with reasoning models)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, creationflags=CREATE_NO_WINDOW)
            for line in proc.stdout or []:
                line = line.rstrip('\n')
                if line:
                    print(line, flush=True)
            proc.wait()
            if proc.returncode != 0:
                print(f"⚠️ Translation exited with code {proc.returncode}")
            if os.path.exists(expected_cn): zh_srt = expected_cn
            elif os.path.exists(expected_zh): zh_srt = expected_zh
        except Exception as e:
            print(f"⚠️ Translation step error: {e}")
    
    if not zh_srt:
        print("❌ Translation failed")
        return 1

    # 3. Merge & Burn
    final_srt = zh_srt
    if args.layout == "bilingual":
        print(f"🔀 Merging {os.path.basename(src_srt)} and {os.path.basename(zh_srt)}...")
        merged = merge_bilingual(src_srt, zh_srt, args.main_lang, args.llm_model)
        if merged and os.path.exists(merged):
            final_srt = merged
        else:
            # Check if there's an existing .bi.srt anyway (maybe created but returned None due to error code)
            bi_path = src_srt[:-7] + ".bi.srt" if src_srt.lower().endswith(".en.srt") else src_srt.replace(".srt", ".bi.srt")
            if os.path.exists(bi_path) and os.path.getsize(bi_path) > 500:
                print(f"✅ Using legacy/existing bilingual file: {os.path.basename(bi_path)}")
                final_srt = bi_path
            else:
                print(f"⚠️ Bilingual merge failed. Falling back to primary translation: {os.path.basename(zh_srt)}")
                final_srt = zh_srt
    
    print(f"📍 Final subtitle for burning: {os.path.basename(final_srt)}", flush=True)
    
    video_base = os.path.splitext(os.path.basename(video_path))[0]
    marker_file = os.path.join(workdir, ".burn_complete")
    
    if is_audio_only(video_path):
        # Audio-only inputs have no burnable video stream. Synthesize one from
        # the embedded cover art (looped over the audio) and burn subtitles into
        # an MP4 so the result is uploadable (e.g. to Bilibili).
        out_video = os.path.join(workdir, video_base + "_hardsub.mp4")
        if os.path.exists(out_video) and os.path.getsize(out_video) > 1024 * 1024 and os.path.exists(marker_file):
            print(f"✅ Found existing hardsub: {os.path.basename(out_video)} (Skipping burn stage)")
        else:
            burn_audio_subtitles(video_path, final_srt, args.layout, args.main_lang, args.cn_font, args.en_font, args.ja_font, args.cn_size, args.en_size, args.cn_color, args.en_color, not args.no_bg_box, args.quality, args.ja_size)
    else:
       # Check if hardsub already exists (video path)
       video_ext = os.path.splitext(video_path)[1]
       out_video = os.path.join(workdir, video_base + "_hardsub" + video_ext)
       if os.path.exists(out_video) and os.path.getsize(out_video) > 1024 * 1024 and os.path.exists(marker_file):
            print(f"✅ Found existing hardsub: {os.path.basename(out_video)} (Skipping burn stage)")
       else:
            burn_subtitle(video_path, final_srt, args.layout, args.main_lang, args.cn_font, args.en_font, args.ja_font, args.cn_size, args.en_size, args.cn_color, args.en_color, not args.no_bg_box, args.quality, args.ja_size)
    
    print("✅ All done!", flush=True)

    # --- Parallel Post-Processing Uploads ---
    run_post_processing_uploads(
        workdir,
        sync_gdrive=getattr(args, 'sync_gdrive', None),
        bili_upload=getattr(args, 'bili_upload', False),
        wechat_upload=getattr(args, 'wechat_upload', False),
        headless=args.headless,
        bili_cover_title=getattr(args, 'bili_cover_title', None),
        wechat_cover_title=getattr(args, 'wechat_cover_title', None),
        wechat_public=getattr(args, 'wechat_public', False),
        llm_model=getattr(args, 'llm_model', None),
        llm_provider=getattr(args, 'llm_provider', None),
    )

    return 0

if __name__ == "__main__":
    sys.exit(main())
