import sys
import os
import sys
import subprocess
import time
import re
import json
import glob
import shutil
from datetime import datetime
from contextlib import contextmanager
import datetime
import tempfile
from pathlib import Path

# --- Global Window Suppression Patch for Windows ---
if sys.platform == "win32":
    _original_popen = subprocess.Popen
    def _patched_popen(*args, **kwargs):
        cflags = kwargs.get('creationflags', 0)
        kwargs['creationflags'] = cflags | getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
        return _original_popen(*args, **kwargs)
    subprocess.Popen = _patched_popen

# --- Configuration ---
# Find working executables
PYTHON_EXE = sys.executable
# Target specifically the one we verified
def log(message):
    import datetime
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")

# Helper to find executables
def find_ytdlp():
    # 0. Check current python's bin folder (e.g. .venv/bin)
    current_py_bin = os.path.join(os.path.dirname(sys.executable), "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")
    if os.path.exists(current_py_bin): return current_py_bin

    # 1. Check local folder
    local_ytdlp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")
    if os.path.exists(local_ytdlp):
        return local_ytdlp

    # 2. Check PATH
    path = shutil.which("yt-dlp")
    if path: return path

    # 3. Fallback common locations
    if sys.platform == "win32":
        fallbacks = [r"C:\Program Files\Python\Python312\Scripts\yt-dlp.exe"]
        for fb in fallbacks:
            if os.path.exists(fb): return fb
    elif sys.platform == "darwin":
        # Check across multiple Python version bins on Mac
        for v in ["3.13", "3.12", "3.11", "3.10", "3.9"]:
            mac_py_bin = os.path.expanduser(f"~/Library/Python/{v}/bin/yt-dlp")
            if os.path.exists(mac_py_bin): return mac_py_bin
        # Check Homebrew and standard locations
        for p in ["/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp"]:
            if os.path.exists(p): return p

    return "yt-dlp"

YTDLP_EXE = find_ytdlp()
_YTDLP_FOUND = YTDLP_EXE != "yt-dlp"


def _cookie_file_candidates():
    """Return cookie files from this checkout and its parent checkouts."""
    candidates = [os.path.join(os.path.expanduser("~"), "Downloads", "cookies.txt")]
    current = os.path.dirname(os.path.abspath(__file__))
    while True:
        candidates.append(os.path.join(current, "cookies.txt"))
        candidates.append(os.path.join(current, "vdown", "cookies.txt"))
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return list(dict.fromkeys(candidates))


def find_cookies(custom_path=None):
    """Find cookies.txt in prioritized locations."""
    # 1. Check custom path if provided
    if custom_path:
        if custom_path.upper() == "NONE":
            return "NONE"
        # Support browser names directly
        if custom_path.lower() in ["chrome", "firefox", "safari", "edge", "opera", "vivaldi"]:
            return f"browser:{custom_path.lower()}"
        if os.path.exists(custom_path):
            return custom_path

    # 2. Project standard locations. The macOS app may run from a nested
    # checkout, so search parent checkouts as well as this one.
    search_paths = _cookie_file_candidates()
    if sys.platform == "win32":
        search_paths.insert(0, r"D:\download\cookies.txt")
        search_paths.insert(1, r"D:\Downloads\cookies.txt")

    for path in search_paths:
        if os.path.exists(path):
            return path
    return None

import contextlib
@contextlib.contextmanager
def use_temp_cookies(cookies_path):
    """Creates a temporary copy of cookies to prevent concurrent write corruption."""
    if not cookies_path or cookies_path == "NONE" or cookies_path.startswith("browser:") or not os.path.exists(cookies_path):
        yield None
        return

    # Create a temp file
    fd, temp_path = tempfile.mkstemp(suffix=".txt", prefix="yt_cookies_")
    os.close(fd)

    try:
        shutil.copy2(cookies_path, temp_path)
        yield temp_path
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except: pass

# Find Node.js
def find_node():
    path = shutil.which("node")
    if path: return path

    possible_node_paths = []
    if sys.platform == "win32":
        possible_node_paths = [
            r"C:\Program Files\nodejs",
            r"D:\Program Files\nodejs",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links"),
        ]
    elif sys.platform == "darwin":
        possible_node_paths = [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
        ]

    for p in possible_node_paths:
        exe_name = "node.exe" if sys.platform == "win32" else "node"
        exe = os.path.join(p, exe_name)
        if os.path.exists(exe):
            return exe
    return "node"

NODE_EXE = find_node()

if getattr(sys, 'frozen', False):
    DOWNLOAD_ROOT = os.path.join(os.path.expanduser("~"), "Documents", "AutoSub", "Downloads")
else:
    # Library/Tools/vdown -> Library/Tools -> Library -> Root
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    DOWNLOAD_ROOT = os.path.join(REPO_ROOT, "download")

# (log function moved up)

def get_progress_from_line(line):
    # Match "[download]  12.3% of" or "[download] 100%"
    match = re.search(r'\[download\]\s+(\d+\.?\d*)%', line)
    if match:
        return match.group(1)
    return None

def get_youtube_args(url):
    """Returns necessary yt-dlp arguments for resolving YouTube challenges."""
    if not ("youtube.com" in url or "youtu.be" in url):
        return []

    args = [
        # yt-dlp 2026.08+ defaults to web_embedded client, which YouTube
        # marks UNPLAYABLE for many videos and then falls back to the web
        # client where SABR streaming strips high-res format URLs (issue
        # #12482). web_safari uses m3u8/HLS and returns all resolutions.
        "--extractor-args", "youtube:player_client=web_safari",
        "--remote-components", "ejs:github",
        "--no-check-certificates"
    ]
    if NODE_EXE != "node":
        args.extend(["--js-runtime", f"node:{NODE_EXE}"])
    else:
        args.extend(["--js-runtime", "node"])
    return args

def get_title(url, cookies=None):
    resolved_cookies = find_cookies(cookies)
    cmd = [
        YTDLP_EXE,
        "--get-title",
        "--no-playlist",
        "--socket-timeout", "30",
        "--quiet",
        "--no-warnings"
    ]
    # Add YouTube challenge solving args
    cmd.extend(get_youtube_args(url))

    if resolved_cookies and resolved_cookies.startswith("browser:"):
        cmd.extend(["--cookies-from-browser", resolved_cookies.split(":")[1]])
        resolved_cookies = None

    with use_temp_cookies(resolved_cookies) as temp_cookies:
        if temp_cookies:
            cmd.extend(["--cookies", temp_cookies])

        cmd.append(url)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if sys.platform == "win32" else 0,
                timeout=35
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except: pass
    return None

def get_restricted_name(url, cookies=None):
    resolved_cookies = find_cookies(cookies)
    cmd = [
        YTDLP_EXE,
        "--get-filename",
        "--restrict-filenames",
        "-o", "%(title)s [%(id)s]",
        "--no-playlist",
        "--socket-timeout", "30",
        "--quiet",
        "--no-warnings"
    ]
    # Add YouTube challenge solving args
    cmd.extend(get_youtube_args(url))

    if resolved_cookies and resolved_cookies.startswith("browser:"):
        cmd.extend(["--cookies-from-browser", resolved_cookies.split(":")[1]])
        resolved_cookies = None

    with use_temp_cookies(resolved_cookies) as temp_cookies:
        if temp_cookies:
            cmd.extend(["--cookies", temp_cookies])

        cmd.append(url)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if sys.platform == "win32" else 0,
                timeout=35
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except: pass
    return None

def download_video(url, custom_cookies=None, out_dir=None, title=None, start_time=None, end_time=None, **kwargs):
    if out_dir:
        target_dir = out_dir
    else:
        target_dir = DOWNLOAD_ROOT

    if not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    log(f"🎬 Starting download via CLI...")
    log(f"   URL: {url}")
    log(f"   Output: {target_dir}")

    # Log title if provided
    if title:
        log(f"   Title: {title}")

    # Build Command
    # YouTube handles cookies best with 'web' or 'mweb' clients.
    # Over-specifying clients or user-agents can trigger bot detection.
    cmd = [
        YTDLP_EXE,
        "--no-playlist",
        "--progress",
        "--newline",
        "--restrict-filenames",
        "--force-ipv4",
        "--no-check-certificates",
        "--geo-bypass",
        "--merge-output-format", "mp4",
        "-o", os.path.join(target_dir, "%(title)s [%(id)s] [%(height)sp].%(ext)s")
    ]
    if sys.platform == "win32":
        cmd.append("--windows-filenames")

    # Subtitles support
    if kwargs.get("write_subs"):
        cmd.extend([
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs", "en.*",
            "--sub-format", "srt/vtt/best"
        ])

    # Time segment support (yt-dlp --download-sections)
    if start_time or end_time:
        # Format: *START-END (e.g. *00:01:30-00:02:00 or *90-180)
        # Default start to 0, end to inf if only one is provided
        s = start_time if start_time else "0"
        e = end_time if end_time else "inf"
        section_str = f"*{s}-{e}"
        log(f"✂️  Requesting section: {section_str}")
        cmd.extend(["--download-sections", section_str])
        # Note: --download-sections requires ffmpeg to be in PATH

    # Handle cookies and platform specific optimizations
    resolved_cookies = find_cookies(custom_cookies)
    cmd.extend(get_youtube_args(url))

    if resolved_cookies and resolved_cookies.startswith("browser:"):
        browser_name = resolved_cookies.split(":")[1]
        log(f"   Cookies: Fetching from browser {browser_name}...")
        cmd.extend(["--cookies-from-browser", browser_name])
        if "youtube.com" in url or "youtu.be" in url:
             cmd.extend(["--user-agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"])
        resolved_cookies = None # Prevent use_temp_cookies from doing anything

    with use_temp_cookies(resolved_cookies) as temp_cookies:
        if temp_cookies:
            log(f"   Cookies: {resolved_cookies} (Isolated via {os.path.basename(temp_cookies)})")
            cmd.extend(["--cookies", temp_cookies])
            # On Mac, browser cookies can sometimes prompt for Keychain access and hang.
            # We'll rely on cookies.txt if provided.
            if "youtube.com" in url or "youtu.be" in url:
                cmd.extend(["--user-agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"])
        else:
            # Without cookies, yt-dlp defaults handle client selection
            pass

        if "youtube.com" in url or "youtu.be" in url:
            # Force 1080p search with fallback
            cmd.extend(["-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"])
        else:
            # For other platforms (Twitter/X, etc.), cap at 1080p. Without a
            # height limit yt-dlp picks 2160p (4K) on X, which produces 3GB+
            # files that routinely stall/timeout mid-download via HLS (see
            # 2026-07-27 incident: failed at 71.8% on a 2.2GB partial).
            cmd.extend(["--format-sort", "res,vbr,abr"])
            cmd.extend(["-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"])

        cmd.append(url)

        try:
            def run_ytdlp(extra_args=None):
                local_cmd = list(cmd)
                if extra_args:
                    local_cmd.extend(extra_args)

                # Ensure node.js path is available for n-challenge solving
                current_env = os.environ.copy()
                path_list = current_env.get("PATH", "").split(os.pathsep)
                mac_extra = ["/opt/homebrew/bin", "/usr/local/bin"]
                for p in mac_extra:
                    if p not in path_list:
                        path_list.insert(0, p)
                current_env["PATH"] = os.pathsep.join(path_list)

                log(f"   Full Command: {' '.join(local_cmd)}")
                return subprocess.Popen(
                    local_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    bufsize=1,
                    env=current_env,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if sys.platform == "win32" else 0
                )

            process = run_ytdlp()
            bot_detected = False
            range_error = False
            output_lines = []

            while True:
                if process.stdout is None:
                    break
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                line = line.strip()
                if not line: continue
                output_lines.append(line)

                p = get_progress_from_line(line)
                if p:
                    # Only forward progress that is below 100% to avoid
                    # yt-dlp fragment-level "100%" lines being mistaken
                    # for overall download completion. The final 100% is
                    # set by run_process when the process exits with code 0.
                    if float(p) < 100.0:
                        print(f"Progress: {p}%", flush=True)
                else:
                    # Allow more platforms (twitter, x, etc.) and all error/warning msgs
                    if any(x in line for x in ["[youtube]", "[twitter]", "[x]", "[info]", "ERROR", "WARNING", "[debug]"]):
                        print(line, flush=True)
                    # More robust bot detection check
                    if any(msg in line.lower() for msg in ["confirm you're not a bot", "confirm youre not a bot", "confirm youre not a bot", "sign in to confirm"]):
                        bot_detected = True
                    # Range error detection
                    if "HTTP Error 416" in line:
                        range_error = True

            process.wait()

            # Auto-retry if 416 error detected
            if process.returncode != 0 and range_error:
                log("⚠️  HTTP Error 416 detected. Retrying without resume (--no-continue)...")
                process = run_ytdlp(["--no-continue"])
                if process.stdout:
                    for line in process.stdout:
                        line = line.strip()
                        if not line: continue
                        p = get_progress_from_line(line)
                        if p:
                            if float(p) < 100.0:
                                print(f"Progress: {p}%", flush=True)
                        else:
                            if "[youtube]" in line or "[info]" in line or "ERROR" in line or "WARNING" in line:
                                print(line, flush=True)
                process.wait()

            if process.returncode == 0:
                log("✅ Download completed successfully.")
                return True

            if bot_detected or (process.returncode != 0 and "403" in "".join(output_lines)):
                if resolved_cookies and resolved_cookies != "NONE":
                    log("⚠️  403 Forbidden or Bot detection detected with cookies. Retrying WITHOUT cookies...")
                    return download_video(url, custom_cookies="NONE", out_dir=out_dir, title=title, start_time=start_time, end_time=end_time, **kwargs)
                log(f"❌ Bot detection triggered. Please verify your cookies.txt or try setting cookies to 'chrome'")
            else:
                log(f"❌ Download failed with exit code {process.returncode}")
            return False

        except Exception as e:
            log(f"❌ Error launching yt-dlp: {e}")
            return False

    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python download.py <URL> [cookies_file] [out_dir] [--title \"Video Title\"] [--write-subs]")
        sys.exit(1)

    video_url = sys.argv[1]

    # Handle optional flags
    provided_title = None
    if "--title" in sys.argv:
        try:
            t_idx = sys.argv.index("--title")
            provided_title = sys.argv[t_idx + 1]
            del sys.argv[t_idx:t_idx+2]
        except: pass

    write_subs = False
    if "--write-subs" in sys.argv:
        write_subs = True
        sys.argv.remove("--write-subs")

    cookies_arg = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].strip() else None
    out_dir_arg = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3].strip() else None

    start_arg = None
    if "--start" in sys.argv:
        try:
            idx = sys.argv.index("--start")
            start_arg = sys.argv[idx + 1]
            del sys.argv[idx:idx+2]
        except: pass

    end_arg = None
    if "--end" in sys.argv:
        try:
            idx = sys.argv.index("--end")
            end_arg = sys.argv[idx + 1]
            del sys.argv[idx:idx+2]
        except: pass

    if "--get-title" in sys.argv:
        title = get_title(video_url, cookies_arg)
        if title:
            # Only print the title, no logging
            print(title)
            sys.exit(0)
        else:
            print("Error: Could not fetch title")
            sys.exit(1)

    if "--get-folder-name" in sys.argv:
        folder_name = get_restricted_name(video_url, cookies_arg)
        if folder_name:
            # Only print the folder name, no logging
            print(folder_name)
            sys.exit(0)
        else:
            print("Error: Could not fetch folder name")
            sys.exit(1)

    if "--get-duration" in sys.argv:
        cmd = [
            YTDLP_EXE,
            "--get-duration",
            "--no-playlist",
            "--socket-timeout", "30",
            "--quiet",
            "--no-warnings"
        ]
        cmd.extend(get_youtube_args(video_url))

        resolved_cookies = find_cookies(cookies_arg)
        if resolved_cookies and resolved_cookies.startswith("browser:"):
            cmd.extend(["--cookies-from-browser", resolved_cookies.split(":")[1]])
            resolved_cookies = None

        with use_temp_cookies(resolved_cookies) as temp_cookies:
            if temp_cookies:
                cmd.extend(["--cookies", temp_cookies])

            cmd.append(video_url)
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if sys.platform == "win32" else 0)
                if result.returncode == 0:
                    print(result.stdout.strip())
                    sys.exit(0)
            except Exception: pass
            print("未知")
            sys.exit(1)



    # Only log environment info in regular download mode
    if _YTDLP_FOUND:
        log(f"Using yt-dlp: {YTDLP_EXE}")

    success = download_video(video_url, cookies_arg, out_dir_arg, title=provided_title, write_subs=write_subs, start_time=start_arg, end_time=end_arg)
    if not success:
        sys.exit(1)
