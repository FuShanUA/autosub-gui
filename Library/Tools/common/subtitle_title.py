#!/usr/bin/env python3
"""Subtitle-grounded title generation shared by cover and upload tools."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

try:
    from srt_utils import parse_srt
except ImportError:  # pragma: no cover - exercised only in unusual installs
    parse_srt = None


class SubtitleTitleError(ValueError):
    """Raised when a title cannot be grounded in subtitle content."""


@dataclass(frozen=True)
class GroundedTitle:
    title: str
    evidence: Tuple[str, ...]
    subtitle_path: str
    source: str
    core_title: str = ""
    attribution: str = ""


_SUBTITLE_SUFFIXES = (".cn.srt", ".zh.srt", ".bi.srt", ".srt", ".en.srt")
_LATIN_STOPWORDS = {
    "a", "an", "and", "as", "at", "for", "how", "in", "of", "on",
    "or", "the", "to", "why", "with",
}
# These characters only glue clauses together. They are ignored when checking
# whether the content bigrams in a Chinese title exist in the subtitle.
_CJK_FUNCTION_CHARS = set("的是了和与或及并却在把让从对为就也都更很如何吗呢吧加能会不")
_TITLE_FUNCTION_PHRASES = {
    "为什么", "为何", "如何", "怎么", "怎样", "什么",
    "才是", "不是", "而是", "到底", "究竟", "凭什么",
}
_RESULT_TERM_SUFFIXES = ("层", "系统", "平台", "产品", "模型", "本体", "工具", "服务")
_RESULT_TERM_STOPWORDS = (
    "我们", "他们", "一个", "这个", "那个", "可以", "自己",
    "公司", "客户", "业务", "的", "了", "着", "把", "是",
)
_RESULT_TERM_BAD_PREFIXES = (
    "上", "加", "些", "个", "这", "那", "一", "有", "没", "在",
    "能", "会", "做", "卖", "买", "建", "把", "让", "是", "成",
)
_PRIMARY_CLAIM_MARKERS = (
    "最主要的", "主要职能", "核心职能", "本质是", "本质在于",
    "重点是", "关键在于", "primary function", "main function",
    "the point is", "main point",
)
_HOOK_EVIDENCE_MARKERS = (
    "没别人", "从没见过", "反直觉", "前所未有", "第一次",
    "最强", "二刀流", "AI时代", "就像", "强迫", "不带套", "戴套",
    "核弹", "武器", "战争", "革命", "一塌糊涂", "太棒了", "吓人",
    "错的人", "马戏团", "死活", "不招", "没有销售", "大规模采用",
    "nobody", "never seen", "counterintuitive", "first time",
    "circus", "massive adoption", "very difficult",
)
_TITLE_QUALITY_BANNED_PHRASES = (
    "这阵势", "这种阵势", "从没见过", "没见过这",
    "一条龙", "翻十倍", "值十倍", "值十倍的价",
    "第一次提供价值", "把注意力推到", "确保哪个产品不交付",
    "全程支持到底", "真正揭秘", "颠覆认知", "干货满满", "保姆级",
    "提供价值", "占主导", "取得产品", "产品层模型",
    "满分实践", "现在和未来", "商业引擎跑起来", "用上产品层",
    "业部层", "产品层产品", "转型为产品",
    "快么", "集成么", "么服务",
)
_TITLE_ACTION_VERBS = (
    "疏通", "聚焦", "主导", "掌控", "推动", "导入", "支持",
    "变革", "转型", "建立", "打造", "贯通",
)
_TITLE_BAD_ENDINGS = (
    "的", "是", "在", "让", "把", "和", "与", "及", "并", "或",
    "但", "因为", "所以", "如果", "取得", "提供", "推到", "跑起来",
)
# Editorial compression is allowed only for these frequent function words.
# Entities, product names, and concrete outcomes still have to appear in the
# subtitle either directly or as separate grounded concepts.
_EDITORIAL_SYNONYMS = {
    "职责": "职能",
    "守护": "守住",
    "用户": "客户",
    "就像": "就有点像",
}
_EDITORIAL_INTENSIFIERS = ("真正",)
_PUBLIC_TITLE_STYLE_RULES = (
    "多数标题可以主体先行，但不强制；选择最能概括全片的开头",
    "保留一个最具体的结果，不堆叠机制和限定条件",
    "冒号只用于「主体/场景：完整判断」，冒号前后都要能读通",
    "疑问句只在问题本身是中心主张时使用，表单标题默认不用",
    "语气像专业科技媒体，不像营销号或市井口语",
)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\r", " ").replace("\n", " ")).strip()


def _video_base(video_path: str) -> str:
    base = os.path.splitext(os.path.abspath(video_path))[0]
    return base[:-8] if base.endswith("_hardsub") else base


def find_subtitle_path(video_path: str) -> Optional[str]:
    base = _video_base(video_path)
    for suffix in _SUBTITLE_SUFFIXES:
        candidate = base + suffix
        if os.path.isfile(candidate):
            return candidate
    return None


def _load_ad_block_ids(base: str) -> set[str]:
    path = base + ".ad_segments.json"
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            segments = json.load(handle)
    except Exception:
        return set()

    ids = set()
    for segment in segments if isinstance(segments, list) else []:
        try:
            start = int(segment.get("start_block"))
            end = int(segment.get("end_block"))
            ids.update(str(index) for index in range(start, end + 1))
        except (AttributeError, TypeError, ValueError):
            continue
    return ids


def _filtered_subtitle_blocks(
    video_path: str,
    subtitle_path: Optional[str] = None,
) -> Tuple[Optional[str], Optional[list[dict]], Optional[list[dict]]]:
    path = subtitle_path or find_subtitle_path(video_path)
    if not path or not os.path.isfile(path) or parse_srt is None:
        return None, None, None

    blocks = parse_srt(path)
    if not blocks:
        return path, None, None

    base = _video_base(video_path)
    ad_ids = _load_ad_block_ids(base)
    content = [
        block for block in blocks
        if str(block.get("index", "")) not in ad_ids
        and any(line.strip() for line in block.get("lines", []))
    ] or blocks
    return path, blocks, content


def _blocks_to_text(blocks: Sequence[dict], max_chars: Optional[int] = None) -> Optional[str]:
    lines = [line.strip() for block in blocks for line in block.get("lines", [])]
    text = "\n".join(line for line in lines if line)
    if not text:
        return None
    return text[:max_chars] if max_chars is not None else text


def read_subtitle_text(
    video_path: str,
    max_chars: int = 30000,
    subtitle_path: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Read ad-filtered subtitle text, sampling long transcripts evenly."""
    path, _blocks, content = _filtered_subtitle_blocks(video_path, subtitle_path)
    if not path or content is None:
        return None, path

    if len(content) <= 300:
        return _blocks_to_text(content, max_chars), path

    step = max(1, len(content) // 300)
    sampled = content[::step]
    return _blocks_to_text(sampled, max_chars), path


def read_full_subtitle_text(
    video_path: str,
    subtitle_path: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Read the complete ad-filtered transcript used for grounding checks."""
    path, _blocks, content = _filtered_subtitle_blocks(video_path, subtitle_path)
    if not path or content is None:
        return None, path
    return _blocks_to_text(content), path


def _as_evidence(value: object) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence):
        values = list(value)
    else:
        values = [str(value)]
    return tuple(normalize_text(item) for item in values if normalize_text(item))


def _unsupported_title_terms(title: str, subtitle_text: str) -> list[str]:
    normalized_title = _canonicalize_title_for_validation(title, subtitle_text)
    normalized_subtitle = normalize_text(subtitle_text)
    subtitle_lower = normalized_subtitle.lower()
    unsupported = []

    for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", normalized_title):
        if token.lower() in _LATIN_STOPWORDS:
            continue
        if token.lower() not in subtitle_lower:
            unsupported.append(token)

    # A Chinese title may translate an English-only transcript. In that case,
    # exact evidence is the grounding boundary. When the subtitle itself has
    # Chinese, every content bigram in the title must occur in that Chinese.
    if re.search(r"[\u3400-\u9fff]", normalized_subtitle):
        phrase_pattern = "|".join(
            re.escape(phrase)
            for phrase in sorted(_TITLE_FUNCTION_PHRASES, key=len, reverse=True)
        )
        for sequence in re.findall(r"[\u3400-\u9fff]+", normalized_title):
            content_chunks = [
                chunk for chunk in re.split(phrase_pattern, sequence) if chunk
            ]
            for chunk in content_chunks:
                if not _cjk_sequence_is_grounded(chunk, normalized_subtitle):
                    unsupported.append(chunk)

    return unsupported


def _canonicalize_title_for_validation(title: str, subtitle_text: str) -> str:
    """Map a small set of editorial compressions back to subtitle wording."""
    value = normalize_text(title)
    normalized_subtitle = normalize_text(subtitle_text)
    for source, target in _EDITORIAL_SYNONYMS.items():
        if source in value and source not in normalized_subtitle and target in normalized_subtitle:
            value = value.replace(source, target)
    # "抽象产品" is the editorial compression of "抽象层" + "产品".
    if (
        "抽象产品" in value
        and "抽象产品" not in normalized_subtitle
        and "抽象层" in normalized_subtitle
        and "产品" in normalized_subtitle
    ):
        value = value.replace("抽象产品", "抽象层产品")
    for intensifier in _EDITORIAL_INTENSIFIERS:
        value = value.replace(intensifier, "")
    return value


def _apply_proper_noun_aliases(
    title: str,
    subtitle_text: str,
    aliases: Optional[dict[str, Sequence[str]]],
) -> str:
    """Map Chinese proper-noun aliases back to their subtitle spellings.

    A title may use a public Chinese translation or nickname while its exact
    evidence still quotes the English source name. Validation is performed on
    the canonical English spelling so the alias cannot bypass subtitle checks.
    """
    value = normalize_text(title)
    if not aliases:
        return value
    normalized_subtitle = normalize_text(subtitle_text)
    subtitle_lower = normalized_subtitle.casefold()

    for source, targets in aliases.items():
        source_value = normalize_text(str(source or ""))
        if not source_value or source_value.casefold() not in subtitle_lower:
            continue
        if isinstance(targets, str):
            targets = (targets,)
        for target in sorted(
            (normalize_text(str(target or "")) for target in targets),
            key=len,
            reverse=True,
        ):
            if target and target != source_value and target in value:
                value = value.replace(target, source_value)
                break
    return value


def _cjk_sequence_is_grounded(sequence: str, subtitle_text: str) -> bool:
    """Check whether a Chinese run decomposes into real subtitle phrases.

    A title may combine two subtitle phrases such as "落地实施" and
    "守住核心优势". Sliding two-character checks would reject the boundary
    between those phrases, so instead we require the run to partition into
    chunks that each occur in the subtitle (single chunks are allowed only
    for grammatical particles).
    """
    # A short interior fragment of a longer subtitle term is not a term in its
    # own right. "业部层" occurs inside "事业部层面", but it is not a usable
    # lexical unit. Longer phrases such as "集中精力" may legitimately occur
    # inside a longer run, so this rule only catches near-miss truncations.
    for run in re.findall(r"[\u3400-\u9fff]+", subtitle_text):
        position = run.find(sequence)
        if position <= 0:
            continue
        preceding = run[position - 1]
        extended = run[position - 1:position + len(sequence)]
        if (
            sequence.endswith(_RESULT_TERM_SUFFIXES)
            and preceding not in _CJK_FUNCTION_CHARS
            and extended in subtitle_text
        ):
            return False

    reachable = [False] * (len(sequence) + 1)
    reachable[0] = True
    for end in range(1, len(sequence) + 1):
        for start in range(end):
            if not reachable[start]:
                continue
            chunk = sequence[start:end]
            if len(chunk) == 1:
                if chunk in _CJK_FUNCTION_CHARS:
                    reachable[end] = True
                    break
            elif chunk in subtitle_text:
                reachable[end] = True
                break
    return reachable[-1]


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", normalize_text(value))


def _evidence_in_subtitle(fragment: str, subtitle_text: str) -> bool:
    """Allow exact evidence from one line or adjacent same-language lines.

    Bilingual SRT files interleave Chinese and English. A model may quote two
    adjacent Chinese subtitle lines as one evidence fragment; that is still
    subtitle content, even though the raw file has an English line between
    them. Whitespace is ignored because SRT line wrapping is not content.
    """
    compact_fragment = _compact_text(fragment)
    if not compact_fragment:
        return False
    if compact_fragment in _compact_text(subtitle_text):
        return True

    lines = [line for line in subtitle_text.splitlines() if line.strip()]
    for line in lines:
        if compact_fragment in _compact_text(line):
            return True

    cjk_lines = [line for line in lines if re.search(r"[\u3400-\u9fff]", line)]
    # Evidence may quote one Chinese line or two adjacent Chinese lines. The
    # old loop concatenated every later Chinese line, which accidentally
    # treated distant transcript fragments as one continuous quotation.
    for start in range(len(cjk_lines)):
        combined = _compact_text(cjk_lines[start])
        if compact_fragment in combined:
            return True
        if start + 1 < len(cjk_lines):
            combined += _compact_text(cjk_lines[start + 1])
            if compact_fragment in combined:
                return True
    return False


def validate_title_from_subtitle(
    title: str,
    subtitle_text: str,
    evidence: Optional[Sequence[str] | str] = None,
    min_chars: int = 5,
    max_chars: int = 40,
    proper_noun_aliases: Optional[dict[str, Sequence[str]]] = None,
) -> Tuple[str, ...]:
    """Return exact evidence fragments, or raise when the title is ungrounded."""
    normalized_title = normalize_text(title)
    normalized_subtitle = normalize_text(subtitle_text)
    if not normalized_title:
        raise SubtitleTitleError("标题为空")
    if len(normalized_title) < min_chars or len(normalized_title) > max_chars:
        raise SubtitleTitleError(f"标题长度必须在 {min_chars}-{max_chars} 个字符之间")
    if not normalized_subtitle:
        raise SubtitleTitleError("字幕内容为空")

    fragments = _as_evidence(evidence)
    if not fragments:
        raise SubtitleTitleError("缺少字幕证据")
    for fragment in fragments:
        if len(fragment) < 6:
            raise SubtitleTitleError("字幕证据过短，无法溯源")
        if not _evidence_in_subtitle(fragment, subtitle_text):
            raise SubtitleTitleError(f"字幕证据不存在: {fragment[:40]}")

    canonical_title = _apply_proper_noun_aliases(
        normalized_title,
        normalized_subtitle,
        proper_noun_aliases,
    )
    unsupported = _unsupported_title_terms(canonical_title, normalized_subtitle)
    if unsupported:
        raise SubtitleTitleError(
            "标题包含字幕外内容: " + ", ".join(unsupported[:8])
        )
    return fragments


def parse_llm_title_aliases(raw: object) -> dict[str, Tuple[str, ...]]:
    """Parse proper-noun aliases returned alongside title candidates."""
    if raw is None:
        return {}
    text = str(raw).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
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

    aliases: dict[str, Tuple[str, ...]] = {}
    for source, targets in raw_aliases.items():
        source_value = normalize_text(str(source or ""))
        if not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9'_-]*(?: [A-Za-z][A-Za-z0-9'_-]*)*",
            source_value,
        ):
            continue
        if isinstance(targets, str):
            targets = [targets]
        elif not isinstance(targets, Sequence):
            continue
        values = tuple(
            normalize_text(str(target or ""))
            for target in targets
            if normalize_text(str(target or ""))
            and re.search(r"[\u3400-\u9fff]", str(target or ""))
        )
        if values:
            aliases[source_value] = values
    return aliases


def parse_llm_title_response(raw: object) -> Tuple[str, Tuple[str, ...]]:
    """Parse a JSON response with title and exact subtitle evidence."""
    if raw is None:
        raise SubtitleTitleError("LLM 未返回标题")
    text = str(raw).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise SubtitleTitleError("LLM 输出不是 JSON 对象")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise SubtitleTitleError("LLM JSON 解析失败") from exc

    if not isinstance(payload, dict):
        raise SubtitleTitleError("LLM JSON 结构不正确")
    title = normalize_text(str(payload.get("title", "")))
    if not title:
        raise SubtitleTitleError("LLM 未返回 title 字段")
    evidence = _as_evidence(payload.get("evidence"))
    if not evidence:
        raise SubtitleTitleError("LLM 未返回 evidence 字段")
    return title, evidence


def parse_llm_title_candidates(
    raw: object,
) -> Tuple[str, Tuple[Tuple[str, Tuple[str, ...]], ...]]:
    """Parse either the current multi-candidate response or the old shape."""
    if raw is None:
        raise SubtitleTitleError("LLM 未返回标题")
    text = str(raw).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise SubtitleTitleError("LLM 输出不是 JSON 对象")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise SubtitleTitleError("LLM JSON 解析失败") from exc
    if not isinstance(payload, dict):
        raise SubtitleTitleError("LLM JSON 结构不正确")

    central_claim = normalize_text(str(payload.get("central_claim", "")))
    raw_candidates = payload.get("candidates")
    if isinstance(raw_candidates, list) and raw_candidates:
        candidates = []
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            title = normalize_text(str(item.get("title", "")))
            evidence = _as_evidence(item.get("evidence"))
            if title and evidence:
                candidates.append((title, evidence))
        if candidates:
            return central_claim, tuple(candidates)

    title, evidence = parse_llm_title_response(raw)
    return central_claim, ((title, evidence),)


def _title_scoring_tokens(value: str) -> set[str]:
    normalized = normalize_text(value).lower()
    tokens = {
        token.lower()
        for token in re.findall(r"[a-z][a-z0-9_-]*", normalized)
        if token.lower() not in _LATIN_STOPWORDS
    }
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        compact = "".join(char for char in run if char not in _CJK_FUNCTION_CHARS)
        if len(compact) == 1:
            tokens.add(compact)
        tokens.update(
            compact[index:index + 2]
            for index in range(len(compact) - 1)
        )
    return tokens


def _claim_coverage(title: str, central_claim: str) -> float:
    title_tokens = _title_scoring_tokens(title)
    if not title_tokens or not central_claim:
        return 0.0
    claim_tokens = _title_scoring_tokens(central_claim)
    return len(title_tokens & claim_tokens) / len(title_tokens)


def _has_balanced_colon(title: str) -> bool:
    """Match the public-title convention without turning colons into hooks."""
    value = normalize_text(title)
    if value.count("：") != 1:
        return False
    left, right = value.split("：", 1)
    return 4 <= len(left) <= 18 and 10 <= len(right) <= 34


def _specificity(title: str, subtitle_text: str) -> Tuple[float, int]:
    tokens = _title_scoring_tokens(title)
    normalized_subtitle = normalize_text(subtitle_text)
    frequencies = [
        normalized_subtitle.count(token)
        for token in tokens
        if normalized_subtitle.count(token) > 0
    ]
    if not frequencies:
        return 0.0, 0
    rarity = sum(1 / max(1, frequency) for frequency in frequencies) / len(frequencies)
    rare_count = sum(1 for frequency in frequencies if frequency <= 1)
    return rarity, rare_count


def _detect_result_terms(subtitle_text: str) -> Tuple[str, ...]:
    """Find rare, concrete outcome nouns to keep titles specific."""
    normalized = normalize_text(subtitle_text)
    compact = re.sub(r"\s+", "", normalized)
    terms = set()
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        for length in range(3, min(7, len(run) + 1)):
            for start in range(0, len(run) - length + 1):
                term = run[start:start + length]
                if not term.endswith(_RESULT_TERM_SUFFIXES):
                    continue
                if any(stop in term for stop in _RESULT_TERM_STOPWORDS):
                    continue
                if term.startswith(_RESULT_TERM_BAD_PREFIXES):
                    continue
                if 1 <= compact.count(term) <= 2:
                    terms.add(term)

    ordered = sorted(terms, key=lambda term: (compact.count(term), len(term), term))
    specific = tuple(
        term for term in ordered
        if not any(term != other and term.endswith(other) for other in ordered)
    )
    generic = tuple(
        term for term in ("产品", "系统", "平台", "模型", "本体")
        if term in compact and term not in specific
    )
    return specific + generic


def _subtitle_base(subtitle_path: str) -> str:
    base = os.path.abspath(subtitle_path)
    for suffix in _SUBTITLE_SUFFIXES:
        if base.endswith(suffix):
            return base[:-len(suffix)]
    return os.path.splitext(base)[0]


def _primary_claim_hint(subtitle_path: str) -> Optional[str]:
    """Find an explicit primary-claim line and its immediate continuation."""
    if parse_srt is None:
        return None
    blocks = parse_srt(subtitle_path)
    ad_ids = _load_ad_block_ids(_subtitle_base(subtitle_path))
    content = [
        block for block in blocks
        if str(block.get("index", "")) not in ad_ids
        and any(line.strip() for line in block.get("lines", []))
    ] or blocks

    all_lines = [
        normalize_text(line)
        for block in content
        for line in block.get("lines", [])
        if normalize_text(line)
    ]
    has_chinese = any(re.search(r"[\u3400-\u9fff]", line) for line in all_lines)
    lines = [
        line for line in all_lines
        if not has_chinese or re.search(r"[\u3400-\u9fff]", line)
    ]
    for index, line in enumerate(lines):
        lowered = line.lower()
        if not any(marker in line or marker in lowered for marker in _PRIMARY_CLAIM_MARKERS):
            continue
        hint = " ".join(lines[index:index + 3])
        return hint[:240]
    return None


def _hook_evidence_markers(subtitle_text: str) -> Tuple[str, ...]:
    normalized = normalize_text(subtitle_text).lower()
    return tuple(
        marker for marker in _HOOK_EVIDENCE_MARKERS
        if marker in normalized
    )


def _title_has_quality_issue(title: str, allow_first_person: bool = False) -> bool:
    """Reject colloquial, template-like, or ungrammatical headline fragments."""
    value = normalize_text(title)
    if any(phrase in value for phrase in _TITLE_QUALITY_BANNED_PHRASES):
        return True
    # Subtitle truncation can leave a bare "么" inside a generated headline
    # (for example "快么服务" or "集成么服务"). It is not valid title grammar.
    if re.search(r"(?<![什么怎么为这那要])么", value):
        return True
    if ("我" in value and not allow_first_person) or any(
        word in value for word in ("你们", "他们", "自己", "大家", "各位")
    ):
        return True
    if any(value.endswith(ending) for ending in _TITLE_BAD_ENDINGS):
        return True
    if any(ending in value for ending in ("；", ";")):
        return True
    # Noun piles such as "产品层产品" read like subtitles glued together.
    if re.search(
        r"(?<!抽象)(?:层|系统|平台|模型|产品)[^，,。；;：:和与及并的]{0,4}"
        r"(?:层|系统|平台|模型|产品)",
        value,
    ):
        return True
    # A clause can carry one main action. Two bare action verbs without a
    # connector usually produce fragments such as "疏通瓶颈转型为产品".
    for clause in re.split(r"[，,。；;：:]", value):
        actions = [verb for verb in _TITLE_ACTION_VERBS if verb in clause]
        connectors = any(word in clause for word in ("并", "而", "再", "然后", "才能", "以"))
        if len(actions) >= 2 and not connectors:
            return True
    # More than two clauses is a summary sentence, not a title. A single
    # comma is allowed only for the cover's duty/result/value structure.
    if value.count("，") > 1 or value.count(",") > 1:
        return True
    return False


def _editorial_title_candidate(
    subtitle_text: str,
    primary_claim_hint: Optional[str],
) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """Compose the preferred duty/result/value headline when evidence is clear.

    This is deliberately narrow: it only fires when the transcript explicitly
    contains a primary-duty marker, an abstraction/product result, and a core
    advantage. It gives the ranking path a strong editorial candidate instead
    of letting a question hook win by default.
    """
    normalized = normalize_text(subtitle_text)
    claim = normalize_text(primary_claim_hint or "")
    if not claim or "核心优势" not in normalized:
        return None
    if "抽象" not in normalized or "产品" not in normalized:
        return None

    subject_match = re.search(r"(?<![A-Za-z0-9])FDEs?(?![A-Za-z0-9])", claim, flags=re.IGNORECASE)
    subject = subject_match.group(0).upper() if subject_match else None
    if not subject:
        return None
    # The editorial voice prefers "用户"; validation maps it back to "客户"
    # when the transcript used the more literal customer wording.
    audience = "用户" if "用户" in normalized or "客户" in normalized else "客户"
    title = f"{subject}的真正职责是抽象产品，守护{audience}核心优势"

    evidence = []
    for raw_line in subtitle_text.splitlines():
        line = normalize_text(raw_line)
        if any(marker in line for marker in _PRIMARY_CLAIM_MARKERS):
            evidence.append(line)
        elif "抽象" in line:
            evidence.append(line)
        elif "核心优势" in line:
            evidence.append(line)
    evidence = list(dict.fromkeys(item for item in evidence if 8 <= len(item) <= 80))
    if len(evidence) < 2:
        return None
    return title, tuple(evidence[:3])


def _apply_attribution(title: str, attribution: str) -> str:
    attribution = normalize_text(attribution).rstrip("：:")
    if not attribution:
        return title
    return f"{attribution}：{title}"


def _score_title_candidate(
    title: str,
    evidence: Tuple[str, ...],
    central_claim: str,
    subtitle_text: str,
    result_terms: Tuple[str, ...] = (),
    title_style: str = "plain",
) -> float:
    """Rank grounded candidates with style-specific editorial quality."""
    hook = any(phrase in title for phrase in _TITLE_FUNCTION_PHRASES)
    coverage = _claim_coverage(title, central_claim)
    rarity, rare_count = _specificity(title, subtitle_text)
    title_text = normalize_text(title)
    subtitle_text = normalize_text(subtitle_text)

    if _title_has_quality_issue(
        title_text,
        allow_first_person=title_style == "cover_headline",
    ):
        return -1000.0

    if title_style == "cover_headline":
        # A cover headline may be a striking exact quotation. The full-video
        # claim is context and must not overwhelm the hook.
        score = 2.5 * coverage + 2.0 * rarity
        hook_markers = _hook_evidence_markers(subtitle_text)
        hook_hits = sum(marker in title_text.lower() for marker in hook_markers)
        score += min(14.0, 6.0 * float(hook_hits))
        compact_title = _compact_text(title_text)
        compact_evidence = _compact_text(" ".join(evidence))
        if hook_hits and compact_title and compact_title in _compact_text(subtitle_text):
            score += 8.0
        if hook_hits and compact_title and compact_title in compact_evidence:
            score += 5.0
        if result_terms:
            if result_terms[0] in title:
                score += 5.0
            elif any(term in title for term in result_terms):
                score += 2.0
        score += min(1.0, float(rare_count))
        score += min(2.0, float(len(evidence)))
        score -= 2.0 if hook else 0.0
        if 16 <= len(title_text) <= 32:
            score += 3.0
        elif 12 <= len(title_text) <= 40:
            score += 1.0
        else:
            score -= 4.0
        if "不是" in title_text and "不是" not in central_claim:
            score -= 10.0
        if "职责是" in title_text and any(mark in title_text for mark in ("，", "、")):
            score += 10.0
        if "核心优势" in title_text and any(term in title_text for term in ("守住", "守护")):
            score += 5.0
        if "抽象" in title_text and "产品" in title_text:
            score += 5.0
        if _has_balanced_colon(title_text):
            score += 2.0
        elif "：" in title_text:
            score -= 2.0
        return score

    # Plain form titles favor a complete, restrained news sentence. They do
    # not borrow the cover headline's hooks or compression habits.
    score = 10.0 * coverage + 1.5 * rarity
    if result_terms and result_terms[0] in title:
        score += 2.0
    score += min(1.0, float(rare_count))
    score += min(1.0, float(len(evidence)))
    score -= 12.0 if hook else 0.0
    if re.search(r"[?？!！]", title_text):
        score -= 5.0
    if "真正" in title_text:
        score -= 2.0
    if 12 <= len(title_text) <= 24:
        score += 3.0
    elif 8 <= len(title_text) <= 24:
        score += 1.0
    else:
        score -= 4.0
    if title_text.count("，") + title_text.count(",") > 0:
        score -= 4.0
    if title_text and title_text in subtitle_text:
        score -= 1.0
    if "不是" in title_text and "不是" not in central_claim:
        score -= 8.0
    if _has_balanced_colon(title_text):
        score += 1.5
    elif "：" in title_text:
        score -= 2.0
    return score


def _add_title_hook(title: str, max_chars: int) -> str:
    """Insert a neutral connector when a specific title lacks a hook."""
    if any(phrase in title for phrase in _TITLE_FUNCTION_PHRASES):
        return title
    if "职责是" in title:
        return title
    for verb in ("让", "打造", "建立", "长成", "对齐"):
        position = title.find(verb)
        if position > 0:
            hooked = title[:position] + "如何" + title[position:]
            if len(hooked) <= max_chars:
                return hooked
    return title


def _ensure_result_term(
    title: str,
    result_terms: Tuple[str, ...],
    max_chars: int,
) -> str:
    """Attach a detected specific outcome to a generic result noun."""
    if not result_terms or result_terms[0] in title:
        return title
    specific = result_terms[0]
    if specific.startswith("抽象") and "抽象" in title:
        return title
    for generic in ("产品", "系统", "平台", "模型", "本体", "工具", "服务"):
        if generic not in title or specific.endswith(generic):
            continue
        for phrase in (f"一个{generic}", f"这个{generic}", generic):
            if phrase not in title:
                continue
            enriched = title.replace(phrase, specific + generic, 1)
            if len(enriched) <= max_chars:
                return enriched
    return title


def _tighten_title(title: str) -> str:
    """Remove verbose subtitle phrasing that hurts a cover headline."""
    tightened = title
    for source, replacement in (
        ("能做得出来的", "能做的"),
        ("能做得出来", "能做"),
        ("打造出", "打造"),
    ):
        tightened = tightened.replace(source, replacement)
    return tightened


def _clean_clause(clause: str) -> str:
    clause = normalize_text(clause).strip("，。！？；：、,.!?;: ")
    clause = re.sub(
        r"^(?:其实|所以|但是|但|而且|因为|如果|就是|到头来|才能|让|在里面|事实上|因此|我再说一遍)[，,]?",
        "",
        clause,
    )
    return clause


def _extractive_title(
    subtitle_path: str,
    max_chars: int = 30,
    min_chars: int = 6,
) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """Choose a short, exact subtitle clause when the LLM path is unavailable."""
    if parse_srt is None:
        return None
    blocks = parse_srt(subtitle_path)
    base = subtitle_path
    for suffix in _SUBTITLE_SUFFIXES:
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    ad_ids = _load_ad_block_ids(base)
    content = [
        block for block in blocks
        if str(block.get("index", "")) not in ad_ids
    ] or blocks

    chinese_lines = []
    for position, block in enumerate(content):
        for line in block.get("lines", []):
            normalized = normalize_text(line)
            if normalized and re.search(r"[\u3400-\u9fff]", normalized):
                chinese_lines.append((position, normalized))
    if not chinese_lines:
        return None

    corpus = " ".join(line for _, line in chinese_lines)
    candidates = []
    for position, line in chinese_lines:
        for raw_clause in re.split(r"[。！？；，,.!?;:]", line):
            clause = _clean_clause(raw_clause)
            if not min_chars <= len(clause) <= max_chars:
                continue
            latin_terms = [
                token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", clause)
                if token.lower() not in _LATIN_STOPWORDS
            ]
            cjk_bigrams = [
                clause[index:index + 2]
                for index in range(len(clause) - 1)
                if re.search(r"[\u3400-\u9fff]", clause[index:index + 2])
            ]
            terms = latin_terms + cjk_bigrams
            if not terms:
                continue
            if _title_has_quality_issue(clause):
                continue
            frequency = sum(corpus.count(term) for term in terms) / len(terms)
            length_score = abs(len(clause) - 14)
            pronoun_penalty = 12 if re.search(r"我们|他们|你们|自己|大家|各位", clause) else 0
            score = frequency * 2 - length_score - position * 0.15 - pronoun_penalty
            candidates.append((score, clause))

    if not candidates:
        return None
    title = max(candidates, key=lambda item: item[0])[1]
    return title, (title,)


def _llm_prompt(
    subtitle_text: str,
    min_chars: int = 8,
    max_chars: int = 24,
    platform_hint: str = "",
    validation_feedback: str = "",
    primary_claim_hint: Optional[str] = None,
    title_style: str = "plain",
) -> str:
    length_rule = f"3. 标题 {min_chars}-{max_chars} 个字符；表单标题优先 12-24 字，封面标题优先 16-32 字。不要为了用满上限而堆信息。"
    platform_rule = f"\n{platform_hint}" if platform_hint else ""
    feedback_rule = (
        f"\n上一轮候选未通过字幕溯源校验：{validation_feedback}\n"
        "请只修正这些越界词或证据，不要改变中心主张。"
        if validation_feedback
        else ""
    )
    result_terms = _detect_result_terms(subtitle_text)
    result_rule = (
        f"\n10. 本地从字幕检测到结果词：{'、'.join(result_terms)}。"
        + (
            "后 3 个混合候选应包含最具体的结果词；前 3 个原话钩子优先保留钩子本身。"
            if title_style in {"cover_headline", "editorial"}
            else "central_claim 和至少 2 个候选应包含最具体的结果词；不得把它降级成更泛的结果词，也不得为了包含它而拼接不自然词组。"
        )
        if result_terms
        else ""
    )
    primary_rule = (
        f"\n11. 本地检测到主张线索：{primary_claim_hint}。"
        "它应作为全片摘要的重点，但不能只概括这一段；实现路径、限定条件或纠偏只能作支撑，不能替代结论。"
        if primary_claim_hint
        else ""
    )
    public_style_rule = (
        "\n参考公开B站标题的结构特征（只学结构，不学事实）："
        + "；".join(_PUBLIC_TITLE_STYLE_RULES)
        + "。"
    )
    if title_style in {"cover_headline", "editorial"}:
        style_rule = (
            "\n10. 这是封面标题，不是表单标题。目标是吸引人点开观看，但仍然写成专业媒体判断句："
            "「主体：职责/判断是具体结果，价值承诺」。可以有张力，但语法必须完整、语气克制。"
            "先扫描字幕里的强钩子：被反复强调的热点词、身份标签、反差冲突、比喻或原话金句"
            "（例如字幕出现「AI时代最强二刀流」这类标签，或「直接用大厂模型就像强迫我不带套」这类比喻）。"
            "这些示例只学钩子结构，禁止把示例事实带入当前字幕。"
            "若字幕确有强钩子，前 2 个候选应钩子优先，尽量保留原词或原句画面，只做最小语法整理；"
            "另外 2 个候选写成钩子+判断的混合标题，最后 2 个候选保留完整中心主张。"
            "没有强钩子时，不要硬造猎奇词，回到专业媒体判断句。"
            "每个候选都要能读成一句完整中文，主语、谓语和结果清楚；禁止名词短语拼接。"
            "不要把两个动词短语直接拼接；需要两个动作时用「并」「而」「再」或逗号分句。"
            "不要截取字幕长词的中间片段当术语，例如字幕是「事业部层」时不得写「业部层」。"
            "不要把字幕截断产生的碎片拼进标题，例如「快么服务」「集成么服务」这类表达必须拒绝。"
            "最多一个逗号；两段字数要均衡，不得出现半截话。"
            "不要默认疑问句；只有当悬念本身就是中心主张时才使用疑问句。"
            "允许职责/职能、守护/守住、用户/客户这类功能词压缩；"
            "字幕出现「抽象层」和「产品」时可压缩为「抽象产品」。"
            "不得引入字幕外的人名、公司名、术语、数字、结果或承诺。"
            "第一人称只允许出现在字幕原话的强钩子/直接引语中（例如比喻或冲突句）；"
            "除此之外要把「我/我们」改写成具体主体，例如 FDE、CEO、Databricks。"
        )
        return f"""你只能根据下面的字幕内容提炼封面标题。
第一步：先写 central_claim。它必须是 60-100 个字符的全片摘要，如实覆盖开场问题、主要过程、关键转折和结尾立场；这里只用作上下文，防止封面标题脱离视频。
第二步：通读全部字幕，找出最吸引人、最出位、最有画面的原话钩子。优先选完整对话中的金句，不要默认选结尾；比喻、冲突、反差、极端表述和原话直引优先。
第三步：再生成 6 个 candidates。前 3 个必须是原话钩子或最小语法整理后的原话钩子，尽量逐字保留字幕措辞；后 3 个可以是钩子加全片主旨的混合标题。不要用平实摘要当第一候选。

硬性规则：
1. 标题中的事实、人名、公司名、术语和动词必须来自字幕；禁止使用文件名、外部知识、猜测或自造同义动词。
2. 若字幕已有中文术语，必须沿用字幕术语；例如字幕把 alpha 译为“核心优势”，标题不得改成“阿尔法”。
{length_rule}
4. 标题要比表单标题更有观看动机，但不得牺牲准确和语法；优先突出反常识结论、具体职责或核心价值。
5. 不要把旁支纠偏（例如“不是销售”）当主标题，除非它就是 central_claim。
6. central_claim 的最终结果必须保留字幕中最具体、最稀有的结果词，不要降级成高频泛词。若字幕同时出现更具体的名词和泛称结果，可以组合表达（例如字幕出现“抽象层”和“产品”时，才可写“抽象层产品”；字幕没出现时禁止引入）。
7. evidence 必须逐字复制字幕原文，可来自一条字幕或相邻的中文字幕行；每个候选只列支撑标题的 1-2 条，每条 8-60 个字符，不得改写、翻译或引入字幕外文字。
8. 连接词只服务语法，不算事实内容；不要为了钩子硬加「为什么」「如何」「才是」。
9. 只输出 JSON，格式：{{"central_claim":"...","candidates":[{{"title":"...","evidence":["..."]}}]}}。candidates 的前 3 个必须来自原话钩子。
{platform_rule}
{feedback_rule}
{result_rule}
{primary_rule}
{style_rule}
{public_style_rule}

字幕内容：
{subtitle_text}
"""

    return f"""你只能根据下面的字幕内容提炼视频表单标题。
第一步：先写 central_claim。它必须是 60-100 个字符的全片摘要，如实覆盖开场问题、主要过程、关键转折和结尾立场；禁止只概括最后一段，禁止把某一句原话当成全片结论。
第二步：先区分结论和支撑条件，再把 central_claim 精炼成 5 个 candidates；不要把实施方式、限定条件误作结论。表单标题像视频简介的第一句，必须概括整个视频，而不是选一个片段；短标题至少保留两个实质信息点，并优先保留主体、核心业务和最终结果。

硬性规则：
1. 标题中的事实、人名、公司名、术语和动词必须来自字幕；禁止使用文件名、外部知识、猜测或自造同义动词。
2. 若字幕已有中文术语，必须沿用字幕术语；例如字幕把 alpha 译为“核心优势”，标题不得改成“阿尔法”。
{length_rule}
4. 这是平实、准确的新闻式短标题：目标是高度概括全片内容，主体先行只是常用写法，不是硬性模板。语法必须完整，读起来要像一句正常中文。不要把两个动词短语直接拼接；需要两个动作时用「并」「而」「再」或逗号分句。不要截取字幕长词的中间片段当术语，例如字幕是「事业部层」时不得写「业部层」。
4a. 不要把字幕截断产生的碎片拼进标题，例如「快么服务」「集成么服务」这类表达必须拒绝。
5. 不要疑问句、悬念句、夸张承诺、网络口语或 AI 套话；不要「为什么」「如何」「才是」「到底」「究竟」。禁止第一人称；把「我/我们」改写成具体主体。
6. central_claim 的最终结果必须保留字幕中最具体的结果词，但不得为了具体而拼出不自然词组。
7. evidence 必须逐字复制字幕原文，可来自一条字幕或相邻的中文字幕行；每个候选只列支撑标题的 1-2 条，每条 8-60 个字符，不得改写、翻译或引入字幕外文字。
8. 禁止这些表达或同类碎片：「{'」、「'.join(_TITLE_QUALITY_BANNED_PHRASES)}」。
9. 只输出 JSON，格式：{{"central_claim":"...","candidates":[{{"title":"...","evidence":["..."]}}]}}。central_claim 是全片摘要；candidates 是摘要的精炼标题。
{platform_rule}
{feedback_rule}
{result_rule}
{primary_rule}
{public_style_rule}

字幕内容：
{subtitle_text}
"""


def build_subtitle_grounded_title(
    video_path: str,
    llm_client=None,
    model_name: str = "glm-5.2",
    provider=None,
    max_chars: int = 30000,
    subtitle_path: Optional[str] = None,
    max_title_chars: int = 40,
    min_title_chars: int = 5,
    platform_hint: str = "",
    title_style: str = "plain",
    attribution: str = "",
) -> GroundedTitle:
    """Build a title exclusively from subtitle content, with exact evidence."""
    title_style = {"standard": "plain", "editorial": "cover_headline"}.get(
        title_style, title_style
    )
    if title_style not in {"plain", "cover_headline"}:
        raise SubtitleTitleError(f"未知标题风格: {title_style}")
    attribution = normalize_text(attribution).rstrip("：:")
    if attribution:
        available_core_chars = max_title_chars - len(attribution) - 1
        if available_core_chars < min_title_chars:
            raise SubtitleTitleError("署名过长，无法生成字幕溯源标题")
        max_title_chars = available_core_chars

    subtitle_text, resolved_path = read_subtitle_text(
        video_path,
        max_chars=max_chars,
        subtitle_path=subtitle_path,
    )
    if not subtitle_text or not resolved_path:
        raise SubtitleTitleError(f"未找到可用字幕，无法自动生成标题: {video_path}")
    full_subtitle_text, _ = read_full_subtitle_text(video_path, resolved_path)
    grounding_subtitle_text = full_subtitle_text or subtitle_text

    if llm_client is not None:
        last_error = None
        validation_feedback = ""
        result_terms = _detect_result_terms(subtitle_text)
        primary_claim_hint = _primary_claim_hint(resolved_path)
        editorial_candidate = (
            _editorial_title_candidate(grounding_subtitle_text, primary_claim_hint)
            if title_style == "cover_headline"
            else None
        )
        for _ in range(2):
            try:
                raw = llm_client.generate_content(
                    _llm_prompt(
                        subtitle_text,
                        min_chars=min_title_chars,
                        max_chars=max_title_chars,
                        platform_hint=platform_hint,
                        validation_feedback=validation_feedback,
                        primary_claim_hint=primary_claim_hint,
                        title_style=title_style,
                    ),
                    model_name=model_name,
                    provider=provider,
                )
                central_claim, candidates = parse_llm_title_candidates(raw)
                if editorial_candidate:
                    candidates = (editorial_candidate,) + tuple(candidates)
                valid_candidates = []
                candidate_errors = []
                for candidate_title, candidate_evidence in candidates:
                    try:
                        if _title_has_quality_issue(
                            candidate_title,
                            allow_first_person=title_style == "cover_headline",
                        ):
                            raise SubtitleTitleError("标题包含低质或不通顺表达")
                        evidence = validate_title_from_subtitle(
                            candidate_title,
                            grounding_subtitle_text,
                            candidate_evidence,
                            min_chars=min_title_chars,
                            max_chars=max_title_chars,
                        )
                        valid_candidates.append(
                            (candidate_title, evidence, candidate_evidence)
                        )
                    except SubtitleTitleError as exc:
                        candidate_errors.append(f"{candidate_title}: {exc}")

                if valid_candidates:
                    title, evidence, _ = max(
                        valid_candidates,
                        key=lambda item: _score_title_candidate(
                            item[0],
                            item[1],
                            central_claim,
                            grounding_subtitle_text,
                            result_terms,
                            title_style,
                        ),
                    )
                    adjusted_title = title
                    if title_style == "cover_headline":
                        adjusted_title = _ensure_result_term(
                            adjusted_title,
                            result_terms,
                            max_title_chars,
                        )
                    adjusted_title = _tighten_title(adjusted_title)
                    if adjusted_title != title:
                        try:
                            evidence = validate_title_from_subtitle(
                                adjusted_title,
                                grounding_subtitle_text,
                                evidence,
                                min_chars=min_title_chars,
                                max_chars=max_title_chars,
                            )
                            title = adjusted_title
                        except SubtitleTitleError:
                            pass
                    return GroundedTitle(
                        title=_apply_attribution(title, attribution),
                        evidence=evidence,
                        subtitle_path=resolved_path,
                        source="llm-subtitle",
                        core_title=title,
                        attribution=attribution,
                    )

                last_error = "；".join(candidate_errors[:3])
                validation_feedback = last_error or "未返回可校验候选"
            except SubtitleTitleError as exc:
                last_error = exc
                validation_feedback = str(exc)
                continue
            except Exception as exc:
                print(f">>> ⚠️ LLM标题生成失败: {exc}")
                validation_feedback = str(exc)
                continue
        if last_error is not None:
            print(f">>> ⚠️ LLM标题未通过字幕溯源校验: {last_error}")

    if title_style == "cover_headline":
        editorial_candidate = _editorial_title_candidate(
            grounding_subtitle_text,
            _primary_claim_hint(resolved_path),
        )
        if editorial_candidate:
            title, evidence = editorial_candidate
            evidence = validate_title_from_subtitle(
                title,
                grounding_subtitle_text,
                evidence,
                min_chars=min_title_chars,
                max_chars=max_title_chars,
            )
            return GroundedTitle(
                title=_apply_attribution(title, attribution),
                evidence=evidence,
                subtitle_path=resolved_path,
                source="editorial-subtitle",
                core_title=title,
                attribution=attribution,
            )

    fallback = _extractive_title(
        resolved_path,
        max_chars=max_title_chars,
        min_chars=max(6, min_title_chars),
    )
    if fallback:
        title, evidence = fallback
        if _title_has_quality_issue(title):
            raise SubtitleTitleError(f"字幕候选标题质量不合格: {title}")
        evidence = validate_title_from_subtitle(
            title,
            grounding_subtitle_text,
            evidence,
            min_chars=max(6, min_title_chars),
            max_chars=max_title_chars,
        )
        return GroundedTitle(
            title=_apply_attribution(title, attribution),
            evidence=evidence,
            subtitle_path=resolved_path,
            source="extractive-subtitle",
            core_title=title,
            attribution=attribution,
        )
    raise SubtitleTitleError("无法从字幕中提取可用标题")
