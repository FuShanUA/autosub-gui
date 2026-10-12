"""Helper functions for spellcheck_srt.py: prompt building and result parsing.
Extracted to avoid code duplication between serial and parallel code paths."""
import re
from typing import List, Dict


def build_spellcheck_prompt(chunk_blocks: List[Dict], terms: List[str], global_corrections: Dict[str, str]) -> str:
    """Builds the LLM prompt for a chunk of subtitle blocks."""
    input_text = ""
    for block in chunk_blocks:
        text = " ".join(block['lines']).replace("\n", " ").strip()
        input_text += f"[{block['index']}] {text}\n"

    terms_formatted = "\n".join([f"- {term}" for term in sorted(terms)])

    if global_corrections:
        corrections_formatted = "\n".join(
            [f'- "{wrong}" to "{right}"' for wrong, right in sorted(global_corrections.items())]
        )
        mandatory_section = f"""
### MANDATORY NAME CORRECTIONS (must apply everywhere):
These names were identified as ASR misspellings across the full transcript. You MUST apply these corrections in every occurrence:
{corrections_formatted}
"""
    else:
        mandatory_section = ""

    prompt = f"""You are an expert subtitle editor.
Your task is to review the following English subtitle transcript and perform **Strict Context-Aware Acoustic/Phonetic Spellchecking** on proper nouns, names, company names, industry platforms, AND cross-block consistency errors.
{mandatory_section}
### CRITICAL RULES TO PREVENT OVER-CORRECTION:
1. **Strict Phonetic/Acoustic Similarity**: Correct proper nouns ONLY if they sound almost identical to a glossary term (e.g., "Trey Stevens" -> "Trae Stephens", "Alex Carp" -> "Alex Karp"). Do NOT map different names or words.
2. **No Hallucination**: Never introduce names (like "Shyam" or "Shyam Sankar") out of thin air at the end of the blocks or anywhere else, unless there is a clear acoustically similar word (e.g. "Sean", "Shame") in the input block.
3. **No Over-Correction**: Do NOT replace correct proper nouns in the input with other glossary terms just because they are in the glossary. Specifically:
   - Do NOT change "Ondas" or similar words to "Anduril" (they are different companies/names).
   - Keep "Ondas" as "Ondas".
4. **Cross-Block Consistency**: If the SAME sentence or phrase appears in multiple blocks with different wording (one correct, one an ASR error), fix the errored version to match the correct one. Example: if block 1 says "by turning open the eye" but block 8 says "by joining OpenAI" in the same context, fix block 1 to "by joining OpenAI".
5. **No Translation**: Keep the text in English.
6. **No Style Alterations**: Do NOT rewrite general conversational phrases, change punctuation, or alter the sentence structure (EXCEPT for rule 4 cross-block consistency fixes).
7. **Keep Original if Unsure**: If there is no clear acoustic mistake or cross-block inconsistency, keep the transcriber's text exactly as-is.

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
    return prompt


def parse_spellcheck_result(result_text: str, chunk_blocks: List[Dict]) -> List[Dict]:
    """Parses LLM output back into corrected blocks.
    Falls back to original blocks if result is empty."""
    if not result_text:
        return chunk_blocks
    corrected_map = {}
    for line in result_text.split('\n'):
        match = re.match(r'\[(\d+)\]\s*(.*)', line.strip())
        if match:
            idx = match.group(1)
            content = match.group(2).strip()
            if content:
                corrected_map[idx] = content
    corrected_blocks = []
    for block in chunk_blocks:
        new_block = block.copy()
        idx = str(block['index'])
        if idx in corrected_map:
            new_block['lines'] = [corrected_map[idx]]
        corrected_blocks.append(new_block)
    return corrected_blocks
