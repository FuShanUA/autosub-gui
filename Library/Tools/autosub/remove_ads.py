"""
remove_ads.py - Auto-detect advertising segments and cut them out of media + subtitles.

Reuses detect_ad_segments() from smart_translate for LLM-based ad detection, then:
  1. Trims ad spans from the source media (mp3/mp4/m4a) via ffmpeg.
  2. Rewrites every SRT/ASS subtitle in the folder: drops ad blocks and re-stamps
     timecodes against the new (shorter) timeline.

Usage:
  python remove_ads.py <project_dir_or_srt> [--media <path>] [--model <m>] [--provider <p>]
                      [--reencode] [--dry-run] [--keep-originals]

If a <name>.ad_segments.json already exists in the project folder, detection is
skipped and that file is reused (unless --redetect).
"""

import os
import sys
import re
import json
import argparse
import subprocess
import tempfile
import shutil
from collections import Counter


if getattr(sys, "frozen", False):
    BUNDLE_DIR = getattr(sys, "_MEIPASS", "")
    TOOLS_DIR = os.path.join(BUNDLE_DIR, "Library", "Tools")
    ENV_PATH = os.path.join(os.path.expanduser("~"), "Documents", "AutoSub", ".env")
else:
    TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(TOOLS_DIR)), ".env")

sys.path.append(os.path.join(TOOLS_DIR, "subtranslator", "lib"))
sys.path.append(os.path.join(TOOLS_DIR, "common"))

from llm_utils import LLMClient, LLMProvider  # noqa: E402
import srt_utils  # noqa: E402
from smart_translate import detect_ad_segments  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
except ImportError:
    pass


def _resolve_media_tool(name):
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "darwin":
        for bin_dir in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"):
            candidate = os.path.join(bin_dir, name)
            if os.path.exists(candidate):
                return candidate
    return name


FFMPEG = os.environ.get("FFMPEG_PATH") or _resolve_media_tool("ffmpeg")
FFPROBE = os.environ.get("FFPROBE_PATH") or _resolve_media_tool("ffprobe")


def fmt_srt(t):
    """Seconds -> 'HH:MM:SS,mmm' (SRT timestamp, comma decimal sep)."""
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    rem = t - h * 3600
    m = int(rem // 60)
    s = rem - m * 60
    sec = int(s)
    ms = int(round((s - sec) * 1000))
    if ms >= 1000:
        ms -= 1000
        sec += 1
        if sec >= 60:
            sec = 0
            m += 1
            if m >= 60:
                m = 0
                h += 1
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def fmt_ass(t):
    """Seconds -> 'H:MM:SS.cc' (ASS timestamp, dot centi-sec, unzero-padded hour)."""
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    rem = t - h * 3600
    m = int(rem // 60)
    s = rem - m * 60
    sec = int(s)
    cs = int(round((s - sec) * 100))
    if cs >= 100:
        cs -= 100
        sec += 1
        if sec >= 60:
            sec = 0
            m += 1
            if m >= 60:
                m = 0
                h += 1
    return f"{h:d}:{m:02d}:{sec:02d}.{cs:02d}"


def parse_ass_time(s):
    """'0:00:02.89' -> seconds (float)."""
    s = s.strip()
    parts = s.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    else:
        return float(parts[0])


def merge_ad_segments(ad_segments):
    """Sort + merge overlapping/adjacent ad spans -> [(start, end), ...]."""
    spans = []
    for seg in ad_segments:
        s = float(seg.get("start_time", seg.get("start", 0)))
        e = float(seg.get("end_time", seg.get("end", 0)))
        if e <= s:
            continue
        spans.append((s, e))
    spans.sort(key=lambda x: x[0])
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def expand_ad_boundaries(merged, blocks, gap_threshold=3.0):
    """Extend each ad span outward to absorb silent ad visuals (B-roll,
    transition cards) that have no subtitle text.

    Ad reads are often surrounded by a few seconds of sponsor visuals with
    no voiceover.  The LLM detects ad *blocks* (which have subtitles), but the
    gap between the last content block and the first ad block -- or between
    the last ad block and the next content block -- is also part of the ad.

    For each ad span we look at the neighbouring content blocks.  If the time
    gap between the ad edge and the neighbour exceeds *gap_threshold* seconds,
    we extend the ad edge to the neighbour's boundary, effectively absorbing
    the silent gap into the cut.
    """
    return _expand_ad_boundaries_impl(merged, blocks, gap_threshold, padding=3.0)


def _expand_ad_boundaries_impl(merged, blocks, gap_threshold=3.0, padding=0.0):
    """Implementation with optional padding (seconds added to each side)."""
    if not merged or not blocks:
        return merged

    # Build a sorted list of (start, end) for every block.
    blk_times = sorted(
        ((float(b.get("start", 0)), float(b.get("end", 0))) for b in blocks),
        key=lambda x: x[0],
    )

    expanded = []
    for ad_s, ad_e in merged:
        new_s = ad_s
        new_e = ad_e

        # --- Extend start backward ---
        # Find the last content block that ends before the ad starts.
        prev_end = None
        for bs, be in blk_times:
            if be <= ad_s:
                prev_end = be  # keep updating; we want the closest one
            elif bs >= ad_s:
                break
        if prev_end is not None and (ad_s - prev_end) > gap_threshold:
            new_s = prev_end

        # --- Extend end forward ---
        # Find the first content block that starts after the ad ends.
        next_start = None
        for bs, be in blk_times:
            if bs >= ad_e:
                next_start = bs
                break
        if next_start is not None and (next_start - ad_e) > gap_threshold:
            new_e = next_start

        # --- Apply padding ---
        # Start padding: clamp to prev_end so we never cut dialogue audio.
        if prev_end is not None:
            new_s = max(new_s - padding, prev_end)
        else:
            new_s = max(new_s - padding, 0.0)

        # End padding: ad visuals (B-roll, transition cards) often linger
        # past the first post-ad subtitle block.  We extend the cut forward
        # but NEVER past the SECOND post-ad block's start -- that block is
        # guaranteed pure content, so cutting into it would drop dialogue.
        # This lets us absorb ad visuals from the first post-ad block
        # (which is often a mixed ad-cleanup + content-resume block) while
        # protecting real content.
        if next_start is not None:
            # Find the second post-ad block start.
            next_next_start = None
            found_first = False
            for bs, be in blk_times:
                if bs >= ad_e:
                    if not found_first:
                        found_first = True
                        continue
                    next_next_start = bs
                    break
            if next_next_start is not None:
                new_e = min(new_e + padding, next_next_start)
            else:
                new_e = new_e + padding
        else:
            new_e = new_e + padding

        expanded.append((new_s, new_e))

    # Re-merge in case expansion caused overlaps.
    expanded.sort(key=lambda x: x[0])
    final = []
    for s, e in expanded:
        if final and s <= final[-1][1]:
            final[-1] = (final[-1][0], max(final[-1][1], e))
        else:
           final.append((s, e))
    return final


def refine_boundaries_with_vad(merged, media_path, search_window=15.0):
    """Use Voice Activity Detection to snap ad boundaries to speech edges.

    Stream-copy cutting is limited to keyframe positions, but the real
    problem is knowing *where* speech resumes after an ad and *where* it
    ends before one.  VAD gives us exact speech timestamps, so we can:

    - For ad START: find the last speech segment that ends before the ad,
      and snap the cut to just after it (protecting the last word of dialogue).
    - For ad END: find the first speech segment that starts after the ad,
      and snap the cut to just before it (protecting the first word of
      resumed dialogue, while cutting any silent ad visuals before it).

    This keeps the gap between content and ad at <1s of silence instead
    of the 3-6s of ad B-roll that subtitle-based boundaries leave behind.
    """
    if not merged or not media_path:
        return merged
    try:
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import get_speech_timestamps, VadOptions
    except ImportError:
        print("   ⚠️ faster_whisper VAD not available, skipping boundary refinement")
        return merged

    # Decode the full audio once (16kHz mono, as expected by Silero VAD).
    try:
        audio = decode_audio(media_path)
    except Exception as e:
        print(f"   ⚠️ VAD audio decode failed ({e}), skipping refinement")
        return merged

    SR = 16000  # sample rate used by decode_audio
    vad_opts = VadOptions(min_silence_duration_ms=300, speech_pad_ms=200,
                          max_speech_duration_s=30)
    try:
        vad_segs = get_speech_timestamps(audio, vad_opts)
    except Exception as e:
        print(f"   ⚠️ VAD failed ({e}), skipping refinement")
        return merged

    # Convert VAD segments (sample offsets) to seconds.
    speech = []
    for seg in vad_segs:
        s = seg.get("start", 0) / SR
        e = seg.get("end", 0) / SR
        if e - s >= 0.3:  # ignore tiny blips
            speech.append((s, e))

    if not speech:
        print("   ⚠️ VAD found no speech, skipping refinement")
        return merged

    print(f"   🎙️ VAD: {len(speech)} speech segments. Refining {len(merged)} ad boundary(ies)...")

    refined = []
    for ad_s, ad_e in merged:
        new_s = ad_s
        new_e = ad_e

        # --- Refine START: snap to last speech ending before ad ---
        # Find speech segments in [ad_s - search_window, ad_s + 2]
        last_speech_end = None
        for ss, se in speech:
            if se <= ad_s + 2 and ss >= ad_s - search_window:
                last_speech_end = se  # closest one before ad
        if last_speech_end is not None and last_speech_end < ad_s:
            # Cut just after the last speech ends, with 0.3s safety margin
            new_s = last_speech_end + 0.3

        # --- Refine END: snap to first speech start after ad ---
        # Find speech segments in [ad_e - 2, ad_e + search_window]
        first_speech_start = None
        for ss, se in speech:
            if ss >= ad_e - 2 and ss <= ad_e + search_window:
                first_speech_start = ss
                break
        if first_speech_start is not None and first_speech_start > ad_e:
            # Cut just before speech resumes, with 0.3s safety margin
            new_e = first_speech_start - 0.3

        refined.append((new_s, new_e))

    # Re-merge
    refined.sort(key=lambda x: x[0])
    final = []
    for s, e in refined:
        if final and s <= final[-1][1]:
            final[-1] = (final[-1][0], max(final[-1][1], e))
        else:
            final.append((s, e))
    return final


def build_remap(merged):
    """Return (remap_fn, total_removed). remap_fn(t) collapses ad spans out of t."""
    total = sum(e - s for s, e in merged)

    def remap(t):
        off = 0.0
        for s, e in merged:
            if t >= e:
                off += e - s
            elif t > s:
                return s - off
        return t - off

    return remap, total


def compute_keep_segments(merged, total_duration):
    """Inverse of ad spans -> the [start,end) ranges we keep from the media."""
    keep = []
    prev = 0.0
    for s, e in merged:
        if s > prev:
            keep.append((prev, s))
        prev = e
    if total_duration is None or prev < total_duration:
        keep.append((prev, total_duration))
    return keep


def block_in_ad(start, end, merged):
    """True if a subtitle block overlaps any ad span (drop it entirely)."""
    for s, e in merged:
        if start < e and end > s:
            return True
    return False


def noads_path(path):
    """Output path inside a 'noads' subdirectory next to the original file.
    e.g. /proj/xxx.en.srt -> /proj/noads/xxx.en.srt
    Keeps the original filename intact so autosub's filename-based stage
    detection still recognizes .bi.ass / .en.srt etc.
    """
    d = os.path.dirname(path)
    fn = os.path.basename(path)
    out_dir = os.path.join(d, "noads")
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, fn)


def probe_duration(path):
    try:
        out = subprocess.check_output(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        if out:
            return float(out)
        return None
    except Exception:
        return None


def probe_has_video(path):
    try:
        out = subprocess.check_output(
            [FFPROBE, "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", path],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return bool(out)
    except Exception:
        return False


def probe_stream_layout(path):
    """Return (video_count, audio_count) for the first input file."""
    try:
        out = subprocess.check_output(
            [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", path],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        types = [line.strip() for line in out.splitlines() if line.strip()]
        return types.count("video"), types.count("audio")
    except Exception:
        return 0, 0


def probe_packets(path, stream_type):
    """Read packet timestamps without decoding the media."""
    cmd = [
        FFPROBE, "-v", "error", "-select_streams", f"{stream_type}:0",
        "-show_entries", "packet=pts_time,dts_time,duration_time,flags",
        "-of", "csv=p=0", path,
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.PIPE).decode()
    packets = []
    for line in out.splitlines():
        fields = line.split(",")
        if len(fields) < 4 or not fields[0]:
            continue
        try:
            pts = float(fields[0])
            dts = float(fields[1]) if fields[1] and fields[1] != "N/A" else None
            duration = float(fields[2]) if fields[2] else 0.0
        except ValueError:
            continue
        packets.append({
            "pts": pts,
            "dts": dts,
            "duration": duration,
            "keyframe": "K" in fields[3],
        })
    return packets


def detect_visual_ad_segments(media_path, min_duration=3.0, max_duration=120.0):
    """Detect resolution-anomaly ad candidates from video keyframes.

    Streamed ad inserts often change the video resolution (for example
    1920x800 feature to 1920x1080 ad). Scanning only keyframes keeps this
    cheap while still finding the transition points that stream-copy can cut.
    """
    if not media_path or not probe_has_video(media_path):
        return []

    cmd = [
        FFPROBE, "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
        "-show_entries", "frame=pts_time,width,height", "-of", "csv=p=0",
        media_path,
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.PIPE).decode()
    except Exception as e:
        print(f"   ⚠️ Visual keyframe scan failed ({e}), skipping visual detector")
        return []

    frames = []
    for line in out.splitlines():
        fields = line.split(",")
        if len(fields) < 3:
            continue
        try:
            frames.append((float(fields[0]), int(fields[1]), int(fields[2])))
        except ValueError:
            continue
    if len(frames) < 3:
        return []

    resolution_counts = Counter((w, h) for _, w, h in frames)
    main_resolution = resolution_counts.most_common(1)[0][0]
    duration = probe_duration(media_path) or frames[-1][0]

    candidates = []
    start = None
    anomaly_count = 0
    for t, w, h in frames:
        if (w, h) == main_resolution:
            if start is not None:
                candidates.append((start, t, anomaly_count))
                start = None
                anomaly_count = 0
            continue
        if start is None:
            start = t
        anomaly_count += 1
    if start is not None:
        candidates.append((start, duration, anomaly_count))

    segments = []
    for s, e, count in candidates:
        if count < 2 and (e - s) >= min_duration:
            # A single keyframe can still be a short ad when GOPs are long.
            pass
        elif count < 2:
            continue
        if min_duration <= e - s <= max_duration:
            segments.append((s, e))

    if segments:
        print(f"   🖥️ Visual detector: main resolution {main_resolution[0]}x{main_resolution[1]}")
        for s, e in segments:
            print(f"      candidate {s:.3f}-{e:.3f} ({e - s:.3f}s)")
    return segments


def _keep_arg(seg):
    s, e = seg
    if e is None:
        return f"{s}"
    return f"{s}:{e}"


def _run_checked(cmd, label):
    proc = subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()
        tail = "\n".join(detail[-8:]) if detail else "no ffmpeg stderr"
        raise RuntimeError(f"{label} failed:\n{tail}")


def _write_concat_list(path, files):
    with open(path, "w", encoding="utf-8") as f:
        for file_path in files:
            escaped = str(file_path).replace("\\", "\\\\").replace("'", "\\'")
            f.write(f"file '{escaped}'\n")


def _precise_segment_plan(video_packets, audio_packets, seg):
    start, end = seg
    end_time = float("inf") if end is None else float(end)

    keyframe_indices = [
        i for i, p in enumerate(video_packets)
        if p["keyframe"] and p["pts"] >= start and p["pts"] < end_time
    ]
    if not keyframe_indices:
        return None
    keyframe_index = keyframe_indices[0]
    keyframe = video_packets[keyframe_index]
    video_start = keyframe["pts"]
    video_seek = keyframe["dts"] if keyframe["dts"] is not None else video_start

    last_index = keyframe_index - 1
    for i in range(keyframe_index, len(video_packets)):
        if video_packets[i]["pts"] >= end_time:
            break
        last_index = i
    video_count = last_index - keyframe_index + 1
    if video_count <= 0:
        return None

    audio_start = None
    audio_count = 0
    if audio_packets is not None:
        audio_candidates = [p for p in audio_packets if p["pts"] >= video_start]
        if not audio_candidates:
            return None
        audio_start = audio_candidates[0]["pts"]
        audio_count = sum(
            1 for p in audio_packets
            if p["pts"] >= audio_start and p["pts"] < end_time
        )
        if audio_count <= 0:
            return None

    return {
        "source": seg,
        "video_start": video_start,
        "video_seek": video_seek,
        "video_count": video_count,
        "audio_start": audio_start,
        "audio_count": audio_count,
    }


def _cut_media_precise(media_path, keep_segments, out_path):
    """Stream-copy video and audio separately, then mux the two timelines.

    Video starts on the first keyframe at or after each requested boundary so
    a copied segment remains independently decodable. Audio is aligned to that
    video start and cut by packet count. This avoids the keyframe rollback and
    packet over-read that occur when both streams are copied together.
    """
    video_count, audio_count = probe_stream_layout(media_path)
    if video_count != 1:
        raise RuntimeError("Precise copy requires exactly one video stream")
    if audio_count > 1:
        raise RuntimeError("Precise copy does not support multiple audio streams")

    video_packets = probe_packets(media_path, "v")
    audio_packets = probe_packets(media_path, "a") if audio_count == 1 else None
    if not video_packets:
        raise RuntimeError("No video packet timestamps found for precise copy")

    plans = []
    for seg in keep_segments:
        plan = _precise_segment_plan(video_packets, audio_packets, seg)
        if plan is None:
            boundary = f"{seg[0]:.3f}-{seg[1]:.3f}" if seg[1] is not None else f"{seg[0]:.3f}-end"
            raise RuntimeError(f"No precise-copy packets in keep segment {boundary}")
        plans.append(plan)

    ext = os.path.splitext(media_path)[1].lower() or ".mp4"
    with tempfile.TemporaryDirectory(prefix="removeads_precise_") as tmpdir:
        video_parts = []
        audio_parts = []
        for i, plan in enumerate(plans):
            video_part = os.path.join(tmpdir, f"v{i:03d}{ext}")
            # ffmpeg selects stream-copy input points by DTS. Seek just before
            # the keyframe DTS so timestamp rounding cannot skip that packet.
            video_seek = max(0.0, plan["video_seek"] - 0.01)
            video_cmd = [
                FFMPEG, "-y", "-seek2any", "1", "-ss", f"{video_seek:.6f}",
                "-i", media_path, "-map", "0:v:0", "-frames:v", str(plan["video_count"]),
                "-an", "-c:v", "copy", "-avoid_negative_ts", "make_zero", video_part,
            ]
            _run_checked(video_cmd, f"precise video segment {i + 1}")
            video_parts.append(video_part)

            if plan["audio_start"] is not None:
                audio_part = os.path.join(tmpdir, f"a{i:03d}.mka")
                audio_cmd = [
                    FFMPEG, "-y", "-i", media_path, "-ss", f"{plan['audio_start']:.6f}",
                    "-map", "0:a:0", "-frames:a", str(plan["audio_count"]),
                    "-vn", "-c:a", "copy", "-avoid_negative_ts", "make_zero",
                    audio_part,
                ]
                _run_checked(audio_cmd, f"precise audio segment {i + 1}")
                audio_parts.append(audio_part)

        video_list = os.path.join(tmpdir, "video.txt")
        _write_concat_list(video_list, video_parts)
        if audio_parts:
            audio_list = os.path.join(tmpdir, "audio.txt")
            _write_concat_list(audio_list, audio_parts)
            cmd = [
                FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", video_list,
                "-f", "concat", "-safe", "0", "-i", audio_list,
                "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", out_path,
            ]
        else:
            cmd = [
                FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", video_list,
                "-map", "0:v:0", "-an", "-c", "copy", out_path,
            ]
        _run_checked(cmd, "precise concat/mux")

    expected_video = sum(p["video_count"] for p in plans)
    actual_video = len(probe_packets(out_path, "v"))
    if actual_video != expected_video:
        raise RuntimeError(
            f"Precise-copy validation failed: expected {expected_video} video packets, got {actual_video}"
        )
    if audio_packets is not None:
        expected_audio = sum(p["audio_count"] for p in plans)
        actual_audio = len(probe_packets(out_path, "a"))
        if actual_audio != expected_audio:
            raise RuntimeError(
                f"Precise-copy validation failed: expected {expected_audio} audio packets, got {actual_audio}"
            )
    return out_path


def _cut_media_legacy(media_path, keep_segments, out_path):
    ext = os.path.splitext(media_path)[1]
    tmpdir = tempfile.mkdtemp(prefix="removeads_")
    parts = []
    concat_list = os.path.join(tmpdir, "concat.txt")
    for i, seg in enumerate(keep_segments):
        s, e = seg
        part = os.path.join(tmpdir, f"part_{i:03d}{ext}")
        c = [FFMPEG, "-y", "-ss", str(s)]
        if e is not None:
            c += ["-to", str(e)]
        c += ["-i", media_path, "-c", "copy", "-avoid_negative_ts", "make_zero", part]
        subprocess.run(c, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        parts.append(part)
    with open(concat_list, "w") as f:
        for p in parts:
            f.write(f"file '{p}'\n")
    cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", out_path]
    subprocess.run(cmd, check=True)
    return out_path


def cut_media(media_path, keep_segments, out_path, reencode=False, cut_mode="auto"):
    """Cut ad spans out of media. Audio-only: filter (atrim+concat). Video: stream-copy."""
    has_video = probe_has_video(media_path)
    use_filter = reencode or not has_video
    n = len(keep_segments)
    if n == 0:
        raise RuntimeError("No keep segments (whole file is ads?). Nothing to write.")

    if use_filter:
        parts = []
        labels = []
        for i, seg in enumerate(keep_segments):
            parts.append(f"[0:a]atrim={_keep_arg(seg)},asetpts=PTS-STARTPTS[a{i}]")
            labels.append(f"[a{i}]")

        if has_video:
            vparts = []
            vlabels = []
            for i, seg in enumerate(keep_segments):
                vparts.append(f"[0:v]trim={_keep_arg(seg)},setpts=PTS-STARTPTS[v{i}]")
                vlabels.append(f"[v{i}]")
            fc = ";".join(vparts + parts)
            concat_in = "".join(l + r for l, r in zip(vlabels, labels))
            fc += f";{concat_in}concat=n={n}:v=1:a=1[outv][outa]"
            cmd = [
                FFMPEG, "-y", "-i", media_path, "-filter_complex", fc,
                "-map", "[outv]", "-map", "[outa]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k", out_path,
            ]
        else:
            fc = ";".join(parts)
            concat_in = "".join(labels)
            fc += f";{concat_in}concat=n={n}:v=0:a=1[outa]"
            ext = os.path.splitext(out_path)[1].lower()
            if ext == ".mp3":
                ac = ["-c:a", "libmp3lame", "-b:a", "192k"]
            else:
                ac = ["-c:a", "aac", "-b:a", "192k"]
            cmd = [
                FFMPEG, "-y", "-i", media_path, "-filter_complex", fc,
                "-map", "[outa]",
            ] + ac + [out_path]
        print(f"   ffmpeg: re-encode (filter) | {n} segment(s)")
        subprocess.run(cmd, check=True)
        return out_path
    else:
        if cut_mode in ("auto", "precise"):
            try:
                print(f"   ffmpeg: precise stream-copy (split A/V) | {n} segment(s)")
                return _cut_media_precise(media_path, keep_segments, out_path)
            except Exception as e:
                if cut_mode == "precise":
                    raise
                print(f"   ⚠️ Precise copy unavailable, falling back to legacy concat: {e}")
        print(f"   ffmpeg: stream-copy (concat) | {n} segment(s)")
        return _cut_media_legacy(media_path, keep_segments, out_path)


def rewrite_srt_file(srt_path, merged, out_path):
    blocks = srt_utils.parse_srt(srt_path)
    remap, total = build_remap(merged)
    out = []
    for b in blocks:
        bs, be = b["start"], b["end"]
        if block_in_ad(bs, be, merged):
            continue
        ns, ne = remap(bs), remap(be)
        nb = dict(b)
        nb["start"] = ns
        nb["end"] = ne
        nb["time"] = f"{fmt_srt(ns)} --> {fmt_srt(ne)}"
        out.append(nb)
    srt_utils.write_srt(out, out_path)
    return len(blocks), len(out)


def rewrite_ass_file(ass_path, merged, out_path):
    remap, total = build_remap(merged)
    dialogue_re = re.compile(r"^(Dialogue|Comment):\s*(\d+),([^,]+),([^,]+),")
    kept = 0
    dropped = 0
    with open(ass_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    out_lines = []
    for line in lines:
        m = dialogue_re.match(line.rstrip("\n"))
        if not m:
            out_lines.append(line)
            continue
        kind = m.group(1)
        start_s = parse_ass_time(m.group(3))
        end_s = parse_ass_time(m.group(4))
        if block_in_ad(start_s, end_s, merged):
            dropped += 1
            continue
        ns, ne = remap(start_s), remap(end_s)
        prefix = f"{kind}: {m.group(2)},{fmt_ass(ns)},{fmt_ass(ne)},"
        rest = line.rstrip("\n")
        comma_positions = [i for i, c in enumerate(rest) if c == ","]
        if len(comma_positions) >= 3:
            tail = rest[comma_positions[2] + 1:]
        else:
            tail = ""
        out_lines.append(prefix + tail + "\n")
        kept += 1
    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(out_lines)
    return dropped + kept, kept


def find_main_srt(project_dir):
    """Pick the SRT to feed ad-detection on: prefer .en.srt, else base .srt."""
    name = None
    for fn in os.listdir(project_dir):
        if "_noads" in fn or fn == "noads":
            continue
        if fn.endswith(".en.srt"):
            return os.path.join(project_dir, fn)
        if not fn.endswith(".srt"):
            continue
        if fn.endswith((".cn.srt", ".bi.srt", ".zh.srt")):
            continue
        name = os.path.join(project_dir, fn)
    return name


def find_all_subs(project_dir):
    subs = []
    for fn in sorted(os.listdir(project_dir)):
        if "_noads" in fn or fn == "noads":
            continue
        if fn.startswith("tmp_"):
            continue
        if not fn.endswith(".srt") and not fn.endswith(".ass"):
            continue
        subs.append(os.path.join(project_dir, fn))
    return subs


def find_media(project_dir, explicit):
    if explicit:
        if os.path.isabs(explicit):
            return explicit
        return os.path.join(project_dir, explicit)
    cands = []
    for fn in os.listdir(project_dir):
        p = os.path.join(project_dir, fn)
        if "_noads" in fn or fn == "noads":
            continue
        if os.path.isfile(p) and fn.lower().endswith((".mp3", ".m4a", ".aac", ".wav", ".flac")):
            return p
        if not os.path.isfile(p):
            continue
        if fn.lower().endswith((".mp4", ".mkv", ".mov", ".webm")):
            cands.append(p)
    for p in cands:
        if "_hardsub" not in os.path.basename(p):
            return p
    if cands:
        return cands[0]
    return None



def find_hardsub_videos(project_dir):
    """Find all _hardsub.* video files that need the same ad-cut treatment."""
    vids = []
    for fn in sorted(os.listdir(project_dir)):
        if "_noads" in fn or fn == "noads":
            continue
        p = os.path.join(project_dir, fn)
        if not os.path.isfile(p):
            continue
        if "_hardsub" in fn and fn.lower().endswith((".mp4", ".mkv", ".mov", ".webm")):
            vids.append(p)
    return vids


def ad_json_path_for(project_dir, srt_path):
    base = os.path.splitext(os.path.basename(srt_path))[0]
    for suf in (".en", ".cn", ".bi", ".zh"):
        if base.lower().endswith(suf):
            base = base[: -len(suf)]
            break
    return os.path.join(project_dir, base + ".ad_segments.json")


def main():
    ap = argparse.ArgumentParser(description="Auto-detect ads and cut them from media + subtitles.")
    ap.add_argument("target", help="Project directory or a single .srt file")
    ap.add_argument("--media", default=None, help="Explicit media path (else auto-detected)")
    ap.add_argument("--model", default="glm-5.2", help="LLM model for ad detection")
    ap.add_argument("--provider", default="dashscope", help="LLM provider")
    ap.add_argument(
        "--detector", choices=("auto", "llm", "visual"), default="auto",
        help="Ad detector: auto tries visual keyframes first, then LLM",
    )
    ap.add_argument(
        "--cut-mode", choices=("auto", "precise", "legacy"), default="auto",
        help="Video cut mode: precise splits A/V for packet-accurate stream copy",
    )
    ap.add_argument("--reencode", action="store_true", help="Re-encode media (slower, frame-accurate)")
    ap.add_argument("--redetect", action="store_true", help="Re-run ad detection even if json exists")
    ap.add_argument("--dry-run", action="store_true", help="Detect + report only; do not cut/rewrite")
    ap.add_argument("--keep-originals", action="store_true", help="Keep originals; write outputs to noads/ subdirectory")
    args = ap.parse_args()

    target = args.target

    if os.path.isdir(target):
        project_dir = target
        srt_path = find_main_srt(project_dir)
    else:
        project_dir = os.path.dirname(target) or "."
        srt_path = target
        if not os.path.isdir(project_dir):
            project_dir = os.path.dirname(os.path.abspath(target))

    if not srt_path or not os.path.isfile(srt_path):
        print(f"No suitable source SRT found in {project_dir}")
        sys.exit(1)

    print(f"Project: {project_dir}")
    print(f"Source SRT: {os.path.basename(srt_path)}")

    ad_json = ad_json_path_for(project_dir, srt_path)
    blocks = srt_utils.parse_srt(srt_path)
    print(f"   Parsed {len(blocks)} subtitle blocks.")
    media = find_media(project_dir, args.media)

    if os.path.isfile(ad_json) and not args.redetect:
        print(f"Reusing existing ad map: {os.path.basename(ad_json)}")
        with open(ad_json, "r", encoding="utf-8") as f:
            ad_segments = json.load(f)
        used_visual = any(
            isinstance(seg, dict) and seg.get("detector") == "visual"
            for seg in ad_segments
        ) if isinstance(ad_segments, list) else False
    else:
        visual_segments = []
        if args.detector in ("auto", "visual"):
            if media:
                print("Detecting ad segments via visual keyframe resolution scan...")
                visual_segments = detect_visual_ad_segments(media)
            elif args.detector == "visual":
                print("Visual detector requires a media file, but none was found.")
                sys.exit(1)
            else:
                print("No media found for visual detector; using LLM detector.")

        if visual_segments:
            ad_segments = [
                {"start_time": s, "end_time": e, "detector": "visual"}
                for s, e in visual_segments
            ]
            used_visual = True
        elif args.detector == "visual":
            ad_segments = []
            used_visual = True
        else:
            print(f"Detecting ad segments via LLM ({args.provider}/{args.model})...")
            client = LLMClient()
            provider_enum = None
            if args.provider:
                try:
                    provider_enum = LLMProvider(args.provider)
                except ValueError:
                    pass
            ad_segments = detect_ad_segments(blocks, client, model=args.model, provider=provider_enum)
            used_visual = False

        with open(ad_json, "w", encoding="utf-8") as f:
            json.dump(ad_segments, f, ensure_ascii=False, indent=2)
        print(f"   Saved -> {os.path.basename(ad_json)}")

    merged = merge_ad_segments(ad_segments)
    if not used_visual:
        merged = expand_ad_boundaries(merged, blocks)
        if media:
            merged = refine_boundaries_with_vad(merged, media)
    remap, total_removed = build_remap(merged)

    print(f"\nAd report: {len(merged)} segment(s), {total_removed:.1f}s (~{int(total_removed // 60)}m{int(total_removed % 60)}s) to cut.")
    for i, (s, e) in enumerate(merged, 1):
        mm, ss = divmod(s, 60)
        em, es = divmod(e, 60)
        print(f"   {i}. {int(mm):02d}:{ss:05.2f} - {int(em):02d}:{es:05.2f}  ({e - s:.1f}s)")

    if not merged:
        print("No ads detected. Nothing to cut.")
        return None

    if args.dry_run:
        print("\n--dry-run: stopping after detection.")
        return None

    if media:
        dur = probe_duration(media)
        keep = compute_keep_segments(merged, dur)
        out_media = noads_path(media)
        print(f"\nCutting media: {os.path.basename(media)} -> {os.path.basename(out_media)}")
        cut_media(media, keep, out_media, reencode=args.reencode, cut_mode=args.cut_mode)
        print(f"   OK {out_media}")
    else:
        print("\nNo media file found; skipping media cut (subtitles only).")

    # Cut ad spans from hardsub videos using the same keep segments (stream-copy concat)
    hardsubs = find_hardsub_videos(project_dir)
    if hardsubs and keep:
        print("\nCutting hardsub videos...")
        for hb in hardsubs:
            out_hb = noads_path(hb)
            print(f"   {os.path.basename(hb)} -> {os.path.basename(out_hb)}")
            try:
                cut_media(hb, keep, out_hb, reencode=False, cut_mode=args.cut_mode)
                print(f"   OK {out_hb}")
            except Exception as he:
                print(f"   WARN {os.path.basename(hb)}: {he}")

    print("\nRewriting subtitles...")
    for sub in find_all_subs(project_dir):
        out = noads_path(sub)
        try:
            ext = os.path.splitext(sub)[1]
            if ext.lower() == ".ass":
                tot, kept = rewrite_ass_file(sub, merged, out)
                print(f"   {os.path.basename(out)}: {tot} -> {kept} dialogue lines")
            else:
                tot, kept = rewrite_srt_file(sub, merged, out)
                print(f"   {os.path.basename(out)}: {tot} -> {kept} blocks")
        except Exception as e:
            print(f"   WARN {os.path.basename(sub)}: {e}")

    print(f"\nDone. New timeline is ~{int(total_removed)}s shorter.")
    return None


if __name__ == "__main__":
    main()
