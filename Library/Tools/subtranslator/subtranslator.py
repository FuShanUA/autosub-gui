
import os
import sys
import argparse
import shutil
import glob
import re
import io

# Ensure we can import from lib and common
current_dir = os.path.dirname(os.path.abspath(__file__))
lib_dir = os.path.join(current_dir, 'lib')
common_dir = os.path.join(os.path.dirname(current_dir), 'common')

if lib_dir not in sys.path:
    sys.path.insert(0, lib_dir)
if common_dir not in sys.path:
    sys.path.insert(0, common_dir)

import srt_utils

# Force UTF-8 for stdout/stderr to handle emojis in logs on Windows (essential for pythonw)
if sys.platform == "win32":
    try:
        if isinstance(sys.stdout, io.TextIOWrapper):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if isinstance(sys.stderr, io.TextIOWrapper):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, io.UnsupportedOperation):
        pass

# Configuration
if getattr(sys, 'frozen', False):
    TOOLS_DIR = os.path.join(getattr(sys, '_MEIPASS', ''), "Library", "Tools")
else:
    TOOLS_DIR = os.environ.get("AUTOSUB_TOOLS_DIR") or os.path.dirname(os.path.dirname(current_dir))
sys.path.append(os.path.join(TOOLS_DIR, "common")) # For gemini_utils and srt_utils

HAS_GENAI = False
try:
    import gemini_utils
    HAS_GENAI = True
except ImportError:
    pass

def get_default_output_dir(input_file):
    return os.path.dirname(os.path.abspath(input_file))

def process_split(args):
    input_path = os.path.abspath(args.input_file)
    output_dir = args.output_dir if args.output_dir else get_default_output_dir(input_path)
    chunks_dir = os.path.join(output_dir, "chunks")

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    if not os.path.exists(chunks_dir):
        os.makedirs(chunks_dir)

    print(f"Processing split for: {input_path}")
    print(f"Output Directory: {output_dir}")

    source_en_path = os.path.join(output_dir, "source.en.srt")
    subs = srt_utils.parse_srt(input_path)
    has_chinese = any(srt_utils.is_chinese(line) for sub in subs for line in sub['lines'])

    if has_chinese:
        print("Detected Chinese characters. Assuming Bilingual/CN input.")
        print(f"Extracting English track to {source_en_path}...")

        subs_en = []
        for sub in subs:
            lines_en = [l for l in sub['lines'] if not srt_utils.is_chinese(l)]
            new_sub = sub.copy()
            new_sub['lines'] = lines_en
            subs_en.append(new_sub)
        srt_utils.write_srt(subs_en, source_en_path)
    else:
        print("Assuming Monolingual English input.")
        if input_path != source_en_path:
            shutil.copy(input_path, source_en_path)

    print(f"Splitting source into chunks (size={args.chunk_size})...")
    srt_utils.split_to_chunks(source_en_path, args.chunk_size, chunks_dir)
    print(f"Split complete. Chunks are in: {chunks_dir}")


def process_merge(args):
    if args.input_file:
        input_path = os.path.abspath(args.input_file)
        base_dir = os.path.dirname(input_path)
        filename = os.path.basename(input_path)
        base_name = os.path.splitext(filename)[0]

        if base_name.lower().endswith('.en'):
            base_name = base_name[:-3]
        if base_name.lower().endswith('.bi'):
            base_name = base_name[:-3]
        source_path = input_path
    else:
        print("Error: Input file (source) must be specified.")
        return

    output_dir = args.output_dir if args.output_dir else base_dir
    chunks_dir = os.path.join(output_dir, "chunks")

    if args.translated_file:
         combined_cn_path = os.path.abspath(args.translated_file)
         print(f"Using provided translated file: {combined_cn_path}")
    else:
         combined_cn_path = os.path.join(output_dir, f"{base_name}.zh.srt")

    if os.path.exists(combined_cn_path) and os.path.getsize(combined_cn_path) > 0:
        print(f"✅ Found existing translated file: {combined_cn_path}")
    else:
        cn_pattern = "chunk_*.cn.srt"
        chunks_exist = glob.glob(os.path.join(chunks_dir, cn_pattern))
        if not chunks_exist:
            srt_pattern = "chunk_*.srt"
            all_chunks = glob.glob(os.path.join(chunks_dir, srt_pattern))
            filtered = [f for f in all_chunks if not f.endswith('.en.srt')]
            if filtered:
                print("Found generic .srt chunks, using them as translation source...")
                cn_pattern = "chunk_*.srt"
            else:
                print("❌ No translated chunks found AND no pre-translated file exists.")
                return
        srt_utils.merge_chunks(chunks_dir, combined_cn_path, cn_pattern)

    print(f"Progress: 5.0% (Loading tracks...)")
    final_bi_path = os.path.join(output_dir, f"{base_name}.bi.srt")
    potential_source_en = os.path.join(output_dir, "source.en.srt")
    if os.path.exists(potential_source_en):
        source_path = potential_source_en

    print(f"Progress: 25.5% (Aligning timelines...)")
    print(f"Using SMART MERGE logic (Time-based alignment) -> {final_bi_path}")
    srt_utils.merge_tracks(combined_cn_path, source_path, final_bi_path)

    print(f"Progress: 60.0% (Filling gaps...)")
    print("\n--- Auto-Filling Gaps ---")
    fill_count = run_fill(final_bi_path)
    if fill_count > 0:
        print(f"✅ Filled {fill_count} gaps. Syncing back to Monolingual tracks...")
        try:
            _, temp_cn = srt_utils.extract_tracks(final_bi_path, output_dir)
            if os.path.exists(temp_cn) and temp_cn != combined_cn_path:
                shutil.move(temp_cn, combined_cn_path)
        except Exception as e:
            print(f"⚠️ Could not sync back to translated file: {e}")
    else:
        print("No gaps found requiring fill.")

    print(f"Progress: 100.0% (Merge complete)")
    print(f"Merge & Fix pipeline complete. Output: {final_bi_path}")

def validate_chunks(chunks_dir):
    print(f"Checking alignment in: {chunks_dir}")
    src_pattern = os.path.join(chunks_dir, "chunk_*.srt")
    src_files = sorted(glob.glob(src_pattern))
    src_files = [f for f in src_files if not f.endswith('.cn.srt') and not f.endswith('.en.srt')]

    issues = 0
    checked = 0
    for src_file in src_files:
        base = os.path.splitext(src_file)[0]
        cn_file = f"{base}.cn.srt"
        if not os.path.exists(cn_file): continue
        checked += 1
        try:
            subs_src = srt_utils.parse_srt(src_file)
            subs_cn = srt_utils.parse_srt(cn_file)
            if len(subs_src) != len(subs_cn):
                print(f"❌ Mismatch in {os.path.basename(src_file)}: Orig={len(subs_src)}, Trans={len(subs_cn)}")
                issues += 1
        except Exception as e:
            print(f"Error parsing {src_file}: {e}")

    if checked == 0: print("No translated chunks found to validate.")
    elif issues == 0: print(f"✅ All {checked} translated chunks match original block counts.")
    else: print(f"Found {issues} chunk mismatches.")

def process_validate(args):
    input_path = os.path.abspath(args.input_file)
    if os.path.isdir(input_path):
        chunks_dir = os.path.join(input_path, "chunks")
        if os.path.exists(chunks_dir): validate_chunks(chunks_dir)
        else: validate_chunks(input_path)
        return

    print(f"Validating {args.input_file}...")
    subs = srt_utils.parse_srt(input_path)
    issues = 0
    for sub in subs:
        text = " ".join(sub['lines']).strip()
        if not text:
             print(f"Warning: Block {sub['index']} is empty.")
             issues += 1
             continue
        lower_text = text.lower()
        if re.search(r'\(\s*line\s*\d+\s*merge\s*\)', lower_text) or \
           re.search(r'\(\s*english\s*\)', lower_text) or \
           re.match(r'^\(.*\)$', text):
             print(f"Warning: Block {sub['index']} contains potential placeholder: '{text}'")
             issues += 1
    if issues == 0: print("Basic validation passed.")
    else: print(f"Found {issues} issues.")

def run_fill(input_path):
    input_path = os.path.abspath(input_path)
    subs = srt_utils.parse_srt(input_path)
    gaps = []

    for i, sub in enumerate(subs):
        lines = sub['lines']
        if not lines: continue

        has_gap = False
        gap_idx = -1
        src_lines = []

        for idx, line in enumerate(lines):
            if "[UNTRANSLATED]" in line:
                has_gap = True
                gap_idx = idx
            elif not srt_utils.is_chinese(line):
                clean_line = line.strip()
                if clean_line: src_lines.append(clean_line)

        if has_gap and gap_idx != -1 and src_lines:
            src_text = " ".join(src_lines)
            prev_sub = subs[i-1] if i > 0 else None
            next_sub = subs[i+1] if i < len(subs)-1 else None

            def get_ctx(s_block):
                if not s_block: return ""
                cn = " ".join([l for l in s_block['lines'] if srt_utils.is_chinese(l)])
                en = " ".join([l for l in s_block['lines'] if not srt_utils.is_chinese(l) and '[UNTRANSLATED]' not in l])
                return f"[{en}] -> [{cn}]"

            gaps.append({
                'id': len(gaps)+1,
                'sub_index': i,
                'gap_inline_index': gap_idx,
                'src_text': src_text,
                'prev': get_ctx(prev_sub),
                'next': get_ctx(next_sub)
            })

    if not gaps: return 0
    print(f"⚡ Found {len(gaps)} gaps. Executing batched fill...")

    BATCH_SIZE = 5
    batches = [gaps[i:i + BATCH_SIZE] for i in range(0, len(gaps), BATCH_SIZE)]
    tasks = []
    for b_idx, batch in enumerate(batches):
        items_str = ""
        for item in batch:
            items_str += f"\nItem {item['id']}:\nContext (Prev): {item['prev']}\nTARGET: \"{item['src_text']}\"\nContext (Next): {item['next']}\n"
        prompt = f"Translate English to Simplified Chinese.\n{items_str}\nOutput Format:\nItem ID: [Translation]\n"
        tasks.append({'batch_id': b_idx, 'items': batch, 'prompt': prompt})

    try:
        if not HAS_GENAI: return -1
        client = gemini_utils.GeminiClient()
        results = client.generate_batch(tasks, os.environ.get("GEMINI_MODEL", "gemini-3-flash"))
        modified = False
        for res in results:
            batch_out = res.get('result')
            if not batch_out: continue
            for line in batch_out.strip().split('\n'):
                m = re.match(r'Item\s+(\d+)[:：]\s*(.*)', line, re.IGNORECASE)
                if m:
                    item_id = int(m.group(1))
                    trans_text = m.group(2).strip()
                    mapping = {item['id']: item for item in res.get('items', [])}
                    if item_id in mapping and trans_text:
                        item = mapping[item_id]
                        subs[item['sub_index']]['lines'][item['gap_inline_index']] = trans_text
                        modified = True
        if modified:
            srt_utils.write_srt(subs, input_path)
            return len([i for i in results if i.get('result')]) # Approximate
    except Exception as e:
        print(f"❌ Fill failed: {e}")
    return 0

def process_fill(args):
    run_fill(args.input_file)

def run_comparison(src_path, trans_path):
    subs_src = srt_utils.parse_srt(src_path)
    subs_trans = srt_utils.parse_srt(trans_path)
    if len(subs_src) != len(subs_trans):
        print(f"⚠️ Mismatch: Source={len(subs_src)}, Trans={len(subs_trans)}")
    else:
        print("✅ Block counts match.")

def process_compare(args):
    run_comparison(args.source_file, args.translated_file)

def main():
    parser = argparse.ArgumentParser(description="Subtranslator Tool")
    subparsers = parser.add_subparsers(dest='step', required=True)

    p_split = subparsers.add_parser('split'); p_split.add_argument('input_file'); p_split.add_argument('--output-dir'); p_split.add_argument('--chunk-size', type=int, default=30)
    p_merge = subparsers.add_parser('merge'); p_merge.add_argument('input_file'); p_merge.add_argument('--translated-file'); p_merge.add_argument('--output-dir'); p_merge.add_argument('--english-top', action='store_true')
    p_val = subparsers.add_parser('validate'); p_val.add_argument('input_file')
    p_fill = subparsers.add_parser('fill'); p_fill.add_argument('input_file')
    p_comp = subparsers.add_parser('compare'); p_comp.add_argument('source_file'); p_comp.add_argument('translated_file')

    args = parser.parse_args()
    if args.step == 'split': process_split(args)
    elif args.step == 'merge': process_merge(args)
    elif args.step == 'validate': process_validate(args)
    elif args.step == 'fill': process_fill(args)
    elif args.step == 'compare': process_compare(args)

if __name__ == "__main__":
    main()
