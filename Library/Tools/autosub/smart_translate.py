import os
import sys
import argparse
import math
import time
import re
import json
from typing import List, Dict
import io

# Force UTF-8 for stdout/stderr to handle emojis in logs on Windows (essential for pythonw)
if sys.platform == "win32":
    # If we are running under pythonw.exe, stdout and stderr might be None
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()

    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, io.UnsupportedOperation):
        pass

# Configuration
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = getattr(sys, '_MEIPASS', '')
    TOOLS_DIR = os.path.join(BUNDLE_DIR, "Library", "Tools")
    USER_DATA_DIR = os.path.join(os.path.expanduser("~"), "Documents", "AutoSub")
    ENV_PATH = os.path.join(USER_DATA_DIR, ".env")
else:
    TOOLS_DIR = os.environ.get("AUTOSUB_TOOLS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(TOOLS_DIR)), ".env")

sys.path.append(os.path.join(TOOLS_DIR, "subtranslator", "lib"))
sys.path.append(os.path.join(TOOLS_DIR, "common"))
from llm_utils import LLMClient, LLMProvider

try:
    from common_utils import load_terms
except ImportError:
    load_terms = None
    print("Warning: common_utils.load_terms not found; terms.yml glossary will not be loaded.")

try:
    import gemini_utils
except ImportError as e:
    print(f"Warning: gemini_utils not found ({e}). Using llm_utils only.")

try:
    import srt_utils
except ImportError as e:
    print(f"❌ Error: srt_utils is mandatory and was not found: {e}")
    print(f"Current sys.path: {sys.path}")
    sys.exit(1)

# --- SKILL INTEGRATION ---

def load_skill_rules(tool_name):
    """
    Reads the README/SKILL.md of a tool to extract prompting rules.
    Searches TOOLS_DIR first, then falls back to the parent Library/Tools
    directory so skills that live outside the autosub package (e.g.
    humanizer-zh, which is shared across tools) are still found.
    """
    skill_dir = os.path.join(TOOLS_DIR, tool_name)
    readme_path = os.path.join(skill_dir, "README.md")
    if not os.path.exists(readme_path):
        # Fallback: look in the shared Library/Tools directory one level up
        # from the autosub package's own Library/Tools. This catches skills
        # like humanizer-zh that are installed globally rather than bundled.
        # TOOLS_DIR = .../autosub/Library/Tools. Go up 3 levels to reach
        # the shared .../Library/Tools directory where humanizer-zh lives.
        shared_tools = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(TOOLS_DIR))), tool_name)
        alt_path = os.path.join(shared_tools, "README.md")
        if os.path.exists(alt_path):
            readme_path = alt_path
    content = ""
    if os.path.exists(readme_path):
        with open(readme_path, 'r', encoding='utf-8') as f:
            content = f.read()
    return content

def _extract_rules(content: str) -> str:
    """
    Drop install/setup/usage/license preamble from a skill README so the real
    rules reach the LLM. Previously the prompt did RULES[:1500], which captured
    only the install instructions (npx / git clone / manual setup) and truncated
    the actual de-AI / tone rules sitting further down the file.

    This extractor splits on ALL heading levels (##, ###, ####) and drops
    sections whose titles clearly indicate install/setup/admin preamble.
    The drop pattern uses full-match semantics on the title to avoid
    false positives (e.g. '基本使用方法' contains '方法' but is a rule
    section, while '方法一：通过npx安装' is install preamble).
    """
    if not content:
        return ""
    # Match titles that are CLEARLY install/admin sections.
    # Uses startswith logic so '方法一：...' and '方法三：...' are caught,
    # but '基本使用方法' (a rules section) is not.
    # Titles that indicate install/admin/usage preamble to drop.
    # Uses startswith so "方法一：..." is caught, but "基本使用方法"
    # (a rules section) is not.
    drop_prefixes = (
        "项目简介", "安装", "验证安装", "贡献", "参考资源", "许可",
        "方法一", "方法二", "方法三",
        "使用场景", "基礎用法", "基础用法",
        "install", "setup", "contributing", "license", "reference",
    )
    # Also drop numbered sub-sections that are usage instructions
    drop_patterns = re.compile(r"^(场景|\d+\.)", re.IGNORECASE)
    parts = re.split(r"\n(?=#{1,4}\s)", content)
    kept = []
    for part in parts:
        m = re.match(r"#{1,4}\s+(.+?)(\n|$)", part)
        title = m.group(1).strip() if m else ""
        if title:
            title_lower = title.lower()
            if any(title_lower.startswith(p) for p in drop_prefixes):
                continue
            # Drop "使用" and its sub-sections (how to use the tool, not rules)
            if title_lower.startswith("使用"):
                continue
            if drop_patterns.match(title):
                continue
        kept.append(part)
    cleaned = "\n".join(kept).strip()
    return cleaned or content

# Load rules once at startup
HUMANIZER_RULES = _extract_rules(load_skill_rules("humanizer-zh"))
VERBALIZER_RULES = _extract_rules(load_skill_rules("verbalizer"))
SUBTRANSLATOR_RULES = load_skill_rules("subtranslator")

# Stronger alignment constraints block - shared across all translation prompts
ALIGNMENT_CONSTRAINTS = """### STEP 3.8: SYNCHRONIZATION & ALIGNMENT CONSTRAINTS
- **Strict ID Alignment**: Every input ID in `[ID]` format MUST have its corresponding translated line in the output. Never skip block IDs or merge consecutive blocks into one line.
- **No Content Skipping (CRITICAL)**: The Chinese translation of block N must correspond to the English content of block N. Do NOT skip an English block entirely and translate the NEXT block's content instead. If the English is a partial sentence fragment, translate exactly that fragment - do not jump ahead to translate a later block's content.
- **Partial Translations Are OK**: Subtitle blocks often contain sentence fragments (a sentence split across 2-3 blocks by timing). It is perfectly fine for the Chinese to also be a fragment. Do NOT pull content from the next block to make the current block "more complete." A partial, fragmentary translation that matches its EN block is BETTER than a complete translation that steals content from the next block.
- **Sense-For-Sense Over Word-For-Word**: When a speaker is colloquial, halting, or speaks in fragments (incomplete sentences, mid-sentence corrections, run-on trains of thought), translate the MEANING of each fragment, not the individual words. Do not mechanically render each English word into Chinese — understand what the speaker is trying to say at that moment, then express it in natural spoken Chinese. A fragment like "and you can\'t put it back in the / bag" should read "你没法把它 / 塞回袋子里" as one coherent thought across two blocks, not "你收不回来了" + "袋子里". The Chinese fragment should make sense as part of the running sentence, even if each block alone is incomplete.
- **Cross-Block Coherence**: When the same sentence spans 2-3 blocks, translate it as a coherent unit first, then split the Chinese across blocks at natural break points. Each block\'s Chinese should read as a natural continuation of the previous block, not as an isolated translation. The listener reads subtitles sequentially — each block should feel connected to the one before and after it.
- **Natural Reorganization Within Sentences**: When a sentence spans multiple blocks, you may redistribute the Chinese across those blocks in a way that reads naturally. The key constraint is: each CN block must still correspond to roughly the same span of meaning as its EN block. Moving words between adjacent blocks for fluency is acceptable; wholesale replacing block N\'s content with block N+1\'s content is not.
- **Output count rule**: Output exactly the same number of [ID] lines as the input. If the input has 20 blocks, output exactly 20 [ID] lines.
- **Pronoun & Role Accuracy**: In business/interview contexts, pay close attention to who is paying whom, who is the buyer vs. the seller, and who "they/them/you" refers to. A phrase like "the person you're paying a lot of money to" means the vendor/lab you're paying, NOT your customer. Getting the direction of money/value transfer wrong completely reverses the meaning. Always resolve pronoun references against the conversational context before translating.
- **Context-Driven Word Sense**: When a word has multiple meanings, choose the sense that fits the speaker's context, not the most common dictionary sense. Examples: "highlighted" in a news segment means "特别指出/重点强调", not "提到"; "pain" for enterprises means "痛苦/困扰", not the business jargon "痛点"; "vulgar" for a blunt business complaint means "直来直去的/不客气的", not "粗俗的"; "world historic figure" means "划时代的人物", not "世界历史意义的人物". Always ask: what does the speaker actually mean here?
- **Proper Noun vs Common Word (CRITICAL)**: Some English words are BOTH a proper noun (company/fund name) and a common word. You MUST distinguish by context. "Conviction" as Sarah Guo\'s venture fund name stays in English ("Conviction内部最大的分歧"); "conviction" as a personal belief/investment thesis is translated as "确信" or "信念" ("leap of conviction" -> "纵身一跃的确信"). "Apple" the company stays "Apple"; "apple" the fruit is "苹果". When a company name appears in a sentence that also uses the same word as a common noun, keep the company name in English and translate the common noun.
- **Idioms, Slang & Cultural References (CRITICAL)**: English idioms and cultural references MUST be translated by their MEANING, not word-for-word, and must NOT be confused with other idioms. "the cat is out of the bag" means "秘密已经泄露了/事情已经公开了" — do NOT confuse it with "the genie is out of the bottle" (which means something similar but is a DIFFERENT idiom — use the correct one). "reverse dog years" means time feels compressed (opposite of dog years where 1 year = 7), translate as "时间过得比狗年还快" or explain the meaning naturally. "leap of faith" -> "纵身一跃的信念". Always: (1) identify the idiom, (2) understand its actual meaning, (3) express that meaning in natural Chinese. Never substitute one English idiom for another in the translation.
- **No Residual English (CRITICAL)**: Every English word in a block MUST be translated into Chinese, with ONLY these exceptions: (1) proper nouns — person names (Sarah, Mike, Andrej Karpathy), company/org names (OpenAI, DeepMind, Toyota Research, Greylock, Conviction), product names (ChatGPT, Cursor, Felix); (2) widely-used acronyms (AI, LP, VC, GPU, SSO, API); (3) brand/term names with no standard Chinese equivalent (DevCon, WorkOS). Common English words like "hopefully", "creativity", "genie", "actually", "so", "like" must ALWAYS be translated — never leave them embedded in a Chinese sentence. When in doubt about whether a word is a proper noun, translate it rather than keeping it in English."""


# Retrieve API Keys from environment
try:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
except ImportError:
    pass

import llm_utils
client = llm_utils.get_client()

def get_context_window(blocks: List[Dict], index: int, window_size: int = 2) -> Dict:
    """
    Returns context lines (before/after) for a given block index.
    """
    start = max(0, index - window_size)
    end = min(len(blocks), index + window_size + 1)

    prev_text = " ".join([" ".join(b['lines']) for b in blocks[start:index]])
    next_text = " ".join([" ".join(b['lines']) for b in blocks[index+1:end]])

    return {"prev": prev_text, "next": next_text}

def detect_translation_style(blocks: List[Dict], model_name: str, provider=None) -> str:
    """
    Analyzes the English subtitle blocks to determine the best translation style.
    Returns: 'casual', 'formal', or 'edgy'.
    """
    print("🔍 Auto-detecting subtitle language style...")
    if not blocks:
        print("⚠️ No subtitle blocks found. Defaulting to CASUAL.")
        return "casual"
    # Sample blocks evenly across the entire transcript to avoid bias
    # from intro ads/promos that only appear at the start
    total = len(blocks)
    if total <= 60:
        sample_blocks = blocks
    else:
        step = max(1, total // 50)
        sample_blocks = blocks[::step][:50]
    sample_text = "\n".join([" ".join(b['lines']).strip() for b in sample_blocks])
    
    prompt = f"""Analyze the following English video subtitle transcript and classify its tone/genre into exactly one of these three categories:
1. `casual`: Conversational, informal speech. Often contains filler words ("you know", "like", "well"), colloquial phrasing, personal anecdotes, or relaxed banter. (Typical of podcasts, casual discussions, vlogs).
2. `formal`: Structured, written, or academic speech. Grammatically precise, lacks filler words, uses professional terminology, or presents structured arguments. (Typical of presentations, formal lectures, business reports, documentaries).
3. `edgy`: Short, punchy, high-impact language. Very short sentences or fragments designed to grab attention. (Typical of TikToks, reels, ads, or high-energy marketing).

Respond with ONLY the category name (`casual`, `formal`, or `edgy`) as a single word in lowercase. Do not include formatting, punctuation, or any other explanation.

### TRANSCRIPT SAMPLE:
{sample_text}
"""
    try:
        # Use fallback=True to ensure robust cascading if Vertex/primary provider fails
        detected = client.generate_content(prompt, model_name=model_name, fallback=True, provider=provider, disable_thinking=True)
        detected = detected.strip().lower() if detected else "casual"
        if detected not in ["casual", "formal", "edgy"]:
            # Clean up if model added markdown or formatting
            for style in ["casual", "formal", "edgy"]:
                if style in detected:
                    detected = style
                    break
            else:
                detected = "casual"
        print(f"🎯 Auto-detected style: {detected.upper()}")
        return detected
    except Exception as e:
        print(f"⚠️ Style detection failed ({e}). Defaulting to CASUAL.")
        return "casual"

def detect_ad_segments(blocks: List[Dict], client, model: str, provider=None) -> List[Dict]:
    """
    Scans the FULL transcript to locate advertising / promotional segments
    (intro sponsors, mid-roll ads, outro plugs). Unlike style detection,
    this needs the whole transcript because ads are continuous spans that
    can appear anywhere — sampling the head would miss mid-roll/outro ads.

    Returns a list of segments, each with block range + timecodes:
      {'start_block', 'end_block', 'start_time', 'end_time', 'type', 'reason'}
    Also mutates blocks in-place to set b['is_ad'] = True for ad blocks,
    so downstream steps (translation, slicing) can skip/handle them.
    """
    if not blocks:
        return []

    # Build an index-tagged transcript so the LLM can cite block numbers.
    lines = []
    for b in blocks:
        text = " ".join(b.get('lines', [])).strip()
        lines.append(f"[{b['index']}] {text}")
    transcript = "\n".join(lines)

    prompt = f"""Analyze the following subtitle transcript and identify any advertising or promotional segments.

Look for:
- "sponsor" — sponsor reads / brand integrations ("this video is sponsored by", "thanks to X for sponsoring")
- "midroll" — promotional content inserted in the middle, unrelated to the main content
- "outro" — end-of-video call-to-action, merchandise, Patreon/channel plugs
- "intro" — pre-roll promotions before the main content starts

Return ONLY a JSON array (no markdown fences, no explanation). Each element:
{{"start": <first block index>, "end": <last block index>, "type": "sponsor"|"midroll"|"outro"|"intro", "reason": "<short reason>"}}
If no ads are found, return [].

### TRANSCRIPT:
{transcript}
"""
    try:
        raw = client.generate_content(prompt, model_name=model, fallback=True, provider=provider, disable_thinking=True)
        if not raw:
            return []
        # LLMs sometimes wrap output in ```json ... ```; extract the first array.
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if not m:
            return []
        segs_raw = json.loads(m.group(0))
    except Exception as e:
        print(f"⚠️ Ad detection failed ({e}). Skipping.")
        return []

    idx_map = {str(b['index']): b for b in blocks}
    ad_segments: List[Dict] = []
    ad_block_ids = set()
    for seg in segs_raw:
        try:
            s = str(seg.get("start"))
            e = str(seg.get("end"))
            if s not in idx_map or e not in idx_map:
                continue
            sb = idx_map[s]
            eb = idx_map[e]
            ad_segments.append({
                'start_block': sb['index'],
                'end_block': eb['index'],
                'start_time': sb.get('start', 0),
                'end_time': eb.get('end', 0),
                'type': seg.get('type', 'unknown'),
                'reason': seg.get('reason', ''),
            })
            # Mark every block in the inclusive range
            si = int(s) if s.isdigit() else None
            ei = int(e) if e.isdigit() else None
            if si is not None and ei is not None:
                for i in range(si, ei + 1):
                    ad_block_ids.add(str(i))
        except Exception:
            continue

    # Mutate blocks in-place so translation / slicing can consult b['is_ad']
    for b in blocks:
        if str(b['index']) in ad_block_ids:
            b['is_ad'] = True

    return ad_segments


def smart_translate_chunk(chunk_blocks: List[Dict], style: str = "casual", model_name: str = "glm-5.3") -> List[Dict]:
    """
    Translates a chunk of subtitle blocks using context-aware prompting.
    """
    # Construct the Prompt
    input_text = ""
    for i, block in enumerate(chunk_blocks):
        # We simplify the input to line format: [ID] Text
        text = " ".join(block['lines']).replace("\n", " ")

        # Apply terminology scrubbing to fix ASR errors (claw -> OpenClaw) before LLM sees it
        scrubbed_text = humanize_text(text, is_source=True)
        if not scrubbed_text.strip() and text.strip():
            # Fallback if scrubbing accidentally wiped everything
            scrubbed_text = text.strip()

        input_text += f"[{block['index']}] {scrubbed_text}\n"

    prompt = f"""
You are an expert subtitle translator and editor.
Translate the following subtitles into Simplified Chinese.
The source language may be English, Japanese, or a mix of both.

### STEP 1: VERBALIZATION (Tone & Persona)
{VERBALIZER_RULES}
TARGET STYLE: {style}
- "Casual": Natural, spoken Chinese. Use "其实", "也就是说".
- "Tech": Accurate terminology. "Code" -> "代码", "Agent" -> "智能体".
- "Edgy": Short, punchy, impactful.

### STEP 2: HUMANIZATION (De-AI)
{HUMANIZER_RULES}
- NO "translationese".
- NO "此外", "意味着", "不可或缺".
- NO long dashes "——".
- Vary sentence length.

### CORE REQUIREMENTS:
1. **Timing & Rhythm (Highest Priority)**: Maintain strict alignment with the time-axis. The Chinese text should match the rhythm of the English speech.
2. **Context-Driven Meaning**: Use the 'Context Lines' to understand the full sentence, but only translate the specific 'Target Line'.
3. **Synchronization Constraint (CRITICAL)**: Do not significantly restructure sentences across time boundaries. Ensure the meaning of each segment is self-contained enough to be understood at that specific moment. NEVER merge the content of consecutive blocks or skip block IDs in your output. Every input ID in `[ID]` format MUST have its exact corresponding translated line in the output. If a sentence spans across block boundaries (e.g., block N ends with a conjunction and block N+1 starts a clause), translate the fragments strictly into their corresponding blocks to prevent timeline shifts.
4. **Glossary Consistency**: Strictly follow the terms provided in the 'ARTICLE ANALYSIS'.
    - NOTE: Pre-translated terms like "前线部署工程师" (person) and "前线部署模式" (concept) may already appear in the input. When "前线部署模式" is used in a verbal/active context (e.g. "doing 前线部署模式 for X"), naturally adjust to "前线部署" for fluent Chinese.

INPUT BLOCK:
{input_text}

OUTPUT FORMAT:
[ID] Translated Text
...
"""

    try:
        translated_text = client.generate_content(prompt, model_name=model_name)
        if not translated_text:
            return chunk_blocks

        # Parse the Output
        translated_map = {}
        for line in translated_text.split('\n'):
            match = re.match(r'\[(\d+)\]\s*(.*)', line.strip())
            if match:
                idx = match.group(1)
                content = match.group(2).strip()
                if content:  # guard: reject empty translations
                    translated_map[idx] = content

        # Apply translations back to blocks
        translated_blocks = []
        for block in chunk_blocks:
            new_block = block.copy()
            idx = str(block['index']) # Ensure string comparison
            if idx in translated_map:
                new_block['lines'] = [translated_map[idx]]
            else:
                # Fallback: keep original if translation missing (better than empty)
                print(f"Warning: Missing translation for block {idx}")
                new_block['lines'] = block['lines']
            translated_blocks.append(new_block)

        return translated_blocks

    except Exception as e:
        print(f"Error translating chunk: {e}")
        return chunk_blocks # Return original on failure


STYLE_GUIDE_PATH = os.path.join(TOOLS_DIR, "common", "HARD_CONSTRAINTS.md")

def load_regex_rules(filepath):
    """
    Loads regex rules from HARD_CONSTRAINTS.md and auto-classifies each rule:
    - "asr": Section 1 rules whose replacement contains NO CJK (English→English ASR correction).
             Safe for pre-LLM source text scrubbing.
    - "term": Section 1 rules whose replacement CONTAINS CJK (English→Chinese terminology).
              Post-LLM only — must never touch source text before LLM sees it.
    - "cleanup": Sections 2+ (text cleanup, compliance). Post-LLM only.
    """
    rules = []
    if os.path.exists(filepath):
        is_section1 = True  # Section 1 = Terminology/Glossary
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if "## 2. Text Cleanup" in line:
                    is_section1 = False
                if line.startswith("|") and not line.startswith("| Rule") and not line.startswith("| description") and not line.startswith("|---"):
                    # Split by pipe but IGNORE escaped pipes \|
                    parts = [p.strip() for p in re.split(r'(?<!\\)\|', line)]
                    # Remove empty parts from start/end
                    if parts[0] == "": parts.pop(0)
                    if parts and parts[-1] == "": parts.pop()

                    if len(parts) >= 2:
                        pattern = parts[1].strip('`').replace(r'\|', '|') # Unescape pipe for regex engine
                        replacement = parts[2].strip('`').replace(r'\|', '|') if len(parts) > 2 else ""
                        if is_section1:
                            has_cjk = bool(re.search(r'[\u4e00-\u9fff]', replacement))
                            rule_type = "term" if has_cjk else "asr"
                        else:
                            rule_type = "cleanup"
                        rules.append((pattern, replacement, rule_type))
    return rules

REGEX_RULES = load_regex_rules(STYLE_GUIDE_PATH)

def humanize_text(text: str, is_source: bool = False) -> str:
    """
    Applies configured Regex rules and basic Humanizer cleanup.

    is_source=True:  Pre-LLM ASR correction ONLY (English→English, no CJK injected).
                     This ensures the LLM always receives clean English source text.
    is_source=False: Post-LLM full pipeline (ASR + terminology translation + cleanup).
    """
    # 0. Apply Regex Rules from STYLE_GUIDE.md
    for pattern, replacement, rule_type in REGEX_RULES:
        if is_source and rule_type != "asr":
            continue  # Pre-LLM: only English→English ASR correction
        try:
            text = re.sub(pattern, replacement, text)
        except Exception:
            pass

    if is_source:
        # Safety guard: ASR correction must never introduce CJK into source text.
        # If this triggers, an ASR-classified rule has a CJK replacement — fix the rule.
        if re.search(r'[\u4e00-\u9fff]', text):
            print(f"⚠️  WARNING: humanize_text(is_source=True) introduced CJK: {text[:80]}", file=sys.stderr)
        return text.strip()

    # 1. Remove common AI connectors (Hardcoded fallbacks)
    text = text.replace("此外，", "另外，")
    text = text.replace("总而言之，", "简单说，")
    text = text.replace("不可或缺", "很重要")
    text = text.replace("意味着", "说明")

    # 2. Fix punctuation
    text = text.replace(",", "，").replace("?", "？").replace("!", "！")

    # 3. Trim extra spaces
    text = re.sub(r'\s+', ' ', text).strip()

    return text


def is_untranslated(block: Dict) -> bool:
    """
    Returns True if a block has not been successfully translated.
    Detects: empty lines, known marker tags, or predominantly English text.
    Blocks containing CJK characters are considered translated UNLESS the text
    is predominantly English with only a few Chinese chars (which can happen
    when terminology pre-replacement injects Chinese into otherwise untranslated
    English lines, e.g. "Head of 前线部署工程师ing at OpenAI").
    """
    text = " ".join(block.get('lines', [])).strip()
    if not text:
        return True
    markers = ['[UNTRANSLATED]', '[TRANSLATION_FAILED]']
    if any(m in text for m in markers):
        return True
    # Japanese kana detection: hiragana + katakana are uniquely Japanese.
    # If present, the block is untranslated Japanese, not Chinese.
    if re.search(r'[\u3040-\u30ff\u31f0-\u31ff]', text):
        return True
    if re.search(r'[\u4e00-\u9fff]', text):
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        english_words = len(re.findall(r'[a-zA-Z]+', text))
        # Partially translated: mostly English with a few Chinese chars
        # (typically from terminology pre-replacement that injected Chinese).
        # Check both absolute count AND ratio: if English words outnumber CJK
        # chars, or if English is >60% of the text, it's likely untranslated.
        if english_words >= 3 and (chinese_chars < english_words or english_chars_ratio(text) > 0.6):
            return True
        return False
    alpha = [c for c in text if c.isalpha()]
    if not alpha:
        return False
    ascii_ratio = sum(1 for c in alpha if ord(c) < 128) / len(alpha)
    # Lowered from 8 to 2 alpha chars to catch short untranslated fragments
    # (e.g. "Yeah.", "OK.", "So.") that were previously slipping through.
    return ascii_ratio > 0.7 and len(alpha) >= 2


def english_chars_ratio(text: str) -> float:
    """Returns the ratio of ASCII alpha chars to total alpha chars."""
    alpha = [c for c in text if c.isalpha()]
    if not alpha:
        return 0.0
    return sum(1 for c in alpha if ord(c) < 128) / len(alpha)


def translate_blocks(blocks: List[Dict], client, model: str, style: str,
                     verbalizer_snippet: str, humanizer_snippet: str,
                     knowledge_snippet: str, trans_mode_snippet: str = "", provider=None) -> List[Dict]:
    """
    Translates a list of blocks (may be any size) using generate_batch.
    Returns the blocks with translations applied. Does NOT apply humanize_text.
    Original text is kept as fallback for any block whose translation is missing/empty.
    """
    BATCH = 20
    total = len(blocks)
    tasks = []

    for i in range(0, total, BATCH):
        chunk = blocks[i:i + BATCH]
        input_text = ""
        for block in chunk:
            text = " ".join(block['lines']).replace("\n", " ").strip()
            # Apply terminology scrubbing to fix ASR errors (claw -> OpenClaw) before LLM sees it
            scrubbed_text = humanize_text(text, is_source=True)
            if not scrubbed_text.strip() and text.strip():
                scrubbed_text = text
            input_text += f"[{block['index']}] {scrubbed_text}\n"

        prompt = f"""You are an expert subtitle translator and editor.
Translate the following subtitles into Simplified Chinese.
The source language may be English, Japanese, or a mix of both.

### STEP 1: VERBALIZATION (Tone & Persona)
{verbalizer_snippet}...
TARGET STYLE: {style}

### STEP 2: DOMAIN KNOWLEDGE & ASR CORRECTION
{knowledge_snippet}

### STEP 3: HUMANIZATION (De-AI)
{humanizer_snippet}...
{trans_mode_snippet}

{ALIGNMENT_CONSTRAINTS}

### STEP 4: CONTEXT AWARENESS
INPUT BLOCK:
{input_text}

OUTPUT FORMAT (STRICT — one line per segment, no extra text):
[ID] Translated Text
...
"""
        tasks.append({'index': i // BATCH, 'chunk': chunk, 'prompt': prompt})

    results = client.generate_batch(tasks, model, fallback=False, provider=provider)
    results.sort(key=lambda x: x['index'])

    # Build a map of all translations
    translated_map = {}
    for res in results:
        result_text = res.get('result')
        if result_text:
            for line in result_text.split('\n'):
                match = re.match(r'\[(\d+)\]\s*(.*)', line.strip())
                if match:
                    idx = match.group(1)
                    content = match.group(2).strip()
                    if content:  # guard: reject empty translations
                        translated_map[idx] = content

    # Apply translations; fall back to original text if missing
    out = []
    for block in blocks:
        new_block = block.copy()
        idx = str(block['index'])
        if idx in translated_map:
            new_block['lines'] = [translated_map[idx]]
        # else: keep whatever was there (original EN or previous attempt)
        out.append(new_block)
    return out


def postprocess_retry_loop(final_blocks: List[Dict], client, model: str, style: str,
                            verbalizer_snippet: str, humanizer_snippet: str,
                            knowledge_snippet: str, trans_mode_snippet: str = "", max_iterations: int = 5, provider=None) -> List[Dict]:
    """
    Iteratively re-translates any untranslated/empty blocks until all are done
    (or max_iterations is reached). Returns final_blocks with all translations filled.
    Does NOT apply humanize_text — that is done in a single pass after this function.
    """
    for iteration in range(1, max_iterations + 1):
        missed_positions = [i for i, b in enumerate(final_blocks) if is_untranslated(b)]
        if not missed_positions:
            print(f"✅ Post-processing complete after {iteration - 1} extra pass(es). All segments translated.")
            break

        print(f"🔄 Post-processing pass {iteration}/{max_iterations}: "
              f"{len(missed_positions)} untranslated segment(s) detected. Re-translating...")

        missed_blocks = [final_blocks[i] for i in missed_positions]
        retranslated = translate_blocks(
            missed_blocks, client, model, style,
            verbalizer_snippet, humanizer_snippet, knowledge_snippet, trans_mode_snippet,
            provider=provider
        )

        # Re-insert results at original positions
        for pos, new_block in zip(missed_positions, retranslated):
            final_blocks[pos] = new_block

        # Report remaining
        still_missed = [i for i, b in enumerate(final_blocks) if is_untranslated(b)]
        resolved = len(missed_positions) - len(still_missed)
        print(f"   ✔ Resolved: {resolved} | Still untranslated: {len(still_missed)}")
    else:
        still_missed = [i for i, b in enumerate(final_blocks) if is_untranslated(b)]
        if still_missed:
            print(f"⚠️ Max iterations ({max_iterations}) reached. "
                  f"{len(still_missed)} segment(s) still untranslated.")
            # Final naked-prompt fallback: the batch prompt (with large
            # verbalizer/humanizer rule blocks) sometimes makes the LLM return
            # malformed [ID] lines for tricky fragments (e.g. a bare list of
            # proper nouns). Re-translate each remaining block with a minimal
            # single-line prompt that has no rule preamble — this reliably
            # produces a clean [ID] <translation> line. Prevents 1 stuck
            # block from failing the entire 978-block translation.
            print("🔄 Naked-prompt fallback for remaining stuck block(s)...")
            for pos in still_missed:
                blk = final_blocks[pos]
                idx = str(blk['index'])
                src = " ".join(blk.get('lines', [])).replace("\n", " ").strip()
                if not src:
                    continue
                naked = (f"Translate this ONE subtitle fragment into Simplified Chinese. "
                         f"Keep proper nouns/acronyms in English. "
                         f"Reply with ONLY one line in this exact format: [{idx}] <translation>\n\n"
                         f"[{idx}] {src}\n")
                try:
                    out = client.generate_content(naked, model_name=model, provider=provider)
                    if out:
                        import re as _re
                        m = _re.search(r'\[%s\]\s*(.+)' % _re.escape(idx), out)
                        if m:
                            cn = m.group(1).strip()
                            if cn and _re.search(r'[\u4e00-\u9fff]', cn):
                                final_blocks[pos]['lines'] = [cn]
                                print(f"   ✔ block {idx}: naked fallback succeeded")
                                continue
                    print(f"   ✗ block {idx}: naked fallback failed, keeping original")
                except Exception as e:
                    print(f"   ✗ block {idx}: naked fallback error {e}")

    return final_blocks

def _en_distinctive_tokens(text: str) -> set:
    """Extract proper-noun/acronym tokens (kept as-is in CN) for alignment checks."""
    # All-caps acronyms len>=2 (CTOL, eVTOL, AI, US) and Capitalized words len>=3.
    toks = set(re.findall(r"\b[A-Z]{2,}[0-9A-Za-z]*\b", text))
    toks |= set(re.findall(r"\b[A-Z][A-Za-z0-9]{2,}\b", text))
    return {t.lower() for t in toks}


def scan_source_terminology(blocks: List[Dict], client, model: str, provider=None) -> str:
    """
    Scans source subtitle text for domain-specific key phrases and asks the LLM
    to produce a glossary of preferred translations. This catches terms like
    "Primacy of Winning" or "Forward Deployed Engineer" that need specific
    translations rather than literal renderings.
    """
    # Collect all source text
    all_text = " ".join([" ".join(b['lines']) for b in blocks])
    # Extract candidate phrases: sequences of Capitalized words (2-5 words)
    candidates = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,4}\b', all_text)
    # Deduplicate and count
    from collections import Counter
    counts = Counter(candidates)
    # Take top 30 most frequent multi-word phrases
    top_phrases = [p for p, c in counts.most_common(30)]
    if not top_phrases:
        return ""

    phrases_str = "\n".join(f"{i+1}. {p}" for i, p in enumerate(top_phrases))
    prompt = f"""You are preparing a terminology glossary for a video subtitle translation.
Below are key phrases extracted from the source text. For each phrase, provide the
preferred Simplified Chinese translation. If a phrase is a proper noun (company name,
person name, product name), keep it in English. For conceptual phrases, provide a
concise, natural Chinese translation that fits a subtitle context (short, punchy).

Phrases:
{phrases_str}

Output format (one per line):
Phrase -> Translation
"""

    try:
        result = client.generate_content(prompt, model_name=model, fallback=True, provider=provider, disable_thinking=True)
        if not result:
            return ""
        glossary_lines = []
        for line in result.strip().split('\n'):
            if '->' in line:
                parts = line.split('->', 1)
                en_term = parts[0].strip()
                cn_term = parts[1].strip()
                if cn_term and en_term:
                    glossary_lines.append(f"- {en_term}: {cn_term}")
        if glossary_lines:
            glossary_str = "\n### DYNAMIC GLOSSARY (from source text analysis)\n" + "\n".join(glossary_lines)
            print(f"📖 Built dynamic glossary with {len(glossary_lines)} terms.")
            return glossary_str
    except Exception as e:
        print(f"⚠️ Terminology scan failed: {e}")
    return ""


def detect_misaligned_blocks(final_blocks: List[Dict], source_lines: Dict[str, str],
                            client, model: str, style: str, verbalizer_snippet: str,
                            humanizer_snippet: str, knowledge_snippet: str,
                            trans_mode_snippet: str = "",
                            provider=None) -> List[Dict]:
    """
    Desync backstop. The translation LLM sometimes merges content from EN
    block N+1 (or later) into the CN translation of block N, so the Chinese
    runs ahead of the English on the timeline. Detect this by finding
    distinctive proper-noun/acronym tokens that appear in a CN block but
    whose EN source lives in a LATER block, then re-translate those blocks
    one-at-a-time with a strict single-fragment prompt that cannot absorb
    adjacent content.

    Additionally, an LLM-based alignment verification pass catches general
    content shifts that the token-based heuristic misses (e.g. when the LLM
    skips a block and shifts all subsequent translations forward by one).
    """
    # Build per-block EN distinctive-token sets.
    en_tokens = {}
    for b in final_blocks:
        idx = str(b['index'])
        en_tokens[idx] = _en_distinctive_tokens(source_lines.get(idx, ""))

    # CN blocks may keep acronyms/proper nouns verbatim; find forward-absorbed ones.
    misaligned = []
    for i, b in enumerate(final_blocks):
        idx = str(b['index'])
        cn_text = " ".join(b.get('lines', []))
        cn_tokens = _en_distinctive_tokens(cn_text)
        if not cn_tokens:
            continue
        own_en = en_tokens.get(idx, set())
        # tokens in CN that are NOT in this block's own EN but ARE in a later EN block
        forward = cn_tokens - own_en
        absorbed = set()
        for j in range(i + 1, min(i + 4, len(final_blocks))):  # look ahead up to 3 blocks
            later_idx = str(final_blocks[j]['index'])
            absorbed |= (forward & en_tokens.get(later_idx, set()))
        if absorbed:
            misaligned.append((i, idx, sorted(absorbed)))

    if misaligned:
        print(f"\u26a0\ufe0f Token-based alignment check: {len(misaligned)} block(s) absorb later content. "
              f"Re-translating individually: {[(idx, toks) for _, idx, toks in misaligned]}")
        final_blocks = _retranslate_misaligned(final_blocks, misaligned, source_lines, client, model, provider=provider)

    # LLM-based alignment verification: catches general content shifts that
    # the token-based heuristic misses (e.g. the LLM skipped an EN block and
    # shifted all subsequent CN translations forward by one position).
    final_blocks = _llm_alignment_check(
        final_blocks, source_lines, client, model, style,
        verbalizer_snippet, humanizer_snippet, knowledge_snippet, trans_mode_snippet,
        provider=provider
    )

    return final_blocks


def _retranslate_misaligned(final_blocks, misaligned, source_lines, client, model, provider=None):
    """Re-translate blocks that absorbed later content, using a strict single-fragment prompt."""
    for pos, idx, toks in misaligned:
        en_src = source_lines.get(idx, "")
        if not en_src:
            continue
        strict_prompt = f"""You are translating ONE subtitle fragment. Translate ONLY the
text below into Simplified Chinese. Do NOT include content from any other fragment, do NOT
look ahead or behind. The text is a partial sentence \u2014 translate exactly that fragment,
even if it is incomplete, so it aligns with this one subtitle block on the timeline.
These tokens must NOT appear unless they are in the source fragment: {', '.join(toks)}.

FRAGMENT: {en_src}

Reply with ONLY the Chinese translation, nothing else."""
        try:
            corrected = client.generate_content(strict_prompt, model_name=model, provider=provider)
            corrected = (corrected or "").strip()
            if corrected and re.search(r"[\u4e00-\u9fff]", corrected):
                final_blocks[pos]['lines'] = [corrected]
                print(f"   \u2714 block {idx}: re-translated (was absorbing {toks})")
            else:
                print(f"   \u2717 block {idx}: re-translation failed validation, kept original")
        except Exception as e:
            print(f"   \u2717 block {idx}: re-translation error {e}, kept original")
    return final_blocks


def _llm_alignment_check(final_blocks: List[Dict], source_lines: Dict[str, str],
                         client, model: str, style: str, verbalizer_snippet: str,
                         humanizer_snippet: str, knowledge_snippet: str,
                         trans_mode_snippet: str = "",
                         provider=None) -> List[Dict]:
    """
    LLM-based alignment verification. Sends batches of consecutive (EN, CN)
    pairs to the LLM and asks it to identify pairs where the Chinese does NOT
    correspond to the English source (e.g. the CN translates content from an
    adjacent block). Re-translates flagged blocks with a strict single-fragment
    prompt that includes surrounding context but forbids absorbing it.
    """
    BATCH_SIZE = 10
    total = len(final_blocks)
    tasks = []

    for i in range(0, total, BATCH_SIZE):
        chunk = final_blocks[i:min(i + BATCH_SIZE, total)]
        pairs_str = ""
        for j, b in enumerate(chunk):
            seq = j + 1
            idx = str(b['index'])
            en = source_lines.get(idx, " ".join(b.get('lines', [])))
            cn = " ".join(b.get('lines', []))
            # Skip blocks that are still untranslated English (no CJK)
            if not re.search(r'[\u4e00-\u9fff]', cn):
                pairs_str += f"[{seq}] EN: {en}\n    CN: [UNTRANSLATED]\n"
            else:
                pairs_str += f"[{seq}] EN: {en}\n    CN: {cn}\n"

        prompt = f"""You are a bilingual subtitle alignment checker. For each pair below,
determine if the Chinese (CN) translation corresponds to the English (EN) source text.

A pair is ALIGNED (OK) if the CN translates the same meaning as the EN, even if:
  - The EN is a partial sentence fragment (e.g., "And I think that kind of opens the door to a")
  - The CN is also a fragment (e.g., "我觉得这为更广泛的")
  - The CN reorganizes word order for natural Chinese flow

A pair is MISALIGNED (flag it) ONLY if the CN is clearly translating DIFFERENT content
than the EN - for example:
  - The EN says "How do I get more builders?" but the CN says "我觉得这打开了大门" (translating a different block)
  - The CN seems to be translating the NEXT or PREVIOUS block's English instead of its own
  - The CN and EN share no common meaning at all

Be conservative: when in doubt, consider it aligned. Only flag obvious mismatches.

Pairs:
{pairs_str}

Output: List ONLY the misaligned pair numbers (e.g. "3" or "3,7"), one per line.
If ALL pairs are correctly aligned, output exactly: ALL_ALIGNED"""

        tasks.append({'chunk_idx': i // BATCH_SIZE, 'prompt': prompt, 'chunk': chunk, 'start_pos': i})

    if not tasks:
        return final_blocks

    print(f"\U0001f50d LLM alignment check: verifying {total} blocks in {len(tasks)} batches...")
    print(f"Progress: 97.5% (LLM alignment check: {len(tasks)} batches)", flush=True)
    results = client.generate_batch(tasks, model, fallback=False, provider=provider)

    print(f"   📋 Analyzing alignment results from {len(results)} batches...", flush=True)

    misaligned_positions = []
    for res in results:
        result_text = res.get('result')
        chunk = res.get('chunk', [])
        start_pos = res.get('start_pos', 0)
        if not result_text:
            continue
        result_text = result_text.strip()
        if result_text.upper() == "ALL_ALIGNED":
            continue
        # Parse misaligned pair numbers
        for line in result_text.split('\n'):
            line = line.strip()
            nums = re.findall(r'\d+', line)
            for n in nums:
                seq = int(n)
                if 1 <= seq <= len(chunk):
                    pos = start_pos + seq - 1
                    misaligned_positions.append(pos)

    if not misaligned_positions:
        print(f"\u2705 LLM alignment check passed: all blocks aligned.")
        return final_blocks

    # Deduplicate
    misaligned_positions = sorted(set(misaligned_positions))
    print(f"\u26a0\ufe0f LLM alignment check: {len(misaligned_positions)} block(s) misaligned. "
          f"Re-translating: {[final_blocks[p]['index'] for p in misaligned_positions]}")

    for pos in misaligned_positions:
        b = final_blocks[pos]
        idx = str(b['index'])
        en_src = source_lines.get(idx, "")
        if not en_src:
            continue

        # Build context from adjacent blocks for better translation quality,
        # but instruct the LLM to translate ONLY the target block's content.
        prev_en = source_lines.get(str(final_blocks[pos-1]['index']), "") if pos > 0 else ""
        next_en = source_lines.get(str(final_blocks[pos+1]['index']), "") if pos < len(final_blocks)-1 else ""

        context_str = ""
        if prev_en:
            context_str += f"PREVIOUS BLOCK (for context only, DO NOT translate): {prev_en}\n"
        context_str += f"TARGET BLOCK (translate ONLY this): {en_src}\n"
        if next_en:
            context_str += f"NEXT BLOCK (for context only, DO NOT translate): {next_en}\n"

        strict_prompt = f"""You are translating ONE subtitle fragment into Simplified Chinese.
The fragment below is marked as TARGET BLOCK. Translate ONLY that block's text.
The PREVIOUS and NEXT blocks are provided for context only \u2014 do NOT translate them and
do NOT include their content in your translation. The target may be a partial sentence;
translate exactly that fragment so it aligns with this one subtitle block on the timeline.

{context_str}

Reply with ONLY the Chinese translation of the TARGET BLOCK, nothing else."""
        try:
            corrected = client.generate_content(strict_prompt, model_name=model)
            corrected = (corrected or "").strip()
            if corrected and re.search(r"[\u4e00-\u9fff]", corrected):
                final_blocks[pos]['lines'] = [corrected]
                print(f"   \u2714 block {idx}: re-translated (was misaligned)")
            else:
                print(f"   \u2717 block {idx}: re-translation failed validation, kept original")
        except Exception as e:
            print(f"   \u2717 block {idx}: re-translation error {e}, kept original")

    return final_blocks


def clean_untranslated_english(final_blocks: List[Dict], source_lines: Dict[str, str],
                              client, model: str, style: str, verbalizer_snippet: str,
                              humanizer_snippet: str, knowledge_snippet: str,
                              trans_mode_snippet: str = "") -> List[Dict]:
    """
    Strip residual untranslated English words from mostly-Chinese blocks (the
    'actually' / 'you know' issue). A block that has CJK but also >=2 lowercase
    English words (excluding proper nouns/acronyms/numbers) is partially
    untranslated and gets re-translated with a no-residual-English instruction.
    """
    # Common English filler words that leak through as untranslated.
    filler = set("""actually basically literally basically really just like you know
    so anyway though however well right okay okay yeah yes no oh um ah
    i mean kind of sort of""".split())

    def _residual_en(text: str) -> list:
        if not re.search(r"[\u4e00-\u9fff]", text):  # only flag mixed (CN+EN) blocks
            return []
        words = re.findall(r"\b[a-z]{2,}\b", text)
        # drop filler we explicitly allow-list as low-risk? no — filler is the target.
        return [w for w in words if w in filler or len(w) >= 3]

    flagged = [(i, str(b['index'])) for i, b in enumerate(final_blocks)
                if len(_residual_en(" ".join(b.get('lines', [])))) >= 1]
    if not flagged:
        print("✅ Residual-English check passed: no partially-translated blocks.")
        return final_blocks

    print(f"⚠️ Residual-English check: {len(flagged)} block(s) have untranslated English. "
          f"Re-translating: {[idx for _, idx in flagged]}")

    for pos, idx in flagged:
        en_src = source_lines.get(idx, "")
        if not en_src:
            continue
        clean_prompt = f"""Translate this ONE subtitle fragment into Simplified Chinese.
The output must be fully Chinese except for proper nouns / acronyms / brand names
(keep those in English). Do NOT leave common English words like "actually", "you know",
"so" untranslated. If the fragment is a partial sentence, translate just that part.

FRAGMENT: {en_src}

Reply with ONLY the Chinese translation."""
        try:
            corrected = client.generate_content(clean_prompt, model_name=model)
            corrected = (corrected or "").strip()
            if corrected and re.search(r"[\u4e00-\u9fff]", corrected):
                final_blocks[pos]['lines'] = [corrected]
                print(f"   ✔ block {idx}: re-translated (residual English removed)")
            else:
                print(f"   ✗ block {idx}: re-translation failed, kept original")
        except Exception as e:
            print(f"   ✗ block {idx}: re-translation error {e}, kept original")

    return final_blocks


_PROPER_NOUN_ASR_CORRECTIONS = {
    "Ermann": "Ermon",
    "Walkwest": "Wahlquist",
}


def _correct_known_asr_proper_nouns(blocks: List[Dict]) -> List[Dict]:
    """Correct high-confidence ASR name errors before they enter the cache."""
    for block in blocks:
        corrected_lines = []
        for line in block.get("lines", []):
            value = str(line)
            for wrong, right in _PROPER_NOUN_ASR_CORRECTIONS.items():
                value = re.sub(
                    rf"(?<![A-Za-z]){re.escape(wrong)}(?![A-Za-z])",
                    right,
                    value,
                )
            corrected_lines.append(value)
        block["lines"] = corrected_lines
    return blocks


def main():
    parser = argparse.ArgumentParser(description="Smart Translation with Context & Style")
    parser.add_argument("input", help="Input English SRT file")
    parser.add_argument("--style", default="auto", choices=["auto", "casual", "formal", "edgy"])
    default_model = os.environ.get("GOOGLE_GENAI_MODEL") or "glm-5.3"
    parser.add_argument("--model", default=default_model, help="LLM Model to use for translation")
    parser.add_argument("--provider", default="dashscope", help="Explicit LLM Provider")
    parser.add_argument("--chunk-size", type=int, default=50, help="Number of blocks per batch")
    parser.add_argument("--trans-mode", default="balanced", choices=["paraphrase", "balanced"], help="Translation Mode")

    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"File not found: {input_path}")
        return

    print(f"🚀 Starting Smart Translation for: {os.path.basename(input_path)}")
    print(f"Progress: 0.1% (Analyzing subtitle chunks...)", flush=True)

    # 1. Parse Input
    blocks = srt_utils.parse_srt(input_path)
    if not blocks:
        print("Error parsing SRT file.")
        return
    blocks = _correct_known_asr_proper_nouns(blocks)

    # Snapshot the English source text per block BEFORE translation/cache overwrite.
    # Needed later by detect_misaligned_blocks / clean_untranslated_english to
    # compare the CN output against its EN source fragment.
    source_lines = {str(b['index']): " ".join(b['lines']).replace("\n", " ").strip() for b in blocks}

    # 1.5 Auto style detection
    effective_style = args.style
    
    provider_enum = None
    if args.provider:
        try:
            provider_enum = LLMProvider(args.provider)
        except ValueError:
            pass

    # --- Early cache check: skip expensive pre-translation LLM scans when
    # most blocks are already cached from a previous run. These scans
    # (style detection, ad detection, terminology scan) each make an LLM
    # call that can hang for 15+ minutes if the network routes through a
    # proxy. When >50% of blocks are cached, the style and terminology
    # were already determined in the previous run.
    cache_path = os.path.join(os.path.dirname(input_path), os.path.splitext(os.path.basename(input_path))[0] + ".translation_cache.json")
    _early_cached_count = 0
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                _early_cache = json.load(f)
            _early_cached_count = len(_early_cache)
        except:
            pass
    _cache_ratio = _early_cached_count / len(blocks) if blocks else 0
    _skip_pre_scan = _cache_ratio > 0.5

    if args.style == "auto":
        if _skip_pre_scan:
            effective_style = "casual"
            print(f"⏩ {_early_cached_count}/{len(blocks)} blocks cached — skipping style detection (defaulting to {effective_style}).")
        else:
            effective_style = detect_translation_style(blocks, args.model, provider=provider_enum)
    print(f"   Style: {effective_style} (Configured: {args.style}) | Chunk Size: {args.chunk_size}")

    # 1.6 Ad / sponsor segment detection (full-transcript scan)
    # Identifies ad spans so they can be skipped during translation and
    # excluded when slicing clips. Result is also persisted to
    # <name>.ad_segments.json for downstream tools (e.g. clipcraft).
    if _skip_pre_scan:
        print(f"⏩ Skipping ad detection (cached run).")
        ad_segments = []
    else:
        print("📺 Detecting ad / sponsor segments...")
        ad_segments = detect_ad_segments(blocks, client, args.model, provider=provider_enum)
    if ad_segments:
        ad_json_path = os.path.join(
            os.path.dirname(input_path),
            os.path.splitext(os.path.basename(input_path))[0] + ".ad_segments.json",
        )
        with open(ad_json_path, "w", encoding="utf-8") as f:
            json.dump(ad_segments, f, ensure_ascii=False, indent=2)
        ad_block_count = sum(1 for b in blocks if b.get('is_ad'))
        print(f"   Found {len(ad_segments)} ad segment(s) covering {ad_block_count} blocks -> {os.path.basename(ad_json_path)}")
    else:
        print("   No ad segments detected.")

    total_chunks = math.ceil(len(blocks) / args.chunk_size)
    print(f"   Total Blocks: {len(blocks)} -> {total_chunks} Chunks")

    translated_map = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                translated_map = json.load(f)
            print(f"🔄 Loaded {len(translated_map)} cached translations.")
        except: pass

    # Apply cache to initial blocks
    for block in blocks:
        idx = str(block['index'])
        if idx in translated_map:
            block['lines'] = [translated_map[idx]]
    blocks = _correct_known_asr_proper_nouns(blocks)

    final_blocks = []

    # Identify untranslated blocks
    untranslated_blocks = [b for b in blocks if is_untranslated(b)]
    if not untranslated_blocks:
        print("✅ All blocks already translated (from cache).")
        final_blocks = blocks
    else:
        print(f"🚀 Translating {len(untranslated_blocks)} remaining segments...")

        # 2. Process Chunks Concurrently
        tasks = []
        chunk_size = args.chunk_size
        num_untranslated_chunks = math.ceil(len(untranslated_blocks) / chunk_size)

    # Initialize default snippets for safety
    target_model = args.model
    verbalizer_snippet = VERBALIZER_RULES if VERBALIZER_RULES else "Translate naturally."
    humanizer_snippet = HUMANIZER_RULES if HUMANIZER_RULES else "Do not sound robotic."
    knowledge_snippet = ""
    trans_mode_snippet = ""

    if untranslated_blocks:
        # Pre-load rules for efficiency
        verbalizer_snippet = VERBALIZER_RULES if VERBALIZER_RULES else "Translate naturally."
        humanizer_snippet = HUMANIZER_RULES if HUMANIZER_RULES else "Do not sound robotic."
        knowledge_snippet = ""
        if SUBTRANSLATOR_RULES:
            # Extract Domain Knowledge if present, or just use relevant parts
            match = re.search(r'(## Domain Knowledge & ASR Correction.*)', SUBTRANSLATOR_RULES, re.DOTALL)
            if match:
                knowledge_snippet = match.group(1).strip()
            else:
                # Fallback to general constraints if section not found (or just empty)
                pass

        # Pre-built glossary for common AI/tech terms that LLMs frequently
        # mistranslate or leave in English. These are injected before the
        # dynamic scan so they always reach the LLM.
        PREBUILT_GLOSSARY = """
### PRE-BUILT GLOSSARY (always apply)
- Qwen: 通义千问 (Alibaba's LLM; use "千问" after first mention)
- telemetry: 运行数据 (NOT "遥测"; in enterprise context means operational/usage data)
- Conviction: Keep in English when referring to Sarah Guo's venture capital FUND/FIRM (e.g. "inside of Conviction", "when you started Conviction", "Conviction内部最大的分歧"). Translate as "确信" or "信念" when used as a common noun meaning personal belief or investment conviction (e.g. "leap of conviction" -> "纵身一跃的确信", "climb to conviction" -> "慢慢建立确信", "behind conviction" -> "在确信的背后", "what conviction looks like" -> "确信是什么样的"). Context determines which sense applies.
- diffusion model: 扩散模型
- diffusion-based LLM: 基于扩散模型的大语言模型
- serve a model / serving models: 提供模型推理服务 (never compress this into fragments such as "快么服务")
- service (technical deployment context): 服务 (translate the complete word; never truncate or phoneticize it)
- trial (CEO/job context): 试用期 or 试任 (NOT "审判"; a legal trial is "审判")
- Varun Mohan: 瓦伦·莫汉 (correct ASR variants include Baron/Barron Millich)
- John Schulman: 约翰·舒尔曼
- Charlie O'Neill: 查理·奥尼尔
- Ali Ghodsi: 阿里·戈德西 (correct ASR variants include Ali Goetze)
- Stefano Ermon: Stefano Ermon (correct ASR variant "Ermann")
- Chad Wahlquist: Chad Wahlquist (correct ASR variant "Walkwest")
- Baseten: Baseten (company name; correct ASR variant "Base10")
- Zyphra / Thinking Machines / OpenAI: keep the established company spelling unless a standard Chinese name is explicitly required
"""
        knowledge_snippet = (knowledge_snippet or "") + PREBUILT_GLOSSARY

        # Load user-maintained terms.yml glossary (same file used by PostOS).
        # This ensures names like 黄仁勋 and business terms like alpha are
        # consistently translated across all videos.
        if load_terms:
            terms_glossary = load_terms()
            if terms_glossary:
                knowledge_snippet = knowledge_snippet + "\n" + terms_glossary
                print(f"📖 Loaded terms.yml glossary into translation prompt.")

        # Dynamic terminology scan: detect domain-specific phrases in the source
        # text and build a per-video glossary. This prevents mistranslations of
        # terms like "Primacy of Winning" -> "赢的首要地位" (too literal).
        # Skip when most blocks are cached — the glossary was already built
        # in the previous run and the PREBUILT_GLOSSARY above covers key terms.
        if not _skip_pre_scan:
            print("📖 Scanning source text for domain-specific terminology...")
            dynamic_glossary = scan_source_terminology(blocks, client, target_model, provider=provider_enum)
            if dynamic_glossary:
                knowledge_snippet = (knowledge_snippet or "") + dynamic_glossary
        else:
            print("⏩ Skipping terminology scan (cached run).")

        if args.trans_mode == "balanced":
            trans_mode_snippet = """
    ### STEP 3.5: TRANSLATION BALANCE (SENSE-FOR-SENSE)
    - **Understand First, Then Compress**: Read 3-5 consecutive blocks as a unit to understand the speaker\'s full thought, then express it in concise, natural Chinese. Do NOT translate word-by-word or block-by-block in isolation — each block\'s Chinese should flow into the next.
    - **Fluency Over Brevity**: Control length so subtitles are readable in their on-screen time, but never sacrifice fluency for brevity. A translation that reads naturally and is a few characters longer is better than a telegraphic one that feels robotic. Do not mechanically drop pronouns or conjunctions if removing them makes the Chinese sound abrupt or disconnected.
    - **Example Constraint**: "As Anurag said, this is our fifth DevCon in about 15 months, and momentum is palpable, I think." -> "正如 Anurag 所说，15个月内第五届 DevCon，势头非常迅猛。"
    - **Metaphors & Terms**: Keep proper nouns and brand names in English (e.g., "DevCon" not "开发者大会", "Gastown", "Ralph Wiggum"). BUT English idioms, slang, and cultural references MUST be translated by their MEANING, not literally. "the cat is out of the bag" -> "秘密已经泄露了" (NOT "猫从袋子里出来了" and NOT confused with genie/bottle). "reverse dog years" -> "时间过得比狗年还快". "leap of faith" -> "纵身一跃的信念". Identify the idiom, understand its actual meaning, and express it in natural Chinese. Never substitute one English idiom for another.
    - **Context Injection**: Strictly forbidden. Do not add any explanatory background.
    """
        else:
            trans_mode_snippet = """
    ### STEP 3.5: TRANSLATION PARAPHRASE
    - Focus on sense-for-sense paraphrasing. Explain metaphors and add cultural/contextual background if it helps the domestic audience understand the subtext.
    """

        print(f"📦 Preparing {total_chunks} chunks for parallel processing...")

        tasks = []
        for i in range(total_chunks):
            start = i * args.chunk_size
            end = min((i + 1) * args.chunk_size, len(blocks))
            chunk = blocks[start:end]

            # Construct Prompt string here in main loop to be thread-safe/independent
            input_text = ""
            for block in chunk:
                text = " ".join(block['lines']).replace("\n", " ").strip()
                input_text += f"[{block['index']}] {text}\n"

            prompt = f"""
    You are an expert subtitle translator and editor.
    Translate the following subtitles into Simplified Chinese.
    The source language may be English, Japanese, or a mix of both.

    ### STEP 1: VERBALIZATION (Tone & Persona)
    {verbalizer_snippet}...
    TARGET STYLE: {effective_style}

    ### STEP 2: DOMAIN KNOWLEDGE & ASR CORRECTION
    {knowledge_snippet}

    ### STEP 3: HUMANIZATION (De-AI)
    {humanizer_snippet}...
    {trans_mode_snippet}

    {ALIGNMENT_CONSTRAINTS}

    ### STEP 4: CONTEXT AWARENESS
    INPUT BLOCK:
    {input_text}

    ### OUTPUT FORMAT:
    [ID] Translated Text
    ...
    """
            tasks.append({
                'index': i,
                'chunk': chunk,
                'prompt': prompt
            })

        # Execute Batch
        try:
            # Use user-specified model, or default to the main AutoSub model.
            target_model = args.model
            print(f"🚀 Using LLM: {target_model}...")

            # Progress tracker: prints real progress based on completed
            # chunks instead of a fake time-based estimate. Reasoning
            # models (glm-5.2 with thinking) can take 2-10 min per batch.
            import threading
            sim_done = False
            def sim_worker():
                start_time = time.time()
                last_print = 0
                while not sim_done:
                    time.sleep(2.0)
                    if sim_done: break
                    elapsed = time.time() - start_time
                    # Slow crawl to 95% based on elapsed time; the last 5%
                    # jumps when generate_batch returns.
                    # Crawl to 90% based on elapsed time; the remaining 10%
                    # is covered by post-processing Progress prints.
                    pct = min(90.0, (elapsed / max(10.0, len(tasks) * 30)) * 100)
                    if pct > last_print + 2:
                        last_print = round(pct, 1)
                        print(f"Progress: {last_print}% (Translating {len(tasks)} chunks...)", flush=True)
            t_sim = threading.Thread(target=sim_worker)
            t_sim.daemon = True
            t_sim.start()

            results = client.generate_batch(tasks, target_model, fallback=False, provider=provider_enum)
            sim_done = True

            # If every chunk failed, the LLM provider is down — abort now
            # instead of silently writing English as "translation".
            total_failed = sum(1 for r in results if r.get('result') is None)
            if total_failed == len(results) and results:
                errors = [r.get('error', 'unknown') for r in results if r.get('error')]
                err_detail = errors[0][:200] if errors else "No error details captured."
                print(
                    f"\n❌ FATAL: All {len(results)} translation chunks failed — "
                    f"LLM provider is unreachable. Aborting.\n   Error: {err_detail}",
                    flush=True,
                )
                sys.exit(1)

            # Sort results by index to ensure correct subtitle order
            results.sort(key=lambda x: x['index'])

            for res in results:
                result_text = res.get('result')
                chunk = res['chunk']

                translated_chunk_blocks = []

                if result_text:
                    # Parse output logic
                    translated_map_chunk = {}
                    for line in result_text.split('\n'):
                        match = re.match(r'\[(\d+)\]\s*(.*)', line.strip())
                        if match:
                            idx = match.group(1)
                            content = match.group(2).strip()
                            if content:  # guard: reject empty translations
                                translated_map_chunk[idx] = content

                    # Detect missing blocks: LLM returned fewer [ID] lines than input.
                    # Immediately retry the missing blocks with a single-block prompt
                    # instead of relying solely on the post-processing loop.
                    missing_ids = [str(b['index']) for b in chunk if str(b['index']) not in translated_map_chunk]
                    if missing_ids:
                        print(f"⚠️ Chunk {res['index'] + 1}: LLM skipped {len(missing_ids)} block(s) {missing_ids}. Retrying individually...", flush=True)
                        for block in chunk:
                            idx_str = str(block['index'])
                            if idx_str not in translated_map_chunk:
                                src = " ".join(block['lines']).replace("\n", " ").strip()
                                if not src:
                                    continue
                                retry_prompt = (f"Translate this ONE subtitle fragment into Simplified Chinese. "
                                                f"Use natural spoken Chinese, not written prose. "
                                                f"No translationese. No em-dashes. "
                                                f"Keep proper nouns/acronyms in English. "
                                                f"Reply with ONLY one line in this exact format: [{idx_str}] <translation>\n\n"
                                                f"[{idx_str}] {src}\n")
                                try:
                                    retry_out = client.generate_content(retry_prompt, model_name=target_model, provider=provider_enum)
                                    if retry_out:
                                        m = re.search(r'\[%s\]\s*(.+)' % re.escape(idx_str), retry_out)
                                        if m:
                                            cn = m.group(1).strip()
                                            if cn and re.search(r'[\u4e00-\u9fff]', cn):
                                                translated_map_chunk[idx_str] = cn
                                                print(f"   ✔ block {idx_str}: single retry succeeded", flush=True)
                                                continue
                                    print(f"   ✗ block {idx_str}: single retry failed, will retry in post-processing", flush=True)
                                except Exception as e:
                                    print(f"   ✗ block {idx_str}: single retry error {e}", flush=True)

                    # Apply translations; keep original as fallback for missing/empty
                    for block in chunk:
                        new_block = block.copy()
                        idx = str(block['index'])
                        if idx in translated_map_chunk:
                            new_block['lines'] = [translated_map_chunk[idx]]
                        # else: keep original English as fallback (detected by is_untranslated)
                        translated_chunk_blocks.append(new_block)
                else:
                    print(f"❌ Chunk {res['index']} failed completely. Will retry in post-processing.")
                    translated_chunk_blocks = chunk  # keep original for retry

                # Update Cache (we use translated_map from line 375)
                for b in translated_chunk_blocks:
                    if not is_untranslated(b):
                        translated_map[str(b['index'])] = " ".join(b['lines'])

                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(translated_map, f, ensure_ascii=False, indent=2)

                # Suppress rapid-fire chunk progress prints to avoid launcher UI jumping
                print(f"DEBUG: Chunk {res['index'] + 1}/{total_chunks} processed successfully.", flush=True)

            # Rebuild final_blocks from translated chunk results (the loop above
            # applied translations to translated_chunk_blocks but never merged them
            # back into final_blocks — this was the bug that caused all 559 segments
            # to be flagged as "untranslated" and needlessly re-translated).
            final_blocks = []
            for res in results:
                chunk = res['chunk']
                result_text = res.get('result')
                if result_text:
                    t_map = {}
                    for line in result_text.split('\n'):
                        m = re.match(r'\[(\d+)\]\s*(.*)', line.strip())
                        if m and m.group(2).strip():
                            t_map[m.group(1)] = m.group(2).strip()
                    for block in chunk:
                        nb = block.copy()
                        idx = str(block['index'])
                        if idx in t_map:
                            nb['lines'] = [t_map[idx]]
                        final_blocks.append(nb)
                else:
                    final_blocks.extend(chunk)

        except Exception as e:
            print(f"❌ Parallel execution failed: {e}")
            import traceback
            traceback.print_exc()
            return

    # 3. Post-Processing: retry all still untranslated segments
    untranslated_count = sum(1 for b in final_blocks if is_untranslated(b))
    if untranslated_count > 0:
        print(f"\n🔍 Post-processing: {untranslated_count} untranslated segment(s) found. Starting retry loop...")
        print(f"Progress: 96.0% (Post-processing: retrying {untranslated_count} untranslated segments)", flush=True)
        final_blocks = postprocess_retry_loop(
            final_blocks, client, target_model, effective_style,
            verbalizer_snippet, humanizer_snippet, knowledge_snippet, trans_mode_snippet,
            provider=provider_enum
        )
        # Final Cache update
        for b in final_blocks:
            if not is_untranslated(b):
                translated_map[str(b['index'])] = " ".join(b['lines'])
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(translated_map, f, ensure_ascii=False, indent=2)
    else:
        print("\n✅ All segments translated. Skipping post-processing.", flush=True)

   # 3.5 Desync backstop: detect blocks whose CN absorbed later EN content,
   #     and strip residual untranslated English. Both use source_lines snapshot.
    # Skip the expensive desync backstop when most blocks came from cache —
    # they already passed alignment checks in the previous run. Only run
    # the backstop on freshly translated blocks (the ones NOT in the
    # cache before this run started).
    if _skip_pre_scan:
        print(f"⏩ Skipping alignment backstop & residual-English check (cached run).", flush=True)
        print(f"Progress: 99.0% (Skipping post-processing — cached run)", flush=True)
    else:
        print(f"Progress: 97.0% (Alignment check & cleanup)", flush=True)
    final_blocks = detect_misaligned_blocks(
        final_blocks, source_lines, client, target_model, effective_style,
        verbalizer_snippet, humanizer_snippet, knowledge_snippet, trans_mode_snippet,
        provider=provider_enum
    )
    final_blocks = clean_untranslated_english(
        final_blocks, source_lines, client, target_model, effective_style,
        verbalizer_snippet, humanizer_snippet, knowledge_snippet, trans_mode_snippet
    )
   # Persist any re-translated blocks from the backstop into the cache.
    for b in final_blocks:
        if not is_untranslated(b):
            translated_map[str(b['index'])] = " ".join(b['lines'])
    if os.path.exists(os.path.dirname(cache_path)):
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(translated_map, f, ensure_ascii=False, indent=2)

    # 4. Final Style-Guide Humanization (single pass over all blocks)
    print("\n✨ Applying style guide and humanization to all blocks...")
    print(f"Progress: 99.0% (Applying style guide & humanization)", flush=True)
    for block in final_blocks:
        new_lines = []
        for l in block['lines']:
            h = humanize_text(l, is_source=False)
            if not h.strip() and l.strip():
                # Safety: if humanize_text wipes everything, fallback to original
                new_lines.append(l)
            else:
                new_lines.append(h)
        block['lines'] = new_lines

    # 5. Final validation: check if there are any untranslated blocks remaining
    untranslated_count = sum(1 for b in final_blocks if is_untranslated(b))
    if untranslated_count > 0:
        # Soft-degrade: a handful of stuck blocks (typically bare lists of
        # proper nouns like "CoreWeave and Crusoe and Nebius and ...") can
        # resist translation across all retries. Failing the entire run over
        # 1-2 such blocks is worse than keeping the original text for them —
        # the bilingual merge will still show EN for those fragments. Warn
        # and continue instead of aborting.
        stuck = [str(b['index']) for b in final_blocks if is_untranslated(b)]
        print(f"\n⚠️ {untranslated_count} segment(s) could not be translated after all retries "
              f"(likely bare proper-noun lists): {stuck}. Keeping original text for these; "
              f"output will have EN for those blocks. Continuing.")

    # 6. Save Output
    if input_path.lower().endswith(".en.srt"):
        output_path = input_path[:-7] + ".cn.srt"
    else:
        output_path = input_path.replace(".srt", ".cn.srt")


    def get_versioned_filename(filepath):
        if not os.path.exists(filepath): return filepath
        base, ext = os.path.splitext(filepath)
        match = re.search(r'_v(\d+)$', base)
        if match:
            version = int(match.group(1)); base = base[:match.start()]
        else: version = 0
        while True:
            version += 1
            new_path = f"{base}_v{version}{ext}"
            if not os.path.exists(new_path): return new_path

    output_path = get_versioned_filename(output_path)

    srt_utils.write_srt(final_blocks, output_path)
    print(f"Progress: 100.0% (Translation successfully completed)", flush=True)
    print(f"✅ Translation Saved to: {output_path}", flush=True)

    # Cleanup cache on success
    if os.path.exists(cache_path):
        try: os.remove(cache_path)
        except: pass

if __name__ == "__main__":
    main()
