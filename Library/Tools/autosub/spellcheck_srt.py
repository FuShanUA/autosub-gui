import os
import sys
import argparse
import re
import json
import yaml
from typing import List, Dict, Tuple, Optional

# Setup paths for internal imports
CURRENT_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, 'frozen', False):
    TOOLS_DIR = os.path.join(getattr(sys, '_MEIPASS', ''), "Library", "Tools")
else:
    TOOLS_DIR = os.environ.get("AUTOSUB_TOOLS_DIR") or os.path.dirname(CURRENT_SCRIPT_DIR)
PROJECT_ROOT = os.path.dirname(TOOLS_DIR)

sys.path.append(os.path.join(TOOLS_DIR, "common"))

try:
    import srt_utils
    import llm_utils
    from spellcheck_helpers import build_spellcheck_prompt, parse_spellcheck_result
except ImportError as e:
    print(f"❌ Error: Required library not found: {e}")
    sys.exit(1)

# Default path configurations
TERMS_YML_PATH = os.path.join(TOOLS_DIR, "postfdry", "config", "terms.yml")
HARD_CONSTRAINTS_PATH = os.path.join(TOOLS_DIR, "common", "HARD_CONSTRAINTS.md")


def load_glossary_terms() -> List[str]:
    """Loads terms from terms.yml and returns them as a list of correct terms."""
    terms_list = []

    # 1. Load from terms.yml
    if os.path.exists(TERMS_YML_PATH):
        try:
            with open(TERMS_YML_PATH, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        # Extract both key and value if they are English proper nouns
                        if isinstance(k, str) and re.match(r'^[a-zA-Z0-9\s.\'-]+$', k.strip()):
                            terms_list.append(k.strip())
                        if isinstance(v, str) and re.match(r'^[a-zA-Z0-9\s.\'-]+$', v.strip()):
                            terms_list.append(v.strip())
        except Exception as e:
            print(f"⚠️ Failed to parse terms.yml: {e}")

    # 2. Add standard known proper nouns as a robust fallback
    known_fallbacks = ["Palantir"]
    for term in known_fallbacks:
        if term not in terms_list:
            terms_list.append(term)

    # Deduplicate and sort
    seen = set()
    unique_terms = []
    for term in terms_list:
        term_lower = term.lower()
        if term_lower not in seen and len(term) > 2:
            seen.add(term_lower)
            unique_terms.append(term)

    return unique_terms


def load_asr_corrections() -> List[Tuple[str, str]]:
    """Loads deterministic ASR correction patterns: hardcoded safety net + HARD_CONSTRAINTS.md Section 1."""
    corrections = []

    # ── Hardcoded known ASR misspellings (Silicon Valley figures & common errors) ──
    # IMPORTANT: Longer/more-specific patterns must come BEFORE shorter ones
    # (e.g., "Trey Stephens" before "Trey", "Alex Carp" before "Carp")
    HARDCODED_ASR_FIXES = [
        # Trae Stephens (Anduril co-founder, Palantir alum)
        (r'\bTrey\s+Stephens\b', 'Trae Stephens'),
        (r'\bTrey\b', 'Trae'),
        # Alex Karp (Palantir CEO)
        (r'\bAlex\s+Carp\b', 'Alex Karp'),
        # Shyam Sankar (Palantir CTO)
        (r'\bSh(?:am|awn)\s+Sankar\b', 'Shyam Sankar'),
        (r'\bSh(?:am|awn|yam)\b', 'Shyam'),
        # Ondas Networks
        (r'\b(?:Andas|OnDos)\b', 'Ondas'),
        # John Wernfeldt
        (r'\bJohn\s+Wernfeldt\b', 'John Wernfeldt'),
        # Sam Altman (OpenAI CEO)
        (r'\bSam\s+Alterman\b', 'Sam Altman'),
        (r'\bAlterman\b', 'Altman'),
        # Varun Mohan (Codeium/Windsurf; ASR often produces Baron Millich)
        (r'\bBarr?on\s+Millich\b', 'Varun Mohan'),
        (r'\bMillich\b', 'Mohan'),
        # Baseten infrastructure company
        (r'\bBase\s*10\b', 'Baseten'),
        # Ali Ghodsi (Databricks CEO)
        (r'\bAli\s+Goetze\b', 'Ali Ghodsi'),
        (r'\bGoetze\b', 'Ghodsi'),
    ]
    corrections.extend(HARDCODED_ASR_FIXES)

    # ── Parse HARD_CONSTRAINTS.md Section 1 for regex→replacement pairs ──
    if os.path.exists(HARD_CONSTRAINTS_PATH):
        try:
            with open(HARD_CONSTRAINTS_PATH, 'r', encoding='utf-8') as f:
                content = f.read()

            # Find Section 1 region (between "## 1." header and "## 2." header)
            section_match = re.search(
                r'##\s*1\.\s*Terminology.*?\n(.*?)(?=\n##\s*2\.|\n---|\Z)',
                content, re.DOTALL
            )
            section_text = section_match.group(1) if section_match else content

            # Parse markdown table rows: | Description | `pattern` | `replacement` | Notes |
            for line in section_text.split('\n'):
                m = re.match(
                    r'\|\s*[^|]+\s*\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|',
                    line.strip()
                )
                if m:
                    pattern = m.group(1).strip().replace(r'\|', '|')
                    replacement = m.group(2).strip().replace(r'\|', '|')
                    if pattern and replacement:
                        # Skip rules that inject CJK into English source text
                        if re.search(r'[\u4e00-\u9fff]', replacement):
                            continue
                        corrections.append((pattern, replacement))
        except Exception as e:
            print(f"⚠️ Failed to parse HARD_CONSTRAINTS.md: {e}")

    return corrections


def apply_deterministic_corrections(blocks: List[Dict], corrections: List[Tuple[str, str]]) -> int:
    """Pass 3: Apply regex corrections mechanically to subtitle blocks.
    Returns total count of individual line corrections made."""
    total_fixes = 0
    for pattern, replacement in corrections:
        count = 0
        for block in blocks:
            for i, line in enumerate(block['lines']):
                new_line = re.sub(pattern, replacement, line)
                if new_line != line:
                    count += 1
                    block['lines'][i] = new_line
        if count > 0:
            print(f"   🔧 Deterministic fix: {pattern} → {replacement} ({count}x)")
            total_fixes += count
    return total_fixes


def scan_global_names(blocks: List[Dict], terms: List[str], client, model_name: str, provider=None) -> Dict[str, str]:
    """Pass 1: Scan the entire transcript to build a global name-correction map.
    The LLM sees the full transcript with glossary context, so it can identify
    ASR misspellings based on who appears alongside whom (e.g., "Trey" next to
    Palmer Luckey → Trae Stephens).
    Returns {misspelled_form: correct_form} dictionary."""

    # Format entire transcript
    all_text = ""
    for block in blocks:
        text = " ".join(block['lines']).replace("\n", " ").strip()
        all_text += f"[{block['index']}] {text}\n"

    terms_formatted = "\n".join([f"- {term}" for term in sorted(terms)])

    prompt = f"""You are analyzing a full subtitle transcript to identify ASR (speech recognition) misspellings of proper nouns — people, companies, and product names.

You have access to the ENTIRE transcript below, which gives you full context. For example, if "Trey" appears alongside Silicon Valley figures like Palmer Luckey, Sam Altman, etc., you should recognize "Trey" as an ASR misspelling of "Trae" (Trae Stephens, Anduril co-founder). Similarly, "Alterman" near "OpenAI" should map to "Altman".

### CORRECT GLOSSARY TERMS & NAMES:
{terms_formatted}

### FULL TRANSCRIPT:
{all_text}

### YOUR TASK:
Identify all proper nouns in the transcript that are likely ASR misspellings of glossary terms or known public figures. Output ONLY a JSON dictionary mapping each misspelled form to its correct form.

Rules:
- Only map names that sound phonetically/acoustically similar (e.g., "Trey" → "Trae", "Carp" → "Karp", "Alterman" → "Altman")
- Do NOT map different names to each other just because they're both in the glossary (e.g., do NOT map "Moxie" to any glossary term)
- Include partial names (e.g., standalone "Carp" → "Karp" if "Alex Karp" context exists in the transcript)
- If a name appears correctly in some places but misspelled in others, include only the misspelled→correct mapping
- Keep game/player names that are NOT public figures as-is (e.g., "Moxie", "Liv" are just game participants)
- Consider the context: who appears alongside whom determines whether a name is a public figure's ASR error or just a game player

### OUTPUT FORMAT:
Return ONLY a JSON object. No explanations, no markdown, no commentary.
Example: {{"Trey": "Trae", "Carp": "Karp", "Alterman": "Altman"}}

If no misspellings are found, return: {{}}
"""

    try:
        response_text = client.generate_content(prompt, model_name=model_name, fallback=True, provider=provider, disable_thinking=True)
        if not response_text:
            print("   ⚠️ Pass 1 returned no response. Skipping global scan.")
            return {}

        # Parse JSON from response
        # Try direct JSON parse first
        try:
            result = json.loads(response_text.strip())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        # Try extracting JSON from markdown-wrapped response (```json ... ```)
        json_match = re.search(r'```(?:json)?\s*(\{[^`]+\})\s*```', response_text, re.DOTALL)
        if json_match:
            try:
                result = json.loads(json_match.group(1))
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

        # Try bare JSON object extraction
        json_match = re.search(r'\{[^{}]+\}', response_text, re.DOTALL)
        if json_match:
            try:
                result = json.loads(json_match.group())
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

        # Fallback: parse key=value or key:value style lines
        corrections = {}
        for line in response_text.split('\n'):
            m = re.match(r'["\']?([^"\'=:]+)["\']?\s*[:=]\s*["\']?([^"\'=:]+)["\']?', line.strip())
            if m:
                key = m.group(1).strip()
                val = m.group(2).strip()
                if key and val and key != val:
                    corrections[key] = val

        if corrections:
            print(f"   ⚠️ JSON parse failed, extracted corrections from raw text: {corrections}")
        return corrections

    except Exception as e:
        print(f"⚠️ Pass 1 global scan error: {e}")
        return {}


def spellcheck_chunk(chunk_blocks: List[Dict], terms: List[str], global_corrections: Dict[str, str], client, model_name: str, provider=None) -> List[Dict]:
    """Pass 2: Sends a chunk of subtitle blocks to LLM to correct proper nouns,
    using the global name-correction map from Pass 1 as a mandatory reference."""
    # Format input block
    input_text = ""
    for block in chunk_blocks:
        text = " ".join(block['lines']).replace("\n", " ").strip()
        input_text += f"[{block['index']}] {text}\n"

    terms_formatted = "\n".join([f"- {term}" for term in sorted(terms)])

    # Format global corrections as mandatory reference
    if global_corrections:
        corrections_formatted = "\n".join(
            [f'- "{wrong}" → "{right}"' for wrong, right in sorted(global_corrections.items())]
        )
        mandatory_section = f"""
### MANDATORY NAME CORRECTIONS (must apply everywhere):
These names were identified as ASR misspellings across the full transcript. You MUST apply these corrections in every occurrence:
{corrections_formatted}
"""
    else:
        mandatory_section = ""

    prompt = f"""You are an expert subtitle editor.
Your task is to review the following English subtitle transcript and perform **Strict Context-Aware Acoustic/Phonetic Spellchecking** on proper nouns, names, company names, or industry platforms.
{mandatory_section}
### CRITICAL RULES TO PREVENT OVER-CORRECTION:
1. **Strict Phonetic/Acoustic Similarity**: Correct proper nouns ONLY if they sound almost identical to a glossary term (e.g., "Trey Stevens" -> "Trae Stephens", "Alex Carp" -> "Alex Karp"). Do NOT map different names or words.
2. **No Hallucination**: Never introduce names (like "Shyam" or "Shyam Sankar") out of thin air at the end of the blocks or anywhere else, unless there is a clear acoustically similar word (e.g. "Sean", "Shame") in the input block.
3. **No Over-Correction**: Do NOT replace correct proper nouns in the input with other glossary terms just because they are in the glossary. Specifically:
   - Do NOT change "Ondas" or similar words to "Anduril" (they are different companies/names).
   - Keep "Ondas" as "Ondas".
4. **No Translation**: Keep the text in English.
5. **No Style Alterations**: Do NOT rewrite general conversational phrases, change punctuation, or alter the sentence structure.
6. **Keep Original if Unsure**: If there is no clear acoustic mistake, keep the transcriber's text exactly as-is.

### CORRECT GLOSSARY TERMS & NAMES:
{terms_formatted}

### INPUT SUBTITLE BLOCKS:
{input_text}

### OUTPUT FORMAT:
Return ONLY the corrected subtitle blocks using the exact format `[ID] Text`. Do not add any introduction, explanations, or formatting.
Example:
[1] So I was chatting with Trae Stephens about Palantir.
[2] And Joe Lonsdale agreed.
"""

    try:
        # Use fallback=True to ensure robust cascading if Vertex/primary provider fails or model is unsupported
        response_text = client.generate_content(prompt, model_name=model_name, fallback=True, provider=provider, disable_thinking=True)
        if not response_text:
            return chunk_blocks

        # Parse the output map
        corrected_map = {}
        for line in response_text.split('\n'):
            match = re.match(r'\[(\d+)\]\s*(.*)', line.strip())
            if match:
                idx = match.group(1)
                content = match.group(2).strip()
                if content:
                    corrected_map[idx] = content

        # Apply corrections back to blocks
        corrected_blocks = []
        for block in chunk_blocks:
            new_block = block.copy()
            idx = str(block['index'])
            if idx in corrected_map:
                # If there were multiple lines, split them back
                new_block['lines'] = [corrected_map[idx]]
            corrected_blocks.append(new_block)

        return corrected_blocks
    except Exception as e:
        print(f"⚠️ Error spellchecking chunk: {e}")
        return chunk_blocks  # Graceful fallback to original chunk


def run_spellcheck(srt_path: str, model_name: str = "glm-5.3", provider="dashscope") -> bool:
    """Parses, spellchecks, and overwrites the target srt file.
    Uses a three-pass approach:
      Pass 1: Global name scan (whole transcript → name-correction map)
      Pass 2: Chunk-by-chunk LLM spellcheck with global map as reference
      Pass 3: Deterministic regex safety net for known ASR errors
    """
    if not os.path.exists(srt_path):
        print(f"❌ SRT file not found: {srt_path}")
        return False

    print(f"🎙️ Starting LLM Acoustic Spellcheck for: {os.path.basename(srt_path)}...")
    blocks = srt_utils.parse_srt(srt_path)
    if not blocks:
        print("⚠️ SRT file contains no valid blocks.")
        return False

    terms = load_glossary_terms()
    client = llm_utils.get_client()

    # Reduced from 60 to 20: smaller prompts complete faster and more reliably.
    # Matches smart_translate BATCH=20. Parallelized via generate_batch below.
    chunk_size = 20
    total_blocks = len(blocks)

    # ── PASS 1: Global name scan ──
    print(f"🔍 Pass 1: Scanning full transcript ({total_blocks} blocks) for name corrections...")
    print(f"   Loaded {len(terms)} glossary terms.")
    global_corrections = scan_global_names(blocks, terms, client, model_name, provider=provider)
    if global_corrections:
        print(f"   📋 Global name corrections found: {global_corrections}")
    else:
        print("   No global name corrections identified by LLM.")

    # ── PASS 2: Parallel spellcheck with global map ──
    print(f"✏️ Pass 2: Parallel spellcheck ({total_blocks} blocks, chunk size={chunk_size}, workers={client.max_workers})...")
    corrected_blocks = []

    # Build tasks for parallel batch processing. generate_batch uses
    # ThreadPoolExecutor(max_workers=5 for tier1) with per-task timeout (180s)
    # and graceful fallback. This replaces the previous serial loop that took
    # 5+ minutes; parallel execution should complete in ~30-60s.
    tasks = []
    for i in range(0, total_blocks, chunk_size):
        chunk = blocks[i:i + chunk_size]
        prompt = build_spellcheck_prompt(chunk, terms, global_corrections)
        tasks.append({'index': i // chunk_size, 'chunk': chunk, 'prompt': prompt})

    results = client.generate_batch(tasks, model_name, fallback=True, provider=provider, disable_thinking=True)
    results.sort(key=lambda x: x['index'])

    for res in results:
        chunk = res['chunk']
        result_text = res.get('result')
        if not result_text:
            print(f"   ⚠️ Chunk {res.get('index','?')} returned no result, keeping original.")
            corrected_blocks.extend(chunk)
        else:
            corrected_blocks.extend(parse_spellcheck_result(result_text, chunk))

    # ── PASS 3: Deterministic safety net ──
    print("🔧 Pass 3: Applying deterministic ASR corrections (safety net)...")
    asr_corrections = load_asr_corrections()
    fix_count = apply_deterministic_corrections(corrected_blocks, asr_corrections)
    if fix_count > 0:
        print(f"   ✅ Deterministic corrections applied: {fix_count}")
    else:
        print("   No deterministic corrections needed.")

    # Save corrected blocks back to SRT (subs first, then path)
    try:
        srt_utils.write_srt(corrected_blocks, srt_path)
        print("✅ LLM Acoustic Spellcheck completed successfully (3-pass).")
        return True
    except Exception as e:
        print(f"❌ Failed to write spellchecked SRT file: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Context-Aware Acoustic/Phonetic Spellcheck for SRT files.")
    parser.add_argument("srt_path", help="Path to the SRT subtitle file to spellcheck.")
    parser.add_argument("--model", default="glm-5.3", help="LLM model name.")
    parser.add_argument("--provider", default="dashscope", help="Explicit LLM Provider.")
    args = parser.parse_args()

    provider_enum = None
    if args.provider:
        from llm_utils import LLMProvider
        try:
            provider_enum = LLMProvider(args.provider)
        except ValueError:
            print(f"❌ Invalid provider: {args.provider}")
            sys.exit(1)

    success = run_spellcheck(args.srt_path, args.model, provider=provider_enum)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
