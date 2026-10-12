#!/usr/bin/env python3
"""Audit and repair high-confidence errors in bilingual SRT files."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import re
import sys
from typing import Dict, Iterable, List, Optional, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.environ.get("AUTOSUB_TOOLS_DIR") or os.path.dirname(CURRENT_DIR)
COMMON_DIR = os.path.join(TOOLS_DIR, "common")
if COMMON_DIR not in sys.path:
    sys.path.insert(0, COMMON_DIR)

from llm_utils import LLMProvider, get_client  # noqa: E402
import srt_utils  # noqa: E402


ISSUE_TYPES = {
    "omission",
    "mistranslation",
    "proper_noun",
    "alignment",
    "residual_english",
    "broken_chinese",
}


def split_bilingual_lines(lines: Iterable[str]) -> Tuple[str, str]:
    """Split a bilingual block into its Chinese and English lines."""
    cn_lines = []
    en_lines = []
    for line in lines:
        value = str(line).strip()
        if not value:
            continue
        if re.search(r"[\u3400-\u9fff]", value):
            cn_lines.append(value)
        else:
            en_lines.append(value)
    return " ".join(cn_lines), " ".join(en_lines)


def _english_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]+", text))


def _chinese_char_count(text: str) -> int:
    return len(re.findall(r"[\u3400-\u9fff]", text))


def local_risk_reasons(cn: str, en: str) -> List[str]:
    """Return deterministic signals that a pair needs semantic review."""
    reasons: List[str] = []
    if not cn:
        reasons.append("missing Chinese translation")
    if not en:
        reasons.append("missing English source")

    en_words = _english_word_count(en)
    cn_chars = _chinese_char_count(cn)
    if en_words >= 8 and cn_chars and cn_chars < en_words * 0.55:
        reasons.append("Chinese is unusually short for the English source")

    en_numbers = set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", en))
    cn_numbers = set(re.findall(r"\d+(?:\.\d+)?", cn))
    if en_numbers and not en_numbers.issubset(cn_numbers):
        reasons.append("numbers differ between source and translation")

    en_negations = len(re.findall(r"\b(?:not|no|never|without|cannot|can't|don't|doesn't|didn't|won't)\b", en, re.I))
    cn_negations = len(re.findall(r"[不没无非未]", cn))
    if en_negations and not cn_negations:
        reasons.append("English negation is absent in Chinese")

    if cn and re.search(r"\b(?:actually|basically|literally|really|just|like|you know|so|anyway)\b", cn, re.I):
        reasons.append("residual English function words")

    return reasons


def _parse_json_object(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    value = raw.strip()
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I)
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}


def parse_audit_response(raw: Optional[str], valid_ids: set[str]) -> List[dict]:
    """Parse model output and reject malformed or out-of-range findings."""
    payload = _parse_json_object(raw)
    issues = payload.get("issues", [])
    if not isinstance(issues, list):
        return []

    valid = []
    seen = set()
    for item in issues:
        if not isinstance(item, dict):
            continue
        block = str(item.get("block", "")).strip()
        issue_type = str(item.get("type", "")).strip().lower().replace("-", "_").replace(" ", "_")
        severity = str(item.get("severity", "")).strip().lower()
        if block not in valid_ids or block in seen:
            continue
        if issue_type not in ISSUE_TYPES or severity not in {"high", "medium"}:
            continue
        suggested = str(item.get("suggested", "")).strip()
        if not suggested:
            continue
        if suggested and not re.search(r"[\u3400-\u9fff]", suggested):
            continue
        seen.add(block)
        valid.append(
            {
                "block": block,
                "type": issue_type,
                "severity": severity,
                "reason": str(item.get("reason", "")).strip(),
                "source": str(item.get("source", "")).strip(),
                "current": str(item.get("current", "")).strip(),
                "suggested": suggested,
            }
        )
    return valid


def _audit_prompt(batch: List[dict], previous_en: str, next_en: str) -> str:
    pairs = []
    for block in batch:
        pairs.append(
            f"[{block['index']}] EN: {block['source']}\n"
            f"    CN: {block['translation']}"
        )
    context = ""
    if previous_en:
        context += f"PREVIOUS CONTEXT (do not audit): {previous_en}\n"
    if next_en:
        context += f"NEXT CONTEXT (do not audit): {next_en}\n"

    return f"""You are a conservative bilingual subtitle quality auditor.
{context}Audit only the numbered EN/CN pairs below. Compare meaning, not wording.

Report an issue ONLY when you have high confidence about one of these:
- omission: a meaningful clause, object, number, condition, or negation is missing
- mistranslation: the Chinese reverses or changes the English meaning, or picks a clearly wrong word sense
- proper_noun: a person, company, product, or technical term is wrong or inconsistent
- alignment: the Chinese clearly translates an adjacent block instead of its own block
- residual_english: common English words remain untranslated
- broken_chinese: the Chinese is ungrammatical or reads as glued subtitle fragments

Do NOT report natural spoken compression, omitted fillers, tone changes, punctuation, or acceptable variants.
Do NOT rewrite the whole subtitle for style. If the current translation is acceptable, omit it.
For every issue, provide a minimal corrected Chinese line for this block only.

Output only JSON:
{{"issues":[{{"block":"ID","type":"omission|mistranslation|proper_noun|alignment|residual_english|broken_chinese","severity":"high|medium","reason":"...","source":"...","current":"...","suggested":"..."}}]}}
If there are no issues, output {{"issues":[]}}.

PAIRS:
{chr(10).join(pairs)}
"""


def audit_bilingual_srt(
    input_path: str,
    client,
    model: str,
    provider: Optional[LLMProvider] = None,
    batch_size: int = 30,
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Return findings, parsed blocks, and batches that remain unresolved."""
    blocks = srt_utils.parse_srt(input_path)
    if not blocks:
        raise ValueError(f"No subtitle blocks found: {input_path}")

    records = []
    for block in blocks:
        cn, en = split_bilingual_lines(block.get("lines", []))
        records.append(
            {
                "index": str(block.get("index", "")),
                "source": en,
                "translation": cn,
                "local_risks": local_risk_reasons(cn, en),
            }
        )

    tasks = []
    for start in range(0, len(records), batch_size):
        batch = records[start : start + batch_size]
        previous_en = records[start - 1]["source"] if start > 0 else ""
        next_start = start + batch_size
        next_en = records[next_start]["source"] if next_start < len(records) else ""
        tasks.append(
            {
                "chunk_idx": len(tasks),
                "prompt": _audit_prompt(batch, previous_en, next_en),
                "batch": batch,
            }
        )

    completed_results = {}
    pending_tasks = list(tasks)
    for attempt in range(3):
        if not pending_tasks:
            break
        results = client.generate_batch(
            pending_tasks, model, fallback=False, provider=provider
        )
        still_pending = []
        for task, result in zip(pending_tasks, results):
            if result.get("result") is None or result.get("error"):
                still_pending.append(task)
            else:
                completed_results[task["chunk_idx"]] = result
        pending_tasks = still_pending
        if pending_tasks and attempt < 2:
            print(
                f"⚠️ {len(pending_tasks)} audit batch(es) failed; "
                f"retry attempt {attempt + 2}/3",
                flush=True,
            )

    results = [
        completed_results[index]
        for index in range(len(tasks))
        if index in completed_results
    ]
    findings = []
    for result in results:
        batch_ids = {item["index"] for item in result["batch"]}
        findings.extend(parse_audit_response(result.get("result"), batch_ids))
    failed_batches = [
        {
            "chunk_idx": task["chunk_idx"],
            "block_range": (
                task["batch"][0]["index"],
                task["batch"][-1]["index"],
            ),
        }
        for task in pending_tasks
    ]
    return findings, blocks, failed_batches


def apply_findings(blocks: List[dict], findings: List[dict], min_severity: str = "high") -> int:
    """Apply reviewed findings in-place and return the number of changed blocks."""
    allowed = {"high"} if min_severity == "high" else {"high", "medium"}
    by_block = {
        str(item.get("block")): item
        for item in findings
        if item.get("severity") in allowed and item.get("suggested")
    }
    changed = 0
    for block in blocks:
        block_id = str(block.get("index", ""))
        finding = by_block.get(block_id)
        if not finding:
            continue
        new_lines = []
        replaced = False
        for line in block.get("lines", []):
            value = str(line).strip()
            if not replaced and re.search(r"[\u3400-\u9fff]", value):
                new_lines.append(finding["suggested"])
                replaced = True
            else:
                new_lines.append(value)
        if replaced:
            block["lines"] = new_lines
            changed += 1
    return changed


def _backup_file(path: str) -> None:
    backup_path = f"{path}.bak_audit"
    if os.path.exists(path) and not os.path.exists(backup_path):
        shutil.copy2(path, backup_path)


def write_srt(blocks: List[dict], output_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        for index, block in enumerate(blocks, start=1):
            handle.write(f"{index}\n{block['time']}\n")
            for line in block.get("lines", []):
                handle.write(f"{line}\n")
            handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Bilingual .bi.srt file")
    parser.add_argument("--model", default="glm-5.3")
    parser.add_argument("--provider", default="dashscope")
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--output", help="JSON audit report path")
    parser.add_argument("--apply", action="store_true", help="apply high-severity findings")
    parser.add_argument("--include-medium", action="store_true", help="also apply medium findings")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.isfile(input_path):
        print(f"File not found: {input_path}")
        return 2

    client = get_client()
    try:
        provider = LLMProvider(args.provider)
    except ValueError:
        provider = None

    findings, blocks, failed_batches = audit_bilingual_srt(
        input_path, client, args.model, provider, args.batch_size
    )
    report = {
        "input": input_path,
        "model": args.model,
        "block_count": len(blocks),
        "finding_count": len(findings),
        "failed_batch_count": len(failed_batches),
        "failed_batches": failed_batches,
        "findings": findings,
    }
    report_path = args.output or os.path.splitext(input_path)[0] + ".audit.json"
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(f"Audit report: {report_path} ({len(findings)} findings)")

    if args.apply:
        _backup_file(input_path)
        changed = apply_findings(
            blocks, findings, "medium" if args.include_medium else "high"
        )
        if changed:
            write_srt(blocks, input_path)
            print(f"Applied {changed} high-confidence fixes to {input_path}")
        else:
            print("No high-confidence fixes to apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
