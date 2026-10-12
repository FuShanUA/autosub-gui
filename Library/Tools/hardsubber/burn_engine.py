import sys
import ctypes
import subprocess
import os
import shutil
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
import threading
import re
import time
import datetime
import glob

# Config
# Config
# Updated to user-provided path
# --- Robust FFmpeg Detection ---
def get_ffmpeg_path():
    # 1. Check PATH
    path = shutil.which("ffmpeg")
    if path: return path

    if sys.platform == "win32":
        # Check WinGet Gyan FFmpeg (User Specific)
        user_home = os.path.expanduser("~")
        winget_base = os.path.join(user_home, "AppData", "Local", "Microsoft", "Winget", "Packages")
        if os.path.exists(winget_base):
            for d in os.listdir(winget_base):
                if "Gyan.FFmpeg" in d:
                    for bin_dir in glob.glob(os.path.join(winget_base, d, "**/bin"), recursive=True):
                        tool_path = os.path.join(bin_dir, "ffmpeg.exe")
                        if os.path.exists(tool_path): return tool_path
        # Check common hardcoded paths
        fallbacks = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"D:\Program Files\CapCut\7.7.0.3143\ffmpeg.exe",
        ]
        for fb in fallbacks:
            if os.path.exists(fb): return fb
    elif sys.platform == "darwin":
        fallbacks = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
        for fb in fallbacks:
            if os.path.exists(fb): return fb

    return "ffmpeg" # Default fallback

FFMPEG_PATH = get_ffmpeg_path()
if FFMPEG_PATH != "ffmpeg":
    print(f"📦 Found FFmpeg at: {FFMPEG_PATH}")

def detect_source_codec(video_path):
    """Detects the source video codec using ffprobe. Returns lowercase codec name."""
    try:
        ffprobe_path = FFMPEG_PATH.replace("ffmpeg", "ffprobe")
        if not os.path.exists(ffprobe_path):
            ffprobe_path = shutil.which("ffprobe") or "ffprobe"
        cmd = [
            ffprobe_path,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().lower()
    except Exception:
        pass
    return "h264"  # default assumption

def get_original_bitrate(video_path):
    """Calculates the average bitrate of the original video using a lightweight ffprobe call."""
    try:
        ffprobe_path = FFMPEG_PATH.replace("ffmpeg", "ffprobe")
        if not os.path.exists(ffprobe_path):
            ffprobe_path = shutil.which("ffprobe") or "ffprobe"

        cmd = [
            ffprobe_path,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            duration = float(result.stdout.strip())
            if duration > 0:
                file_size = os.path.getsize(video_path)
                bitrate_bps = (file_size * 8) / duration
                return int(bitrate_bps)
    except Exception as e:
        print(f"⚠️ Failed to calculate original bitrate: {e}")
    return None

def get_optimized_encoder(ffmpeg_path, quality="high", original_bitrate=None, source_codec="h264"):
    """Detects available hardware encoders and dynamically maps the target bitrate to match the original video size if original_bitrate is provided.
    For AV1/HEVC sources on Mac, uses VideoToolbox HEVC with codec-adjusted bitrate for fast GPU encoding."""
    quality = quality.lower() if quality else "high"

    # For AV1/HEVC sources on Mac: use VideoToolbox HEVC with codec-adjusted bitrate.
    # AV1 is ~2x as efficient as HEVC, HEVC is ~2x as efficient as H.264.
    # So AV1→HEVC needs ~2x bitrate (vs ~4x for AV1→H.264), making GPU encoding viable.
    if source_codec in ("av1", "hevc", "h265") and quality != "lossless":
        if sys.platform == "darwin" and original_bitrate:
            # AV1→HEVC: 2.5x (high) / 2.0x (standard). HEVC→HEVC: 1.5x (high) / 1.0x (standard)
            if source_codec == "av1":
                scale = 2.5 if quality == "high" else 2.0
            else:
                scale = 1.5 if quality == "high" else 1.0
            target_b = int(original_bitrate * scale)
            print(f"ℹ️ Source is {source_codec} — using hevc_videotoolbox at {target_b // 1000} kbps ({scale}x bitrate).")
            return "hevc_videotoolbox", ["-b:v", f"{target_b}", "-tag:v", "hvc1"]
        else:
            # No bitrate info or non-Mac: fall back to libx264 CRF
            crf_map = {"high": "20", "standard": "23"}
            crf = crf_map.get(quality, "20")
            print(f"ℹ️ Source is {source_codec} — using libx264 CRF {crf} preset=medium.")
            return "libx264", ["-preset", "medium", "-crf", crf, "-threads", "0"]
    
    # Lossless quality should always use Constant Quality/CRF/QP mode
    if quality == "lossless":
        original_bitrate = None

    # 1. Check VideoToolbox (Mac)
    if sys.platform == "darwin":
        try:
            test_cmd = [
                ffmpeg_path,
                "-v", "error",
                "-f", "lavfi",
                "-i", "nullsrc=s=128x128:d=0.1",
                "-c:v", "h264_videotoolbox",
                "-f", "null",
                "-"
            ]
            result = subprocess.run(test_cmd, capture_output=True)
            if result.returncode == 0:
                print("🚀 Hardware Acceleration (VideoToolbox) Detected!")
                if original_bitrate:
                    if quality == "lossless":
                        target_b = int(original_bitrate * 2.5)
                    elif quality == "standard":
                        target_b = int(original_bitrate * 1.2)
                    else: # "high" (visually lossless, maps 1.8x original to prevent bloat)
                        target_b = int(original_bitrate * 1.8)
                    print(f"   Target dynamic bitrate: {target_b // 1000} kbps (matching original source)")
                    return "h264_videotoolbox", ["-b:v", f"{target_b}", "-profile:v", "high"]
                else:
                    if quality == "lossless":
                        vt_q = "95"
                    elif quality == "standard":
                        vt_q = "55"
                    else: # "high"
                        vt_q = "75"
                    return "h264_videotoolbox", ["-q:v", vt_q, "-profile:v", "high"]
        except:
            pass

    try:
        # 2. Check NVENC (NVIDIA)
        if sys.platform == "win32":
            STARTUPINFO = getattr(subprocess, 'STARTUPINFO', None)
            if STARTUPINFO:
                startupinfo = STARTUPINFO()
                startupinfo.dwFlags |= getattr(subprocess, 'STARTF_USESHOWWINDOW', 0)
            else:
                startupinfo = None
        else:
            startupinfo = None

        test_cmd = [
            ffmpeg_path,
            "-v", "error",
            "-f", "lavfi",
            "-i", "nullsrc=s=128x128:d=0.1",
            "-c:v", "h264_nvenc",
            "-f", "null",
            "-"
        ]

        result = subprocess.run(test_cmd, capture_output=True, startupinfo=startupinfo)

        if result.returncode == 0:
            print("🚀 Hardware Acceleration (NVENC) Enabled & Verified!")
            if original_bitrate:
                if quality == "lossless":
                    target_b = int(original_bitrate * 2.5)
                elif quality == "standard":
                    target_b = int(original_bitrate * 1.2)
                else: # "high"
                    target_b = int(original_bitrate * 1.8)
                print(f"   Target dynamic NVENC bitrate: {target_b // 1000} kbps")
                return "h264_nvenc", ["-preset", "p4", "-b:v", f"{target_b}"]
            else:
                if quality == "lossless":
                    nv_qp = "12"
                elif quality == "standard":
                    nv_qp = "23"
                else: # "high"
                    nv_qp = "18"
                return "h264_nvenc", ["-preset", "p4", "-rc", "constqp", "-qp", nv_qp]
    except Exception as e:
        pass

    if original_bitrate:
        if quality == "lossless":
            target_b = int(original_bitrate * 2.5)
        elif quality == "standard":
            target_b = int(original_bitrate * 1.2)
        else: # "high"
            target_b = int(original_bitrate * 1.8)
        print(f"ℹ️ Using CPU encoding (libx264, preset=veryfast, bitrate={target_b // 1000} kbps) for maximum stability.")
        return "libx264", ["-preset", "veryfast", "-b:v", f"{target_b}", "-threads", "0"]
    else:
        if quality == "lossless":
            x_crf = "12"
        elif quality == "standard":
            x_crf = "23"
        else: # "high"
            x_crf = "18"
        print(f"ℹ️ Using CPU encoding (libx264, preset=veryfast, crf={x_crf}) for maximum stability.")
        return "libx264", ["-preset", "veryfast", "-crf", x_crf, "-threads", "0"]

def parse_time_str(time_str):
    """Converts HH:MM:SS.mm to seconds."""
    try:
        h, m, s = time_str.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except:
        return 0.0

def format_seconds(seconds):
    """Converts seconds to HH:MM:SS."""
    return str(datetime.timedelta(seconds=int(seconds)))


def validate_ass(ass_path):
    """
    Validates the ASS file for common timing errors.
    Returns: (is_valid, messages)
    is_valid: False if critical errors found (like Start > End).
    messages: List of warning/error strings.
    """
    errors = []
    warnings = []

    try:
        with open(ass_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        for i, line in enumerate(lines):
            if line.startswith("Dialogue:"):
                # Format: Dialogue: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
                parts = line.split(",", 9)
                if len(parts) < 10:
                    continue

                start_str = parts[1]
                end_str = parts[2]

                try:
                    start_sec = parse_time_str(start_str)
                    end_sec = parse_time_str(end_str)
                except:
                    continue

                if start_sec > end_sec:
                    errors.append(f"Line {i+1}: Start ({start_str}) > End ({end_str})")
                elif (end_sec - start_sec) > 60.0:
                    warnings.append(f"Line {i+1}: Duration > 60s ({end_sec - start_sec:.2f}s)")

    except Exception as e:
        return False, [f"Failed to read/parse ASS file: {e}"]

    if errors:
        return False, errors + warnings
    return True, warnings

class BurnProgressApp:
    def __init__(self, root, video_path, ass_path, output_path, headless=False, quality="high",
                 target_width=None, target_height=None):
        self.root = root
        self.headless = headless
        self.quality = quality
        # Optional upscaling: render subtitles on a larger canvas (target_width x
        # target_height) then encode to that size. Prevents blurry subs on
        # low-resolution source videos (360p/480p -> burn at 720p/1080p).
        self.target_width = int(target_width) if target_width else None
        self.target_height = int(target_height) if target_height else None

        self.video_path = video_path
        self.ass_path = ass_path
        self.output_path = output_path
        self.total_duration_sec = 0.0
        self.finished = False
        self.retry_count = 0
        self.max_hw_retries = 3

        if not self.headless:
            self.root.title("Hardsub Burning Progress")
            self.root.geometry("600x300") # Increased size for potential error msg
            self.root.bind('<space>', self.on_space)

            # UI Elements
            main_frame = ttk.Frame(root, padding="20")
            main_frame.pack(fill=tk.BOTH, expand=True)

            ttk.Label(main_frame, text="Burning Subtitles...", font=("Helvetica", 12, "bold")).pack(pady=(0, 10))

            info_frame = ttk.Frame(main_frame)
            info_frame.pack(fill=tk.X, pady=5)

            ttk.Label(info_frame, text=f"Input: {os.path.basename(video_path)}").pack(anchor="w")
            ttk.Label(info_frame, text=f"Output: {os.path.basename(output_path)}").pack(anchor="w")

            # Progress Bar
            self.progress_var = tk.DoubleVar()
            self.progress_bar = ttk.Progressbar(main_frame, variable=self.progress_var, maximum=100)
            self.progress_bar.pack(fill=tk.X, pady=20)

            # Stats Labels
            stats_frame = ttk.Frame(main_frame)
            stats_frame.pack(fill=tk.X)

            self.lbl_total = ttk.Label(stats_frame, text="Total Duration: Calculating...")
            self.lbl_total.grid(row=0, column=0, sticky="w", padx=5)

            self.lbl_elapsed = ttk.Label(stats_frame, text="Elapsed: 00:00:00")
            self.lbl_elapsed.grid(row=0, column=1, sticky="w", padx=5)

            self.lbl_remaining = ttk.Label(stats_frame, text="Remaining: Calculating...")
            self.lbl_remaining.grid(row=0, column=2, sticky="w", padx=5)

            # Status
            self.status_label = ttk.Label(main_frame, text="Starting...", foreground="blue")
            self.status_label.pack(pady=10)

            # Close Button (Initially Hidden)
            self.btn_close = ttk.Button(main_frame, text="Close (Space)", command=self.close_app)
        else:
            # Headless initialization
            print(f"🔥 Burning Subtitles: {os.path.basename(video_path)}")
            print(f"   Output: {os.path.basename(output_path)}")
            self.start_time = time.time()
            self.progress_var = None  # type: ignore # No GUI var

        # Validate ASS
        self.validate_and_start()

    def validate_and_start(self):
        valid, msgs = validate_ass(self.ass_path)

        if not valid:
             # Critical errors
             if self.headless:
                  print(f"Error: Critical ASS errors found: {msgs}")
                  sys.exit(1)

             msg_text = "\n".join(msgs[:10])
             if len(msgs) > 10: msg_text += "\n..."
             messagebox.showerror("Subtitle Validation Error", f"Critical errors found in ASS file:\n{msg_text}")
             self.status_label.config(text="Validation Failed.", foreground="red")
             self.btn_close.pack(pady=10)
             return

        if msgs:
             # Warnings
             if self.headless:
                  print(f"Warning: ASS validation warnings: {msgs}")
                  # Continue in headless
             else:
                  msg_text = "\n".join(msgs[:10])
                  if len(msgs) > 10: msg_text += "\n..."
                  if not messagebox.askyesno("Subtitle Validation Warning", f"Warnings found in ASS file:\n{msg_text}\n\nContinue burning?"):
                      self.status_label.config(text="Cancelled by user.", foreground="orange")
                      self.btn_close.pack(pady=10)
                      return

        # Start Process
        self.start_process()

    def start_process(self):
        self.is_running = True
        self.start_time = time.time()
        # Actually for headless we want blocking usually? Or threaded?
        # If we use Thread, we must join it or wait.
        # But existing logic uses Thread for GUI.

        if self.headless:
             self.run_ffmpeg()
        else:
             self.thread = threading.Thread(target=self.run_ffmpeg)
             self.thread.daemon = True
             self.thread.start()
             self.update_timer()

    def update_timer(self):
        if self.is_running:
            elapsed = time.time() - self.start_time
            if not self.headless:
                self.lbl_elapsed.config(text=f"Elapsed: {format_seconds(elapsed)}")
                self.root.after(1000, self.update_timer)

    def on_space(self, event):
        if self.finished:
            self.close_app()

    def close_app(self):
        self.root.destroy()
        sys.exit(0)

    def flash_window(self):
        """Flashes the window in the taskbar."""
        if sys.platform != "win32":
            return
        try:
            # The correct handle is:
            # winfo_id might return it.
            hwnd = int(self.root.wm_frame(), 16)
            windll = getattr(ctypes, 'windll', None)
            if windll:
                windll.user32.FlashWindow(hwnd, True)
        except Exception as e:
            print(f"Flash Error: {e}")

    def run_ffmpeg(self, force_cpu=False):
        out_abs_path = os.path.abspath(self.output_path)
        work_dir = os.path.dirname(out_abs_path)
        os.makedirs(work_dir, exist_ok=True)

        if os.path.exists(out_abs_path):
            try:
                os.remove(out_abs_path)
            except:
                self.update_status("Error: Output locked.", "red")
                return

        # Always make a temporary short file name to avoid FFmpeg filter parsing errors!
        import uuid
        uid = uuid.uuid4().hex[:8]
        temp_ass_name = f"tmp_sub_{uid}.ass"
        temp_ass_path = os.path.join(work_dir, temp_ass_name)

        try:
            shutil.copy2(os.path.abspath(self.ass_path), temp_ass_path)
        except Exception as e:
            self.update_status(f"Error copying subtitle: {str(e)[:50]}...", "red")
            return

        quality = getattr(self, "quality", "high")
        original_bitrate = get_original_bitrate(os.path.abspath(self.video_path))
        source_codec = detect_source_codec(os.path.abspath(self.video_path))
        print(f"   Source codec: {source_codec}")

        # Lossless quality should always use Constant Quality/CRF/QP mode to ensure no visual loss.
        if quality == "lossless":
            original_bitrate = None

        if force_cpu:
            if source_codec in ("av1", "hevc", "h265") and quality != "lossless":
                crf_map = {"high": "20", "standard": "23"}
                crf = crf_map.get(quality, "20")
                print(f"ℹ️ Source is {source_codec} — using libx264 CRF {crf} preset=medium (CPU fallback).")
                encoder_name, encoder_opts = "libx264", ["-preset", "medium", "-crf", crf, "-threads", "0"]
            elif original_bitrate:
                if quality == "lossless":
                    target_b = int(original_bitrate * 2.5)
                elif quality == "standard":
                    target_b = int(original_bitrate * 1.2)
                else: # "high"
                    target_b = int(original_bitrate * 1.8)
                encoder_name, encoder_opts = "libx264", ["-preset", "veryfast", "-b:v", f"{target_b}", "-threads", "0"]
            else:
                if quality == "lossless":
                    x_crf = "12"
                elif quality == "standard":
                    x_crf = "23"
                else: # "high"
                    x_crf = "18"
                encoder_name, encoder_opts = "libx264", ["-preset", "veryfast", "-crf", x_crf, "-threads", "0"]
        else:
            encoder_name, encoder_opts = get_optimized_encoder(FFMPEG_PATH, quality=quality, original_bitrate=original_bitrate, source_codec=source_codec)

        # Robust escaping for FFmpeg filters
        escaped_ass_path = temp_ass_name.replace('\\', '/').replace(':', '\\:')

        # Build the video filter chain. When an upscale target is set, scale the
        # source up first so subtitles render on a crisp high-res canvas (avoids
        # the blurry-subs problem on low-res sources like 360p/480p).
        if self.target_width and self.target_height:
            vf = f"scale={self.target_width}:{self.target_height},ass=filename='{escaped_ass_path}'"
        else:
            vf = f"ass=filename='{escaped_ass_path}'"

        # Pipe mode: decode to rawvideo via a first ffmpeg process, then encode
        # with VideoToolbox via a second. This bypasses the zero-copy decode path
        # that fails on some sources with malformed SEI NAL units (ffmpeg 8.x bug).
        use_pipe = getattr(self, "use_pipe_mode", False)

        if use_pipe:
            decode_cmd = [
                FFMPEG_PATH, "-y",
                "-i", os.path.abspath(self.video_path),
                "-vf", vf,
                "-c:v", "rawvideo",
                "-c:a", "pcm_s16le",
                "-f", "nut",
                "-",
            ]
            encode_cmd = [
                FFMPEG_PATH, "-y",
                "-i", "-",
                "-c:a", "aac", "-b:a", "128k",
                "-c:v", encoder_name,
            ]
            encode_cmd.extend(encoder_opts)
            encode_cmd.extend(["-sn", os.path.abspath(self.output_path)])
            cmd = None  # signal pipe mode
        else:
            cmd = [
                FFMPEG_PATH,
                "-y",
                "-i", os.path.abspath(self.video_path),
                "-vf", vf,
                "-c:a", "copy",
                "-c:v", encoder_name
            ]
            cmd.extend(encoder_opts)

            cmd.extend([
                "-sn",
                os.path.abspath(self.output_path)
            ])

        self.temp_ass_path = temp_ass_path
        status_msg = f"Running {encoder_name}..."
        if self.retry_count > 0:
            status_msg = f"Retrying HW ({self.retry_count}/{self.max_hw_retries})..."
        self.update_status(status_msg, "blue")

        if sys.platform == "win32":
            STARTUPINFO = getattr(subprocess, 'STARTUPINFO', None)
            if STARTUPINFO:
                startupinfo = STARTUPINFO()
                startupinfo.dwFlags |= getattr(subprocess, 'STARTF_USESHOWWINDOW', 0)
            else:
                startupinfo = None
        else:
            startupinfo = None

        try:
            if cmd is None:
                # Pipe mode: launch decode and encode processes, connect stdout->stdin
                import subprocess as sp
                self._decode_proc = sp.Popen(
                    decode_cmd,
                    cwd=work_dir,
                    stdout=sp.PIPE,
                    stderr=sp.PIPE,
                    text=True,
                    encoding='utf-8',
                    errors='ignore',
                )
                self.process = sp.Popen(
                    encode_cmd,
                    cwd=work_dir,
                    stdin=self._decode_proc.stdout,
                    stdout=sp.PIPE,
                    stderr=sp.STDOUT,
                    text=True,
                    encoding='utf-8',
                    errors='ignore',
                    startupinfo=startupinfo
                )
                # Close encode's copy of decode stdout so decode can get EOF
                self._decode_proc.stdout.close()
            else:
                self.process = subprocess.Popen(
                    cmd,
                    cwd=work_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8',
                    errors='ignore',
                    startupinfo=startupinfo
                )
        except Exception as e:
            self.update_status(f"Launch Error: {e}", "red")
            if hasattr(self, 'temp_ass_path') and os.path.exists(self.temp_ass_path):
                try: os.remove(self.temp_ass_path)
                except: pass
            return

        time_pattern = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d{2})")
        duration_pattern = re.compile(r"Duration: (\d{2}:\d{2}:\d{2}\.\d{2})")

        error_log = []
        while True:
            if self.process.stdout is None:
                break
            line = self.process.stdout.readline()
            if not line and self.process.poll() is not None:
                break
            if line:
                error_log.append(line.strip())
                if len(error_log) > 100:
                    error_log.pop(0)

                if self.total_duration_sec == 0.0:
                    dur_match = duration_pattern.search(line)
                    if dur_match:
                        self.total_duration_sec = parse_time_str(dur_match.group(1))
                        if not self.headless:
                            self.root.after(0, lambda: self.lbl_total.config(text=f"Total: {format_seconds(self.total_duration_sec)}"))
                        else:
                            print(f"   Total Duration: {format_seconds(self.total_duration_sec)}")

                time_match = time_pattern.search(line)
                if time_match and self.total_duration_sec > 0:
                    current_time_sec = parse_time_str(time_match.group(1))
                    percentage = (current_time_sec / self.total_duration_sec) * 100

                    elapsed = time.time() - self.start_time
                    if percentage > 0:
                        eta_sec = (elapsed / percentage) * 100 - elapsed
                        eta_str = format_seconds(eta_sec)
                    else:
                        eta_str = "Calculating..."

                    if not self.headless:
                        self.root.after(0, lambda p=percentage, e=eta_str: self.update_progress(p, e))
                    else:
                        print(f"Progress: {percentage:.1f}% (ETA: {eta_str})")
                        sys.stdout.flush()

        ret_code = self.process.poll()
        self.is_running = False

        if hasattr(self, 'temp_ass_path') and os.path.exists(self.temp_ass_path):
            try: os.remove(self.temp_ass_path)
            except: pass

        if ret_code == 0:
            self.finished = True
            self.update_status("Burning Completed! Press SPACE to close.", "green")
            if not self.headless:
                self.root.after(0, self.show_completion_ui)
        else:
            # Handle Retry Logic
            if encoder_name != "libx264" and self.retry_count < self.max_hw_retries:
                self.retry_count += 1
                print(f"⚠️ FFmpeg (Hardware) failed with code {ret_code}. Wait 3s and retrying ({self.retry_count}/{self.max_hw_retries})...")
                time.sleep(3)
                self.run_ffmpeg(force_cpu=False) # Retry same hardware
            elif encoder_name != "libx264" and self.retry_count >= self.max_hw_retries and not getattr(self, "use_pipe_mode", False):
                # Try pipe mode (rawvideo bridge) before giving up on GPU entirely.
                # This bypasses the zero-copy decode path that fails on some sources.
                print("❌ All Hardware retries failed. Trying rawvideo pipe mode...")
                self.use_pipe_mode = True
                self.retry_count = 0
                self.run_ffmpeg(force_cpu=False)
            elif encoder_name != "libx264" and getattr(self, "use_pipe_mode", False):
                print("❌ Pipe mode also failed. Falling back to CPU for final attempt...")
                self.use_pipe_mode = False
                self.run_ffmpeg(force_cpu=True)
            else:
                self.update_status(f"Error Code: {ret_code}", "red")
                if self.headless:
                    print("\n--- FFmpeg Error Output ---")
                    print("\n".join(error_log))
                if not self.headless:
                    self.root.after(0, lambda: messagebox.showerror("FFmpeg Error", f"FFmpeg exited with code {ret_code}.\nCheck console output."))

    def show_completion_ui(self):
        """Updates UI for completion: Shows Close button and Flashes window."""
        if self.root:
             self.btn_close.pack(pady=10)
             self.btn_close.focus_set()
             self.flash_window()

    def update_progress(self, percentage, eta):
        if not self.headless and self.progress_var:
             self.progress_var.set(percentage)
             self.lbl_remaining.config(text=f"Remaining: {eta}")

    def update_status(self, text, color):
        if self.root:
             self.root.after(0, lambda: self.status_label.config(text=text, foreground=color))

if __name__ == "__main__":
    if len(sys.argv) < 4:
        # Fallback for testing/debugging info
        print("Usage: python burn_engine.py <video> <ass> <output>")
        print("Missing arguments. Opening dummy window.")
        # sys.exit(1) # Commented out to allow import testing or dev

    video = sys.argv[1] if len(sys.argv) > 1 else "video.mp4"
    ass = sys.argv[2] if len(sys.argv) > 2 else "subs.ass"
    out = sys.argv[3] if len(sys.argv) > 3 else "out.mp4"

    quality = "high"
    if "--quality" in sys.argv:
        try:
            idx = sys.argv.index("--quality")
            quality = sys.argv[idx + 1]
        except Exception:
            pass

    target_width = None
    target_height = None
    for flag in ["--target-width", "--target-height"]:
        if flag in sys.argv:
            try:
                idx = sys.argv.index(flag)
                val = int(sys.argv[idx + 1])
                if flag == "--target-width":
                    target_width = val
                else:
                    target_height = val
            except Exception:
                pass

    if "--headless" in sys.argv:
         # Headless mode: No GUI
         app = BurnProgressApp(None, video, ass, out, headless=True, quality=quality,
                               target_width=target_width, target_height=target_height)
         # In headless mode, start_process calls run_ffmpeg synchronously
         if not app.finished:
             sys.exit(1)
    else:
         root = tk.Tk()
         app = BurnProgressApp(root, video, ass, out, quality=quality,
                               target_width=target_width, target_height=target_height)
         root.mainloop()
