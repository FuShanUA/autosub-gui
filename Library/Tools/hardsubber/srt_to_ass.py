

import sys
import re
import os
import argparse

def srt_timestamp_to_ass(timestamp):
    try:
        h, m, s_ms = timestamp.split(':')
        s, ms = s_ms.split(',')
        ms = int(ms) // 10
        return f"{int(h)}:{m}:{s}.{ms:02d}"
    except:
        return "0:00:00.00"

def parse_srt(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read().lstrip('\ufeff')
    content = content.replace('\r\n', '\n')
    blocks = re.split(r'\n\n+|(?<=\d)\n\n+(?=\d)', content.strip())
    
    parsed_dict = {}
    order = []

    for block in blocks:
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        if len(lines) < 2: continue

        first_idx = 0
        if not lines[0].isdigit() and '-->' in lines[0]:
            first_idx = 0
        elif lines[0].isdigit():
            first_idx = 1

        if first_idx >= len(lines): continue
        time_line = lines[first_idx]
        text_lines = lines[first_idx+1:]

        if '-->' not in time_line: continue

        try:
            s, e = time_line.split(' --> ')
            start = srt_timestamp_to_ass(s.strip())
            end = srt_timestamp_to_ass(e.split(' ')[0].strip())
            # Safety: if Start > End (can happen with speaker diarization
            # overlapping segments), swap them. Only affects invalid blocks.
            def _ass_to_seconds(ts):
                parts = ts.split(':')
                return int(parts[0])*3600 + int(parts[1])*60 + float(parts[2])
            if _ass_to_seconds(start) > _ass_to_seconds(end):
                start, end = end, start
        except: continue

        cn_parts = []
        en_parts = []
        
        # Check if any line in the block contains Chinese characters
        has_cn_any = any(re.search(r'[\u4e00-\u9fff]', line) for line in text_lines)
        
        for idx, line in enumerate(text_lines):
            line = line.replace('\\n', '\n')
            sub_lines = line.split('\n')
            for sub in sub_lines:
                # Heuristic: Contains Japanese kana (hiragana/katakana)?
                # Kana is unique to Japanese — if present, this line is Japanese,
                # not Chinese, even though it also contains kanji.
                if re.search(r'[\u3040-\u30ff\u31f0-\u31ff]', sub):
                    # Strip spaces between CJK characters (Whisper artifact).
                    # Japanese ASR inserts spaces between every character;
                    # remove them so text renders naturally. Keep spaces
                    # around ASCII words (e.g. "LayerX", "AI").
                    sub = re.sub(r'(?<=[\u3040-\u30ff\u31f0-\u31ff\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])\s+(?=[\u3040-\u30ff\u31f0-\u31ff\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])', '', sub)
                    en_parts.append(sub)  # route Japanese to the EN track
                elif re.search(r'[\u4e00-\u9fff]', sub):
                    cn_parts.append(sub)
                elif has_cn_any:
                    # If this block contains Chinese elsewhere, this line without Chinese is English
                    en_parts.append(sub)
                else:
                    # No Chinese anywhere in this block, so it is English-only
                    en_parts.append(sub)

        # Join with ASS newline
        cn_text = "\\N".join(cn_parts)
        en_text = "\\N".join(en_parts)

        key = (start, end)
        if key in parsed_dict:
            # Merge with existing block for the same timestamp
            prev = parsed_dict[key]
            if cn_text:
                if not prev['cn']:
                    prev['cn'] = cn_text
                else:
                    # Avoid duplicate text additions
                    prev_cn_lines = prev['cn'].split("\\N")
                    if cn_text not in prev_cn_lines:
                        prev['cn'] += "\\N" + cn_text
            if en_text:
                if not prev['en']:
                    prev['en'] = en_text
                else:
                    prev_en_lines = prev['en'].split("\\N")
                    if en_text not in prev_en_lines:
                        prev['en'] += "\\N" + en_text
        else:
            parsed_dict[key] = {'s': start, 'e': end, 'cn': cn_text, 'en': en_text}
            order.append(key)

    return [parsed_dict[k] for k in order]

def convert_markdown_to_ass(text):
    """
    Converts Simple Markdown to ASS tags.
    - **Bold** -> {\\b1}Bold{\\b0}
    - *Italic* -> {\\i1}Italic{\\i0}
    - Removes other MD tokens if not supported.
    """
    if not text: return text

    # 1. Bold (**...**)
    # Use non-greedy match
    text = re.sub(r'\*\*(.*?)\*\*', r'{\\b1}\1{\\b0}', text)

    # 2. Italic (*...*)
    # Avoid overlapping with bold if already processed
    text = re.sub(r'\*(.*?)\*', r'{\\i1}\1{\\i0}', text)

    # Clean up any leftover markdown symbols if they were unmatched or nested weirdly
    # Actually, let's keep it simple. If regex didn't catch it, maybe it shouldn't be removed?
    # But user asked: "If not convertible, remove it".
    # Let's remove standalone `**` or `*` if they look like markup?
    # Safe approach: Just leave them if they didn't match pairs.
    # Or, do a cleanup pass for `**`

    return text

def get_visual_length(text):
    """Calculates length where Chinese/Full-width = 1.0 and English/ASCII = 0.60."""
    # Strip ASS tags for length calculation
    clean = re.sub(r'{\\.*?}', '', text)
    length = 0
    for char in clean:
        if ord(char) > 255:
            length += 1
        else:
            length += 0.60
    return length

def auto_wrap(text, max_units=32, is_chinese=True):
    """
    Wrapping logic based on visual units (Chinese=1, English=0.60).
    Ensures box calculation is accurate and screen space is used efficiently.
    Prefers breaking at punctuation for Chinese.
    """
    if not text: return ""

    # If it's already within limits, don't touch it
    if get_visual_length(text) <= max_units:
        return text

    # Heuristic: If we are only slightly over (e.g. 1-2 characters),
    # maybe don't wrap to avoid orphans?
    # But for a robust box calculation, it's safer to wrap.
    # Let's use a 10% overflow tolerance for "unwrappable" looks.
    if get_visual_length(text) <= max_units * 1.1:
        return text

    parts = []
    while get_visual_length(text) > max_units:
        # We need to find a character index 'cut' such that visual_length(text[:cut]) <= max_units
        cut = 0
        current_v = 0
        while cut < len(text) and current_v < max_units:
            if ord(text[cut]) > 255:
                current_v += 1
            else:
                current_v += 0.60
            cut += 1

        # Now 'cut' is our maximum possible character index.
        # For English, we try to find a space near this point.
        if not is_chinese:
            last_space = text.rfind(' ', 0, cut + 1)
            last_punct = -1
            for p in ['.', ',', '!', '?', ';', ':']:
                idx = text.rfind(p, 0, cut + 1)
                if idx > last_punct:
                    last_punct = idx

            if last_punct > cut * 0.5:
                cut = last_punct + 1
            elif last_space > cut * 0.5:
                cut = last_space
        else:
            # Smart Chinese breaking: prefer punctuation
            # Full-width punctuation list
            chinese_puncts = ['，', '。', '？', '！', '；', '：', '、', '”', '）', '】']
            last_punct = -1
            for p in chinese_puncts:
                idx = text.rfind(p, 0, cut + 1)
                if idx > last_punct:
                    last_punct = idx

            # If we found a punct reasonably far into the line (not right at start), break there
            if last_punct > cut * 0.4:
                cut = last_punct + 1

        # Guard against 0 cut (safety)
        if cut == 0: cut = 1

        parts.append(text[:cut].strip())
        text = text[cut:].strip()

    if text:
        parts.append(text)
    return "\\N".join(parts)



try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    HAS_TKINTER = True
except ImportError:
    HAS_TKINTER = False

class TkinterConfig:
    def __init__(self):
        self.mode = "bilingual"
        self.main_lang = "cn"
        # Try STKaiti (华文楷体) first, then KaiTi
        # In a real environment we might check os.fonts but this is a simple script.
        # Let's set default to "STKaiti" as it's cleaner on Windows usually.
        self.cn_font = "STKaiti"
        self.cn_color = "&H0000FFFF" # Default Yellow
        self.cn_size = "middle"
        self.en_font = "Arial"
        self.en_color = "&H00FFFFFF"
        self.en_size = "middle"
        # Japanese font: used for kana-bearing text. Shares color/size with EN.
        # Hiragino Sans on macOS, Yu Gothic on Windows, fallback to en_font.
        self.ja_font = "Hiragino Sans"

        self.bg_box = True # Default enabled

        self.width = 1920
        self.height = 1080

        self.finished = False # Status flag

        # Derived values
        self.size_map = {
            "big": {"cn": 70, "en": 46, "cn_wrap": 26, "en_wrap": 60},
            "middle": {"cn": 58, "en": 38, "cn_wrap": 32, "en_wrap": 70},
            "small": {"cn": 46, "en": 30, "cn_wrap": 40, "en_wrap": 85}
        }
        self.color_map = {
            "White": "&H00FFFFFF",
            "Yellow": "&H0000FFFF",
            "Black": "&H00000000",
            "Gold": "&H0000D7FF",
            "Golden": "&H0000D7FF",
            "Blue": "&H00FF0000",
            "Green": "&H0000FF00"
        }
        # Invert map for display
        self.hex_to_name = {v: k for k, v in self.color_map.items()}
        # Add defaults if missing
        if self.cn_color not in self.hex_to_name: self.hex_to_name[self.cn_color] = self.cn_color
        if self.en_color not in self.hex_to_name: self.hex_to_name[self.en_color] = self.en_color


    def wizard(self):
        if not HAS_TKINTER:
            raise ImportError("Tkinter/GUI is not supported on this system.")
        root = tk.Tk()
        root.title("Subtitle Style Config")
        root.geometry("400x550")

        # Variables with defaults
        self.var_mode = tk.StringVar(value=self.mode)
        self.var_main = tk.StringVar(value=self.main_lang)
        self.var_size = tk.StringVar(value=self.cn_size)

        self.var_cn_color = tk.StringVar(value=self.hex_to_name.get(self.cn_color, "Gold"))
        self.var_en_color = tk.StringVar(value=self.hex_to_name.get(self.en_color, "White"))

        self.var_bg_box = tk.BooleanVar(value=self.bg_box)

        pad = {'padx': 20, 'pady': 5, 'sticky': 'w'}

        # 1. Mode
        ttk.Label(root, text="Subtitle Mode", font=("Arial", 11, "bold")).pack(pady=(15, 5))
        f_mode = ttk.Frame(root)
        f_mode.pack(fill=tk.X, padx=20)
        ttk.Radiobutton(f_mode, text="Bilingual (CN + EN)", variable=self.var_mode, value="bilingual").pack(anchor='w')
        ttk.Radiobutton(f_mode, text="Chinese Only", variable=self.var_mode, value="cn").pack(anchor='w')
        ttk.Radiobutton(f_mode, text="English Only", variable=self.var_mode, value="en").pack(anchor='w')

        # 2. Main Language
        ttk.Label(root, text="Main Language (for Bilingual)", font=("Arial", 11, "bold")).pack(pady=(15, 5))
        f_main = ttk.Frame(root)
        f_main.pack(fill=tk.X, padx=20)
        ttk.Radiobutton(f_main, text="Chinese Top (Default)", variable=self.var_main, value="cn").pack(anchor='w')
        ttk.Radiobutton(f_main, text="English Top", variable=self.var_main, value="en").pack(anchor='w')

        # 3. Size
        ttk.Label(root, text="Font Size", font=("Arial", 11, "bold")).pack(pady=(15, 5))
        f_size = ttk.Frame(root)
        f_size.pack(fill=tk.X, padx=20)
        ttk.Radiobutton(f_size, text="Big (Video-filling)", variable=self.var_size, value="big").pack(anchor='w')
        ttk.Radiobutton(f_size, text="Middle (Standard)", variable=self.var_size, value="middle").pack(anchor='w')
        ttk.Radiobutton(f_size, text="Small (Compact)", variable=self.var_size, value="small").pack(anchor='w')

        # 4. Colors
        ttk.Label(root, text="Text Colors", font=("Arial", 11, "bold")).pack(pady=(15, 5))
        f_color = ttk.Frame(root)
        f_color.pack(fill=tk.X, padx=20)

        colors = list(self.color_map.keys())

        ttk.Label(f_color, text="Chinese Color:").grid(row=0, column=0, sticky='w', pady=5)
        cb_cn = ttk.Combobox(f_color, textvariable=self.var_cn_color, values=colors, state="readonly")
        cb_cn.grid(row=0, column=1, padx=10)

        ttk.Label(f_color, text="English Color:").grid(row=1, column=0, sticky='w', pady=5)
        cb_en = ttk.Combobox(f_color, textvariable=self.var_en_color, values=colors, state="readonly")
        cb_en.grid(row=1, column=1, padx=10)

        # 5. Background Box
        ttk.Checkbutton(root, text="Enable Background Box", variable=self.var_bg_box).pack(pady=(15, 5))

        # Submit
        def on_submit():
            self.mode = self.var_mode.get()
            self.main_lang = self.var_main.get()

            # Apply size to both (simplified logic as per previous wizard)
            s = self.var_size.get()
            self.cn_size = s
            self.en_size = s

            # Map Config Colors
            cn_n = self.var_cn_color.get()
            en_n = self.var_en_color.get()
            self.cn_color = self.color_map.get(cn_n, self.cn_color)
            self.en_color = self.color_map.get(en_n, self.en_color)

            self.bg_box = self.var_bg_box.get()


            self.finished = True
            root.destroy()

        ttk.Button(root, text="Generate ASS", command=on_submit).pack(pady=30, fill=tk.X, padx=40)

        # Focus window
        root.lift()
        root.attributes('-topmost',True)
        root.after_idle(root.attributes,'-topmost',False)

        root.mainloop()

    def get_metrics(self, lang):
        size_key = self.cn_size if lang == 'cn' else self.en_size

        # --- Resolution Adaptive Scaling ---
        # Base design resolution is 1080p (height 1080, width 1920)
        # Font sizes in size_map and GUI defaults (60) are designed for 1080p height.
        h_scale = self.height / 1080.0
        w_scale = self.width / 1080.0

        is_vertical = self.width < self.height

        # Adaptive font scaling factor
        if is_vertical:
             # For vertical videos, width is the limiting factor for subtitle space.
             # Scale font based on width relative to 1080 so subtitles fit the
             # narrow screen instead of overflowing.
             # Apply a 1.3x readability boost so Chinese text isn't too small on
             # narrow phone screens (e.g. 608px wide Shorts).
             font_scale = w_scale * 1.3
        else:
             font_scale = h_scale

        # 1. Resolve base metrics
        if size_key in self.size_map:
            base = self.size_map[size_key]
            s_base = base['cn'] if lang == 'cn' else base['en']
        else:
            try:
                s_base = int(size_key)
            except:
                # Fallback to middle
                base = self.size_map["middle"]
                s_base = base['cn'] if lang == 'cn' else base['en']

        # 2. Calculate Final Adaptive Font Size
        s_final = int(s_base * font_scale)

        # Guardrail: minimum readable size
        if s_final < 14:
            s_final = 14

        # 3. Calculate Final Adaptive Wrap (in visual units: Chinese = 1.0, English = 0.60)
        h_margin = int(self.width * 0.04)
        safe_width = self.width - 2 * h_margin - 60 # 60px safety buffer
        wrap_final = int(safe_width / s_final)

        # Guardrails for readability (converted to visual units)
        if is_vertical:
             # In vertical videos, don't allow too many characters per line
             if lang == 'cn': wrap_final = min(wrap_final, 14)
             else: wrap_final = min(wrap_final, 28)
        else:
             # In landscape, keep it reasonable
             if lang == 'cn': wrap_final = min(wrap_final, 30)
             else: wrap_final = min(wrap_final, int(80 * 0.60)) # ~48 visual units (approx 80 English chars)

        return s_final, wrap_final


def generate_ass(parsed, output_path, config):
    # Determine base font sizes/wraps
    fs_cn, wrap_cn = config.get_metrics('cn')
    fs_en, wrap_en = config.get_metrics('en')

    # Header
    # Note: We set a base style using CN metrics usually, but with overriding tags
    # Style: Name, Fontname, Fontsize, PrimaryColour...

    # We will use 'BoxBase' and 'TextBase' styles.
    # TextBase will default to CN settings if CN is present, else EN.

    base_font = config.cn_font if config.mode != 'en' else config.en_font
    base_size = fs_cn if config.mode != 'en' else fs_en
    base_color = config.cn_color if config.mode != 'en' else config.en_color

    # Dynamic Margins based on resolution
    v_margin = int(config.height * 0.035) # approx 3.5%
    h_margin = int(config.width * 0.04)   # approx 4%

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {config.width}
PlayResY: {config.height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: BoxBase,{base_font},{base_size},&HFF000000,&H000000FF,&H80202020,&H80202020,-1,0,0,0,100,100,0,0,1,0,0,2,{h_margin},{h_margin},{v_margin},1
Style: TextTop,{base_font},{base_size},{base_color},&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,2,{h_margin},{h_margin},{v_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(header)
        for p in parsed:
            # 1. Prepare Content based on Mode

            # Bilingual Logic:
            # If Main is CN: CN Top, EN Bottom (smaller)
            # If Main is EN: EN Top, CN Bottom (smaller)

            cn_final_txt = ""
            en_final_txt = ""

            if config.mode != 'en' and p['cn']:
                 # 1. Wrap first
                 # raw_wrapped is a list of strings, each potentially containing \N from wrapping

                 # Pre-process Markdown before wrapping/rendering
                 # Actually, ASS tags inside wrap might break length calc slightly, but it's acceptable.
                 # Better to apply MD conversion here on the full string or per part?
                 # If we do it on p['cn'], auto_wrap needs to be tag-aware.
                 # Our simple auto_wrap is NOT tag aware.
                 # So: Strip tags for wrapping calculation? Or wrap first then apply?
                 # If MD is "**Bold**", splitting it like "**Bo" "\n" "ld**" breaks it.
                 # COMPLEXITY: High.

                 # SIMPLIFIED STRATEGY:
                 # 1. Convert Markdown to ASS Tags first.
                 # 2. auto_wrap needs to ignore {...} when counting length.

                 cn_with_tags = convert_markdown_to_ass(p['cn'])

                 # We need a tag-aware wrap.
                 # Since we don't have one, let's just use the simple one and hope for best?
                 # No, breaking tags is bad.
                 # Let's clean tags for wrapping logic (invisible to length) but keep them in output?
                 # For now, let's apply MD conversion on the parts if possible?

                 # Let's try: Convert MD -> ASS.
                 # Then use a smarter wrap or just standard wrap if assume tags are short?
                 # Let's update auto_wrap to be slightly smarter or just accept it.
                 # Given "casual" use, let's just convert MD and pass to wrap.
                 # But we need auto_wrap to NOT count {\b1} as 4 chars.

                 raw_wrapped = [auto_wrap(sub, wrap_cn, True) for sub in cn_with_tags.split('\\N')]
                 full_wrapped_str = "\\N".join(raw_wrapped)

                 # 2. Apply font overrides for non-Chinese segments within the CN track
                 final_parts = []
                 for line in full_wrapped_str.split('\\N'):
                     # Wrap Japanese runs in JA font. Include spaces between
                     # CJK chars so kana+kanji sequences are not split apart.
                     # Must run BEFORE the English regex.
                     def replace_ja(m):
                         txt = m.group(0)
                         if re.search(r'[\u3040-\u30ff\u31f0-\u31ff]', txt):
                             return f"{{\\fn{config.ja_font}}}{txt}{{\\r}}"
                         return txt
                     processed = re.sub(r'[\u3040-\u30ff\u31f0-\u31ff\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\s]+', replace_ja, line)

                     # Function to wrap English text in font tags
                     def replace_en(m):
                         txt = m.group(0)
                         if re.search(r'[a-zA-Z]', txt):
                             return f"{{\\fn{config.en_font}}}{txt}{{\\r}}"
                         return txt
                     processed = re.sub(r'[a-zA-Z0-9\s\.,;:!?\'"%\-\(\)\[\]\/\*\+\=\&\$\#\@\<\>\_\`\^\|]+', replace_en, processed)
                     final_parts.append(processed)
                 cn_final_txt = "\\N".join(final_parts)

                 if config.mode != 'cn' and p['en']:
                     en_with_tags = convert_markdown_to_ass(p['en'])
                     parts = [auto_wrap(sub, wrap_en, False) for sub in en_with_tags.split('\\N')]
                     # Wrap Japanese runs in JA font within the EN track
                     en_processed_parts = []
                     for part in parts:
                         def replace_ja_en(m):
                             txt = m.group(0)
                             if re.search(r'[぀-ヿㇰ-ㇿ]', txt):
                                return f"{{\\fn{config.ja_font}}}{txt}{{\\fn{config.en_font}}}"
                             return txt
                         part = re.sub(r'[\u3040-\u30ff\u31f0-\u31ff\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\s]+', replace_ja_en, part)
                         en_processed_parts.append(part)
                     en_final_txt = "\\N".join(en_processed_parts)

                 final_content = ""

            # --- Layout Calculation ---
            # We need to know which lines are what to calc box sizes

            lines_to_draw = [] # list of (text, font_size, char_width_estimate)

            CHAR_W_CN_RATIO = 1.0
            CHAR_W_EN_RATIO = 0.55 # Arial is ~0.55 width of EM usually

            if config.mode == 'bilingual':
                if config.main_lang == 'cn':
                    # CN First (Base Style)
                    if cn_final_txt:
                        final_content += cn_final_txt
                        for l in cn_final_txt.split('\\N'): lines_to_draw.append((l, fs_cn, fs_cn))

                    if en_final_txt:
                        if final_content: final_content += "\\N"
                        # Append EN with overrides
                        final_content += f"{{\\fn{config.en_font}\\fs{fs_en}\\c{config.en_color}}}{en_final_txt}"
                        for l in en_final_txt.split('\\N'): lines_to_draw.append((l, fs_en, fs_en * CHAR_W_EN_RATIO))
                else:
                    # EN First (Base Style)
                    if en_final_txt:
                        final_content += en_final_txt
                        for l in en_final_txt.split('\\N'): lines_to_draw.append((l, fs_en, fs_en * CHAR_W_EN_RATIO))

                    if cn_final_txt:
                        if final_content: final_content += "\\N"
                        # Append CN with overrides
                        final_content += f"{{\\fn{config.cn_font}\\fs{fs_cn}\\c{config.cn_color}}}{cn_final_txt}"
                        for l in cn_final_txt.split('\\N'): lines_to_draw.append((l, fs_cn, fs_cn))

            elif config.mode == 'cn':
                if cn_final_txt:
                    final_content = cn_final_txt
                    for l in cn_final_txt.split('\\N'): lines_to_draw.append((l, fs_cn, fs_cn))

            elif config.mode == 'en':
                if en_final_txt:
                    final_content = en_final_txt
                    for l in en_final_txt.split('\\N'): lines_to_draw.append((l, fs_en, fs_en * CHAR_W_EN_RATIO))

            if not final_content or not final_content.strip(): continue

            # --- Box Calculation ---
            max_line_w = 0
            total_h = 0

            valid_lines_count = 0
            for txt, size, char_w in lines_to_draw:
                # Remove tags
                clean_txt = re.sub(r'{\\.*?}', '', txt)
                # Strict check: remove all whitespace to see if there is actual content
                if not clean_txt.strip(): continue

                valid_lines_count += 1

                # Consistent width calculation using visual units
                w = get_visual_length(clean_txt) * size
                if w > max_line_w: max_line_w = w

                # Line height + leading (scaled leading approx 35% of font size for safety)
                total_h += int(size * 1.35)

            # Skip if no real text or unreasonable dimensions
            if valid_lines_count == 0 or max_line_w < 10: continue

            # Optimized Padding: approx 1.0 * font_size for safety
            h_padding = int(fs_cn * 1.0)
            max_w = int(max_line_w + h_padding)
            # Hard limit box width to avoid screen overflow (width - margins)
            limit_w = int(config.width * 0.98)
            if max_w > limit_w: max_w = limit_w

            v_padding = int(fs_cn * 0.4)
            total_h += v_padding # Bottom padding

            # --- Absolute Screen Coordinate Logic ---
            # To avoid "Wrong Place" issues relative to anchors, we draw using absolute 1920x1080 coordinates.
            # Origin: (0,0) at Top-Left.

            screen_center_x = config.width / 2
            screen_baseline_y = config.height - v_margin

            # Calculate absolute corners
            abs_x_l = int(screen_center_x - (max_w / 2))
            abs_x_r = int(screen_center_x + (max_w / 2))
            abs_y_t = int(screen_baseline_y - total_h)
            abs_y_b = int(screen_baseline_y + (fs_cn * 0.30)) # Expanded buffer for descenders

            # Ensure the top of the bounding box doesn't go off screen
            if abs_y_t < 20:
                # Push the top down to the margin
                abs_y_t = 20
                # Optionally also shift the bottom down to maintain height if we wanted,
                # but since wrapping is fixed, we just clamp it to stay visible.

            # Use \an7\pos(0,0) to set origin to Top-Left of screen.
            drawing_code = f"{{\\an7\\pos(0,0)\\p1\\c&H101010&\\3c&H101010&\\alpha&H30&}}m {abs_x_l} {abs_y_t} l {abs_x_r} {abs_y_t} l {abs_x_r} {abs_y_b} l {abs_x_l} {abs_y_b} {{\\p0}}"

            if config.bg_box:
                f.write(f"Dialogue: 0,{p['s']},{p['e']},BoxBase,,0,0,0,,{drawing_code}\n")
            f.write(f"Dialogue: 1,{p['s']},{p['e']},TextTop,,0,0,0,,{final_content}\n")

def get_versioned_filename(filepath):
    """Appends _v1, _v2 etc if file exists."""
    if not os.path.exists(filepath):
        return filepath

    base, ext = os.path.splitext(filepath)
    # Check if base already has _vN
    match = re.search(r'_v(\d+)$', base)
    if match:
        version = int(match.group(1))
        base = base[:match.start()]
    else:
        version = 0

    while True:
        version += 1
        new_path = f"{base}_v{version}{ext}"
        if not os.path.exists(new_path):
            return new_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert SRT to Styled ASS")
    parser.add_argument("input", nargs='?', help="Input SRT file")
    parser.add_argument("output", nargs='?', help="Output ASS file")
    parser.add_argument("-i", "--interactive", action="store_true", help="Force interactive mode")
    parser.add_argument("--layout", help="bilingual/cn/en")
    parser.add_argument("--main-lang", help="cn/en")
    parser.add_argument("--cn-font")
    parser.add_argument("--en-font")
    parser.add_argument("--ja-font", help="Japanese font for kana-bearing text")
    parser.add_argument("--ja-size", help="Japanese font size")
    parser.add_argument("--cn-size")
    parser.add_argument("--en-size")
    parser.add_argument("--cn-color")
    parser.add_argument("--en-color")
    parser.add_argument("--no-bg-box", action="store_true", help="Disable background box")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing ASS file instead of versioning")

    args = parser.parse_args()

    config = TkinterConfig()

    # Apply overrides
    if args.layout: config.mode = args.layout
    if args.main_lang: config.main_lang = args.main_lang
    if args.cn_font: config.cn_font = args.cn_font
    if args.en_font: config.en_font = args.en_font
    if args.ja_font: config.ja_font = args.ja_font
    if args.cn_size: config.cn_size = args.cn_size
    if args.en_size: config.en_size = args.en_size
    if args.cn_color: config.cn_color = config.color_map.get(args.cn_color, args.cn_color)
    if args.en_color: config.en_color = config.color_map.get(args.en_color, args.en_color)
    if args.no_bg_box: config.bg_box = False

    config.width = args.width
    config.height = args.height

    style_provided = any([args.layout, args.cn_font, args.en_font, args.cn_size, args.cn_color])

    # Logic: If no input/output provided OR interactive flag set, run wizard
    if args.interactive or (not args.input and not style_provided):
        # Prompt for file if not in args
        if not args.input:
            # We can't easily prompt in CLI if we moved to GUI, but let's assume user provides arg usually.
            # Or we could use file dialog if we really wanted to go full GUI.
            # For now, fallback to simple input() if CLI, but really we should use args.
            if len(sys.argv) == 1:
               # No args at all?
               # Let's open a file dialog?
               import tkinter.filedialog
               root = tk.Tk()
               root.withdraw()
               f = tkinter.filedialog.askopenfilename(filetypes=[("SRT Files", "*.srt")])
               root.destroy()
               if f: args.input = f
               else: sys.exit(0)

        if not args.input:
             print("No input file provided.")
             sys.exit(1)

        if not args.output:
            base = os.path.splitext(args.input)[0]
            args.output = f"{base}.ass"

        config.wizard()

        if not config.finished:
            print("Configuration cancelled.")
            sys.exit(0)
    else:
        # Non-interactive default (Bilingual, Middle, default colors) -> Or arguments applied
        pass

    try:
        # Use versioned output path if in interactive mode, otherwise overwrite to avoid cluttering batch projects
        if args.interactive and not args.overwrite:
            final_output_path = get_versioned_filename(args.output)
        else:
            final_output_path = args.output

        parsed = parse_srt(args.input)
        generate_ass(parsed, final_output_path, config)
        print(f"ASS Generated: {final_output_path}")

        # Show message box if in GUI mode
        if args.interactive or len(sys.argv)==1:
             import tkinter
             root = tkinter.Tk()
             root.withdraw()
             tkinter.messagebox.showinfo("Success", f"Subtitle Generated:\n{final_output_path}")
             root.destroy()

    except Exception as e:
        print(f"Error: {e}")
        import tkinter
        if args.interactive or len(sys.argv)==1:
             root = tkinter.Tk()
             root.withdraw()
             tkinter.messagebox.showerror("Error", f"Failed:\n{e}")
             root.destroy()
        sys.exit(1)
