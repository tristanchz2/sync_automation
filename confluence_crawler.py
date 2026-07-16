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

load_dotenv(override=True)

CONFLUENCE_BASE_URL = os.getenv("CONFLUENCE_BASE_URL", "").rstrip("/")
CONFLUENCE_USERNAME = os.getenv("CONFLUENCE_USERNAME", "")
CONFLUENCE_PASSWORD = os.getenv("CONFLUENCE_PASSWORD", "")

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))


def normalize_last_modified(iso_str: str) -> str:
    """
    将 Confluence 返回的 ISO 8601 时间（如 2026-07-16T11:25:00.000+08:00）
    转为 yyyy-MM-dd HH:mm 格式，与 DB / LAST_SYNC_TIME 保持一致。
    """
    if not iso_str:
        return ""
    try:
        # 取前16位: "2026-07-16T11:25" → 替换T为空格 → "2026-07-16 11:25"
        return iso_str[:16].replace("T", " ")
    except Exception:
        return iso_str

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



def get_page_detail(session, page_id: str) -> dict:
    """获取页面完整内容，包括附件列表和正文"""
    url = f"{CONFLUENCE_BASE_URL}/rest/api/content/{page_id}"
    params = {"expand": "body.storage,children.attachment,version"}
    resp = session.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def get_child_pages(session, page_id: str) -> list:
    """获取页面的所有直接子页面"""
    url = f"{CONFLUENCE_BASE_URL}/rest/api/content/{page_id}/child/page"
    params = {"expand": "version,space", "limit": 100}
    all_children = []
    start = 0
    while True:
        params["start"] = start
        resp = session.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        all_children.extend(results)
        if data.get("_links", {}).get("next"):
            start += len(results)
        else:
            break
    return all_children


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


def get_linked_page_ids(session, page_data: dict) -> list:
    """从页面正文中提取链接的其他 Confluence 页面 ID"""
    body = page_data.get("body", {}).get("storage", {}).get("value", "")
    if not body:
        return []

    linked_ids = []

    # 1. <ri:page ri:content-id="12345" /> 直接引用页面 ID
    for pid in re.findall(r'<ri:page[^>]*ri:content-id="(\d+)"', body):
        if pid not in linked_ids:
            linked_ids.append(pid)

    # 2. <ri:page ri:content-title="xxx" ri:space-key="yyy" /> 按标题+空间引用
    for m in re.finditer(r'<ri:page[^>]*ri:content-title="([^"]+)"[^>]*?(?:ri:space-key="([^"]*)")?', body):
        title = m.group(1)
        space_key = m.group(2) or ""
        # 通过 API 查找页面 ID
        params = {"title": title, "limit": 1}
        if space_key:
            params["spaceKey"] = space_key
        try:
            r = session.get(f"{CONFLUENCE_BASE_URL}/rest/api/content", params=params)
            if r.status_code == 200:
                results = r.json().get("results", [])
                if results:
                    pid = results[0]["id"]
                    if pid not in linked_ids:
                        linked_ids.append(pid)
        except Exception:
            pass

    # 3. 普通 href 链接指向本站页面: /display/xxx/yyy 或 /spaces/xxx/pages/12345
    base_host = re.match(r'(https?://[^/]+)', CONFLUENCE_BASE_URL)
    if base_host:
        host_pattern = re.escape(base_host.group(1))
        for m in re.finditer(rf'href="{host_pattern}/display/([^/]+)/([^"#?]+)"', body):
            space_key = m.group(1)
            title = m.group(2).replace('+', ' ')
            from urllib.parse import unquote
            title = unquote(title)
            try:
                r = session.get(f"{CONFLUENCE_BASE_URL}/rest/api/content",
                                params={"title": title, "spaceKey": space_key, "limit": 1})
                if r.status_code == 200:
                    results = r.json().get("results", [])
                    if results:
                        pid = results[0]["id"]
                        if pid not in linked_ids:
                            linked_ids.append(pid)
            except Exception:
                pass

        for m in re.finditer(rf'href="{host_pattern}/spaces/([^/]+)/pages/(\d+)"', body):
            pid = m.group(2)
            if pid not in linked_ids:
                linked_ids.append(pid)

    return linked_ids


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


def _process_page(session, page_id: str, page_title: str, space_name: str,
                   last_modified: str, visited: set = None,
                   space_key: str = "", space_id: int = 0) -> list:
    """处理单个页面：下载自己 + 递归下载子页面。返回列表。"""
    if visited is None:
        visited = set()

    if page_id in visited:
        return []
    visited.add(page_id)

    print(f"处理页面: {page_title}")
    print(f"  空间: {space_name}")
    print(f"  修改时间: {last_modified}")

    safe_title = safe_filename(page_title)
    page_dir = os.path.join(OUTPUT_DIR, "downloads", safe_title)
    os.makedirs(page_dir, exist_ok=True)

    page_result = {
        "page_id": page_id,
        "title": page_title,
        "space": space_name,
        "space_key": space_key,
        "space_id": space_id,
        "last_modified": last_modified,
        "dir": page_dir,
        "pdf_path": "",
        "attachments": [],
    }

    # 1. 导出 PDF
    pdf_path = os.path.join(page_dir, f"{safe_title}.pdf")
    try:
        success = download_page_as_pdf(session, page_id, pdf_path)
        if success:
            size = os.path.getsize(pdf_path)
            print(f"  [PDF] 已导出 ({size:,} bytes)")
            page_result["pdf_path"] = pdf_path
    except Exception as e:
        print(f"  [PDF] 导出失败: {e}")

    # # 2. 识别并下载附件
    # try:
    #     page_data = get_page_detail(session, page_id)
    #     attachments = get_downloadable_attachments(page_data)

    #     if attachments:
    #         print(f"  [附件] 发现 {len(attachments)} 个可下载文件:")
    #         for att in attachments:
    #             filename = att["filename"]
    #             size_kb = att["size"] / 1024 if att["size"] else 0
    #             print(f"    - {filename} ({size_kb:.0f} KB)")
    #             try:
    #                 local_path = download_attachment(session, page_id, att, page_dir)
    #                 if local_path:
    #                     page_result["attachments"].append({
    #                         "filename": filename,
    #                         "local_path": local_path,
    #                         "size": os.path.getsize(local_path),
    #                     })
    #                     print(f"      [已下载]")
    #                 else:
    #                     print(f"      [下载失败: 无法获取下载链接]")
    #             except Exception as e:
    #                 print(f"      [下载失败: {e}]")
    #     else:
    #         print(f"  [附件] 无需要下载的附件")
    # except Exception as e:
    #     print(f"  [附件] 获取附件列表失败: {e}")

    results = [page_result]

    # # 3. 递归下载子页面
    # try:
    #     child_pages = get_child_pages(session, page_id)
    #     if child_pages:
    #         print(f"  [子页面] 发现 {len(child_pages)} 个子页面，递归下载...")
    #         for child in child_pages:
    #             child_id = child["id"]
    #             if child_id in visited:
    #                 continue
    #             child_title = child.get("title", "N/A")
    #             child_space = child.get("space", {}).get("name", space_name)
    #             child_space_key = child.get("space", {}).get("key", space_key)
    #             child_space_id = child.get("space", {}).get("id", space_id)
    #             child_modified = child.get("version", {}).get("when", "")
    #             print()
    #             child_results = _process_page(
    #                 session, child_id, child_title, child_space,
    #                 child_modified, visited, child_space_key, child_space_id
    #             )
    #             results.extend(child_results)
    # except Exception as e:
    #     print(f"  [子页面] 获取子页面失败: {e}")

    return results


def _print_summary(results: list):
    """打印下载汇总"""
    total_att = sum(len(r['attachments']) for r in results)
    total_pdf = sum(1 for r in results if r['pdf_path'])

    print("=" * 60)
    print(f"  下载汇总: {len(results)} 个页面, {total_pdf} 个 PDF, {total_att} 个附件")
    for r in results:
        print(f"\n  [DIR] {r['dir']}")
        if r['pdf_path']:
            print(f"    [PDF] {os.path.basename(r['pdf_path'])}")
        for a in r['attachments']:
            print(f"    [ATT] {a['filename']} ({a['size']:,} bytes)")
    print("=" * 60)


def resolve_page(session, page_input: str) -> dict:
    """
    解析用户输入的页面标识，返回页面摘要信息
    支持:
      - 纯数字: 作为 page ID
      - URL: 如 https://finkms.kingdee.com/display/KJBQT/2024-H1
             或 https://finkms.kingdee.com/spaces/KJBQT/pages/69654995/2024-H1
    """
    page_input = page_input.strip()

    # URL 格式1: /display/{spaceKey}/{title}
    m = re.search(r'/display/([^/]+)/([^?#]+)', page_input)
    if m:
        space_key = m.group(1)
        title = m.group(2).replace('+', ' ')
        from urllib.parse import unquote
        title = unquote(title)
        r = session.get(f"{CONFLUENCE_BASE_URL}/rest/api/content",
                        params={"title": title, "spaceKey": space_key, "expand": "version,space"})
        r.raise_for_status()
        results = r.json().get("results", [])
        if results:
            return results[0]
        raise ValueError(f"未找到页面: space={space_key}, title={title}")

    # URL 格式2: /spaces/{spaceKey}/pages/{pageId}[/{title}]
    m = re.search(r'/spaces/([^/]+)/pages/(\d+)', page_input)
    if m:
        page_id = m.group(2)
        r = session.get(f"{CONFLUENCE_BASE_URL}/rest/api/content/{page_id}",
                        params={"expand": "version,space"})
        r.raise_for_status()
        data = r.json()
        return {
            "id": data["id"],
            "title": data.get("title", ""),
            "space": data.get("space", {}),
            "version": data.get("version", {}),
        }

    # 提取输入中的数字部分作为 page ID（兼容 ID 中夹杂非数字字符的情况）
    digits = re.sub(r'\D', '', page_input)
    if digits:
        r = session.get(f"{CONFLUENCE_BASE_URL}/rest/api/content/{digits}",
                        params={"expand": "version,space"})
        if r.status_code == 200:
            data = r.json()
            if data.get("id"):
                return data

    raise ValueError(f"无法解析页面标识: {page_input}\n"
                     f"  支持格式: 页面ID(如 69654995) 或 URL(如 https://.../display/KJBQT/2024-H1)")


def download_single_page(page_input: str) -> list:
    """下载指定的单个页面"""
    if not all([CONFLUENCE_BASE_URL, CONFLUENCE_USERNAME, CONFLUENCE_PASSWORD]):
        raise ValueError("请在 .env 文件中填写完整的 Confluence 配置信息")

    session = get_session()
    print(f"[INFO] 解析页面标识: {page_input}")

    page_info = resolve_page(session, page_input)
    page_id = page_info["id"]
    page_title = page_info.get("title", "N/A")
    space_name = page_info.get("space", {}).get("name", "未知空间")
    last_modified = normalize_last_modified(page_info.get("version", {}).get("when", ""))

    print(f"[INFO] 找到页面: {page_title} (ID: {page_id})\n")

    space_key = page_info.get("space", {}).get("key", "")
    space_id = page_info.get("space", {}).get("id", 0)
    visited = set()
    results = _process_page(session, page_id, page_title, space_name,
                            last_modified, visited, space_key, space_id)
    print()
    _print_summary(results)
    return results


def get_trashed_pages_by_space(space_key: str) -> list:
    """
    查询指定 space 中所有在回收站里的页面。
    使用 Confluence REST API: /rest/api/content?spaceKey=xxx&status=trashed&type=page
    自动处理分页。

    返回:
        list of dict，每个包含:
          - id: 页面 ID
          - title: 页面标题
    """
    if not all([CONFLUENCE_BASE_URL, CONFLUENCE_USERNAME, CONFLUENCE_PASSWORD]):
        raise ValueError("请在 .env 文件中填写完整的 Confluence 配置信息")

    session = get_session()
    url = f"{CONFLUENCE_BASE_URL}/rest/api/content"
    all_trashed = []
    start = 0

    while True:
        params = {
            "spaceKey": space_key,
            "status": "trashed",
            "type": "page",
            "limit": 100,
            "start": start,
        }
        try:
            resp = session.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            for page in results:
                all_trashed.append({
                    "id": page["id"],
                    "title": page.get("title", ""),
                })
            # 分页
            if data.get("_links", {}).get("next"):
                start += len(results)
            else:
                break
        except Exception as e:
            print(f"  [警告] 查询 space '{space_key}' 回收站失败: {e}")
            break

    return all_trashed


def check_trashed_by_ids(page_ids: list) -> list:
    """
    批量检查指定的 page ID 中哪些已进入回收站。
    使用 CQL 搜索: id in (...) AND status = trashed
    每批最多查 20 个 ID，避免 CQL 过长。

    返回:
        list of dict，每个包含:
          - id: 页面 ID
          - title: 页面标题
    """
    if not page_ids:
        return []

    if not all([CONFLUENCE_BASE_URL, CONFLUENCE_USERNAME, CONFLUENCE_PASSWORD]):
        raise ValueError("请在 .env 文件中填写完整的 Confluence 配置信息")

    session = get_session()
    url = f"{CONFLUENCE_BASE_URL}/rest/api/content/search"
    trashed = []
    batch_size = 20

    for i in range(0, len(page_ids), batch_size):
        batch = page_ids[i:i + batch_size]
        # 构建 CQL: (id = "1" OR id = "2" OR ...) AND status = trashed
        id_clauses = " OR ".join(f'id = "{pid}"' for pid in batch)
        cql = f'({id_clauses}) AND status = trashed'

        try:
            params = {
                "cql": cql,
                "limit": len(batch),
            }
            resp = session.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            for page in data.get("results", []):
                trashed.append({
                    "id": page["id"],
                    "title": page.get("title", ""),
                })
        except Exception as e:
            print(f"  [警告] 批量检查 trashed 状态失败 (batch {i // batch_size + 1}): {e}")

    return trashed


def get_latest_pages(limit: int = 50) -> list:
    """
    获取 Confluence 中最近修改的页面（仅 current 状态），按 lastModified 倒序。

    参数:
        limit: 最大返回数量，默认 50

    返回:
        list of dict，每个包含:
          - id: 页面 ID
          - title: 页面标题
          - status: "current"
          - space_key: Space Key
          - space_id: Space ID
          - space_name: Space 名称
          - last_modified: 最后修改时间
    """
    if not all([CONFLUENCE_BASE_URL, CONFLUENCE_USERNAME, CONFLUENCE_PASSWORD]):
        raise ValueError("请在 .env 文件中填写完整的 Confluence 配置信息")

    session = get_session()

    # CQL: 按 lastModified 倒序获取最近的页面
    cql = 'type=page order by lastModified desc'

    url = f"{CONFLUENCE_BASE_URL}/rest/api/content/search"
    all_pages = []
    start = 0

    while len(all_pages) < limit:
        params = {
            "cql": cql,
            "limit": min(limit - len(all_pages), 100),
            "start": start,
            "expand": "version,space",
        }
        resp = session.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])

        for page in results:
            page_info = {
                "id": page["id"],
                "title": page.get("title", ""),
                "status": page.get("status", "current"),
                "space_key": page.get("space", {}).get("key", ""),
                "space_id": page.get("space", {}).get("id", 0),
                "space_name": page.get("space", {}).get("name", ""),
                "last_modified": normalize_last_modified(page.get("version", {}).get("when", "")),
            }
            all_pages.append(page_info)

        if data.get("_links", {}).get("next") and len(all_pages) < limit:
            start += len(results)
        else:
            break

    print(f"[查询] 获取最近修改的 {len(all_pages)} 个页面")
    return all_pages


def get_recently_changed_pages(since: str, limit: int = 100) -> list:
    """
    获取自指定时间以来所有变更的页面（包括已删除到回收站的）。
    
    参数:
        since: 起始时间，格式 "yyyy-MM-dd HH:mm"
        limit: 最大返回数量
    
    返回:
        list of dict，每个包含:
          - id: 页面 ID
          - title: 页面标题
          - status: "current" 或 "trashed"
          - space_key: Space Key
          - space_id: Space ID
          - space_name: Space 名称
          - last_modified: 最后修改时间
    """
    if not all([CONFLUENCE_BASE_URL, CONFLUENCE_USERNAME, CONFLUENCE_PASSWORD]):
        raise ValueError("请在 .env 文件中填写完整的 Confluence 配置信息")

    session = get_session()

    # Confluence CQL: 查询指定时间之后修改的所有页面（含 current 和 trashed）
    # 将时间转换为 Confluence 格式
    since_clean = since.strip()
    cql = f'type=page AND lastModified >= "{since_clean}" order by lastModified desc'

    url = f"{CONFLUENCE_BASE_URL}/rest/api/content/search"
    all_pages = []
    start = 0

    while True:
        params = {
            "cql": cql,
            "limit": min(limit - len(all_pages), 100),
            "start": start,
            "expand": "version,space",
        }
        resp = session.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])

        for page in results:
            page_info = {
                "id": page["id"],
                "title": page.get("title", ""),
                "status": page.get("status", "current"),
                "space_key": page.get("space", {}).get("key", ""),
                "space_id": page.get("space", {}).get("id", 0),
                "space_name": page.get("space", {}).get("name", ""),
                "last_modified": normalize_last_modified(page.get("version", {}).get("when", "")),
            }
            all_pages.append(page_info)

        if data.get("_links", {}).get("next") and len(all_pages) < limit:
            start += len(results)
        else:
            break

    print(f"[查询] 自 {since} 以来共 {len(all_pages)} 个页面发生变更")
    current_count = sum(1 for p in all_pages if p["status"] == "current")
    trashed_count = sum(1 for p in all_pages if p["status"] == "trashed")
    print(f"  - 活跃: {current_count}, 已删除: {trashed_count}")

    return all_pages
