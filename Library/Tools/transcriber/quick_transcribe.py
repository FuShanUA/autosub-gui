
import os
import sys
import re
import json
import argparse
import subprocess
from typing import List, Dict

# Setup paths
if getattr(sys, 'frozen', False):
    TOOLS_DIR = os.path.join(getattr(sys, '_MEIPASS', ''), "Library", "Tools")
else:
    TOOLS_DIR = os.environ.get("AUTOSUB_TOOLS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(TOOLS_DIR, "common"))

try:
    import llm_utils
    import srt_utils
except ImportError:
    print("❌ Critical utilities missing.")
    sys.exit(1)

import tempfile
import shutil
import contextlib

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

def fetch_youtube_subs(url, output_dir, cookies=None):
    """
    Downloads English subtitles from YouTube using yt-dlp.
    """
    ytdlp_exe = os.path.join(TOOLS_DIR, "vdown", "yt-dlp.exe")
    cmd = [
        ytdlp_exe,
        "--skip-download",
        "--write-auto-subs",
        "--write-subs",
        "--sub-langs", "en.*",
        "--sub-format", "srt/vtt/best",
        "--output", os.path.join(output_dir, "yt_subs"),
        url
    ]

    print(f"📡 Fetching YouTube subtitles...")
    try:
        with use_temp_cookies(cookies) as temp_cookies:
            if temp_cookies:
                cmd.extend(["--cookies", temp_cookies])
            subprocess.run(cmd, check=True, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)

        # Find the downloaded file
        for f in os.listdir(output_dir):
            if f.startswith("yt_subs.") and f.endswith((".srt", ".vtt")):
                return os.path.join(output_dir, f)
    except Exception as e:
        print(f"❌ Failed to fetch subtitles: {e}")
    return None

def resegment_with_llm(input_srt, output_srt, model="glm-5.3"):
    """
    Uses LLM to re-segment and polish original YouTube subtitles.
    """
    blocks = srt_utils.parse_srt(input_srt)
    if not blocks:
        print("❌ Failed to parse input subtitles.")
        return False

    # Group into larger chunks to give LLM context (e.g., 2-3 minute chunks)
    # 100 blocks is roughly 5-8 minutes
    BATCH_SIZE = 80
    total_blocks = len(blocks)
    resegmented_blocks = []

    client = llm_utils.get_client()

    print(f"🧠 Re-segmenting {total_blocks} blocks via {model}...")

    for i in range(0, total_blocks, BATCH_SIZE):
        chunk = blocks[i : i + BATCH_SIZE]

        # Prepare text with indices for the LLM
        # Format: [HH:MM:SS,ms] Text
        input_text = ""
        for b in chunk:
            start_t = b['start']
            h, m, s, ms = int(start_t//3600), int((start_t%3600)//60), int(start_t%60), int((start_t%1)*1000)
            timestamp = f"{h:02}:{m:02}:{s:02},{ms:03}"
            text = " ".join(b['lines']).replace("\n", " ").strip()
            input_text += f"[{timestamp}] {text}\n"

        prompt = f"""
You are a professional video editor and subtitle specialist.
I have a raw transcript from YouTube with rough timestamps.
Your task is to RE-SEGMENT this transcript into high-quality, readable English subtitles (SRT format).

### CONSTRAINTS:
1. **Readable Length**: Each subtitle should be 40-80 characters.
2. **Logical Grouping**: Break segments at natural pauses, ends of sentences, or logical phrase boundaries.
3. **Timestamp Accuracy**: Keep the start and end times consistent with the flow of speech provided in the input.
4. **SRT Output**: Output ONLY the valid SRT content. No conversational filler.

INPUT (Timestamps mark the start of the original segments):
{input_text}

OUTPUT:
(Valid SRT format)
"""
        try:
            res = client.generate_content(prompt, model_name=model)
            if res:
                # Basic cleaning of LLM output (remove markdown code blocks)
                res = re.sub(r'```srt\n|```', '', res).strip()
                # Parse the returned SRT chunk
                chunk_blocks = srt_utils.parse_srt(res)
                if chunk_blocks:
                    resegmented_blocks.extend(chunk_blocks)
                else:
                    # Fallback to original if parsing fails
                    print(f"⚠️ Failed to parse LLM output for chunk {i//BATCH_SIZE}, using original.")
                    resegmented_blocks.extend(chunk)
            else:
                resegmented_blocks.extend(chunk)
        except Exception as e:
            print(f"❌ Error during LLM re-segmentation: {e}")
            resegmented_blocks.extend(chunk)

    # Renumber blocks
    for idx, b in enumerate(resegmented_blocks, 1):
        b['index'] = idx

    srt_utils.write_srt(resegmented_blocks, output_srt)
    return True

def main():
    parser = argparse.ArgumentParser(description="YouTube Quick Transcribe (Experimental)")
    parser.add_argument("url_or_file", help="YouTube URL or local video file")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--cookies", help="Path to cookies.txt")
    parser.add_argument("--model", default="glm-5.3", help="LLM model")
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)

    input_path = args.url_or_file
    is_url = input_path.startswith(("http://", "https://"))

    # 1. Get Subtitles
    raw_sub_path = None
    if is_url:
        raw_sub_path = fetch_youtube_subs(input_path, args.output, args.cookies)
    else:
        # Check if subtitles already exist in the same folder
        base = os.path.splitext(input_path)[0]
        for ext in [".en.srt", ".en.vtt", ".srt", ".vtt"]:
            if os.path.exists(base + ext):
                raw_sub_path = base + ext
                break

        if not raw_sub_path:
            # Try to fetch from URL if we can find one (e.g. from task metadata)
            # For simplicity, we just look for 'yt_subs' in the directory
            for f in os.listdir(args.output):
                if f.startswith("yt_subs.") and f.endswith((".srt", ".vtt")):
                    raw_sub_path = os.path.join(args.output, f)
                    break

    if not raw_sub_path:
        print("❌ No English subtitles found for this video.")
        sys.exit(1)

    # 2. Re-segment
    video_name = os.path.splitext(os.path.basename(input_path))[0]
    final_srt_path = os.path.join(args.output, f"{video_name}.srt")

    success = resegment_with_llm(raw_sub_path, final_srt_path, model=args.model)
    if success:
        print(f"✅ Quick Transcription complete: {final_srt_path}")
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
