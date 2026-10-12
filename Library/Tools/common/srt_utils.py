
import os
import re
import glob

def time_to_seconds(timestr):
    parts = timestr.replace(',', '.').split(':')
    return float(parts[0])*3600 + float(parts[1])*60 + float(parts[2])

def parse_srt(content_or_path):
    """
    Parses SRT content (string) or file path into a list of blocks.
    Each block is a dictionary: {'index': str, 'time': str, 'lines': list}
    """
    if os.path.isfile(content_or_path):
        with open(content_or_path, 'r', encoding='utf-8') as f:
            content = f.read().lstrip('\ufeff')
    else:
        content = content_or_path

    content = content.replace('\r\n', '\n')
    blocks = re.split(r'\n\n+', content.strip())
    parsed = []

    idx_counter = 1

    for block in blocks:
        lines = block.split('\n')
        lines = [l.strip() for l in lines if l.strip()]
        if len(lines) >= 2:
            idx = None
            time = None
            text = []

            # Robust parsing for malformed blocks
            if lines[0].isdigit() and '-->' in lines[1]:
                idx = lines[0]
                time = lines[1]
                text = lines[2:]
            elif '-->' in lines[0]:
                idx = str(idx_counter)
                time = lines[0]
                text = lines[1:]
            else:
                found_time = False
                for i, l in enumerate(lines):
                    if '-->' in l:
                        time = l
                        text = lines[i+1:]
                        idx = str(idx_counter)
                        found_time = True
                        break
                if not found_time:
                    continue

            if '-->' in time:
                # Handle cases where text might be merged into the timestamp line
                time_parts = time.split('-->')
                start_str = time_parts[0].strip()
                rem = time_parts[1].strip()

                # Check if there is text joined to the end timestamp
                # Format: 00:00:10,000Text...
                m = re.match(r"(\d{1,2}:\d{2}:\d{2}[\.,]\d{3})(.*)", rem)
                if m:
                    end_str = m.group(1)
                    extra_text = m.group(2).strip()
                    if extra_text:
                        text = [extra_text] + text
                else:
                    end_str = rem

                start_sec = time_to_seconds(start_str)
                end_sec = time_to_seconds(end_str)
            else:
                start_sec = 0.0
                end_sec = 0.0

            parsed.append({
                'index': idx,
                'time': time,
                'start': start_sec,
                'end': end_sec,
                'lines': text
            })
            idx_counter += 1
    return parsed

def write_srt(subs, path):
    """Writes a list of subtitle blocks to a file."""
    with open(path, 'w', encoding='utf-8') as f:
        for i, sub in enumerate(subs):
            f.write(f"{i+1}\n")
            f.write(f"{sub['time']}\n")
            for line in sub['lines']:
                f.write(f"{line}\n")
            f.write("\n")
    print(f"Wrote {len(subs)} entries to {path}")

def is_chinese(text):
    """Checks if text contains Chinese characters."""
    return bool(re.search(r'[\u4e00-\u9fff]', text))

def extract_tracks(input_path, output_dir=None):
    """
    Splits a bilingual SRT into separate .en.srt and .cn.srt files.
    Returns paths to (en_path, cn_path).
    """
    if not output_dir:
        output_dir = os.path.dirname(input_path)

    base = os.path.splitext(os.path.basename(input_path))[0]
    # Clean extensions if present
    for ext in ['.bi', '.cn', '.en']:
         if base.lower().endswith(ext):
             base = base[:-len(ext)]

    path_en = os.path.join(output_dir, f"{base}.en.srt")
    path_cn = os.path.join(output_dir, f"{base}.zh.srt")

    subs = parse_srt(input_path)
    subs_en = []
    subs_cn = []

    for sub in subs:
        lines_en = [l for l in sub['lines'] if not is_chinese(l) and '[UNTRANSLATED]' not in l]
        lines_cn = [l for l in sub['lines'] if is_chinese(l) or '[UNTRANSLATED]' in l]

        # If a block has NO English but has Chinese (or vice versa), we might want to keep the block structure?
        # Standard practice: keep the block but with empty text? Or skip?
        # Let's keep the block structure to maintain sync easily.

        sub_en = sub.copy()
        sub_en['lines'] = lines_en
        subs_en.append(sub_en)

        sub_cn = sub.copy()
        sub_cn['lines'] = lines_cn
        subs_cn.append(sub_cn)

    write_srt(subs_en, path_en)
    write_srt(subs_cn, path_cn)
    return path_en, path_cn

def split_to_chunks(input_path, chunk_size=30, output_dir=None):
    """Splits an SRT file into chunks of `chunk_size` blocks."""
    if not output_dir:
        output_dir = os.path.join(os.path.dirname(input_path), "chunks")

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    subs = parse_srt(input_path)
    total_chunks = (len(subs) + chunk_size - 1) // chunk_size

    created_files = []

    for i in range(total_chunks):
        chunk = subs[i*chunk_size : (i+1)*chunk_size]
        filename = os.path.join(output_dir, f"chunk_{i:03d}.srt")
        write_srt(chunk, filename)
        created_files.append(filename)

    return created_files

def merge_chunks(chunks_dir, output_path, pattern="chunk_*.srt"):
    """Merges SRT chunks matching `pattern` in `chunks_dir` into `output_path`."""
    files = sorted(glob.glob(os.path.join(chunks_dir, pattern)))
    if not files:
        print(f"No files found matching {pattern} in {chunks_dir}")
        return False

    all_subs = []
    # We must trust that chunks are ordered correctly by filename
    for fp in files:
        subs = parse_srt(fp)
        all_subs.extend(subs)

    # Renumbering
    for i, sub in enumerate(all_subs):
        sub['index'] = str(i+1)

    write_srt(all_subs, output_path)
    return True

def merge_tracks(path_master, path_secondary, output_path):
    """
    Merges two SRT files using time-based overlap detection (Smart Merge).
    path_master: The translated track (usually CN).
    path_secondary: The source track (usually EN/Original).

    CRITICAL CHANGE: This function now drives from the secondary (source) track to ensure no lines are dropped.
    If a translated block is missing, the original line is kept with an empty translation field.
    """
    try:
        from tqdm import tqdm
    except ImportError:
        def tqdm(iterable, desc=None, unit=None):
            print(f"Processing... {desc}")
            return iterable

    print(f"Loading Master (Translation): {path_master}")
    subs_master = parse_srt(path_master)
    print(f"Loading Secondary (Source): {path_secondary}")
    subs_secondary = parse_srt(path_secondary)

    print("\\n--- Synchronization Safety Check ---")
    len_m = len(subs_master)
    len_s = len(subs_secondary)
    print(f"Block Count: Master={len_m}, Secondary={len_s}")

    if abs(len_m - len_s) > 0:
        print(f"⚠️ WARNING: Block count mismatch ({abs(len_m - len_s)} blocks).")
        print("Driving merge from Secondary (Source) to prevent data loss.")

    if len_m > 0 and len_s > 0:
        duration_m = subs_master[-1]['end'] - subs_master[0]['start'] if 'end' in subs_master[-1] else 0
        duration_s = subs_secondary[-1]['end'] - subs_secondary[0]['start'] if 'end' in subs_secondary[-1] else 0
        print(f"Duration: Master={duration_m:.2f}s, Secondary={duration_s:.2f}s")

        if abs(duration_m - duration_s) > 5.0:
            print(f"⚠️ WARNING: Duration mismatch of {abs(duration_m - duration_s):.2f}s.")

    print("------------------------------------\\n")

    merged = []

    if len_m == len_s and len_m > 0:
        # FAST PATH: When both tracks have the same block count (the normal
        # case for smart_translate.py output), pair blocks by index. This is
        # simpler and more reliable than time-based overlap detection, which
        # can mismatch blocks when adjacent timestamps are very close.
        print("Using INDEX-BASED merge (block counts match).")
        for i in range(len_s):
            s_block = subs_secondary[i]
            m_block = subs_master[i]

            m_text = " ".join([l.strip() for l in m_block.get('lines', [])]).replace('\n', ' ')
            m_text = re.sub(r'\s+', ' ', m_text).strip()
            m_text = re.sub(r'\s*[\(\（][^\)\）]*[\)\）]', '', m_text).strip()

            new_lines = []
            if m_text:
                new_lines.append(m_text)
            else:
                new_lines.append("[UNTRANSLATED]")
            new_lines.extend(s_block['lines'])

            merged.append({
                'index': str(i+1),
                'time': s_block['time'],
                'lines': new_lines
            })
        write_srt(merged, output_path)
        return

    # FALLBACK: Time-based overlap detection for mismatched block counts
    # (e.g. chunk-based translation via subtranslator.py where blocks may
    # have different counts).
    print("Using TIME-BASED merge (block counts differ).")

    # We iterate the SECONDARY (Source) track to ensure we don't lose any original lines
    # We look for overlapping MASTER (Translated) blocks to ensure we don't lose any original lines
    # We look for overlapping MASTER (Translated) blocks

    start_search_idx = 0
    total_master = len(subs_master)

    for i, s_block in enumerate(tqdm(subs_secondary, desc="Smart Merging Blocks", unit="block")):
        s_start = s_block['start']
        s_end = s_block['end']

        # Move search window forward on Master track
        while start_search_idx < total_master and subs_master[start_search_idx]['end'] < s_start:
            start_search_idx += 1

        matched_text = [] # Usually Master text

        # Iterate from safe start index
        for j in range(start_search_idx, total_master):
            m_block = subs_master[j]

            # Stop if Master block starts after Secondary block ends
            if m_block['start'] > s_end:
                break

            # Overlap calculation
            overlap_start = max(s_start, m_block['start'])
            overlap_end = min(s_end, m_block['end'])
            overlap_duration = max(0, overlap_end - overlap_start)

            m_duration = m_block['end'] - m_block['start']

            # Match rules: significant overlap
            if m_duration > 0 and (overlap_duration / m_duration > 0.3):
                matched_text.extend(m_block['lines'])
            elif overlap_duration > 0.5:
                 matched_text.extend(m_block['lines'])

        # Deduplicate -> " ".join -> clean
        joined_trans = " ".join([l.strip() for l in matched_text]).replace('\n', ' ')
        joined_trans = re.sub(r'\s+', ' ', joined_trans).strip()

        # CLEANING: Remove "Chinese (English)" pattern
        # Remove (English) or （English）
        joined_trans = re.sub(r'\s*[\(\（][^\)\）]*[\)\）]', '', joined_trans).strip()

        # Construct merged block
        # Translation on top? No, usually Translation is Master, Source is Secondary.
        # Check standard: usually we want CN on Top, EN on bottom?
        # Wait, the previous logic did: Master lines + Secondary lines.
        # If Master was Top, Secondary was Bottom.

        # Here we drive from Secondary. So we have Secondary lines.
        # We found matched Master lines (Translation).

        # Let's put Translation (Master) FIRST, then Source (Secondary).
        new_lines = []
        if joined_trans:
            new_lines.append(joined_trans)
        else:
            # Mark missing translation explictly for gap-filling tools
            new_lines.append("[UNTRANSLATED]")

        new_lines.extend(s_block['lines'])

        merged.append({
            'index': str(i+1),
            'time': s_block['time'], # Use Source timing which is reliable
            'lines': new_lines
        })

    write_srt(merged, output_path)

def get_srt_duration(path):
    """Returns the total duration of an SRT file in seconds."""
    subs = parse_srt(path)
    if not subs: return 0
    return subs[-1]['end'] - subs[0]['start']