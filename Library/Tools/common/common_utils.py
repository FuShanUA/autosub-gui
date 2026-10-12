import os
import re
import json
import yaml # Added for terms.yml support
from pathlib import Path

# Paths
AGENTS_DIR = os.path.dirname(os.path.abspath(__file__))
POSTOS_DIR = os.path.dirname(AGENTS_DIR)
CONFIG_DIR = os.path.join(POSTOS_DIR, "config")
TERMS_FILE = os.path.join(CONFIG_DIR, "terms.yml")

# Added missing globals for skill loading
ROOT_DIR = os.path.dirname(POSTOS_DIR)
TOOLS_DIR = os.path.dirname(ROOT_DIR)
COMMON_DIR = os.path.abspath(os.path.join(ROOT_DIR, "..", "common"))

class MetadataEngine:
    """Postfdry 统一元数据解析与处理器。"""
    def __init__(self, content=None):
        self.raw_meta = {}
        if content:
            self.parse(content)

    def parse(self, text):
        """同时支持 YAML Frontmatter 和 Key-Value 格式探测。"""
        # 移除 BOM 并首尾去空格，处理可能存在的不可见字符
        text = text.lstrip('\ufeff').strip() if text else ""
        if not text: return

        # 1. 优先提取 YAML (通过分割符提取，比正则更鲁棒)
        # 允许 --- 前后有空行或空格
        parts = re.split(r'^---\s*[\r\n]+', text, maxsplit=2, flags=re.MULTILINE)

        if len(parts) >= 3:
            yaml_part = parts[1]
            body_part = parts[2]
            try:
                extracted = yaml.safe_load(yaml_part) or {}
                if isinstance(extracted, dict):
                    for k, v in extracted.items():
                        if v:
                            self.raw_meta[str(k).lower()] = str(v).strip().strip("'").strip('"').replace("'''", "").strip()
            except Exception as e:
                print(f"  [Meta] YAML Parse Error: {e}")

        # 2. 补充提取 Key-Value (仅在元数据不全时补充)
        if not self.raw_meta:
            meta_patterns = {
                'title': re.compile(r'^(?:Title|标题)\s*[:：]\s*(.*)$', re.IGNORECASE | re.MULTILINE),
                'author': re.compile(r'^(?:Author|作者)\s*[:：]\s*(.*)$', re.IGNORECASE | re.MULTILINE),
                'source': re.compile(r'^(?:Source|来源|机构|Publisher|source)\s*[:：]\s*(.*)$', re.IGNORECASE | re.MULTILINE),
                'date': re.compile(r'^(?:Date|Publish_Date|date|日期|发布时间)\s*[:：]\s*(.*)$', re.IGNORECASE | re.MULTILINE),
                'url': re.compile(r'^(?:Url|Link|url|link|原文链接)\s*[:：]\s*(.*)$', re.IGNORECASE | re.MULTILINE)
            }
            for key, pattern in meta_patterns.items():
                m = pattern.search(text)
                if m:
                    val = m.group(1).strip().strip("'").strip('"').replace("'''", "").strip()
                    self.raw_meta[key] = val

    def get(self, key, default=""):
        # 更多别名映射 (双向兼容)
        aliases = {
            'title': ['title', 'headline', '标题', 'Title', 'EngTitle', 'eng_title', 'original_title'],
            'date': ['publish_date', 'Published_Date', 'published_date', '日期', '发布时间', 'date', 'date published', 'datepublished', 'publication_date'],
            'source': ['publisher', 'Publisher', '来源', '机构', 'source', 'institution', 'site_name'],
            'publish_date': ['date', 'Published_Date', 'published_date', '日期', '发布时间', 'publish_date', 'date published', 'datepublished'],
            'author': ['author', 'Author', '作者', 'writer', 'author name', 'written by', 'by'],
            'url': ['url', 'URL', '原文链接', 'link', 'original_url']
        }

        # 1. 尝试直接取值 (不分大小写，因为 parse 已 lowercase)
        search_key = key.lower()
        val = self.raw_meta.get(search_key)

        # 2. 如果没取到，尝试别名
        if not val and search_key in aliases:
            for alt in aliases[search_key]:
                val = self.raw_meta.get(alt.lower())
                if val: break

        if val is None or val == "" or str(val).lower() == "none":
            return default
        return str(val).strip()

    def to_yaml(self):
        """生成标准 YAML 块。"""
        import yaml
        # 过滤空值
        clean_meta = {k: v for k, v in self.raw_meta.items() if v}
        return f"---\n{yaml.dump(clean_meta, allow_unicode=True)}---\n"

    def clean_body(self, text):
        """从正文中剥离所有已识别的元数据行。"""
        return extract_clean_body(text)

def load_skill_content(name):
    """Load SKILL.md for a given skill name."""
    path = os.path.join(TOOLS_DIR, name, "SKILL.md")
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    return ""

def load_readme_content(name):
    """Load README.md for a given skill name."""
    path = os.path.join(TOOLS_DIR, name, "README.md")
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    return ""

def load_unslop_rules(domain):
    """Load unslop constraints from the common global directory."""
    if not domain:
        return ""
    # Check Common first
    path = os.path.join(COMMON_DIR, "unslop_skills", domain, "skill.md")
    if not os.path.exists(path):
        # Fallback to local unslop tool
        path = os.path.join(TOOLS_DIR, "unslop", "skills", domain, "SKILL.md")

    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    return ""

def load_user_style():
    """Loads the custom user style profile if it exists."""
    style_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'styles', 'user_style.md')
    if os.path.exists(style_path):
        with open(style_path, 'r', encoding='utf-8') as f:
            return f.read()
    return ""

def load_terms():
    """Load terminology glossary from multiple possible locations and format as a Markdown list."""
    from pathlib import Path

    # Use robust absolute pathing relative to this file
    current_dir = Path(__file__).resolve().parent
    project_root = current_dir.parent

    possible_paths = [
        current_dir.parent / "config" / "terms.yml",
        current_dir.parent / "postfdry" / "config" / "terms.yml",
        project_root.parent / "PostOS" / "config" / "terms.yml"
    ]

    terms_file = None
    for p in possible_paths:
        if p.exists():
            terms_file = p
            break

    if not terms_file:
        print(f"  [Terms] ❌ CRITICAL: No terms.yml found. Tried: {[str(p) for p in possible_paths]}")
        return ""

    print(f"  [Terms] 📖 Loading from: {terms_file}")
    try:
        with open(terms_file, 'r', encoding='utf-8') as f:
            content = f.read()
            terms = yaml.safe_load(content)

        if not terms:
            print(f"  [Terms] ⚠️ WARNING: YAML loaded, but result is empty/None.")
            return ""

        print(f"  [Terms] ✅ Successfully loaded {len(terms)} term pairs.")
        md_content = "### TERMINOLOGY GLOSSARY (专有名词对照表)\n"
        md_content += "在翻译或重写过程中，必须严格遵守以下术语定义，不得根据上下文随意更改：\n\n"
        for source, target in terms.items():
            if isinstance(target, str):
                md_content += f"- **{source}**: {target}\n"
            else:
                md_content += f"- **{source}**: {str(target)}\n"
        return md_content + "\n"
    except Exception as e:
        print(f"  [Terms] ❌ ERROR loading terms: {e}")
        return ""

def load_style_guide():
    """Load HARD_CONSTRAINTS.md specific rules."""
    path = os.path.join(COMMON_DIR, "HARD_CONSTRAINTS.md")
    rules = []
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith("|") and not line.startswith("| Rule") and not line.startswith("| description") and not line.startswith("|---"):
                    parts = [p.strip() for p in re.split(r'(?<!\\)\|', line) if p.strip()]
                    if len(parts) >= 2:
                        pattern = parts[1].strip('`').replace(r'\|', '|')
                        replacement = parts[2].strip('`').replace(r'\|', '|') if len(parts) > 2 else ""
                        rules.append((pattern, replacement))
    return rules


def load_writing_style():
    """Load WritingStyle rules and blacklist words."""
    content = load_skill_content("WritingStyle")
    if not content:
        content = load_readme_content("WritingStyle")

    blacklist = []
    # Extract blacklist words from patterns
    matches = re.findall(r'- \*\*.+?\*\*: (.+)', content)
    for m in matches:
        words = [w.strip() for w in m.split(',')]
        blacklist.extend(words)
    return content, blacklist

def deterministic_scrub(text):
    """Hard-fixes that MUST be applied regardless of LLM output."""
    if not text: return ""

    lines = text.split('\n')
    processed_lines = []

    guide_rules = load_style_guide()

    in_metadata = True
    metadata_keys = ["Title", "Author", "Date", "Source", "Url", "EngTitle", "OriginalTitle", "Publish_Date", "Published_Date", "标题", "作者", "日期", "来源", "原文链接"]
    in_yaml = False

    for line in lines:
        stripped = line.strip()

        # 1. YAML 状态切换
        if stripped == "---":
            if not in_yaml and in_metadata:
                in_yaml = True
                processed_lines.append(line)
                continue
            elif in_yaml:
                in_yaml = False
                in_metadata = False # YAML 结束通常意味着元数据阶段结束
                processed_lines.append(line)
                continue

        # 2. 如果在 YAML 块内，完全跳过清洗逻辑 (保护 ASCII 冒号)
        if in_yaml:
            processed_lines.append(line)
            continue

        # 3. 探测非 YAML 元数据阶段的结束：看到第一个 H1 或 图片即认为进入正文
        if in_metadata:
             if stripped.startswith("#") or stripped.startswith("!"):
                 in_metadata = False

        # 4. 如果还在元数据阶段，且是非 YAML 格式，则尝试识别并保护元数据行
        if in_metadata:
             key_pattern = r'^(?:' + '|'.join(metadata_keys) + r')\s*[:：].*$'
             if re.match(key_pattern, stripped, re.IGNORECASE):
                 # 这种行也属于元数据，直接原样保留，不参与下面的正则清洗
                 processed_lines.append(line)
                 continue

        # 5. 下面是正文内容的清洗（包括标记保护）
        if stripped.startswith("#") or stripped.startswith("!") or stripped.startswith("<!--") or stripped.startswith("- **"):
            # 5.5 Strip Substack/Medium author avatars even from image lines
            if re.search(r'!\[.*?(?:avatar|头像).*?\]', stripped, re.IGNORECASE):
                continue
            processed_lines.append(line)
            continue

        # 5.6 Strip Read time / 阅读时长 / 阅读时间 lines (not caught by marker protection)
        if re.match(r'^\s*\*?\*?(?:Read Time|阅读时长|阅读时间|Reading Time|Read time)\s*[:：]?\s*', stripped, re.IGNORECASE):
            continue

        # Avoid corrupting technical list markers (e.g. "1. " -> "1。")
        is_numeric_list = re.match(r'^\d+\.\s', line)

        for pattern, replacement in guide_rules:
            try:
                # Don't replace dot/colon in list markers or code
                if is_numeric_list and pattern in [r"\.", r"\:"]:
                    continue

                # Protect URLs and Links
                if pattern in ["!", "?", ":", ";"] and ("](" in line or "http" in line):
                    continue
                line = re.sub(pattern, replacement, line)
            except: pass

        processed_lines.append(line)

    final_text = '\n'.join(processed_lines)

    # 6. Hard Terminology Scrub (Data Steward / Data Owner)
    # This ensures consistency even if LLM hallucinates or follows its own training data
    term_fixes = [
        (r'数据所有(?:者|人|权属人)', '数据主人'), # High Priority Fix
        (r'数据所有权', '数据权属'),
        (r'数据管护(?:员|人|职责)?', '数据管家'), # 捕捉“数据管护员”等变体
        (r'数据管理(?:员|员职能|职能)', '数据管家'),
        (r'(?<!数据)管护员', '数据管家'),           # 捕捉漏掉“数据”前缀的“管护员”
        (r'数据管护', '数据认责'),                 # Stewardship 对应数据认责
        (r'所有者', '数据主人'), # 强制映射文中简写的 Owner
        (r'数据认责(?:制)?', '数据认责'),
        (r'数据管家(?:制)?', '数据管家')
    ]
    for pattern, repl in term_fixes:
        final_text = re.sub(pattern, repl, final_text)

    return final_text

def extract_clean_body(text):
    """
    Strips YAML frontmatter, H1 titles, covers, and lead-ins from content.
    Returns only the 'meat' of the article.
    """
    # 1. Strip YAML frontmatter (Only if it exists at the absolute beginning)
    text = re.sub(r'\A---\s*.*?\s*---\s*', '', text, flags=re.DOTALL)

    # 1.5 Strip ANY leading image (DISABLED for translation mode to preserve header images)
    # text = re.sub(r'^\s*(!\[.*?\]\(.*?\)|<img.*?>)\s*', '', text, count=1, flags=re.IGNORECASE | re.DOTALL)

    # 2. Strip standard decorations
    # Remove ![Cover] or [ARTICLE_COVER_HERE]
    text = re.sub(r'!\[Cover\].*?(\n|$)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[ARTICLE_COVER_HERE\].*?(\n|$)', '', text, flags=re.IGNORECASE)

    # 1.6 Strip Substack/Medium author avatars and read-time metadata
    # Author avatar images: ![User's avatar](...) or ![Dylan Anderson的头像](...)
    text = re.sub(r'!\[.*?(?:avatar|头像).*?\]\(.*?\)', '', text, flags=re.IGNORECASE)
    # Read time / 阅读时长 / 阅读时间 lines
    text = re.sub(r'(?im)^\s*\*?\*?(?:Read Time|阅读时长|阅读时间|Reading Time|Read time)\b.*$', '', text)
    # Substack subscriber/like counts and other metadata lines
    text = re.sub(r'(?im)^\s*\*?\*?(?:Subscribers|Likes|Comments|订阅|点赞|评论)\s*:?\s*.*$', '', text)
    # Substack cross-article promotion cards: image + heading + "Read full story" link
    promo_pattern = r'!\[(?:Issue\s*#|第\s*\d+\s*期).*?\]\(.*?\)\s*\n+#{2,4}\s*(?:Issue\s*#|第\s*\d+\s*期).*?\n+\[(?:Read full story|阅读完整文章|阅读完整报道).*?\]\(.*?\)\s*\n*'
    text = re.sub(promo_pattern, '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[(?:Read full story|阅读完整文章|阅读完整报道)\]\(.*?\)', '', text, flags=re.IGNORECASE)

    # Remove Lead-in blocks: > **导读** or > **Lead-in**
    text = re.sub(r'>\s*\*\*导读：?\*\*.*?(\n\n|\n---|$)', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'>\s*\*\*Lead-in：?\*\*.*?(\n\n|\n---|$)', '', text, flags=re.DOTALL | re.IGNORECASE)

    # Remove Author/Column Bios - Deterministic fallback
    # Expanded to handle both header-based and plain-text/bold-text intros seen in Substack/Medium.
    # We use a greedy match until the next ACTUAL article section (H1/H2/H3 or divider) or End of File ($).
    # [CAUTION] Only strip bios/ads at the very beginning or end to avoid middle-content loss
    bio_pattern = r'(?i)(?:#+|\*\*|__)?\s*(?:About the Author|Author Bio|作者简介|关于作者|About the Authors|Author Connect)\s*(?:#+|\*\*|__)?[:：]?.*?(?=\n\s*(?:#{1,3}|---|$)|\s*$)'

    # Check if a bio-like block exists near the end
    if len(text) > 1000:
        footer_zone = text[-1500:]
        cleaned_footer = re.sub(bio_pattern, '', footer_zone, flags=re.DOTALL)
        text = text[:-1500] + cleaned_footer
    else:
        text = re.sub(bio_pattern, '', text, flags=re.DOTALL)

    # Strip subscriber-only noise (often found in newsletters)
    text = re.sub(r'(?i)>?\s*This post is for (?:paid )?subscribers.*?\n+', '', text)
    text = re.sub(r'(?i)>?\s*仅限订阅者阅读.*?\n+', '', text)

    # Remove trailing hashtags (e.g. #AI #Tech at the end of the post)
    text = re.sub(r'(?m)^\s*(?:#[\w\u4e00-\u9fa5]+\s*)+$', '', text)

    # 3. Strip redundant horizontal rules at the top ONLY
    # DO NOT strip rules within the body as they are section dividers
    text = re.sub(r'\A---\s*', '', text)

    # 4. Strip leading H1 (we will re-add it if needed in assembly)
    text = re.sub(r'^#\s+.*?\n+', '', text, count=1).strip()

    # 5. Cleanup leaked YAML fields in body (Title: Author: etc)
    # We include publish_date and other common variants. Handle potential spaces.
    leaked_meta_pattern = r'^\s*(?:Title|Author|Date|Source|EngTitle|Url|OriginalTitle|publish_date|date|author|source|url)\s*[:：].*$'
    text = re.sub(leaked_meta_pattern, '', text, flags=re.MULTILINE | re.IGNORECASE)

    # 6. Final safety: Remove any isolated YAML dividers that might have been left behind near the top
    # but only if they are within the first 800 characters to avoid breaking content dividers.
    # We look for dividers that are NOT part of a valid metadata block.
    head = text[:800]
    tail = text[800:]
    head = re.sub(r'^\s*---\s*$', '', head, flags=re.MULTILINE)

    # Also remove empty or whitespace-only yaml code blocks at the top
    head = re.sub(r'^\s*```yaml\s*[\s\n]*```\s*$', '', head, flags=re.MULTILINE)

    text = head + tail

    return text.strip()

def log_prompt(md_path, name, content, project_root=None):
    """
    Save a prompt to the prompts/ directory.
    Priority:
    1. project_root/prompts/
    2. md_path/../prompts/
    """
    if not md_path and not project_root:
        return

    if project_root:
        prompts_dir = os.path.join(project_root, "prompts")
    else:
        project_dir = os.path.dirname(os.path.abspath(md_path))
        # Check if we are in a subfolder (wip/source/output)
        if os.path.basename(project_dir) in ["wip", "source", "output", "work"]:
            prompts_dir = os.path.join(os.path.dirname(project_dir), "prompts")
        else:
            prompts_dir = os.path.join(project_dir, "prompts")

    if not os.path.exists(prompts_dir):
        os.makedirs(prompts_dir)

    filename = f"{name}_prompt.txt"
    save_path = os.path.join(prompts_dir, filename)
    with open(save_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  [Log] Prompt saved to: prompts/{filename}")

def build_de_ai_protocol(unslop_domain="B2B", custom_style=False):
    """
    Builds the De-AI protocol by loading terms, unslop rules, and humanizer guidelines.
    If custom_style is True, also loads and appends the user's specific style profile.
    """
    terms_xml = load_terms()
    unslop_rules = load_unslop_rules(unslop_domain)

    # Load foundational de-AI skills (as strings)
    remove_ai_flavor_content = load_skill_content("remove-ai-flavor")
    humanizer_zh_content = load_skill_content("humanizer-zh")

    protocol = "<DE_AI_PROTOCOL>\n"
    protocol += "You MUST adhere to the following publication standards to eliminate 'AI flavor' and 'translationese':\n\n"

    if terms_xml:
        protocol += f"<GLOSSARY>\n{terms_xml}\n</GLOSSARY>\n\n"

    protocol += "<TONE_EGALITARIAN>\n"
    protocol += "- **Egalitarian Dialogue vs. Immersive Authority**:\n"
    protocol += f"{remove_ai_flavor_content[:2000]}\n"
    protocol += "</TONE_EGALITARIAN>\n\n"

    protocol += "<NATIVE_PHRASING>\n"
    protocol += "- **Faithfulness, Fluency, Elegance (信达雅)**:\n"
    protocol += f"{humanizer_zh_content[:2000]}\n"
    protocol += "</NATIVE_PHRASING>\n\n"

    if unslop_rules:
         protocol += "<VOCAB_UNSLOP>\n"
         protocol += f"- **Domain-Specific Anti-Slops ({unslop_domain})**:\n"
         protocol += f"{unslop_rules[:1800]}\n"
         protocol += "</VOCAB_UNSLOP>\n\n"

    if custom_style:
        user_style = load_user_style()
        if user_style:
            protocol += "<USER_STYLE_PROFILE>\n"
            protocol += "Apply the following custom writing style extracted from the user's samples:\n"
            protocol += f"{user_style[:2000]}\n"
            protocol += "</USER_STYLE_PROFILE>\n\n"

    protocol += "</DE_AI_PROTOCOL>\n"

    return protocol
def get_versioned_path(base_path):
    """
    If file exists, generate a versioned path (e.g., file_v1.pdf, file_v2.pdf).
    """
    if not os.path.exists(base_path):
        return base_path

    path_obj = Path(base_path)
    directory = path_obj.parent
    name = path_obj.stem
    ext = path_obj.suffix

    # Try to find existing version suffix
    match = re.search(r'_v(\d+)$', name)
    if match:
        base_name = name[:match.start()]
        version = int(match.group(1))
    else:
        base_name = name
        version = 0 # Starting from v1 if original exists

    while True:
        version += 1
        new_name = f"{base_name}_v{version}{ext}"
        new_path = os.path.join(directory, new_name)
        if not os.path.exists(new_path):
            return new_path
