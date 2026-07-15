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
    safe_title = re.sub(r'[<>:"/\\|?*]', "_", page_title)
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
    body {{
        font-family: "Microsoft YaHei", "SimSun", Arial, sans-serif;
        font-size: 14px;
        line-height: 1.6;
        color: #333;
        padding: 30px;
        max-width: 900px;
        margin: 0 auto;
    }}
    h1 {{
        font-size: 22px;
        color: #172b4d;
        border-bottom: 2px solid #0052cc;
        padding-bottom: 6px;
        margin-top: 28px;
    }}
    h2 {{
        font-size: 18px;
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

    from xhtml2pdf import pisa
    import io

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

        # 构建完整 HTML
        full_html = build_full_html(page_title, clean_html)

        # 保存 HTML（方便调试）
        safe_title = re.sub(r'[<>:"/\\|?*]', "_", page_title)
        html_path = os.path.join(OUTPUT_DIR, f"{safe_title}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(full_html)

        # 生成 PDF
        pdf_path = os.path.join(OUTPUT_DIR, f"{safe_title}.pdf")
        with open(pdf_path, "wb") as pdf_file:
            pisa_status = pisa.CreatePDF(io.StringIO(full_html), dest=pdf_file, encoding="utf-8")

        if pisa_status.err:
            print(f"  [PDF] 生成有警告，但已保存: {pdf_path}")
        else:
            print(f"  [PDF] 已生成: {pdf_path}")

        pdf_paths.append(pdf_path)
        print()

    return pdf_paths
