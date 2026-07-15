"""
WeKnora API 客户端
封装 JWT 登录、知识库创建、文件上传、知识删除等操作
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

WEKNORA_BASE_URL = os.getenv("WEKNORA_BASE_URL", "http://localhost:8080").rstrip("/")
WEKNORA_EMAIL = os.getenv("WEKNORA_EMAIL", "")
WEKNORA_PASSWORD = os.getenv("WEKNORA_PASSWORD", "")

# 缓存的 JWT token
_jwt_token: str = ""


def _login() -> str:
    """
    通过邮箱密码登录，获取 JWT token
    POST /api/v1/auth/login
    """
    url = f"{WEKNORA_BASE_URL}/api/v1/auth/login"
    payload = {
        "email": WEKNORA_EMAIL,
        "password": WEKNORA_PASSWORD,
    }
    resp = requests.post(url, json=payload)
    resp.raise_for_status()
    data = resp.json()
    token = data.get("token") or data.get("access_token") or data.get("jwt", "")
    if not token:
        raise RuntimeError(f"登录成功但未获取到 token，响应: {data}")
    print(f"[WeKnora] JWT 登录成功")
    return token


def _get_token() -> str:
    """获取有效的 JWT token（自动登录/刷新）"""
    global _jwt_token
    if not _jwt_token:
        _jwt_token = _login()
    return _jwt_token


def _headers(json: bool = False) -> dict:
    """构建带 JWT 认证的请求头"""
    token = _get_token()
    headers = {"Authorization": f"Bearer {token}"}
    if json:
        headers["Content-Type"] = "application/json"
    return headers


def _extract_data(resp_json: dict) -> dict | list:
    """
    WeKnora 统一响应格式: {"data": {...}, "success": true}
    提取 data 字段，若 success 为 false 则抛异常
    """
    if not resp_json.get("success", True):
        msg = resp_json.get("message") or resp_json.get("error") or resp_json
        raise RuntimeError(f"WeKnora 返回错误: {msg}")
    return resp_json.get("data", resp_json)


def create_knowledge_base(name: str, description: str = "",
                          chunk_size: int = 1000,
                          chunk_overlap: int = 200) -> dict:
    """
    创建 WeKnora 知识库（含模型配置）
    """
    url = f"{WEKNORA_BASE_URL}/api/v1/knowledge-bases"
    payload = {
        "name": name,
        "description": description,
        "chunking_config": {
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
        },
        "embedding_model_id": "builtin-embedding-default",
        "summary_model_id": "builtin-llm-default",
    }
    resp = requests.post(url, json=payload, headers=_headers(json=True))
    resp.raise_for_status()
    result = _extract_data(resp.json())
    kb_id = result.get("id", "N/A") if isinstance(result, dict) else "N/A"
    print(f"[WeKnora] 知识库已创建: {kb_id} - {name}")
    return result


def list_knowledge_bases() -> list:
    """获取所有知识库列表"""
    url = f"{WEKNORA_BASE_URL}/api/v1/knowledge-bases"
    resp = requests.get(url, headers=_headers())
    resp.raise_for_status()
    return _extract_data(resp.json())


def upload_file(knowledge_base_id: str, file_path: str) -> dict:
    """
    上传文件到指定知识库
    返回包含 knowledge_id 等信息的响应
    """
    url = f"{WEKNORA_BASE_URL}/api/v1/knowledge-bases/{knowledge_base_id}/knowledge/file"
    filename = os.path.basename(file_path)

    with open(file_path, "rb") as f:
        files = {"file": (filename, f)}
        resp = requests.post(url, files=files, headers=_headers())

    resp.raise_for_status()
    result = _extract_data(resp.json())
    knowledge_id = ""
    if isinstance(result, dict):
        knowledge_id = result.get("id") or result.get("knowledge_id") or ""
    print(f"[WeKnora] 文件已上传: {filename} → knowledge_id={knowledge_id}")
    return result


def delete_knowledge(knowledge_base_id: str, knowledge_id: str) -> bool:
    """
    删除指定知识
    DELETE /api/v1/knowledge/{knowledge_id}
    """
    url = f"{WEKNORA_BASE_URL}/api/v1/knowledge/{knowledge_id}"
    resp = requests.delete(url, headers=_headers())
    resp.raise_for_status()
    print(f"[WeKnora] 知识已删除: knowledge_id={knowledge_id}")
    return True


def check_connection() -> bool:
    """检查 WeKnora 服务是否可用"""
    try:
        _login()
        list_knowledge_bases()
        return True
    except Exception as e:
        print(f"[WeKnora] 连接失败: {e}")
        return False

