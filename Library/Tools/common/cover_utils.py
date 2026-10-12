#!/usr/bin/env python3
"""Generate platform-ready video covers from an existing frame or FFmpeg."""

from __future__ import annotations

import functools
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    import cv2
except ImportError:  # OpenCV is optional; brightness still works without it.
    cv2 = None

try:
    import numpy as np
except ImportError:
    np = None


COVER_SIZES = {
    "landscape": (1146, 717),
    "wide": (1920, 1080),
    "portrait": (1080, 1440),
    "share": (1080, 810),
}

_PLATFORM_ALIASES = {
    "bilibili": "bili",
    "bili": "bili",
    "wechat": "wechat",
    "weixin": "wechat",
    "wx": "wechat",
}

_COVER_STYLE_VERSION = "kaiti-alliance-hard-shadow-lower-card-v12"
_FONT_FALLBACKS = (
    os.path.join(os.path.dirname(__file__), "fonts", "LXGWWenKai-Medium.ttf"),
    "/System/Library/Fonts/ヒラギノ角ゴシック W9.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W8.ttc",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
)

_COVER_BACKGROUND = (216, 8, 0)
_COVER_TITLE = (255, 255, 255)
_COVER_SHADOW = (255, 255, 255)
_TITLE_CENTER_Y_RATIO = 0.62
_MIN_FRAME_BRIGHTNESS = 40.0
_MIN_FALLBACK_FRAME_BRIGHTNESS = 8.0
_SAMPLE_FRAME_COUNT = 16
_DENSE_SAMPLE_FRAME_COUNT = 64


def _normalize_orientation(orientation: str) -> Tuple[str, Tuple[int, int]]:
    key = str(orientation).strip().lower()
    if key in ("bilibili", "bili", "horizontal"):
        key = "landscape"
    if key in ("bilibili_wide", "bili_wide", "16:9", "16x9"):
        key = "wide"
    if key in ("bilibili_4x3", "bili_4x3", "4:3", "4x3"):
        key = "share"
    if key in ("wechat", "weixin", "wx", "vertical", "card"):
        key = "portrait"
    if key in ("wechat_share", "share_card"):
        key = "share"
    if key not in COVER_SIZES:
        raise ValueError(f"Unsupported cover orientation: {orientation}")
    return key, COVER_SIZES[key]


def _default_platform_orientation(platform_key: str) -> str:
    if platform_key == "bili":
        return "wide"
    if platform_key == "wechat":
        return "portrait"
    raise ValueError(f"Unsupported cover platform: {platform_key}")


def cover_output_path(
    video_path: str, platform: str, orientation: Optional[str] = None
) -> str:
    """Return a stable, platform-specific cover path beside a video."""
    key = _PLATFORM_ALIASES.get(str(platform).strip().lower())
    if not key:
        raise ValueError(f"Unsupported cover platform: {platform}")
    orientation_key, _ = _normalize_orientation(
        orientation or _default_platform_orientation(key)
    )
    if key == "bili":
        if orientation_key == "wide":
            suffix = "bili"
        elif orientation_key == "share":
            suffix = "bili.4x3"
        else:
            suffix = f"bili.{orientation_key}"
    elif key == "wechat":
        if orientation_key == "portrait":
            suffix = "wechat"
        elif orientation_key == "share":
            suffix = "wechat.share"
        else:
            suffix = f"wechat.{orientation_key}"
    else:
        suffix = key
    return os.path.splitext(os.path.abspath(video_path))[0] + f".cover.{suffix}.jpg"


def find_existing_frame(video_path: str) -> Optional[str]:
    """Find an existing still image that can seed a generated cover."""
    base = os.path.splitext(os.path.abspath(video_path))[0]
    original_base = base[:-8] if base.endswith("_hardsub") else base
    candidates = [
        base + ".jpg",
        base + ".jpeg",
        base + ".png",
        original_base + ".jpg",
        original_base + ".jpeg",
        original_base + ".png",
        original_base + ".poster.jpg",
    ]
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        try:
            with Image.open(candidate) as image:
                image.verify()
            return candidate
        except Exception:
            continue
    return None


def _find_ffmpeg() -> Optional[str]:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for path in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.isfile(path):
            return path
    return None


def _video_duration_seconds(video_path: str, ffmpeg: str) -> Optional[float]:
    try:
        result = subprocess.run(
            [ffmpeg, "-i", video_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=15,
            check=False,
        )
    except Exception:
        return None
    match = re.search(
        r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", result.stderr or ""
    )
    if not match:
        return None
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def _frame_brightness(path: str) -> float:
    with Image.open(path) as image:
        gray = image.convert("L").resize((64, 64))
        return sum(gray.getdata()) / (64 * 64)


def _image_brightness(image: Image.Image) -> float:
    gray = image.convert("L").resize((64, 64))
    return sum(gray.getdata()) / (64 * 64)


def _frame_is_usable(path: str) -> bool:
    """Whether a frame is bright enough to be preferred for a cover."""
    try:
        return (
            _frame_is_valid(path)
            and _frame_brightness(path) >= _MIN_FRAME_BRIGHTNESS
        )
    except Exception:
        return False


def _frame_is_valid(path: str) -> bool:
    """Reject corrupt, tiny, and near-black frames before cover generation."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < 3000:
            return False
        return _frame_brightness(path) >= _MIN_FALLBACK_FRAME_BRIGHTNESS
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def _face_cascades():
    if cv2 is None:
        return ()
    cascades = []
    cascade_names = (
        "haarcascade_frontalface_default.xml",
        "haarcascade_frontalface_alt2.xml",
        "haarcascade_profileface.xml",
    )
    for name in cascade_names:
        path = os.path.join(cv2.data.haarcascades, name)
        cascade = cv2.CascadeClassifier(path)
        if not cascade.empty():
            cascades.append((cascade, name == "haarcascade_profileface.xml"))
    return tuple(cascades)


def _boxes_match(first, second) -> bool:
    """Whether two Haar detections describe the same face."""
    x0, y0, w0, h0 = first
    x1, y1, w1, h1 = second
    ix = max(0, min(x0 + w0, x1 + w1) - max(x0, x1))
    iy = max(0, min(y0 + h0, y1 + h1) - max(y0, y1))
    intersection = ix * iy
    if intersection == 0:
        return False
    area0 = w0 * h0
    area1 = w1 * h1
    union = area0 + area1 - intersection
    return intersection / union >= 0.25 or intersection / min(area0, area1) >= 0.55


def _deduplicate_faces(detections):
    unique = []
    for face in detections:
        if not any(_boxes_match(face, existing) for existing in unique):
            unique.append(face)
    return unique


def _face_bboxes(image: Image.Image):
    """Return deduplicated face boxes, including profile-facing faces."""
    cascades = _face_cascades()
    if not cascades or np is None:
        return []

    rgb = image.convert("RGB")
    detection_widths = {image.width}
    if image.width > 640:
        detection_widths.add(640)

    detections = []
    for detection_width in sorted(detection_widths, reverse=True):
        if detection_width == image.width:
            candidate_image = rgb
            scale = 1.0
        else:
            detection_height = max(
                1, round(image.height * detection_width / image.width)
            )
            candidate_image = rgb.resize(
                (detection_width, detection_height), Image.Resampling.BILINEAR
            )
            scale = image.width / detection_width

        array = np.asarray(candidate_image)
        frame = cv2.cvtColor(array, cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray_variants = [gray]
        if float(np.mean(gray)) < 80.0:
            clahe = cv2.createCLAHE(
                clipLimit=2.5, tileGridSize=(8, 8)
            )
            gray_variants.append(clahe.apply(gray))

        min_size = (
            max(24, int(candidate_image.width * 0.025)),
            max(24, int(candidate_image.height * 0.025)),
        )

        for detection_gray in gray_variants:
            for cascade, detect_flipped_profile in cascades:
                target_gray = (
                    cv2.flip(detection_gray, 1)
                    if detect_flipped_profile
                    else detection_gray
                )
                faces = cascade.detectMultiScale(
                    target_gray,
                    scaleFactor=1.1,
                    minNeighbors=5,
                    minSize=min_size,
                )
                if faces is None or len(faces) == 0:
                    continue
                for face in np.asarray(faces).reshape(-1, 4):
                    x, y, width, height = map(int, face)
                    if detect_flipped_profile:
                        x = candidate_image.width - x - width
                    detections.append(
                        (
                            int(x * scale),
                            int(y * scale),
                            int(width * scale),
                            int(height * scale),
                        )
                    )

    return _deduplicate_faces(detections)


def _largest_face_bbox(image: Image.Image):
    faces = _face_bboxes(image)
    return max(faces, key=lambda face: face[2] * face[3]) if faces else None


def _face_score(
    image: Image.Image,
    faces: Sequence[Tuple[int, int, int, int]],
    brightness: float,
) -> float:
    """Score a frame after face detection has already been run."""
    if cv2 is None or np is None or not faces:
        return 2.0 * brightness

    gray = cv2.cvtColor(
        cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR), cv2.COLOR_BGR2GRAY
    )
    sharpness = min(1.0, cv2.Laplacian(gray, cv2.CV_64F).var() / 300.0)
    image_area = max(1, image.width * image.height)
    face_areas = [
        (width * height) / image_area for _, _, width, height in faces
    ]
    total_face_area_ratio = min(1.0, sum(face_areas))
    largest_face_area_ratio = max(face_areas)
    center_distances = [
        abs((x + width / 2) / image.width - 0.5)
        for x, _, width, _ in faces
    ]

    score = 35.0 * len(faces)
    score += 25.0 * total_face_area_ratio
    score += 10.0 * largest_face_area_ratio
    score += 4.0 * sharpness
    score += 2.0 * brightness
    score -= 1.5 * sum(center_distances) / len(center_distances)
    return score


def _frame_face_score(path: str) -> float:
    """Prefer the most people, then total face area, sharpness, and brightness."""
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        brightness = _frame_brightness(path) / 255.0
        faces = _face_bboxes(rgb)
        return _face_score(rgb, faces, brightness)


def _frame_face_rank(path: str) -> Tuple[int, float]:
    """Return face count first, then the subject score for cover ranking."""
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        faces = _face_bboxes(rgb)
        brightness = _frame_brightness(path) / 255.0
        return len(faces), _face_score(rgb, faces, brightness)


def _sample_video_frames(
    video_path: str,
    temp_dir: str,
    ffmpeg: str,
    duration: Optional[float],
    frame_count: Optional[int] = None,
) -> list[Tuple[float, str]]:
    """Sample a bounded set of timestamps instead of scanning the whole video.

    A whole-video low-fps scan can exceed its timeout on long files. Seeking a
    fixed number of timestamps keeps runtime bounded. Dim but visible frames
    are retained so dark videos can still get a subject-aware cover.
    """
    sample_count = max(2, int(frame_count or _SAMPLE_FRAME_COUNT))
    if duration and duration > 0:
        start = duration * 0.05
        end = duration * 0.95
        timestamps = [
            start + (end - start) * index / (sample_count - 1)
            for index in range(sample_count)
        ]
    else:
        timestamps = [
            1.0,
            5.0,
            10.0,
            20.0,
            30.0,
            45.0,
            60.0,
            90.0,
        ][:sample_count]

    frames = []
    for index, timestamp in enumerate(timestamps, start=1):
        candidate = os.path.join(temp_dir, f"sample-{index:04d}.jpg")
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-ss",
                    f"{max(0.0, timestamp):.3f}",
                    "-i",
                    video_path,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:-2",
                    "-q:v",
                    "3",
                    candidate,
                    "-y",
                    "-update",
                    "1",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except Exception:
            continue
        if _frame_is_valid(candidate):
            frames.append((max(0.0, timestamp), candidate))
    return frames


def extract_best_frame(video_path: str, output_path: str) -> str:
    """Extract a subject-aware, non-blank frame from a video."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found for cover frame extraction")

    duration = _video_duration_seconds(video_path, ffmpeg)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    candidates = []
    with tempfile.TemporaryDirectory(prefix="autosub-cover-") as temp_dir:
        sampled_frames = _sample_video_frames(
            video_path, temp_dir, ffmpeg, duration
        )
        for timestamp, frame in sampled_frames:
            face_count, score = _frame_face_rank(frame)
            candidates.append((score, face_count, frame, timestamp))

        # A single face is an acceptable fallback, but it is also weak evidence
        # that a multi-person interview was missed by sparse sampling.
        if candidates and max(item[1] for item in candidates) <= 1:
            dense_frames = _sample_video_frames(
                video_path,
                temp_dir,
                ffmpeg,
                duration,
                frame_count=_DENSE_SAMPLE_FRAME_COUNT,
            )
            existing_timestamps = {item[3] for item in candidates}
            for timestamp, frame in dense_frames:
                if timestamp in existing_timestamps:
                    continue
                face_count, score = _frame_face_rank(frame)
                candidates.append((score, face_count, frame, timestamp))

        if not candidates:
            raise RuntimeError(
                "ffmpeg could not extract a bright enough cover frame"
            )

        ranked = sorted(
            candidates, key=lambda item: (item[1], item[0]), reverse=True
        )
        validated = []
        selected = None
        for rank, (sample_score, sample_face_count, sample_path, timestamp) in enumerate(
            ranked[:8], start=1
        ):
            full_frame = os.path.join(temp_dir, f"validated-{rank}.jpg")
            extracted = False
            try:
                subprocess.run(
                    [
                        ffmpeg,
                        "-ss",
                        f"{timestamp:.3f}",
                        "-i",
                        video_path,
                        "-frames:v",
                        "1",
                        "-q:v",
                        "2",
                        full_frame,
                        "-y",
                        "-update",
                        "1",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=25,
                    check=False,
                )
                extracted = _frame_is_valid(full_frame)
            except Exception:
                extracted = False

            if not extracted:
                continue
            full_face_count, full_score = _frame_face_rank(full_frame)
            validated.append((full_score, full_face_count, full_frame, timestamp))
            # A real subject should keep most of its score after returning to
            # full resolution; low-resolution Haar false positives often do not.
            if (
                full_face_count >= sample_face_count
                and full_score >= max(5.0, sample_score * 0.6)
            ):
                selected = (full_score, full_face_count, full_frame, timestamp)
                break

        if selected is None and validated:
            selected = max(validated, key=lambda item: (item[1], item[0]))
        if selected is None:
            raise RuntimeError("ffmpeg could not extract a bright enough cover frame")

        best_score, best_face_count, best, timestamp = selected
        shutil.copy2(best, output_path)
        print(
            f">>> 封面选帧: {timestamp:.2f}s "
            f"(人脸: {best_face_count}, 主体评分: {best_score:.2f})"
        )
    return output_path


@functools.lru_cache(maxsize=1)
def _find_kaiti_font() -> Optional[Tuple[str, int]]:
    """Find a bold Kai face on macOS without depending on one asset hash."""
    paths = []
    fc_match = shutil.which("fc-match")
    if fc_match:
        try:
            result = subprocess.run(
                [fc_match, "-f", "%{file}", "Kaiti SC:style=Black"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                errors="replace",
                timeout=5,
                check=False,
            )
            matched = result.stdout.strip()
            if matched and os.path.isfile(matched):
                paths.append(matched)
        except Exception:
            pass

    paths.extend(
        glob.glob(
            "/System/Library/AssetsV2/com_apple_MobileAsset_Font8/*/AssetData/Kaiti.ttc"
        )
    )

    for wanted_style in ("Black", "Bold"):
        for path in paths:
            for index in range(12):
                try:
                    font = ImageFont.truetype(path, size=16, index=index)
                except Exception:
                    break
                family, style = font.getname()
                if (
                    family in ("Kaiti SC", "Kaiti TC", "STKaiti")
                    and style == wanted_style
                ):
                    return path, index
    return None


@functools.lru_cache(maxsize=1)
def _find_alliance_font() -> Optional[Tuple[str, int]]:
    """Find an authorized Alliance No. 2 face without accepting fc-match fallbacks."""
    paths = []
    fc_list = shutil.which("fc-list")
    if fc_list:
        try:
            result = subprocess.run(
                [fc_list, "Alliance No. 2", "file"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                errors="replace",
                timeout=5,
                check=False,
            )
            paths.extend(line.strip() for line in result.stdout.splitlines() if line.strip())
        except Exception:
            pass

    paths.extend(
        glob.glob("/Library/Fonts/*Alliance*No*2*")
        + glob.glob(os.path.join(os.path.dirname(__file__), "fonts", "*Alliance*No*2*"))
    )

    for path in paths:
        if not os.path.isfile(path):
            continue
        for index in range(16):
            try:
                font = ImageFont.truetype(path, size=16, index=index)
            except Exception:
                break
            family, _style = font.getname()
            normalized_family = family.lower().replace(" ", "")
            if normalized_family and "allianceno.2" in normalized_family:
                return path, index
    return None


@functools.lru_cache(maxsize=1)
def _find_latin_font() -> Tuple[str, int]:
    """Use Alliance No. 2 when installed, otherwise a close system grotesque."""
    alliance = _find_alliance_font()
    if alliance:
        return alliance
    if os.path.isfile("/System/Library/Fonts/HelveticaNeue.ttc"):
        return "/System/Library/Fonts/HelveticaNeue.ttc", 1
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
    ):
        if os.path.isfile(candidate):
            return candidate, 0
    return os.path.join(os.path.dirname(__file__), "fonts", "LXGWWenKai-Medium.ttf"), 0


def _load_title_fonts(size: int) -> Tuple[object, object]:
    kaiti = _find_kaiti_font()
    if kaiti:
        cjk_path, cjk_index = kaiti
    else:
        cjk_path, cjk_index = _FONT_FALLBACKS[0], 0

    latin_path, latin_index = _find_latin_font()
    try:
        cjk_font = ImageFont.truetype(cjk_path, size=size, index=cjk_index)
    except Exception:
        cjk_font = ImageFont.load_default()
    try:
        latin_font = ImageFont.truetype(latin_path, size=size, index=latin_index)
    except Exception:
        latin_font = ImageFont.load_default()
    return cjk_font, latin_font


def _is_cjk_character(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x2E80 <= codepoint <= 0x9FFF
        or 0x3000 <= codepoint <= 0x303F
        or 0x3040 <= codepoint <= 0x30FF
        or 0x31F0 <= codepoint <= 0x31FF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0xFE30 <= codepoint <= 0xFE4F
        or 0xFF00 <= codepoint <= 0xFFEF
    )


def _script_segments(text: str) -> Sequence[str]:
    segments = []
    current = ""
    current_cjk = None
    for character in text:
        is_cjk = _is_cjk_character(character)
        if current_cjk is None or is_cjk == current_cjk:
            current += character
            current_cjk = is_cjk
        else:
            segments.append(current)
            current = character
            current_cjk = is_cjk
    if current:
        segments.append(current)
    return segments


def _font_for_segment(segment: str, fonts: Tuple[object, object]):
    return fonts[0] if any(_is_cjk_character(c) for c in segment) else fonts[1]


def _line_metrics(line: str, fonts: Tuple[object, object]) -> Tuple[int, int, int]:
    width = 0
    ascent = 0
    descent = 0
    for segment in _script_segments(line):
        font = _font_for_segment(segment, fonts)
        width += font.getlength(segment)
        segment_ascent, segment_descent = font.getmetrics()
        ascent = max(ascent, segment_ascent)
        descent = max(descent, segment_descent)
    return int(width), ascent, descent


def _title_tokens(title: str) -> Sequence[str]:
    return re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*[,.!?;:]*|\s+|.", title)


def _wrap_title(title: str, fonts: Tuple[object, object], max_width: int) -> Sequence[str]:
    title = re.sub(r"\s+", " ", str(title)).strip() or "未命名视频"
    if _line_metrics(title, fonts)[0] <= max_width:
        return [title]

    tokens = _title_tokens(title)
    candidates = []
    for split in range(1, len(tokens)):
        first = "".join(tokens[:split]).strip()
        second = "".join(tokens[split:]).strip()
        if not first or not second:
            continue
        first_width, _, _ = _line_metrics(first, fonts)
        second_width, _, _ = _line_metrics(second, fonts)
        if first_width > max_width or second_width > max_width:
            continue
        score = abs(first_width - second_width)
        if len(first.replace(" ", "")) <= 2 or len(second.replace(" ", "")) <= 2:
            score += max_width
        candidates.append((score, first, second))

    if candidates:
        _, first, second = min(candidates, key=lambda item: item[0])
        return [first, second]

    # Extremely long single tokens cannot be split sensibly yet; the fitter will
    # shrink them and add an ellipsis if they still do not fit.
    return [title]


def _truncate_line(line: str, fonts: Tuple[object, object], max_width: int) -> str:
    if _line_metrics(line, fonts)[0] <= max_width:
        return line
    while line and _line_metrics(line + "...", fonts)[0] > max_width:
        line = line[:-1]
    return (line + "...").strip()


def _fit_font(title: str, width: int, start_size: int) -> Tuple[Tuple[object, object], Sequence[str]]:
    # Sentence-style headlines may span nearly the full cover. Keep a small
    # margin for the card padding and the hard shadow.
    max_width = int(width * 0.88)
    size = start_size
    while size >= 30:
        fonts = _load_title_fonts(size)
        lines = _wrap_title(title, fonts, max_width)
        if all(_line_metrics(line, fonts)[0] <= max_width for line in lines):
            return fonts, lines
        size -= 3

    fonts = _load_title_fonts(30)
    lines = _wrap_title(title, fonts, max_width)
    return fonts, [_truncate_line(line, fonts, max_width) for line in lines]


def _title_box_y(
    height: int,
    box_height: int,
    face_bbox: Optional[Tuple[int, int, int, int]],
) -> int:
    """Place the card in the lower middle, moving around the subject."""
    centered = int(height * _TITLE_CENTER_Y_RATIO - box_height / 2)
    if not face_bbox:
        return max(0, min(height - box_height, centered))

    _, face_y, _, face_height = face_bbox
    face_bottom = face_y + face_height
    margin = max(24, int(box_height * 0.12))
    overlaps_subject = (
        centered < face_bottom + margin
        and centered + box_height > face_y - margin
    )
    below = face_bottom + margin
    if overlaps_subject and below + box_height <= height:
        return below

    above = face_y - margin - box_height
    if overlaps_subject and above >= 0:
        return above

    if not overlaps_subject:
        return max(0, min(height - box_height, centered))

    below_clearance = height - face_bottom
    above_clearance = face_y
    if below_clearance >= above_clearance:
        return max(0, min(height - box_height, face_bottom + margin))
    return max(0, min(height - box_height, face_y - margin - box_height))


def _draw_title(
    image: Image.Image,
    title: str,
    orientation: str,
    face_bbox: Optional[Tuple[int, int, int, int]] = None,
) -> None:
    width, height = image.size
    if orientation == "portrait":
        font_size = 84
    elif orientation == "share":
        font_size = 74
    elif orientation == "wide":
        font_size = 94
    else:
        font_size = 78

    fonts, lines = _fit_font(title, width, font_size)
    metrics = [_line_metrics(line, fonts) for line in lines]
    line_heights = [ascent + descent for _, ascent, descent in metrics]
    line_gap = max(10, int(fonts[0].size * 0.16))
    text_height = sum(line_heights) + line_gap * (len(lines) - 1)
    text_width = max(item[0] for item in metrics)

    horizontal_padding = max(34, int(fonts[0].size * 0.58))
    vertical_padding = max(20, int(fonts[0].size * 0.34))
    compact_title_length = len(re.sub(r"\s+", "", str(title)))
    box_min_ratio = 0.88 if compact_title_length >= 14 else 0.70
    box_width = min(
        int(width * 0.94),
        max(int(width * box_min_ratio), text_width + horizontal_padding * 2),
    )
    box_height = min(height, text_height + vertical_padding * 2)
    box_x = (width - box_width) // 2
    box_y = _title_box_y(height, box_height, face_bbox)
    corner_radius = max(24, int(box_height * 0.24))
    shadow_offset = max(10, int(box_height * 0.12))

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (
            box_x + shadow_offset,
            box_y + shadow_offset,
            box_x + box_width + shadow_offset,
            box_y + box_height + shadow_offset,
        ),
        radius=corner_radius,
        fill=_COVER_SHADOW,
    )
    draw.rounded_rectangle(
        (box_x, box_y, box_x + box_width, box_y + box_height),
        radius=corner_radius,
        fill=_COVER_BACKGROUND,
    )

    y = box_y + vertical_padding
    for line, (_, ascent, _), line_height in zip(lines, metrics, line_heights):
        line_width = _line_metrics(line, fonts)[0]
        x = box_x + (box_width - line_width) / 2
        baseline = y + ascent
        for segment in _script_segments(line):
            font = _font_for_segment(segment, fonts)
            segment_ascent, _ = font.getmetrics()
            draw.text(
                (x, baseline - segment_ascent),
                segment,
                font=font,
                fill=_COVER_TITLE,
            )
            x += font.getlength(segment)
        y += line_height + line_gap


def _centering_for_face(
    source_size: Tuple[int, int],
    target_size: Tuple[int, int],
    face_bbox: Optional[Tuple[int, int, int, int]],
) -> Tuple[float, float]:
    """Keep the dominant face inside the platform crop."""
    if not face_bbox:
        return (0.5, 0.5)

    source_width, source_height = source_size
    target_width, target_height = target_size
    face_x, face_y, face_width, face_height = face_bbox
    source_ratio = source_width / source_height
    target_ratio = target_width / target_height

    if source_ratio > target_ratio:
        crop_width = round(source_height * target_ratio)
        max_left = max(0, source_width - crop_width)
        desired_left = face_x + face_width / 2 - crop_width / 2
        left = max(0, min(max_left, desired_left))
        center_x = left / max_left if max_left else 0.5
        return (center_x, 0.5)

    crop_height = round(source_width / target_ratio)
    max_top = max(0, source_height - crop_height)
    desired_top = face_y + face_height / 2 - crop_height / 2
    top = max(0, min(max_top, desired_top))
    center_y = top / max_top if max_top else 0.5
    return (0.5, center_y)


def _centering_for_faces(
    source_size: Tuple[int, int],
    target_size: Tuple[int, int],
    faces: Sequence[Tuple[int, int, int, int]],
) -> Tuple[float, float]:
    """Crop to keep the most visible people, not only the largest face."""
    if not faces:
        return (0.5, 0.5)

    source_width, source_height = source_size
    target_width, target_height = target_size
    source_ratio = source_width / source_height
    target_ratio = target_width / target_height

    if source_ratio > target_ratio:
        crop_width = round(source_height * target_ratio)
        max_left = max(0, source_width - crop_width)
        if max_left == 0:
            return (0.5, 0.5)

        best_left = 0
        best_key = (-1, -1.0, 0.0)
        for left in range(max_left + 1):
            full_count = 0
            visible_area = 0.0
            for x, _, width, height in faces:
                visible_width = max(
                    0, min(x + width, left + crop_width) - max(x, left)
                )
                visible_area += visible_width * height
                if visible_width == width:
                    full_count += 1
            center_bonus = -abs(left + crop_width / 2 - source_width / 2)
            key = (full_count, visible_area, center_bonus)
            if key > best_key:
                best_key = key
                best_left = left
        return (best_left / max_left, 0.5)

    crop_height = round(source_width / target_ratio)
    max_top = max(0, source_height - crop_height)
    if max_top == 0:
        return (0.5, 0.5)

    best_top = 0
    best_key = (-1, -1.0, 0.0)
    for top in range(max_top + 1):
        full_count = 0
        visible_area = 0.0
        for _, y, width, height in faces:
            visible_height = max(
                0, min(y + height, top + crop_height) - max(y, top)
            )
            visible_area += visible_height * width
            if visible_height == height:
                full_count += 1
        center_bonus = -abs(top + crop_height / 2 - source_height / 2)
        key = (full_count, visible_area, center_bonus)
        if key > best_key:
            best_key = key
            best_top = top
    return (0.5, best_top / max_top)


def _choose_source_frame(
    video_path: str,
    source_path: Optional[str],
    temporary_path: str,
) -> str:
    """Use an explicit still, or pick the better of an existing and new frame."""
    if source_path and os.path.isfile(source_path):
        return source_path

    existing = find_existing_frame(video_path)
    if existing and not _frame_is_valid(existing):
        existing = None
    extract_best_frame(video_path, temporary_path)
    if existing:
        existing_rank = _frame_face_rank(existing)
        extracted_rank = _frame_face_rank(temporary_path)
        if existing_rank >= extracted_rank:
            return existing
    return temporary_path


def _load_source_frame(video_path: str, source_path: Optional[str]) -> Image.Image:
    temporary_dir = None
    if not source_path or not os.path.isfile(source_path):
        temporary_dir = tempfile.TemporaryDirectory(prefix="autosub-cover-")
        temporary_path = os.path.join(temporary_dir.name, "source-frame.jpg")
        frame_path = _choose_source_frame(video_path, source_path, temporary_path)
    else:
        frame_path = source_path

    try:
        with Image.open(frame_path) as frame:
            return ImageOps.exif_transpose(frame).convert("RGB")
    finally:
        if temporary_dir is not None:
            temporary_dir.cleanup()


def _cover_cache_marker(title: str, orientation: str) -> bytes:
    title_digest = hashlib.sha256(str(title).encode("utf-8")).hexdigest()
    metadata = {
        "app": "autosub",
        "type": "cover",
        "style_version": _COVER_STYLE_VERSION,
        "orientation": orientation,
        "title": str(title),
        "title_sha256": title_digest,
    }
    return json.dumps(
        metadata,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _legacy_cover_cache_marker(title: str, orientation: str) -> bytes:
    title_digest = hashlib.sha256(str(title).encode("utf-8")).hexdigest()
    return (
        f"autosub-cover-style:{_COVER_STYLE_VERSION}:{orientation}:{title_digest}"
    ).encode("utf-8")


def _decode_cover_comment(comment) -> Optional[dict]:
    if isinstance(comment, str):
        comment = comment.encode("utf-8")
    if not comment:
        return None
    try:
        value = json.loads(comment.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_cover_metadata(path: str) -> Optional[dict]:
    """Read the title and style metadata embedded in a generated cover."""
    try:
        with Image.open(path) as image:
            metadata = _decode_cover_comment(image.info.get("comment", b""))
            if metadata is not None:
                return metadata
            legacy = image.info.get("comment", b"")
            if isinstance(legacy, str):
                legacy = legacy.encode("utf-8")
            if legacy.startswith(b"autosub-cover-style:"):
                parts = legacy.split(b":", 3)
                if len(parts) == 4:
                    return {
                        "app": "autosub",
                        "type": "cover",
                        "style_version": parts[1].decode("utf-8", "replace"),
                        "orientation": parts[2].decode("utf-8", "replace"),
                        "title": None,
                        "title_sha256": parts[3].decode("ascii", "replace"),
                        "legacy_marker": True,
                    }
    except Exception:
        pass
    return None


def _cover_style_matches(path: str, title: str, orientation: str) -> bool:
    try:
        with Image.open(path) as image:
            comment = image.info.get("comment", b"")
            metadata = _decode_cover_comment(comment)
            if metadata is not None:
                title_digest = hashlib.sha256(str(title).encode("utf-8")).hexdigest()
                return (
                    metadata.get("style_version") == _COVER_STYLE_VERSION
                    and metadata.get("orientation") == orientation
                    and metadata.get("title_sha256") == title_digest
                )
            if isinstance(comment, str):
                comment = comment.encode("utf-8")
            return comment == _legacy_cover_cache_marker(title, orientation)
    except Exception:
        return False


def cover_style_matches(path: str, title: str, orientation: str) -> bool:
    """Public cache check used by uploaders before sending a cover upstream."""
    return _cover_style_matches(path, title, orientation)


def generate_cover(
    video_path: str,
    output_path: str,
    title: str,
    orientation: str = "landscape",
    source_path: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Generate a video-frame cover with a hard-shadowed red title card."""
    orientation_key, size = _normalize_orientation(orientation)
    output_path = os.path.abspath(output_path)
    if (
        os.path.isfile(output_path)
        and not overwrite
        and _cover_style_matches(output_path, title, orientation_key)
    ):
        print(f">>> 复用已有封面: {os.path.basename(output_path)}")
        return output_path

    source = _load_source_frame(video_path, source_path)
    if _image_brightness(source) < _MIN_FALLBACK_FRAME_BRIGHTNESS:
        raise RuntimeError("cover source frame is too dark")
    centering = _centering_for_faces(source.size, size, _face_bboxes(source))
    cover = ImageOps.fit(
        source,
        size,
        method=Image.Resampling.LANCZOS,
        centering=centering,
    )
    _draw_title(
        cover,
        title,
        orientation_key,
        face_bbox=_largest_face_bbox(cover),
    )
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    cover.save(
        output_path,
        "JPEG",
        quality=92,
        optimize=True,
        comment=_cover_cache_marker(title, orientation_key),
    )
    print(f">>> 已生成封面: {os.path.basename(output_path)} ({size[0]}x{size[1]})")
    return output_path


def generate_platform_cover(
    video_path: str,
    platform: str,
    title: str,
    source_path: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Generate the default cover shape for a supported platform."""
    key = _PLATFORM_ALIASES.get(str(platform).strip().lower())
    if not key:
        raise ValueError(f"Unsupported cover platform: {platform}")
    orientation = _default_platform_orientation(key)
    return generate_cover(
        video_path,
        cover_output_path(video_path, platform, orientation),
        title,
        orientation=orientation,
        source_path=source_path,
        overwrite=overwrite,
    )


def generate_platform_covers(
    video_path: str,
    platform: str,
    title: str,
    source_path: Optional[str] = None,
    overwrite: bool = False,
) -> Tuple[str, ...]:
    """Generate every cover shape used by a platform from one source frame."""
    key = _PLATFORM_ALIASES.get(str(platform).strip().lower())
    if not key:
        raise ValueError(f"Unsupported cover platform: {platform}")
    orientations = ("wide", "share") if key == "bili" else ("portrait", "share")

    source = source_path if source_path and os.path.isfile(source_path) else None
    temporary_source = None
    if not source:
        temporary_source = tempfile.NamedTemporaryFile(
            prefix="autosub-cover-frame-", suffix=".jpg", delete=False
        )
        temporary_source.close()
        source = _choose_source_frame(
            video_path, source_path, temporary_source.name
        )

    try:
        return tuple(
            generate_cover(
                video_path,
                cover_output_path(video_path, platform, orientation),
                title,
                orientation=orientation,
                source_path=source,
                overwrite=overwrite,
            )
            for orientation in orientations
        )
    finally:
        if temporary_source:
            try:
                os.remove(temporary_source.name)
            except OSError:
                pass
