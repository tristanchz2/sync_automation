"""
Confluence 爬虫模块
从 Atlassian Confluence (v7.4.9) 获取最近更新的页面，直接导出为 PDF
"""

import os
import re
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
        "expand": "version,space,ancestors",
    }
    resp = session.get(url, params=params)
    resp.raise_for_status()
    return resp.json().get("results", [])


def download_page_as_pdf(session, page_id: str, output_path: str) -> bool:
    """
    使用 Confluence 的 PDF 导出功能下载页面
    URL: /spaces/flyingpdf/pdfpageexport.action?pageId={page_id}
    """
    url = f"{CONFLUENCE_BASE_URL}/spaces/flyingpdf/pdfpageexport.action"
    params = {"pageId": page_id}
    
    # 需要允许重定向，因为会 302 到实际的 PDF 文件
    resp = session.get(url, params=params, allow_redirects=True)
    
    if resp.status_code != 200:
        print(f"  [错误] HTTP {resp.status_code}")
        return False
    
    # 检查是否是 PDF
    content_type = resp.headers.get('Content-Type', '')
    if 'pdf' not in content_type.lower() and len(resp.content) < 100:
        print(f"  [错误] 返回的不是 PDF: {content_type}")
        return False
    
    with open(output_path, 'wb') as f:
        f.write(resp.content)
    
    return True


def crawl_and_generate_pdfs(limit: int = 3) -> list:
    """
    主流程：爬取最近更新的页面，直接从 Confluence 导出 PDF
    返回生成的 PDF 文件路径列表
    """
    if not all([CONFLUENCE_BASE_URL, CONFLUENCE_USERNAME, CONFLUENCE_PASSWORD]):
        raise ValueError("请在 .env 文件中填写完整的 Confluence 配置信息")

    session = get_session()
    print(f"[INFO] 正在从 Confluence 获取所有 Space 下最近 {limit} 条更新的页面...")

    pages_summary = get_recently_updated_pages(session, limit)
    print(f"[INFO] 找到 {len(pages_summary)} 个页面\n")

    pdf_paths = []

    for i, page_summary in enumerate(pages_summary, 1):
        page_id = page_summary["id"]
        page_title = page_summary.get("title", "N/A")
        space_name = page_summary.get("space", {}).get("name", "未知空间")
        last_modified = page_summary.get("version", {}).get("when", "")
        
        print(f"[{i}/{len(pages_summary)}] 处理页面: {page_title}")
        print(f"  空间: {space_name}")
        print(f"  修改时间: {last_modified}")

        # 生成安全的文件名
        safe_title = safe_filename(page_title)
        pdf_path = os.path.join(OUTPUT_DIR, f"{safe_title}.pdf")

        # 直接从 Confluence 下载 PDF
        try:
            success = download_page_as_pdf(session, page_id, pdf_path)
            if success:
                file_size = os.path.getsize(pdf_path)
                print(f"  [PDF] 已生成: {pdf_path} ({file_size} bytes)")
                pdf_paths.append(pdf_path)
            else:
                print(f"  [PDF] 生成失败")
        except Exception as e:
            print(f"  [PDF] 生成失败: {e}")

        print()

    return pdf_paths
