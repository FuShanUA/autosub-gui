import sys
import os

# Add common Mac Homebrew paths to PATH to ensure ffmpeg is found by MLX Whisper and standard engines
for path in ["/opt/homebrew/bin", "/usr/local/bin"]:
    if os.path.exists(path) and path not in os.environ.get("PATH", ""):
        os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")

import json
import time
import ctypes
import re
import subprocess
import argparse
import io
import traceback

try:
    from faster_whisper import WhisperModel # type: ignore
except ImportError:
    import site
    from importlib import reload
    reload(site)
    from faster_whisper import WhisperModel # type: ignore

# Try to import MLX support
try:
    from mlx_engine import transcribe_mlx, is_apple_silicon, HAS_MLX
except ImportError:
    HAS_MLX = False

# Try to import pyannote.audio for speaker diarization
HAS_PYANNOTE = False
try:
    from pyannote.audio import Pipeline as PyannotePipeline
    HAS_PYANNOTE = True
except ImportError:
    pass

# Force UTF-8 for stdout/stderr
if sys.platform == "win32":
    os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"
    try:
        if isinstance(sys.stdout, io.TextIOWrapper):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if isinstance(sys.stderr, io.TextIOWrapper):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, io.UnsupportedOperation):
        pass

import multiprocessing
multiprocessing.freeze_support()

# Configuration
STD_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "faster-whisper")
DEFAULT_MODEL_SIZE = "large-v2"
# Anchor RESULT_ROOT to the repository structure
CURRENT_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Library/Tools/transcriber -> Library/Tools -> Library -> Root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(CURRENT_SCRIPT_DIR)))
RESULT_ROOT = os.path.join(PROJECT_ROOT, "Projects")
LOCAL_MODELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
DOCS_MODELS = os.path.join(os.path.expanduser("~"), "Documents", "AutoSub", "Models")

class SegmentChunk:
    def __init__(self, start, end, text, id):
        self.start = start
        self.end = end
        self.text = text
        self.id = id

from typing import Any

def get_v(obj: Any, key: str, default: Any = 0) -> Any:
    if hasattr(obj, key):
        res = getattr(obj, key, default)
        return res if (res is not None) else default
    if isinstance(obj, dict):
        res = obj.get(key, default)
        return res if (res is not None) else default
    return default

CHUNK_PROFILES = {
    'formal': {'max_chars': 80, 'max_duration': 8.0, 'gap_threshold': 2.5, 'min_context': 45, 'min_words': 5, 'min_yield_chars': 20},
    'spoken': {'max_chars': 80, 'max_duration': 8.0, 'gap_threshold': 1.5, 'min_context': 45, 'min_words': 5, 'min_yield_chars': 20},
}

def detect_content_type(segments_list):
    if not segments_list: return 'spoken'
    durations = [get_v(s, 'end') - get_v(s, 'start') for s in segments_list if get_v(s, 'end') > get_v(s, 'start')]
    gaps = []
    for i in range(1, len(segments_list)):
        g = get_v(segments_list[i], 'start') - get_v(segments_list[i-1], 'end')
        if g >= 0: gaps.append(g)
    avg_dur = sum(durations) / len(durations) if durations else 0
    avg_gap = sum(gaps) / len(gaps) if gaps else 0
    style = 'formal' if (avg_dur > 4.5 or avg_gap > 0.6) else 'spoken'
    # print(f"📊 Pacing: avg_seg={avg_dur:.1f}s, avg_gap={avg_gap:.2f}s → style='{style}'")
    return style

# Languages allowed for multilang detection. Short audio segments
# misdetected as Welsh/Portuguese/etc. cause Whisper to hallucinate
# gibberish in those languages. Restricting to the actual languages
# present in the video prevents this.
ALLOWED_LANGS = {'ja', 'en', 'zh'}

def _clamp_language(lang):
    """Clamp detected language to the allowed set. Falls back to 'en' for
    anything exotic (Welsh, Portuguese, etc.) to prevent hallucinations."""
    if lang in ALLOWED_LANGS:
        return lang
    return 'en'

def chunk_segments(segments_list, content_type=None):
    if content_type is None: content_type = detect_content_type(segments_list)
    p = CHUNK_PROFILES[content_type]
    max_chars, max_duration, gap_threshold = p['max_chars'], p['max_duration'], p['gap_threshold']
    min_context, min_words, min_yield_chars = p['min_context'], p['min_words'], p['min_yield_chars']

    chunk_id = 1
    def word_streamer(segs):
        for seg in segs:
            words = get_v(seg, 'words', None)
            if words:
                for w in words: yield w
            else:
                # Segment has no word-level timestamps (e.g. from checkpoint resume).
                # Yield a synthetic word from the full segment text so it isn't silently dropped.
                text = get_v(seg, 'text', '')
                if text and text.strip():
                    yield {'word': text, 'start': get_v(seg, 'start'), 'end': get_v(seg, 'end')}

    all_words = list(word_streamer(segments_list))
    if not all_words:
        # Fallback to segments if no word-level timestamps are available
        for i, s in enumerate(segments_list):
            yield SegmentChunk(get_v(s, 'start'), get_v(s, 'end'), get_v(s, 'text', ""), i+1)
        return

    # Build a per-word language map for boundary detection.
    # In multilang mode, each segment carries a 'lang' field. We use this
    # to break subtitle blocks at language boundaries (ja->en or en->ja),
    # so that Japanese and English text don't end up in the same block.
    seg_langs = []
    for seg in segments_list:
        lang = get_v(seg, 'lang', None)
        if lang:
            seg_langs.append(lang)
        else:
            text = get_v(seg, 'text', '')
            has_kana = any(0x3040 <= ord(c) <= 0x30FF for c in text)
            has_cjk = any(0x4E00 <= ord(c) <= 0x9FFF for c in text)
            seg_langs.append('ja' if (has_kana or has_cjk) else 'en')
    word_langs = []
    seg_idx = 0
    for seg in segments_list:
        words = get_v(seg, 'words', None)
        count = len(words) if words else (1 if get_v(seg, 'text', '').strip() else 0)
        lang = seg_langs[seg_idx] if seg_idx < len(seg_langs) else 'en'
        for _ in range(count):
            word_langs.append(lang)
        seg_idx += 1
    while len(word_langs) < len(all_words):
        word_langs.append('en')
    word_langs = word_langs[:len(all_words)]

    def smart_join(t1, t2):
        if not t1: return t2
        # Don't insert a space between CJK characters. faster-whisper with
        # word_timestamps=True splits Japanese/Chinese into single-char tokens,
        # and inserting spaces produces garbage like "F DE モ デ ル".
        if t1.endswith(' ') or t2.startswith(' '):
            return t1 + t2
        last_char = t1[-1] if t1 else ''
        first_char = t2[0] if t2 else ''
        def _is_cjk(ch):
            return bool(ch) and (0x3000 <= ord(ch) <= 0x9FFF or 0xFF00 <= ord(ch) <= 0xFFEF)
        if _is_cjk(last_char) or _is_cjk(first_char):
            return t1 + t2
        # Don't insert space between single English letters — they are
        # likely split acronym tokens (e.g. "F" + "D" + "E" = "FDE",
        # not "F D E"). Whisper splits acronyms into single-char tokens
        # when transcribing Japanese with word_timestamps=True.
        if len(t2.strip()) == 1 and t2.strip().isalpha() and last_char.isalpha():
            return t1 + t2
        return t1 + ' ' + t2

    i = 0
    while i < len(all_words):
        current_chunk_words = [all_words[i]]
        current_start = get_v(all_words[i], 'start')
        current_text = get_v(all_words[i], 'word', "") or get_v(all_words[i], 'text', "")
        i += 1
        while i < len(all_words):
            word = all_words[i]
            word_text = get_v(word, 'word', "") or get_v(word, 'text', "")
            gap = get_v(word, 'start') - get_v(all_words[i-1], 'end')
            would_exceed_chars = len(current_text) + len(word_text.strip()) > max_chars
            would_exceed_duration = (get_v(word, 'end') - current_start) > max_duration
            is_large_gap = gap > gap_threshold
            is_sentence_end = word_text.strip()[-1] in '.?!。？！' if word_text.strip() else False

            if would_exceed_chars and not would_exceed_duration:
                found_near_end = False
                if is_sentence_end and len(current_text) + len(word_text.strip()) < max_chars * 1.4:
                    found_near_end = True
                if not found_near_end:
                    for j in range(1, 6):
                        if i + j < len(all_words):
                            w_peek_text = get_v(all_words[i+j], 'word', "") or get_v(all_words[i+j], 'text', "")
                            if w_peek_text.strip() and w_peek_text.strip()[-1] in '.?!。？！' and len(current_text) < max_chars * 1.4:
                                found_near_end = True; break
                if found_near_end: would_exceed_chars = False

            # Detect language boundary: break when the language changes
            # (ja->en or en->ja), but only if the current block has enough
            # text to not be a tiny fragment. Use a lower threshold (5 chars)
            # than min_yield_chars (20) to allow breaks at language boundaries
            # while still avoiding single-word fragments.
            cur_lang = word_langs[i-1] if i-1 < len(word_langs) else 'en'
            next_lang = word_langs[i] if i < len(word_langs) else 'en'
            is_lang_boundary = (cur_lang != next_lang)

            should_break = (would_exceed_chars or would_exceed_duration or (is_large_gap and len(current_chunk_words) >= min_words) or (is_sentence_end and len(current_text) > min_context) or is_lang_boundary)
            # Use a lower min threshold for language boundary breaks
            min_check = 5 if is_lang_boundary else min_yield_chars
            if should_break and len(current_text.strip()) >= min_check:
                if is_sentence_end and not (would_exceed_chars or would_exceed_duration):
                    current_chunk_words.append(word); current_text = smart_join(current_text, word_text); i += 1
                break
            current_chunk_words.append(word); current_text = smart_join(current_text, word_text); i += 1
        yield SegmentChunk(current_start, get_v(current_chunk_words[-1], 'end'), current_text.strip(), chunk_id)
        chunk_id += 1

def get_ffmpeg_path():
    import shutil
    path = shutil.which("ffmpeg")
    if path: return path
    for fb in [r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe", r"D:\Program Files\CapCut\7.7.0.3143\ffmpeg.exe"]:
        if os.path.exists(fb): return fb
    return "ffmpeg"

FFMPEG_EXE = get_ffmpeg_path()

def get_duration(file_path):
    cmd = [FFMPEG_EXE, "-i", file_path, "-hide_banner"]
    try:
        startupinfo = None
        if hasattr(subprocess, 'STARTUPINFO'):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= getattr(subprocess, 'STARTF_USESHOWWINDOW', 0)
        
        kwargs = {}
        if startupinfo:
            kwargs['startupinfo'] = startupinfo
            
        result = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8', errors='ignore', **kwargs)
        for line in result.stderr.split('\n'):
            if "Duration" in line:
                time_str = line.split("Duration:")[1].split(",")[0].strip()
                h, m, s = time_str.split(':')
                return float(h) * 3600 + float(m) * 60 + float(s)
    except: pass
    return 0

def show_notification(title, message):
    try: 
        if hasattr(ctypes, 'windll'):
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x1)
    except: pass

def get_project_folder(file_path):
    base = os.path.splitext(os.path.basename(file_path))[0]
    clean = re.sub(r'[^a-zA-Z0-9]', '_', base)
    folder_name = "_".join([p for p in clean.split('_') if p][:5]) or "Untitled_Project"
    return os.path.join(RESULT_ROOT, folder_name)

def serialize_segment(s, lang=None):
    """Converts a faster-whisper segment into a serializable dict."""
    res = {
        'start': getattr(s, 'start', 0),
        'end': getattr(s, 'end', 0),
        'text': getattr(s, 'text', ""),
        'words': []
    }
    if lang:
        res['lang'] = lang
    words = getattr(s, 'words', None)
    if words:
        for w in words:
            res['words'].append({'start': getattr(w, 'start', 0), 'end': getattr(w, 'end', 0), 'word': getattr(w, 'word', ""), 'probability': getattr(w, 'probability', 0)})
    return res

def save_checkpoint(path, segments):
    """Saves transcription state atomically using a temporary file."""
    temp_path = path + ".tmp"
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump({'segments': segments}, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except Exception as e:
        print(f"⚠️ Failed to save checkpoint: {e}")

def _multilang_transcribe(model, file_path, total_duration, clip_start, kwargs):
    """Multi-language transcription using VAD + Tiny + sliding window.

    Uses faster-whisper's VAD to split audio into speech segments, detects
    language per segment with the Tiny model, refines language boundaries with
    a sliding window, and transcribes each language group with the main model.
    Long groups (>30s) are split at internal VAD pauses to prevent Whisper
    auto-detect from drifting to the wrong language on long segments.
    """
    yield from _multilang_transcribe_sliding_window(model, file_path, total_duration, clip_start, kwargs)

def _multilang_transcribe_sliding_window(model, file_path, total_duration, clip_start, kwargs):
    """Multi-language transcription: detects language per VAD segment and transcribes each with the correct language."""
    # Use tiny model for fast language detection; large-v2 for actual transcription.
    # tiny's detect_language is ~100x faster than large-v2's (far fewer params).
    print(f"🌐 Loading tiny model for fast language detection...", flush=True)
    detect_model = WhisperModel("tiny", device="cpu", compute_type="int8")
    print(f"🌐 Tiny model loaded. Detecting languages per segment...", flush=True)
    from faster_whisper.audio import decode_audio
    audio = decode_audio(file_path)
    if clip_start > 0:
        audio = audio[int(clip_start * 16000):]
    try:
        from faster_whisper.vad import get_vad_model, VadOptions
        # max_speech_duration_s: split long segments at silence points,
        # naturally approximating speaker turns for language detection.
        vad_opts = VadOptions(min_silence_duration_ms=200, speech_pad_ms=200, max_speech_duration_s=10)
        from faster_whisper.vad import get_speech_timestamps
        vad_segments = get_speech_timestamps(audio, vad_opts)
    except Exception as e:
        print(f"⚠️ VAD failed ({e}), falling back to single-pass transcription", flush=True)
        return model.transcribe(file_path, **kwargs)
    speech_segs = []
    for seg in vad_segments:
        start = seg.get("start", 0) / 16000 + clip_start
        end = seg.get("end", 0) / 16000 + clip_start
        if end - start >= 1.0:
            speech_segs.append((start, end))
    # VAD with max_speech_duration_s already splits at silence points,
    # approximating speaker turns. No merging or fixed-window splitting needed.
    print(f"🌐 Found {len(speech_segs)} speech segments (VAD auto-split at pauses). Detecting language...", flush=True)
    all_segs = []
    for i, (seg_start, seg_end) in enumerate(speech_segs):
        seg_audio = audio[int((seg_start - clip_start) * 16000):int((seg_end - clip_start) * 16000)]
        if len(seg_audio) < 16000:
            continue
        try:
            lang, prob, _ = detect_model.detect_language(audio=seg_audio)
            lang = _clamp_language(lang)
        except Exception:
            lang, prob = "auto", 0.0
        print(f"   [{i+1}/{len(speech_segs)}] {seg_start:.1f}-{seg_end:.1f}s -> {lang} ({prob:.2f})", flush=True)
        all_segs.append((seg_start, seg_end, lang, prob))
    if not all_segs:
        print(f"⚠️ No speech segments found, falling back to single-pass", flush=True)
        return model.transcribe(file_path, **kwargs)
    # Refine language boundaries: when a segment's language differs from the
    # next segment's, the boundary may be imprecise (VAD didn't detect a pause
    # at the actual language switch point). Use a sliding-window Tiny model
    # scan to find the real switch point and split the segment there.
    LANG_CONF_THRESHOLD = 0.80
    refined_segs = []
    for idx in range(len(all_segs)):
        seg_start, seg_end, lang, prob = all_segs[idx]
        # Only refine if this segment is long enough and the next segment
        # has a different language (i.e. there's a language boundary here)
        next_lang = all_segs[idx + 1][2] if idx + 1 < len(all_segs) else None
        if (next_lang and next_lang != lang and seg_end - seg_start >= 3.0
                and lang in ('ja', 'en', 'zh') and next_lang in ('ja', 'en', 'zh')):
            # Sliding window from front to back: 2s windows with 1s step
            # to find the approximate switch point, then 1s windows with
            # 0.5s step to refine. Scan only the last 6s of the segment.
            sample_rate = 16000
            def _detect_lang(t_start, t_end):
                w_start = int((t_start - clip_start) * sample_rate)
                w_end = int((t_end - clip_start) * sample_rate)
                w_audio = audio[w_start:w_end]
                if len(w_audio) < sample_rate // 2:
                    return None, 0
                try:
                    wl, wp, _ = detect_model.detect_language(audio=w_audio)
                    return _clamp_language(wl), wp
                except Exception:
                    return None, 0

            # Pass 1: 2s windows, 1s step, from scan_start forward
            switch_point = None
            scan_start = max(seg_start, seg_end - 6.0)
            coarse_point = None
            t = scan_start
            while t < seg_end - 2.0:
                wl, wp = _detect_lang(t, t + 2.0)
                if wl == next_lang and wp >= 0.60:
                    coarse_point = t
                    break
                t += 1.0
            if coarse_point is not None:
                # Pass 2: 1s windows, 0.5s step, refine around coarse_point
                fine_start = max(seg_start, coarse_point - 2.0)
                t = fine_start
                while t < coarse_point + 2.0 and t < seg_end - 1.0:
                    wl, wp = _detect_lang(t, t + 1.0)
                    if wl == next_lang and wp >= 0.55:
                        switch_point = t
                        break
                    t += 0.5
                if switch_point is None:
                    switch_point = coarse_point
            if switch_point is not None and switch_point > seg_start + 0.5:
                # Split: [seg_start, switch_point] stays as current lang,
                # [switch_point, seg_end] becomes next lang
                refined_segs.append((seg_start, switch_point, lang, prob))
                refined_segs.append((switch_point, seg_end, next_lang, 0.90))
                print(f"   🔀 Refined boundary: {seg_start:.1f}-{switch_point:.1f}s ({lang}) + {switch_point:.1f}-{seg_end:.1f}s ({next_lang})", flush=True)
            else:
                refined_segs.append((seg_start, seg_end, lang, prob))
        else:
            refined_segs.append((seg_start, seg_end, lang, prob))

    # Group consecutive same-language segments
    all_segs = refined_segs
    groups = []
    cur_lang = all_segs[0][2]
    cur_start, cur_end = all_segs[0][0], all_segs[0][1]
    cur_min_prob = all_segs[0][3]
    for seg_start, seg_end, lang, prob in all_segs[1:]:
        if lang == cur_lang:
            cur_end = seg_end
            cur_min_prob = min(cur_min_prob, prob)
        else:
            groups.append((cur_start, cur_end, cur_lang, cur_min_prob))
            cur_start, cur_end, cur_lang, cur_min_prob = seg_start, seg_end, lang, prob
    groups.append((cur_start, cur_end, cur_lang, cur_min_prob))
    print(f"🌐 Grouped into {len(groups)} language segments. Transcribing with main model...", flush=True)
    info = type('Info', (), {'language': 'multilang', 'language_probability': 1.0})()
    import tempfile
    for i, (g_start, g_end, g_lang, g_min_prob) in enumerate(groups):
        overall_pct = g_start / total_duration * 100 if total_duration > 0 else 0
        print(f"Progress: {min(99.0, overall_pct):.1f}% (Transcribing {g_start:.0f}-{g_end:.0f}s as {g_lang})", flush=True)
        # Extract this group's audio as a temp WAV using ffmpeg, then
        # transcribe the extracted file directly. This is more reliable
        # than clip_timestamps, which can cause Whisper to silently skip
        # portions of the audio within the clip range.
        g_duration = g_end - g_start

        # For long groups (>30s), split at internal VAD pauses to prevent
        # Whisper auto-detect from drifting to the wrong language.
        MAX_GROUP_DURATION = 30.0
        if g_duration > MAX_GROUP_DURATION:
            try:
                from faster_whisper.vad import get_speech_timestamps, VadOptions
                group_audio = audio[int((g_start - clip_start) * 16000):int((g_end - clip_start) * 16000)]
                vad_opts = VadOptions(min_silence_duration_ms=300, speech_pad_ms=100, max_speech_duration_s=MAX_GROUP_DURATION)
                sub_segs = get_speech_timestamps(group_audio, vad_opts)
                if len(sub_segs) > 1:
                    print(f"   📏 Split long group {g_start:.0f}-{g_end:.0f}s ({g_duration:.0f}s) into {len(sub_segs)} sub-segments", flush=True)
                    # Process each sub-segment
                    for sub in sub_segs:
                        sub_start = sub.get("start", 0) / 16000 + g_start
                        sub_end = sub.get("end", 0) / 16000 + g_start
                        sub_dur = sub_end - sub_start
                        if sub_dur < 0.3:
                            continue
                        sub_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False, dir=tempfile.gettempdir())
                        sub_wav.close()
                        try:
                            import subprocess as _sp
                            _sp.run([FFMPEG_EXE, '-y', '-ss', str(sub_start), '-t', str(sub_dur),
                                    '-i', file_path, '-vn', '-ac', '1', '-ar', '16000',
                                    '-f', 'wav', sub_wav.name], capture_output=True, timeout=60)
                        except:
                            sub_wav = None
                        sub_kwargs = dict(kwargs)
                        sub_kwargs.pop("vad_filter", None)
                        sub_kwargs.pop("clip_timestamps", None)
                        sub_kwargs.pop("language", None)
                        sub_kwargs["vad_filter"] = False
                        if g_lang and g_lang != "auto" and g_min_prob >= 0.80:
                            sub_kwargs["language"] = g_lang
                        try:
                            t_input = sub_wav.name if sub_wav and os.path.exists(sub_wav.name) else file_path
                            if sub_wav and os.path.exists(sub_wav.name):
                                sub_kwargs.pop("clip_timestamps", None)
                            else:
                                sub_kwargs["clip_timestamps"] = [float(sub_start), float(sub_end)]
                            seg_iter, info_local = model.transcribe(t_input, **sub_kwargs)
                            seg_list = list(seg_iter)
                            sub_lang = getattr(info_local, 'language', g_lang)
                            # MLX fallback
                            if seg_list and sub_dur > 3.0:
                                last_end = getattr(seg_list[-1], 'end', 0)
                                coverage = last_end / sub_dur if sub_dur > 0 else 1.0
                                if coverage < 0.5 and HAS_MLX:
                                    print(f"⚠️ Low coverage ({coverage:.0%}) for sub-segment {sub_start:.0f}-{sub_end:.0f}s. MLX retry...", flush=True)
                                    try:
                                        mlx_segs = transcribe_mlx(sub_wav.name if sub_wav and os.path.exists(sub_wav.name) else file_path,
                                            model_name="mlx-community/whisper-large-v2-mlx", language=None)
                                        if mlx_segs:
                                            mlx_list = list(mlx_segs)
                                            if mlx_list and sub_wav and os.path.exists(sub_wav.name):
                                                mlx_cov = getattr(mlx_list[-1], 'end', 0) / sub_dur if sub_dur > 0 else 1.0
                                                if mlx_cov < 0.5:
                                                    try:
                                                        mlx_en = transcribe_mlx(sub_wav.name, model_name="mlx-community/whisper-large-v2-mlx", language='en')
                                                        if mlx_en:
                                                            en_list = list(mlx_en)
                                                            en_cov = getattr(en_list[-1], 'end', 0) / sub_dur if sub_dur > 0 and en_list else 0
                                                            if en_cov > mlx_cov:
                                                                mlx_list = en_list
                                                                print(f"   English retry: {en_cov:.0%} (better)", flush=True)
                                                    except: pass
                                            seg_list = mlx_list
                                    except: pass
                            for s in seg_list:
                                s_start_orig = getattr(s, 'start', 0) + sub_start
                                s_end_orig = getattr(s, 'end', 0) + sub_start
                                try: s.start = s_start_orig
                                except: pass
                                try: s.end = s_end_orig
                                except: pass
                                words = getattr(s, 'words', None)
                                if words:
                                    for w in words:
                                        try: w.start = getattr(w, 'start', 0) + sub_start
                                        except: pass
                                        try: w.end = getattr(w, 'end', 0) + sub_start
                                        except: pass
                                try: s._lang = sub_lang
                                except: pass
                                if total_duration and total_duration > 0:
                                    p = round((s_end_orig / total_duration) * 100, 1)
                                    print(f"Progress: {min(99.0, p)}% ({s_end_orig:.0f}/{total_duration:.0f}s | {sub_lang} @ {sub_start:.0f}-{sub_end:.0f}s)", flush=True)
                                yield s
                        except Exception as e:
                            print(f"⚠️ Failed to transcribe sub-segment {sub_start:.0f}-{sub_end:.0f}s: {e}", flush=True)
                        finally:
                            if sub_wav and os.path.exists(sub_wav.name):
                                try: os.unlink(sub_wav.name)
                                except: pass
                    continue  # Skip the normal transcription below for this group
            except Exception as e:
                print(f"⚠️ VAD split failed, using full group: {e}", flush=True)

        tmp_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False, dir=tempfile.gettempdir())
        tmp_wav.close()
        try:
            import subprocess as _sp
            _extract_cmd = [FFMPEG_EXE, '-y', '-ss', str(g_start), '-t', str(g_duration),
                            '-i', file_path, '-vn', '-ac', '1', '-ar', '16000',
                            '-f', 'wav', tmp_wav.name]
            _sp.run(_extract_cmd, capture_output=True, timeout=60)
        except Exception as e:
            print(f"⚠️ ffmpeg extract failed for {g_start:.0f}-{g_end:.0f}s: {e}", flush=True)
            tmp_wav = None

        group_kwargs = dict(kwargs)
        group_kwargs.pop("vad_filter", None)
        group_kwargs.pop("clip_timestamps", None)
        group_kwargs.pop("language", None)
        group_kwargs["vad_filter"] = False
        LANG_CONFIDENCE_THRESHOLD = 0.80
        if g_lang and g_lang != "auto" and g_min_prob >= LANG_CONFIDENCE_THRESHOLD:
            group_kwargs["language"] = g_lang
            print(f"   Language: {g_lang} (confidence >= {g_min_prob:.2f})", flush=True)
        else:
            print(f"   Language: auto (min confidence {g_min_prob:.2f} < {LANG_CONFIDENCE_THRESHOLD})", flush=True)
        try:
            transcribe_input = tmp_wav.name if tmp_wav and os.path.exists(tmp_wav.name) else file_path
            if tmp_wav and os.path.exists(tmp_wav.name):
                group_kwargs.pop("clip_timestamps", None)
            else:
                group_kwargs["clip_timestamps"] = [float(g_start), float(g_end)]
            seg_iter, _ = model.transcribe(transcribe_input, **group_kwargs)
            seg_list = list(seg_iter)
            # Check if transcription covered the full group duration.
            # If the last segment ends much earlier than the group end,
            # faster-whisper may have silently dropped speech. Fall back
            # to MLX engine which handles these cases better.
            if seg_list and g_duration > 3.0:
                last_end = getattr(seg_list[-1], 'end', 0)
                # If using extracted WAV, timestamps are relative to 0
                if tmp_wav and os.path.exists(tmp_wav.name):
                    coverage = last_end / g_duration if g_duration > 0 else 1.0
                else:
                    coverage = (last_end - g_start) / g_duration if g_duration > 0 else 1.0
                if coverage < 0.5 and HAS_MLX:
                    print(f"⚠️ Low coverage ({coverage:.0%}) for {g_start:.0f}-{g_end:.0f}s. Retrying with MLX engine...", flush=True)
                    try:
                        mlx_input = tmp_wav.name if tmp_wav and os.path.exists(tmp_wav.name) else file_path
                        mlx_kwargs = {"language": g_lang} if g_lang and g_lang != "auto" else {}
                        mlx_segs = transcribe_mlx(mlx_input, model_name="mlx-community/whisper-large-v2-mlx",
                                                  language=g_lang if g_lang and g_lang != "auto" else None)
                        if mlx_segs:
                            # Check if MLX auto-detect also had low coverage.
                            # If so, try English as fallback (English Whisper
                            # correctly transcribes English speech, while
                            # Japanese Whisper mistranslates it).
                            mlx_list = list(mlx_segs)
                            if mlx_list and tmp_wav and os.path.exists(tmp_wav.name):
                                mlx_last_end = getattr(mlx_list[-1], 'end', 0)
                                mlx_coverage = mlx_last_end / g_duration if g_duration > 0 else 1.0
                                if mlx_coverage < 0.5 and g_lang != 'en':
                                    print(f"   MLX auto-detect also low ({mlx_coverage:.0%}). Retrying with English...", flush=True)
                                    try:
                                        mlx_en = transcribe_mlx(tmp_wav.name,
                                            model_name="mlx-community/whisper-large-v2-mlx",
                                            language='en')
                                        if mlx_en:
                                            en_list = list(mlx_en)
                                            en_last_end = getattr(en_list[-1], 'end', 0) if en_list else 0
                                            en_coverage = en_last_end / g_duration if g_duration > 0 else 0
                                            if en_coverage > mlx_coverage:
                                                mlx_list = en_list
                                                print(f"   English retry: {en_coverage:.0%} coverage (better)", flush=True)
                                    except Exception as en_err:
                                        print(f"   English retry failed: {en_err}", flush=True)
                            seg_list = mlx_list
                            # Note: timestamp offset is applied later in the
                            # for s in seg_list: loop, so no need to offset here
                    except Exception as mlx_err:
                        print(f"⚠️ MLX retry also failed: {mlx_err}", flush=True)
            for s in seg_list:
                # Offset timestamps back to the original audio timeline
                s_start_orig = getattr(s, 'start', 0) + g_start
                s_end_orig = getattr(s, 'end', 0) + g_start
                try:
                    s.start = s_start_orig
                except Exception:
                    pass
                try:
                    s.end = s_end_orig
                except Exception:
                    pass
                # Also offset word timestamps
                words = getattr(s, 'words', None)
                if words:
                    for w in words:
                        try:
                            w.start = getattr(w, 'start', 0) + g_start
                        except Exception:
                            pass
                        try:
                            w.end = getattr(w, 'end', 0) + g_start
                        except Exception:
                            pass
                # Attach the detected language
                try:
                    s._lang = g_lang
                except Exception:
                    pass
                if total_duration and total_duration > 0:
                    p = round((s_end_orig / total_duration) * 100, 1)
                    print(f"Progress: {min(99.0, p)}% ({s_end_orig:.0f}/{total_duration:.0f}s | {g_lang} @ {g_start:.0f}-{g_end:.0f}s)", flush=True)
                yield s
        except Exception as e:
            print(f"⚠️ Failed to transcribe {g_start:.0f}-{g_end:.0f}s ({g_lang}): {e}", flush=True)
        finally:
            if tmp_wav and os.path.exists(tmp_wav.name):
                try:
                    os.unlink(tmp_wav.name)
                except Exception:
                    pass
    print(f"🌐 Multi-language transcription complete.", flush=True)

def main():
    print(f"🚀 Transcriber engine started (PID: {os.getpid()})")
    print(f"Progress: 0.1% (Initializing engine...)", flush=True)
    print(f"DEBUG: sys.argv = {sys.argv}")
    parser = argparse.ArgumentParser(description="Standalone Faster-Whisper Transcriber")
    parser.add_argument("file", help="Path to video/audio file")
    parser.add_argument("--device", default=None, help="Device (cuda/cpu)")
    parser.add_argument("--model", default=DEFAULT_MODEL_SIZE, help="Whisper model name/path")
    parser.add_argument("--output", default=None, help="Output directory for SRT")
    parser.add_argument("--no-gui", action="store_true", help="Run without UI progress")
    parser.add_argument("--quick", action="store_true", help="Experimental: Quick Transcribe (YouTube Subs + LLM)")
    parser.add_argument("--language", default=None, help="Source language code (e.g. ja, en, zh). Auto-detect if omitted.")
    parser.add_argument("--multilang", action="store_true", help="Enable per-segment language detection for mixed-language videos")
    args = parser.parse_args()

    file_path = os.path.abspath(args.file)
    output_dir = os.path.abspath(args.output) if args.output else get_project_folder(file_path)
    raw_model_name = args.model
    if raw_model_name.startswith("faster-whisper-"): raw_model_name = raw_model_name.replace("faster-whisper-", "", 1)

    os.makedirs(output_dir, exist_ok=True)
    lock_file = os.path.join(output_dir, ".transcribe.lock")
    if os.path.exists(lock_file):
        try:
            with open(lock_file, "r") as f:
                old_pid = int(f.read().strip())
            
            running = False
            if sys.platform == "win32":
                import psutil
                running = psutil.pid_exists(old_pid)
            else:
                try:
                    os.kill(old_pid, 0)
                    running = True
                except OSError:
                    running = False
                    
            if running:
                print(f"⚠️ Project folder locked by PID {old_pid}. Exiting.")
                sys.exit(0)
        except Exception as e:
            pass

    with open(lock_file, "w") as f: f.write(str(os.getpid()))

    try:
        if not os.path.exists(file_path):
            print(f"❌ File not found: {file_path}"); sys.exit(1)

        print(f"🎙️ File: {os.path.basename(file_path)}\n📂 Output: {output_dir}\n🧠 Model: {raw_model_name}")

        # --- Quick Mode (YouTube + LLM) ---
        if args.quick:
            print("⚡ Mode: Quick Transcribe (Experimental)")
            quick_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quick_transcribe.py")
            cmd = [sys.executable, quick_script, file_path, "--output", output_dir]
            # Try to pass cookies if we can find them
            cookies = r"D:\Downloads\cookies.txt"
            if os.path.exists(cookies):
                cmd.extend(["--cookies", cookies])

            try:
                creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if sys.platform == "win32" else 0
                subprocess.run(cmd, check=True, creationflags=creationflags)
                print("✅ Quick Transcribe finished.")
                if os.path.exists(lock_file): os.remove(lock_file)
                sys.exit(0)
            except Exception as e:
                print(f"❌ Quick Transcribe failed: {e}. Falling back to normal transcription.")

        total_duration = get_duration(file_path)

        # UI
        root, progress_var, lbl_time = None, None, None
        if not args.no_gui:
            try:
                import tkinter as tk
                from tkinter import ttk
                root = tk.Tk(); root.title("Transcribing...")
                root.geometry('400x150')
                tk.Label(root, text=f"Processing: {os.path.basename(file_path)}", wraplength=380).pack(pady=10)
                progress_var = tk.DoubleVar()
                ttk.Progressbar(root, variable=progress_var, maximum=100, length=350).pack(pady=10)
                lbl_time = tk.Label(root, text="Initializing..."); lbl_time.pack()
                root.update()
            except: root = None

        # Model Load
        if args.device:
            device = args.device
        else:
            if sys.platform == "win32":
                try:
                    device = "cuda" if ctypes.windll.kernel32.GetModuleHandleW("nvcuda.dll") else "cpu"
                except:
                    device = "cpu"
            else:
                # For Mac/Linux, stick to CPU for maximum compatibility unless specifically requested
                device = "cpu"
        search_paths = [LOCAL_MODELS, DOCS_MODELS, STD_CACHE]
        best_root = next((d for d in search_paths if d and os.path.exists(d)), None)
        print(f"Progress: 0.8% (Analyzing audio...)", flush=True)

        # Checkpoint
        checkpoint_path = os.path.join(output_dir, "transcribe_state.json")
        segments_list, clip_start = [], 0
        if os.path.exists(checkpoint_path):
            try:
                with open(checkpoint_path, 'r', encoding='utf-8') as f:
                    segments_list = json.load(f).get('segments', [])
                    if segments_list:
                        clip_start = segments_list[-1].get('end', 0)
                        if clip_start is None: clip_start = 0
                        print(f"⏩ Resuming from {clip_start:.2f}s")
                        # 立即向 TUI 汇报当前真实进展，避免用户误以为从头开始
                        if total_duration > 0:
                            p = round(min(99.9, (clip_start / total_duration) * 100), 1)
                            print(f"Progress: {p}% (Resuming from {clip_start:.0f}s...)", flush=True)
            except Exception as e:
                print(f"⚠️ State file corrupted or unreadable ({e}). Starting fresh.")
                segments_list, clip_start = [], 0

        kwargs: dict[str, Any] = {"beam_size": 5, "vad_filter": True, "word_timestamps": True}
        if clip_start > 0: kwargs["clip_timestamps"] = [float(clip_start), float(total_duration)]
        if args.language:
            kwargs["language"] = args.language
            print(f"🌐 Language override: {args.language}")
        if args.multilang:
            kwargs.pop("language", None)
            kwargs["condition_on_previous_text"] = False
            print(f"🌐 Multi-language detection enabled (per-segment auto-detect, condition_on_previous_text=False)")

        # --- High-level Skip: If already finished, don't even start the engine ---
        if total_duration > 0 and clip_start >= total_duration - 1.0:
            print(f"✅ Transcription already completed ({clip_start:.1f}s / {total_duration:.1f}s). Skipping to output.", flush=True)
            segments = []
        else:
            # --- Transcription Engine Selection ---
            skip_loop_progress = False
            # Standard Path: faster-whisper (CPU)
            print(f"Progress: 0.2% (Loading model {raw_model_name} on {device}...)", flush=True)
            model = WhisperModel(raw_model_name, device=device, compute_type="auto", download_root=best_root)
            print(f"Progress: 0.5% (Model loaded)", flush=True)
            if args.multilang:
                segments = _multilang_transcribe(model, file_path, total_duration, clip_start, kwargs)
                skip_loop_progress = True
                # Consume the generator fully (multilang yields segments)
                segments = list(segments)
                for s in segments:
                    seg_lang = getattr(s, '_lang', None)
                    segments_list.append(serialize_segment(s, lang=seg_lang))
            elif total_duration > 300:
                # --- Chunked transcription for long videos ---
                # model.transcribe() on a long video submits all chunks at once
                # to ctranslate2's thread pool. The generator won't yield the
                # first segment until the entire batch is processed, which can
                # take 20+ minutes on CPU with no progress or checkpoint.
                # Splitting into 5-minute chunks with per-segment checkpointing
                # ensures progress is visible and resumable.
                CHUNK_SEC = 300  # 5 minutes per chunk
                segments = []
                # Resume from the checkpoint instead of submitting the whole
                # remaining audio to faster-whisper as one uncheckpointed batch.
                chunk_start = float(clip_start)
                while chunk_start < total_duration:
                    chunk_end = min(chunk_start + CHUNK_SEC, total_duration)
                    chunk_kwargs = dict(kwargs)
                    chunk_kwargs.pop("clip_timestamps", None)
                    chunk_kwargs["clip_timestamps"] = [float(chunk_start), float(chunk_end)]
                    chunk_kwargs.pop("vad_filter", None)
                    chunk_kwargs["vad_filter"] = False
                    pct = round((chunk_start / total_duration) * 100, 1)
                    print(f"Progress: {pct}% (Transcribing {chunk_start:.0f}-{chunk_end:.0f}s)", flush=True)
                    seg_iter, info = model.transcribe(file_path, **chunk_kwargs)
                    # Iterate generator one segment at a time so each segment
                    # is checkpointed immediately (enables resume + progress).
                    for s in seg_iter:
                        segments.append(s)
                        segments_list.append(serialize_segment(s))
                        s_end = getattr(s, 'end', 0) or 0
                        if total_duration > 0:
                            p = round((s_end / total_duration) * 100, 1)
                            print(f"Progress: {p}% ({s_end:.0f}/{total_duration:.0f}s)", flush=True)
                        if len(segments_list) % 2 == 0:
                            save_checkpoint(checkpoint_path, segments_list)
                    save_checkpoint(checkpoint_path, segments_list)
                    chunk_start = chunk_end
                skip_loop_progress = True
            else:
                segments, info = model.transcribe(file_path, **kwargs)
        detected_style = None

        # Chunked and multilang paths already appended to segments_list
        # during their loops. Skip re-append to avoid duplicating segments.
        if not skip_loop_progress:
            for s in segments:
                seg_lang = getattr(s, '_lang', None)
                segments_list.append(serialize_segment(s, lang=seg_lang))

        # Sort segments by start time and fix overlaps.
        segments_list.sort(key=lambda x: x.get('start', 0))
        for i in range(1, len(segments_list)):
            prev_end = segments_list[i-1].get('end', 0)
            curr_start = segments_list[i].get('start', 0)
            if curr_start < prev_end:
                segments_list[i]['start'] = prev_end
                if segments_list[i].get('end', 0) <= prev_end:
                    segments_list[i]['end'] = prev_end + 0.1
                for w in segments_list[i].get('words', []):
                    if w.get('start', 0) < prev_end:
                        w['start'] = prev_end
            if len(segments_list) % 2 == 0:
                save_checkpoint(checkpoint_path, segments_list)

            s_end = segments_list[i].get('end', 0)
            if s_end is None: s_end = 0

            if not detected_style and (s_end > 60 or len(segments_list) > 20):
                detected_style = detect_content_type(segments_list)

            if total_duration and total_duration > 0:
                p = round((s_end / total_duration) * 100, 1)
                if root and progress_var:
                    try:
                        progress_var.set(p); root.update()
                    except: pass
                elif not skip_loop_progress:
                    print(f"Progress: {p}% ({s_end:.0f}/{total_duration:.0f}s)", flush=True)

        save_checkpoint(checkpoint_path, segments_list)
        if skip_loop_progress:
            print(f"Progress: 100.0% (Transcription finished successfully)", flush=True)
        if not detected_style: detected_style = detect_content_type(segments_list)

        srt_path = os.path.join(output_dir, os.path.splitext(os.path.basename(file_path))[0] + ".srt")
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, segment in enumerate(chunk_segments(segments_list, detected_style)):
                def fmt(t):
                    if t is None: t = 0
                    h, m, s, ms = int(t//3600), int((t%3600)//60), int(t%60), int((t%1)*1000)
                    return f"{h:02}:{m:02}:{s:02},{ms:03}"
                f.write(f"{i+1}\n{fmt(get_v(segment,'start'))} --> {fmt(get_v(segment,'end'))}\n{get_v(segment,'text','')}\n\n")

        print(f"✅ Saved: {srt_path}")
        if root: root.destroy()
        if os.path.exists(lock_file): os.remove(lock_file)

    except Exception as e:
        print(f"❌ Error: {e}")
        traceback.print_exc()
        if os.path.exists(lock_file):
            try: os.remove(lock_file)
            except: pass
        sys.exit(1)

if __name__ == "__main__":
    main()
