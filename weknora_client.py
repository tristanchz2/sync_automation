"""
Weknora API 客户端模块
负责将爬取的内容上传到 Weknora 知识库
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

WEKNORA_BASE_URL = os.getenv("WEKNORA_BASE_URL", "").rstrip("/")
WEKNORA_API_KEY = os.getenv("WEKNORA_API_KEY", "")
WEKNORA_KNOWLEDGE_BASE_ID = os.getenv("WEKNORA_KNOWLEDGE_BASE_ID", "")


def get_headers() -> dict:
    """构建 Weknora API 请求头"""
    return {
        "Authorization": f"Bearer {WEKNORA_API_KEY}",
        "Accept": "application/json",
    }


def upload_file(knowledge_base_id: str, file_path: str) -> dict:
    """
    将文件上传到指定知识库
    POST /api/v1/knowledge-bases/{id}/files
    """
    url = f"{WEKNORA_BASE_URL}/api/v1/knowledge-bases/{knowledge_base_id}/files"
    headers = get_headers()

    with open(file_path, "rb") as f:
        files = {"file": (os.path.basename(file_path), f)}
        resp = requests.post(url, headers=headers, files=files)

    resp.raise_for_status()
    return resp.json()


def upload_html_content(knowledge_base_id: str, title: str, html_content: str) -> dict:
    """
    将 HTML 内容作为文件上传到知识库
    先将 HTML 写入临时文件，再上传
    """
    import tempfile

    safe_title = title.replace(" ", "_")[:50]
    tmp_path = os.path.join(tempfile.gettempdir(), f"{safe_title}.html")

    full_html = f"<html><head><meta charset='utf-8'><title>{title}</title></head><body>{html_content}</body></html>"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(full_html)

    result = upload_file(knowledge_base_id, tmp_path)
    os.remove(tmp_path)
    return result


def get_knowledge_base_status(knowledge_base_id: str) -> dict:
    """
    获取知识库状态
    GET /api/v1/knowledge-bases/{id}/status
    """
    url = f"{WEKNORA_BASE_URL}/api/v1/knowledge-bases/{knowledge_base_id}/status"
    headers = get_headers()
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


def sync_pages_to_weknora(pages: list) -> list:
    """
    将爬取的页面列表同步上传到 Weknora 知识库
    返回上传结果列表
    """
    if not all([WEKNORA_BASE_URL, WEKNORA_API_KEY, WEKNORA_KNOWLEDGE_BASE_ID]):
        raise ValueError("请在 .env 文件中填写完整的 Weknora 配置信息")

    kb_id = WEKNORA_KNOWLEDGE_BASE_ID
    results = []

    print(f"[INFO] 开始上传到 Weknora 知识库 (ID: {kb_id})\n")

    for i, page in enumerate(pages, 1):
        title = page["title"]
        print(f"[{i}/{len(pages)}] 上传页面: {title}")

        # 上传页面 HTML 内容
        try:
            result = upload_html_content(kb_id, title, page["body_html"])
            print(f"  [页面] 上传成功")
            results.append({"title": title, "type": "page", "status": "success", "result": result})
        except Exception as e:
            print(f"  [页面] 上传失败: {e}")
            results.append({"title": title, "type": "page", "status": "failed", "error": str(e)})

        # 上传附件
        for att_path in page.get("attachments", []):
            att_name = os.path.basename(att_path)
            try:
                result = upload_file(kb_id, att_path)
                print(f"  [附件] {att_name} 上传成功")
                results.append({"title": att_name, "type": "attachment", "status": "success", "result": result})
            except Exception as e:
                print(f"  [附件] {att_name} 上传失败: {e}")
                results.append({"title": att_name, "type": "attachment", "status": "failed", "error": str(e)})

        print()

    return results
