"""
Confluence 爬虫模块
从 Atlassian Confluence (v7.4.9) 获取最近更新的页面：
1. 直接导出页面为 PDF
2. 识别并下载页面中的附件（PPT/Excel/Word/ZIP 等需要下载才能查看的文件）
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

# 不需要下载的扩展名（这些在 PDF 中已经可见）
# 只有图片格式会被内联渲染到 PDF/网页中，其他所有格式都需要下载才能查看
SKIP_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.bmp', '.ico', '.webp',
}


def safe_filename(name: str) -> str:
    """将字符串转为安全的文件名，去除所有特殊字符和控制字符"""
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip()
    return name[:80] if name else "untitled"


def get_session():
    """创建带 Basic Auth 的请求会话"""
    session = requests.Session()
    session.auth = HTTPBasicAuth(CONFLUENCE_USERNAME, CONFLUENCE_PASSWORD)
    session.headers.update({"Accept": "application/json"})
    return session


def get_recently_updated_pages(session, limit: int = 3) -> list:
    """通过 CQL 查询获取所有 space 下最近更新的页面"""
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


def get_page_detail(session, page_id: str) -> dict:
    """获取页面完整内容，包括附件列表和正文"""
    url = f"{CONFLUENCE_BASE_URL}/rest/api/content/{page_id}"
    params = {"expand": "body.storage,children.attachment,version"}
    resp = session.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def get_downloadable_attachments(page_data: dict) -> list:
    """
    从页面数据中提取需要下载的附件
    判断逻辑：
    1. children.attachment 中的非图片附件
    2. body 中通过 view-file 宏或 ri:attachment 引用的 office/压缩包等文件
    """
    attachments = []

    # 方法1: 从 children.attachment 获取
    att_results = (
        page_data.get("children", {}).get("attachment", {}).get("results", [])
    )
    for att in att_results:
        filename = att.get("title", "")
        ext = os.path.splitext(filename)[1].lower()
        # 排除图片（图片已在 PDF 中内嵌）
        if ext in SKIP_EXTENSIONS:
            continue
        download_link = att.get("_links", {}).get("download", "")
        if download_link:
            attachments.append({
                "filename": filename,
                "download_link": download_link,
                "size": att.get("extensions", {}).get("fileSize", 0),
            })

    # 方法2: 从 body 中查找 view-file 宏引用的附件（补充）
    body = page_data.get("body", {}).get("storage", {}).get("value", "")
    if body:
        # 查找 view-file 宏中引用的附件
        viewfile_refs = re.findall(
            r'<ac:structured-macro[^>]*ac:name="view-file"[^>]*>.*?<ri:attachment\s+ri:filename="([^"]+)".*?</ac:structured-macro>',
            body, re.DOTALL
        )
        existing_names = {a["filename"] for a in attachments}
        for filename in viewfile_refs:
            if filename not in existing_names:
                ext = os.path.splitext(filename)[1].lower()
                if ext not in SKIP_EXTENSIONS:
                    attachments.append({
                        "filename": filename,
                        "download_link": "",  # 需要从 attachment API 获取
                        "size": 0,
                    })

    return attachments


def download_attachment(session, page_id: str, attachment: dict, output_dir: str) -> str:
    """下载单个附件，返回本地路径"""
    filename = attachment["filename"]
    download_link = attachment["download_link"]

    if not download_link:
        # 尝试通过 attachment API 获取下载链接
        url = f"{CONFLUENCE_BASE_URL}/rest/api/content/{page_id}/child/attachment"
        params = {"filename": filename}
        resp = session.get(url, params=params)
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                download_link = results[0].get("_links", {}).get("download", "")

    if not download_link:
        return ""

    file_url = f"{CONFLUENCE_BASE_URL}{download_link}"
    local_path = os.path.join(output_dir, filename)

    resp = session.get(file_url, allow_redirects=True)
    resp.raise_for_status()

    with open(local_path, 'wb') as f:
        f.write(resp.content)

    return local_path


def download_page_as_pdf(session, page_id: str, output_path: str) -> bool:
    """使用 Confluence 的 PDF 导出功能下载页面"""
    url = f"{CONFLUENCE_BASE_URL}/spaces/flyingpdf/pdfpageexport.action"
    params = {"pageId": page_id}
    resp = session.get(url, params=params, allow_redirects=True)

    if resp.status_code != 200:
        print(f"  [错误] HTTP {resp.status_code}")
        return False

    content_type = resp.headers.get('Content-Type', '')
    if 'pdf' not in content_type.lower() and len(resp.content) < 100:
        print(f"  [错误] 返回的不是 PDF: {content_type}")
        return False

    with open(output_path, 'wb') as f:
        f.write(resp.content)

    return True


def crawl_and_download(limit: int = 3) -> list:
    """
    主流程：爬取最近更新的页面
    1. 导出页面为 PDF
    2. 识别并下载页面中的附件（PPT/Excel/Word 等）
    返回结果列表
    """
    if not all([CONFLUENCE_BASE_URL, CONFLUENCE_USERNAME, CONFLUENCE_PASSWORD]):
        raise ValueError("请在 .env 文件中填写完整的 Confluence 配置信息")

    session = get_session()
    print(f"[INFO] 正在从 Confluence 获取所有 Space 下最近 {limit} 条更新的页面...")

    pages_summary = get_recently_updated_pages(session, limit)
    print(f"[INFO] 找到 {len(pages_summary)} 个页面\n")

    results = []

    for i, page_summary in enumerate(pages_summary, 1):
        page_id = page_summary["id"]
        page_title = page_summary.get("title", "N/A")
        space_name = page_summary.get("space", {}).get("name", "未知空间")
        last_modified = page_summary.get("version", {}).get("when", "")

        print(f"[{i}/{len(pages_summary)}] 处理页面: {page_title}")
        print(f"  空间: {space_name}")
        print(f"  修改时间: {last_modified}")

        safe_title = safe_filename(page_title)
        page_result = {
            "page_id": page_id,
            "title": page_title,
            "space": space_name,
            "last_modified": last_modified,
            "pdf_path": "",
            "attachments": [],
        }

        # 1. 导出 PDF
        pdf_path = os.path.join(OUTPUT_DIR, f"{safe_title}.pdf")
        try:
            success = download_page_as_pdf(session, page_id, pdf_path)
            if success:
                size = os.path.getsize(pdf_path)
                print(f"  [PDF] 已导出 ({size:,} bytes)")
                page_result["pdf_path"] = pdf_path
        except Exception as e:
            print(f"  [PDF] 导出失败: {e}")

        # 2. 获取页面详情，识别附件
        try:
            page_data = get_page_detail(session, page_id)
            attachments = get_downloadable_attachments(page_data)

            if attachments:
                print(f"  [附件] 发现 {len(attachments)} 个可下载文件:")
                # 创建附件目录
                att_dir = os.path.join(OUTPUT_DIR, "downloads", safe_title)
                os.makedirs(att_dir, exist_ok=True)

                for att in attachments:
                    filename = att["filename"]
                    size_kb = att["size"] / 1024 if att["size"] else 0
                    print(f"    - {filename} ({size_kb:.0f} KB)")

                    try:
                        local_path = download_attachment(session, page_id, att, att_dir)
                        if local_path:
                            page_result["attachments"].append({
                                "filename": filename,
                                "local_path": local_path,
                                "size": os.path.getsize(local_path),
                            })
                            print(f"      [已下载]")
                        else:
                            print(f"      [下载失败: 无法获取下载链接]")
                    except Exception as e:
                        print(f"      [下载失败: {e}]")
            else:
                print(f"  [附件] 无需要下载的附件")
        except Exception as e:
            print(f"  [附件] 获取附件列表失败: {e}")

        results.append(page_result)
        print()

    # 打印汇总
    print("=" * 60)
    print("  下载汇总:")
    for r in results:
        print(f"\n  页面: {r['title']}")
        if r['pdf_path']:
            print(f"    PDF: {r['pdf_path']}")
        if r['attachments']:
            print(f"    附件 ({len(r['attachments'])} 个):")
            for a in r['attachments']:
                print(f"      - {a['filename']} ({a['size']:,} bytes)")
    print("=" * 60)

    return results
