import os
import sys
import json
import time
import shutil

try:
    import mlx_whisper
    HAS_MLX = True
except ImportError:
    HAS_MLX = False

class MLXSegment:
    def __init__(self, start, end, text, words=None):
        self.start = start
        self.end = end
        self.text = text
        self.words = words or []

def transcribe_mlx(file_path, model_name="large-v2", initial_prompt=None, clip_timestamps=None, total_duration=0, checkpoint_path=None, language=None):
    """
    Transcribe using mlx-whisper for Apple Silicon GPU acceleration.
    """
    if not HAS_MLX:
        raise ImportError("mlx-whisper not installed")

    # Optimizing Memory and VRAM allocation on Mac: disable hoarding cache
    try:
        import mlx.core as mx
        mx.set_cache_limit(0)
        mx.clear_cache()
        print(f"⚙️ [MLX Memory Optimizer] Set GPU allocation cache limit to 0 and cleared cache.", flush=True)
    except Exception as e:
        print(f"⚠️ [MLX Memory Optimizer] Warning: failed to configure cache limit: {e}", flush=True)

    print(f"🚀 [MLX Mode] Using Apple Silicon GPU acceleration...", flush=True)

    # Resolve Hugging Face Repo Name accurately
    if "/" in model_name:
        path_or_hf_repo = model_name
    else:
        m_lower = model_name.lower().strip()
        if m_lower == "turbo":
            path_or_hf_repo = "mlx-community/whisper-large-v3-turbo"
        elif m_lower == "large-v3-turbo":
            path_or_hf_repo = "mlx-community/whisper-large-v3-turbo"
        elif m_lower in ["tiny", "base", "small", "medium"]:
            path_or_hf_repo = f"mlx-community/whisper-{m_lower}"
        elif m_lower in ["large", "large-v1", "large-v2", "large-v3"]:
            path_or_hf_repo = f"mlx-community/whisper-{m_lower}-mlx"
        else:
            path_or_hf_repo = f"mlx-community/whisper-{model_name}-mlx"

    kwargs = {
        "path_or_hf_repo": path_or_hf_repo,
        "initial_prompt": initial_prompt,
        "verbose": True,
        "word_timestamps": True,
        # NOTE: beam_size is NOT supported by mlx-whisper (Beam search decoder
        # is not yet implemented). Omitting it avoids the MLX load failure
        # that previously forced a fallback to the slower CPU engine.
        **({"language": language} if language else {}),
        # --- Anti-hallucination guards (fixes "it's the most" feedback loop) ---
        # Stop the previous window's text from seeding the next one over silence.
        "condition_on_previous_text": False,
        # Skip silence longer than 2s instead of hallucinating over it.
        "hallucination_silence_threshold": 2.0,
    }

    # --- External Silero VAD pre-segmentation ---
    # mlx-whisper has no built-in VAD. Run Silero (via faster_whisper.vad) to
    # find speech regions, then feed them as clip_timestamps so the decoder
    # never sees long silences where hallucination loops originate.
    if not clip_timestamps:
        try:
            from faster_whisper.vad import VadOptions, get_speech_timestamps, get_vad_model
            import numpy as np
            import wave as _wave
            import subprocess as _sp
            import tempfile as _tmpf
            # Silero VAD needs 16kHz mono PCM. Input may be a video (mp4/mkv)
            # or non-WAV audio, so decode to a temp WAV via ffmpeg first.
            _vad_wav = _tmpf.mktemp(suffix="_vad.wav")
            _ff = shutil.which("ffmpeg") or "ffmpeg"
            _sp.run([_ff, "-y", "-i", file_path, "-vn", "-ac", "1",
                     "-ar", "16000", "-c:a", "pcm_s16le", _vad_wav],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, check=True)
            with _wave.open(_vad_wav, "rb") as _wf:
                _sr = _wf.getframerate()
                _raw = _wf.readframes(_wf.getnframes())
            try:
                os.remove(_vad_wav)
            except OSError:
                pass
            _audio = np.frombuffer(_raw, dtype=np.int16).astype(np.float32) / 32768.0
            _ts = get_speech_timestamps(_audio, VadOptions(), sampling_rate=_sr)
            if _ts:
                _clips = []
                for _seg in _ts:
                    _clips.extend([round(_seg["start"] / _sr, 3),
                                   round(_seg["end"] / _sr, 3)])
                kwargs["clip_timestamps"] = _clips
                print(f"\u23ec\ufe0f\u200d[ VAD] {len(_ts)} speech segments pre-isolated.", flush=True)
            else:
                print("VAD: no speech detected; transcribing whole file.", flush=True)
        except Exception as _e:
            print(f"VAD unavailable ({_e}); transcribing without pre-segmentation.", flush=True)

    if clip_timestamps:
        # clip_timestamps can be [start, end]
        # Safety: subtract a tiny bit from end to avoid MLX hanging bug
        start, end = clip_timestamps
        kwargs["clip_timestamps"] = [float(start), max(float(start), float(end) - 0.1)]

    # Real-time stdout interception to extract segments and print progress
    import re
    original_stdout = sys.stdout
    # Pattern to match: [MM:SS.mmm --> MM:SS.mmm] text or [HH:MM:SS.mmm --> HH:MM:SS.mmm] text
    pattern = re.compile(r"^\[(?:(\d{2}):)?(\d{2}):(\d{2})\.(\d{3}) --> (?:(\d{2}):)?(\d{2}):(\d{2})\.(\d{3})\] (.*)")

    def parse_timestamp(h_str, m_str, s_str, ms_str):
        h = int(h_str) if h_str else 0
        m = int(m_str)
        s = int(s_str)
        ms = int(ms_str)
        return h * 3600 + m * 60 + s + ms / 1000.0

    def handle_line(line):
        line_strip = line.strip()
        m = pattern.match(line_strip)
        if m:
            end_time = parse_timestamp(m.group(5), m.group(6), m.group(7), m.group(8))
            if total_duration > 0:
                p = round((end_time / total_duration) * 100, 1)
                original_stdout.write(f"Progress: {p}% ({end_time:.0f}/{total_duration:.0f}s)\n")
                original_stdout.flush()
        else:
            # Forward other prints to real stdout
            original_stdout.write(line + "\n")
            original_stdout.flush()

    class StdoutRedirector:
        def __init__(self):
            self.buffer = ""

        def write(self, s):
            self.buffer += s
            while "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                handle_line(line)

        def flush(self):
            original_stdout.flush()

    sys.stdout = StdoutRedirector()
    try:
        # mlx-whisper.transcribe returns a dict: {"text": "...", "segments": [...]}
        result = mlx_whisper.transcribe(file_path, **kwargs)
    finally:
        sys.stdout = original_stdout

    segments = result.get("segments", [])
    for s in segments:
        words = []
        if "words" in s:
            for w in s["words"]:
                # Normalize word format to match faster-whisper (getattr compatible)
                words.append(type('Word', (), {'start': w['start'], 'end': w['end'], 'word': w['word'], 'probability': w.get('probability', 0)}))
        
        yield MLXSegment(s["start"], s["end"], s["text"], words=words)

    # Clean up and release Metal allocations back to OS immediately
    try:
        import mlx.core as mx
        mx.clear_cache()
        print(f"🧹 [MLX Memory Optimizer] Cleared Metal allocation cache upon completion.", flush=True)
    except:
        pass

def is_apple_silicon():
    if sys.platform != "darwin": return False
    # Use sysctl to check for arm64
    try:
        import subprocess
        output = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"]).decode("utf-8").lower()
        return "apple" in output
    except:
        return False
