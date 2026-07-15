"""
Confluence 爬虫模块
从 Atlassian Confluence (v7.4.9) 获取最近更新的页面，清洗 HTML，下载图片，生成 PDF
"""

import os
import re
import base64
import mimetypes
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

CONFLUENCE_BASE_URL = os.getenv("CONFLUENCE_BASE_URL", "").rstrip("/")
CONFLUENCE_USERNAME = os.getenv("CONFLUENCE_USERNAME", "")
CONFLUENCE_PASSWORD = os.getenv("CONFLUENCE_PASSWORD", "")

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))


def safe_filename(name: str) -> str:
    """将字符串转为安全的文件名，去除所有特殊字符和控制字符"""
    # 先替换控制字符和空白字符
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)
    # 再替换文件系统不允许的字符
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    # 去除首尾空白
    name = name.strip()
    # 限制长度
    return name[:80] if name else "untitled"


def get_session():
    """创建带 Basic Auth 的请求会话"""
    session = requests.Session()
    session.auth = HTTPBasicAuth(CONFLUENCE_USERNAME, CONFLUENCE_PASSWORD)
    session.headers.update({"Accept": "application/json"})
    return session


def get_recently_updated_pages(session, limit: int = 3) -> list:
    """
    通过 CQL 查询获取所有 space 下最近更新的页面（不限定 space）
    """
    cql = 'type=page ORDER BY lastmodified DESC'
    url = f"{CONFLUENCE_BASE_URL}/rest/api/content/search"
    params = {
        "cql": cql,
        "limit": limit,
        "expand": "version,space,ancestors,children.attachment",
    }
    resp = session.get(url, params=params)
    resp.raise_for_status()
    return resp.json().get("results", [])


def get_page_content(session, page_id: str) -> dict:
    """获取单个页面的完整内容"""
    url = f"{CONFLUENCE_BASE_URL}/rest/api/content/{page_id}"
    params = {"expand": "body.storage,children.attachment,version,ancestors"}
    resp = session.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def download_attachment(session, attachment: dict, page_title: str) -> str:
    """下载单个附件，返回本地文件路径"""
    download_link = attachment.get("_links", {}).get("download", "")
    if not download_link:
        return ""
    filename = attachment.get("title", "attachment")
    safe_title = safe_filename(page_title)
    att_dir = os.path.join(OUTPUT_DIR, "downloads", safe_title)
    os.makedirs(att_dir, exist_ok=True)
    local_path = os.path.join(att_dir, filename)

    file_url = f"{CONFLUENCE_BASE_URL}{download_link}"
    resp = session.get(file_url)
    resp.raise_for_status()
    with open(local_path, "wb") as f:
        f.write(resp.content)
    return local_path


def image_to_base64(file_path: str) -> str:
    """将图片文件转为 base64 data URI"""
    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = "image/png"
    with open(file_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime_type};base64,{data}"


def clean_confluence_html(body_html: str, downloaded_images: dict) -> str:
    """
    清洗 Confluence XHTML，转为干净的 HTML
    downloaded_images: {filename: local_path} 已下载的图片映射
    """
    html = body_html

    # 1. 处理 ac:structured-macro (代码块) -> <pre><code>
    def replace_code_block(match):
        full = match.group(0)
        # 提取语言
        lang_match = re.search(r'ac:name="language"[^>]*>([^<]+)<', full)
        language = lang_match.group(1).strip() if lang_match else ""
        # 提取标题
        title_match = re.search(r'ac:name="title"[^>]*>([^<]+)<', full)
        title = title_match.group(1).strip() if title_match else ""
        # 提取代码内容
        code_match = re.search(r'<ac:plain-text-body><!\[CDATA\[(.*?)\]\]></ac:plain-text-body>', full, re.DOTALL)
        code = code_match.group(1) if code_match else ""
        header = f'<div class="code-header">{title}</div>' if title else ""
        lang_class = f' class="{language}"' if language else ""
        return f'{header}<pre{lang_class}><code>{code}</code></pre>'

    html = re.sub(
        r'<ac:structured-macro[^>]*>.*?</ac:structured-macro>',
        replace_code_block,
        html,
        flags=re.DOTALL,
    )

    # 2. 处理 ac:task-list -> 复选框列表
    def replace_task_list(match):
        block = match.group(0)
        items = re.findall(
            r'<ac:task>.*?<ac:task-status>(\w+)</ac:task-status>.*?<ac:task-body>(.*?)</ac:task-body>.*?</ac:task>',
            block,
            re.DOTALL,
        )
        lines = []
        for status, body in items:
            checked = "checked" if status == "complete" else ""
            body_clean = re.sub(r'<[^>]+>', '', body).strip()
            lines.append(f'<li class="task-item"><input type="checkbox" {checked} disabled /> {body_clean}</li>')
        return '<ul class="task-list">' + "\n".join(lines) + '</ul>'

    html = re.sub(
        r'<ac:task-list>.*?</ac:task-list>',
        replace_task_list,
        html,
        flags=re.DOTALL,
    )

    # 3. 处理 ac:image -> <img> 标签
    def replace_image(match):
        block = match.group(0)
        # 提取高度
        height_match = re.search(r'ac:height="(\d+)"', block)
        height_style = f'height:{height_match.group(1)}px;' if height_match else ''
        # 提取附件文件名
        att_match = re.search(r'ri:filename="([^"]+)"', block)
        if att_match:
            filename = att_match.group(1)
            if filename in downloaded_images:
                data_uri = image_to_base64(downloaded_images[filename])
                return f'<img src="{data_uri}" style="{height_style}max-width:100%;" />'
            else:
                return f'<span class="missing-image">[图片缺失: {filename}]</span>'
        # 处理 URL 类型
        url_match = re.search(r'ri:url="([^"]+)"', block)
        if url_match:
            return f'<img src="{url_match.group(1)}" style="{height_style}max-width:100%;" />'
        return ""

    html = re.sub(
        r'<ac:image[^>]*>.*?</ac:image>',
        replace_image,
        html,
        flags=re.DOTALL,
    )

    # 4. 移除 Confluence 布局标签（保留内容）
    html = re.sub(r'</?ac:layout[^>]*>', '', html)
    html = re.sub(r'</?ac:layout-section[^>]*>', '', html)
    html = re.sub(r'</?ac:layout-cell[^>]*>', '', html)

    # 5. 移除其他残余 ac: 标签
    html = re.sub(r'</?ac:[^>]+>', '', html)

    # 6. 移除 ri: 残余
    html = re.sub(r'<ri:[^>]+/?>', '', html)

    return html


def build_full_html(title: str, body_html: str) -> str:
    """构建完整的 HTML 文档（含 CSS 样式）"""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
    @font-face {{
        font-family: 'SimHei';
        src: url('C:/Windows/Fonts/simhei.ttf');
    }}
    body {{
        font-family: SimHei;
        font-size: 14px;
        line-height: 1.6;
        color: #333;
        padding: 30px;
        max-width: 900px;
        margin: 0 auto;
    }}
    h1 {{
        font-size: 22px;
        font-family: SimHei;
        color: #172b4d;
        border-bottom: 2px solid #0052cc;
        padding-bottom: 6px;
        margin-top: 28px;
    }}
    h2 {{
        font-size: 18px;
        font-family: SimHei;
        color: #172b4d;
        margin-top: 22px;
    }}
    table {{
        border-collapse: collapse;
        width: 100%;
        margin: 12px 0;
        font-size: 13px;
    }}
    th, td {{
        border: 1px solid #dfe1e6;
        padding: 8px 10px;
        text-align: left;
        vertical-align: top;
        font-family: SimHei;
    }}
    th {{
        background-color: #f4f5f7;
        font-weight: bold;
        color: #172b4d;
    }}
    tr:nth-child(even) {{
        background-color: #fafbfc;
    }}
    pre {{
        background-color: #f4f5f7;
        border: 1px solid #dfe1e6;
        border-radius: 4px;
        padding: 12px;
        overflow-x: auto;
        font-size: 13px;
        line-height: 1.4;
    }}
    code {{
        font-family: Consolas, "Courier New", monospace;
    }}
    .code-header {{
        background-color: #ebecf0;
        padding: 6px 12px;
        font-size: 12px;
        font-family: SimHei;
        color: #6b778c;
        border: 1px solid #dfe1e6;
        border-bottom: none;
        border-radius: 4px 4px 0 0;
    }}
    .code-header + pre {{
        border-radius: 0 0 4px 4px;
        margin-top: 0;
    }}
    .task-list {{
        list-style: none;
        padding-left: 4px;
    }}
    .task-item {{
        margin: 3px 0;
        font-family: SimHei;
    }}
    .task-item input[type="checkbox"] {{
        margin-right: 6px;
    }}
    img {{
        max-width: 100%;
        height: auto;
        margin: 8px 0;
    }}
    .missing-image {{
        color: #de350b;
        font-style: italic;
        font-size: 12px;
    }}
    ul {{
        padding-left: 20px;
    }}
    p {{
        margin: 6px 0;
    }}
</style>
</head>
<body>
<h1 style="border-bottom: none; text-align: center; color: #0052cc;">{title}</h1>
{body_html}
</body>
</html>"""


def crawl_and_generate_pdfs(limit: int = 3) -> list:
    """
    主流程：爬取最近更新的页面，下载图片，生成 PDF
    返回生成的 PDF 文件路径列表
    """
    if not all([CONFLUENCE_BASE_URL, CONFLUENCE_USERNAME, CONFLUENCE_PASSWORD]):
        raise ValueError("请在 .env 文件中填写完整的 Confluence 配置信息")

    from fpdf import FPDF

    # 中文字体路径
    font_path = "C:/Windows/Fonts/simhei.ttf"

    session = get_session()
    print(f"[INFO] 正在从 Confluence 获取所有 Space 下最近 {limit} 条更新的页面...")

    pages_summary = get_recently_updated_pages(session, limit)
    print(f"[INFO] 找到 {len(pages_summary)} 个页面\n")

    pdf_paths = []

    for i, page_summary in enumerate(pages_summary, 1):
        page_id = page_summary["id"]
        page_title = page_summary.get("title", "N/A")
        print(f"[{i}/{len(pages_summary)}] 处理页面: {page_title} (ID: {page_id})")

        # 获取完整页面内容
        page_data = get_page_content(session, page_id)
        body_html = page_data.get("body", {}).get("storage", {}).get("value", "")

        if not body_html:
            print(f"  [跳过] 页面内容为空\n")
            continue

        # 下载所有附件图片
        attachments = (
            page_data.get("children", {}).get("attachment", {}).get("results", [])
        )
        downloaded_images = {}  # {filename: local_path}
        for att in attachments:
            filename = att.get("title", "")
            mime, _ = mimetypes.guess_type(filename)
            if mime and mime.startswith("image/"):
                try:
                    local_path = download_attachment(session, att, page_title)
                    downloaded_images[filename] = local_path
                    print(f"  [图片] 已下载: {filename}")
                except Exception as e:
                    print(f"  [图片] 下载失败 {filename}: {e}")

        # 清洗 HTML
        clean_html = clean_confluence_html(body_html, downloaded_images)

        # 保存 HTML（方便调试）
        safe_title = safe_filename(page_title)
        html_path = os.path.join(OUTPUT_DIR, f"{safe_title}.html")
        debug_html = build_full_html(page_title, clean_html)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(debug_html)

        # 生成 PDF
        pdf_path = os.path.join(OUTPUT_DIR, f"{safe_title}.pdf")
        try:
            generate_pdf_with_fpdf2(pdf_path, page_title, clean_html, downloaded_images, font_path)
            print(f"  [PDF] 已生成: {pdf_path}")
            pdf_paths.append(pdf_path)
        except Exception as e:
            print(f"  [PDF] 生成失败: {e}")

        print()

    return pdf_paths


def generate_pdf_with_fpdf2(pdf_path: str, title: str, body_html: str, images: dict, font_path: str):
    """使用 fpdf2 生成 PDF，支持中文和图片"""
    from fpdf import FPDF

    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=15)

    # 注册中文字体
    pdf.add_font("SimHei", "", font_path)
    pdf.add_font("SimHei", "B", font_path)  # 黑体本身较粗，用作 bold

    pdf.add_page()

    # 标题
    pdf.set_font("SimHei", "B", 18)
    pdf.cell(0, 12, title, new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(4)
    # 标题下划线
    pdf.set_draw_color(0, 82, 204)
    pdf.set_line_width(0.5)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # 解析 HTML 并渲染
    _render_html_to_pdf(pdf, body_html, images)

    pdf.output(pdf_path)


def _render_html_to_pdf(pdf, html: str, images: dict):
    """将清洗后的 HTML 渲染到 PDF 中"""
    from html.parser import HTMLParser
    import html as html_module

    class ConfluencePDFRenderer(HTMLParser):
        def __init__(self, pdf, images):
            super().__init__()
            self.pdf = pdf
            self.images = images
            self.current_tag = ""
            self.tag_stack = []
            self.in_table = False
            self.in_row = False
            self.in_header = False
            self.current_row_cells = []
            self.current_cell_text = ""
            self.table_headers = []
            self.col_count = 0
            self.in_pre = False
            self.pre_text = ""
            self.in_li = False
            self.li_text = ""
            self.in_h1 = False
            self.in_h2 = False
            self.in_h3 = False
            self.in_bold = False
            self.in_checkbox = False
            self.checkbox_checked = False

        def _set_normal_font(self):
            style = "B" if self.in_bold else ""
            self.pdf.set_font("SimHei", style, 10)

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            self.tag_stack.append(tag)
            self.current_tag = tag
            attrs_dict = dict(attrs)

            if tag == "h1":
                self.in_h1 = True
                self.pdf.ln(4)
                self.pdf.set_font("SimHei", "B", 16)
                self.pdf.set_text_color(23, 43, 77)
            elif tag == "h2":
                self.in_h2 = True
                self.pdf.ln(3)
                self.pdf.set_font("SimHei", "B", 14)
                self.pdf.set_text_color(23, 43, 77)
            elif tag == "h3":
                self.in_h3 = True
                self.pdf.ln(2)
                self.pdf.set_font("SimHei", "B", 12)
                self.pdf.set_text_color(23, 43, 77)
            elif tag == "strong" or tag == "b":
                self.in_bold = True
                self._set_normal_font()
            elif tag == "p":
                self._set_normal_font()
                self.pdf.set_text_color(51, 51, 51)
                self.pdf.ln(2)
            elif tag == "br":
                self.pdf.ln(4)
            elif tag == "table":
                self.in_table = True
                self.table_headers = []
                self.col_count = 0
            elif tag == "tr":
                self.in_row = True
                self.current_row_cells = []
                self.current_cell_text = ""
            elif tag == "th":
                self.in_header = True
                self.current_cell_text = ""
            elif tag == "td":
                self.in_header = False
                self.current_cell_text = ""
            elif tag == "pre":
                self.in_pre = True
                self.pre_text = ""
            elif tag == "code":
                pass
            elif tag == "li":
                self.in_li = True
                self.li_text = ""
                # 检查是否有 checkbox
                self.in_checkbox = False
                self.checkbox_checked = False
            elif tag == "input":
                if attrs_dict.get("type") == "checkbox":
                    self.in_checkbox = True
                    self.checkbox_checked = "checked" in attrs_dict
            elif tag == "img":
                src = attrs_dict.get("src", "")
                if src.startswith("data:"):
                    # base64 图片
                    try:
                        import base64
                        header, data = src.split(",", 1)
                        img_data = base64.b64decode(data)
                        import tempfile
                        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                        tmp.write(img_data)
                        tmp.close()
                        # 计算合适的大小
                        max_w = 170
                        try:
                            from PIL import Image
                            import io
                            img = Image.open(io.BytesIO(img_data))
                            w, h = img.size
                            aspect = h / w if w > 0 else 1
                            img_w = min(max_w, 170)
                            img_h = img_w * aspect
                            if img_h > 120:
                                img_h = 120
                                img_w = img_h / aspect
                        except:
                            img_w = 80
                            img_h = 60
                        self.pdf.image(tmp.name, x=self.pdf.l_margin, w=img_w, h=img_h)
                        self.pdf.ln(img_h + 2)
                        os.unlink(tmp.name)
                    except Exception as e:
                        self.pdf.set_font("SimHei", "", 8)
                        self.pdf.cell(0, 5, f"[图片加载失败]", new_x="LMARGIN", new_y="NEXT")

        def handle_endtag(self, tag):
            tag = tag.lower()
            if self.tag_stack and self.tag_stack[-1] == tag:
                self.tag_stack.pop()

            if tag == "h1":
                self.in_h1 = False
                self.pdf.set_text_color(0, 0, 0)
                self.pdf.ln(2)
                # 画底线
                self.pdf.set_draw_color(0, 82, 204)
                self.pdf.set_line_width(0.3)
                y = self.pdf.get_y()
                self.pdf.line(10, y, 200, y)
                self.pdf.ln(4)
            elif tag == "h2":
                self.in_h2 = False
                self.pdf.set_text_color(0, 0, 0)
                self.pdf.ln(2)
            elif tag == "h3":
                self.in_h3 = False
                self.pdf.set_text_color(0, 0, 0)
                self.pdf.ln(2)
            elif tag == "strong" or tag == "b":
                self.in_bold = False
                self._set_normal_font()
            elif tag == "p":
                self.pdf.ln(2)
            elif tag == "table":
                self.in_table = False
                self.pdf.ln(3)
            elif tag == "tr":
                self.in_row = False
                if self.current_row_cells:
                    try:
                        if not self.table_headers and self.col_count == 0:
                            # 第一行作为表头
                            self.table_headers = self.current_row_cells
                            self.col_count = len(self.current_row_cells)
                            # 计算列宽，确保足够空间
                            col_w = 180 / max(self.col_count, 1)
                            font_size = max(5, min(9, int(col_w / 2.5)))
                            self.pdf.set_fill_color(244, 245, 247)
                            self.pdf.set_font("SimHei", "B", font_size)
                            for cell in self.current_row_cells:
                                text = cell.strip()[:20]
                                self.pdf.cell(col_w, 7, text, border=1, fill=True)
                            self.pdf.ln()
                        else:
                            if self.col_count == 0:
                                self.col_count = len(self.current_row_cells)
                            col_w = 180 / max(self.col_count, 1)
                            font_size = max(5, min(9, int(col_w / 2.5)))
                            self.pdf.set_font("SimHei", "", font_size)
                            for j, cell in enumerate(self.current_row_cells):
                                text = cell.strip()
                                max_chars = max(2, int(col_w / 2))
                                if len(text) > max_chars:
                                    text = text[:max_chars-1] + ".."
                                self.pdf.cell(col_w, 7, text, border=1)
                            self.pdf.ln()
                    except Exception:
                        # 列太密无法渲染，跳过该行
                        pass
            elif tag == "th" or tag == "td":
                self.current_row_cells.append(self.current_cell_text)
                self.current_cell_text = ""
            elif tag == "pre":
                self.in_pre = False
                self.pdf.set_font("SimHei", "", 8)
                self.pdf.set_fill_color(244, 245, 247)
                self.pdf.set_draw_color(200, 200, 200)
                # 限制代码块高度
                lines = self.pre_text.strip().split("\n")
                for line in lines[:30]:  # 最多显示30行
                    self.pdf.cell(0, 5, "  " + line[:100], border=0, fill=True, new_x="LMARGIN", new_y="NEXT")
                self.pdf.ln(2)
                self.pre_text = ""
            elif tag == "li":
                self.in_li = False
                self._set_normal_font()
                prefix = ""
                if self.in_checkbox:
                    prefix = "[v] " if self.checkbox_checked else "[ ] "
                else:
                    prefix = "  - "
                self.pdf.cell(0, 5, prefix + self.li_text.strip(), new_x="LMARGIN", new_y="NEXT")
            elif tag == "ul":
                self.pdf.ln(1)

        def handle_data(self, data):
            text = data.strip()
            if not text:
                return

            if self.in_pre:
                self.pre_text += data
            elif self.in_li:
                self.li_text += text
            elif self.in_table:
                self.current_cell_text += text
            elif self.in_h1 or self.in_h2 or self.in_h3:
                self._set_normal_font()
                self.pdf.set_text_color(23, 43, 77)
                self.pdf.cell(0, 8, text, new_x="LMARGIN", new_y="NEXT")
            else:
                self._set_normal_font()
                self.pdf.set_text_color(51, 51, 51)
                # 处理普通段落文本
                try:
                    self.pdf.multi_cell(0, 5, text)
                except Exception:
                    pass

        def handle_entityref(self, name):
            char = html_module.unescape(f"&{name};")
            self._handle_char(char)

        def handle_charref(self, name):
            char = html_module.unescape(f"&#{name};")
            self._handle_char(char)

        def _handle_char(self, char):
            if self.in_pre:
                self.pre_text += char
            elif self.in_li:
                self.li_text += char
            elif self.in_table:
                self.current_cell_text += char

    renderer = ConfluencePDFRenderer(pdf, images)
    renderer.feed(html)
