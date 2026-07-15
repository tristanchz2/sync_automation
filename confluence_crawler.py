"""
Confluence 爬虫模块
从 Atlassian Confluence (v7.4.9) 获取最近更新的页面及其附件
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
CONFLUENCE_SPACE_KEY = os.getenv("CONFLUENCE_SPACE_KEY", "")

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")


def get_session():
    """创建带 Basic Auth 的请求会话"""
    session = requests.Session()
    session.auth = HTTPBasicAuth(CONFLUENCE_USERNAME, CONFLUENCE_PASSWORD)
    session.headers.update({"Accept": "application/json"})
    return session


def get_recently_updated_pages(session, space_key: str, limit: int = 3) -> list:
    """
    通过 CQL 查询获取指定 space 下最近更新的页面
    使用 /rest/api/content/search 接口
    """
    cql = f'space="{space_key}" AND type=page ORDER BY lastmodified DESC'
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
    """
    获取单个页面的完整内容（含 body.storage 和 children.attachment）
    """
    url = f"{CONFLUENCE_BASE_URL}/rest/api/content/{page_id}"
    params = {
        "expand": "body.storage,children.attachment,version,ancestors",
    }
    resp = session.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def download_attachments(session, page_data: dict) -> list:
    """
    下载页面中的所有附件到本地 downloads/ 目录
    返回本地文件路径列表
    """
    attachments = (
        page_data.get("children", {}).get("attachment", {}).get("results", [])
    )
    if not attachments:
        return []

    page_title = page_data.get("title", "unknown")
    safe_title = re.sub(r'[<>:"/\\|?*]', "_", page_title)
    page_dir = os.path.join(DOWNLOAD_DIR, safe_title)
    os.makedirs(page_dir, exist_ok=True)

    downloaded = []
    for att in attachments:
        # 获取附件下载链接
        download_link = att.get("_links", {}).get("download", "")
        if not download_link:
            continue
        filename = att.get("title", "attachment")
        file_url = f"{CONFLUENCE_BASE_URL}{download_link}"
        local_path = os.path.join(page_dir, filename)

        resp = session.get(file_url)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            f.write(resp.content)
        downloaded.append(local_path)
        print(f"  [附件] 已下载: {filename}")

    return downloaded


def process_body_with_attachments(session, page_data: dict, local_attachments: list) -> str:
    """
    处理页面 body.storage 内容，将 <ri:attachment> 引用替换为本地文件路径说明
    """
    body = page_data.get("body", {}).get("storage", {}).get("value", "")
    # 提取所有附件引用
    attachment_refs = re.findall(
        r'<ri:attachment\s+ri:filename="([^"]+)"', body
    )
    if attachment_refs:
        print(f"  [正文] 发现 {len(attachment_refs)} 个附件引用: {attachment_refs}")
    return body


def crawl_recent_pages(limit: int = 3) -> list:
    """
    主爬取流程：获取最近更新的 limit 条页面，下载附件，返回结构化数据
    """
    if not all([CONFLUENCE_BASE_URL, CONFLUENCE_USERNAME, CONFLUENCE_PASSWORD, CONFLUENCE_SPACE_KEY]):
        raise ValueError("请在 .env 文件中填写完整的 Confluence 配置信息")

    session = get_session()
    print(f"[INFO] 正在从 Confluence Space '{CONFLUENCE_SPACE_KEY}' 获取最近 {limit} 条更新的页面...")

    pages = get_recently_updated_pages(session, CONFLUENCE_SPACE_KEY, limit)
    print(f"[INFO] 找到 {len(pages)} 个页面\n")

    results = []
    for i, page_summary in enumerate(pages, 1):
        page_id = page_summary["id"]
        page_title = page_summary.get("title", "N/A")
        print(f"[{i}/{len(pages)}] 处理页面: {page_title} (ID: {page_id})")

        # 获取完整页面内容
        page_data = get_page_content(session, page_id)

        # 下载附件
        local_attachments = download_attachments(session, page_data)

        # 处理正文
        body_html = process_body_with_attachments(session, page_data, local_attachments)

        results.append({
            "id": page_id,
            "title": page_title,
            "version": page_data.get("version", {}).get("number", 0),
            "last_modified": page_data.get("version", {}).get("when", ""),
            "body_html": body_html,
            "attachments": local_attachments,
            "ancestors": [
                {"id": a["id"], "title": a.get("title", "")}
                for a in page_data.get("ancestors", [])
            ],
        })
        print(f"  [完成] 附件数: {len(local_attachments)}\n")

    return results
