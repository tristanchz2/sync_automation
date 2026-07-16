"""
PostgreSQL 数据库管理模块
管理 Confluence page ID 与 WeKnora knowledge ID / knowledge base ID 的映射关系
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv(override=True)

PG_CONFIG = {
    "host": os.getenv("PG_HOST", "127.0.0.1"),
    "port": os.getenv("PG_PORT", "5433"),
    "dbname": os.getenv("PG_DATABASE", "confluence_sync"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", "postgres"),
}

# ─────────────────────────────────────────────
# 连接 & 初始化
# ─────────────────────────────────────────────

def get_connection():
    """获取数据库连接（调用方负责关闭）"""
    return psycopg2.connect(**PG_CONFIG)


def init_db():
    """
    初始化数据库：
    1. 创建 confluence_sync 数据库（如不存在）
    2. 创建所需表
    """
    # 先连接默认数据库，创建业务库
    conn_params = {k: v for k, v in PG_CONFIG.items() if k != "dbname"}
    conn_params["dbname"] = "postgres"

    conn = psycopg2.connect(**conn_params)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s",
        (PG_CONFIG["dbname"],),
    )
    if not cur.fetchone():
        cur.execute(f'CREATE DATABASE "{PG_CONFIG["dbname"]}"')
        print(f"[DB] 数据库 '{PG_CONFIG['dbname']}' 已创建")
    cur.close()
    conn.close()

    # 连接业务库，创建表
    conn = get_connection()
    cur = conn.cursor()

    # 迁移：如果旧表没有 space_id 列，说明是旧版（用 space_key 做主键），需重建
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'knowledge_bases' AND column_name = 'space_id'
    """)
    if not cur.fetchone():
        cur.execute("DROP TABLE IF EXISTS knowledge_bases")
        print("[DB] 旧版 knowledge_bases 表已删除，将重建")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_bases (
            space_id        INTEGER PRIMARY KEY,
            space_key       VARCHAR(255) NOT NULL,
            space_name      VARCHAR(255) NOT NULL,
            knowledge_base_id VARCHAR(255) NOT NULL,
            created_at      TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS page_mappings (
            confluence_page_id  VARCHAR(255) PRIMARY KEY,
            knowledge_base_id   VARCHAR(255) NOT NULL,
            knowledge_id        VARCHAR(255) NOT NULL,
            page_title          VARCHAR(500),
            last_modified       VARCHAR(100),
            uploaded_at         TIMESTAMP DEFAULT NOW()
        )
    """)

    # 迁移：如果旧 page_mappings 表没有 last_modified 列，自动添加
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'page_mappings' AND column_name = 'last_modified'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE page_mappings ADD COLUMN last_modified VARCHAR(100)")
        print("[DB] page_mappings 表已添加 last_modified 列")

    conn.commit()
    cur.close()
    conn.close()
    print("[DB] 数据表初始化完成")


# ─────────────────────────────────────────────
# knowledge_bases 表操作（space → knowledge base）
# ─────────────────────────────────────────────

def get_kb_by_space(space_id: int) -> dict | None:
    """根据 space_id 查询已创建的 knowledge base"""
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT * FROM knowledge_bases WHERE space_id = %s",
        (space_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None


def save_kb_mapping(space_id: int, space_key: str, space_name: str,
                    knowledge_base_id: str):
    """保存 space → knowledge base 映射"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO knowledge_bases (space_id, space_key, space_name, knowledge_base_id)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (space_id) DO UPDATE
            SET space_key = EXCLUDED.space_key,
                space_name = EXCLUDED.space_name,
                knowledge_base_id = EXCLUDED.knowledge_base_id
        """,
        (space_id, space_key, space_name, knowledge_base_id),
    )
    conn.commit()
    cur.close()
    conn.close()


def get_all_space_keys() -> list:
    """获取所有已记录的 space_key 列表"""
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT space_key FROM knowledge_bases")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [row["space_key"] for row in rows]


def get_synced_page_ids_by_space() -> dict:
    """
    按 space_key 分组获取所有已同步的 page_id。
    返回: {space_key: [page_id, page_id, ...], ...}
    """
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT kb.space_key, pm.confluence_page_id
        FROM page_mappings pm
        JOIN knowledge_bases kb ON pm.knowledge_base_id = kb.knowledge_base_id
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    result = {}
    for row in rows:
        sk = row["space_key"]
        if sk not in result:
            result[sk] = []
        result[sk].append(row["confluence_page_id"])
    return result


# ─────────────────────────────────────────────
# page_mappings 表操作（page → knowledge）
# ─────────────────────────────────────────────

def get_page_mapping(confluence_page_id: str) -> dict | None:
    """
    根据 Confluence page ID 查询已有的 WeKnora 映射。
    返回 dict 包含 knowledge_base_id, knowledge_id, page_title, last_modified 等，
    若不存在返回 None。
    """
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT * FROM page_mappings WHERE confluence_page_id = %s",
        (confluence_page_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None


def save_page_mapping(
    confluence_page_id: str,
    knowledge_base_id: str,
    knowledge_id: str,
    page_title: str,
    last_modified: str = "",
):
    """保存或更新 page → knowledge 映射（含 last_modified）"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO page_mappings
            (confluence_page_id, knowledge_base_id, knowledge_id, page_title, last_modified, uploaded_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT (confluence_page_id) DO UPDATE
            SET knowledge_base_id = EXCLUDED.knowledge_base_id,
                knowledge_id      = EXCLUDED.knowledge_id,
                page_title        = EXCLUDED.page_title,
                last_modified     = EXCLUDED.last_modified,
                uploaded_at       = NOW()
        """,
        (confluence_page_id, knowledge_base_id, knowledge_id, page_title, last_modified),
    )
    conn.commit()
    cur.close()
    conn.close()


def delete_page_mapping(confluence_page_id: str) -> dict | None:
    """
    删除 page 映射记录，返回被删除的记录（供调用方先删除 WeKnora 中的知识）。
    若不存在返回 None。
    """
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "DELETE FROM page_mappings WHERE confluence_page_id = %s RETURNING *",
        (confluence_page_id,),
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return dict(row) if row else None
