"""
主入口脚本 - Confluence → WeKnora 自动同步
用法:
  python main.py -p <页面ID或URL>    # 爬取指定页面并同步到 WeKnora
  python main.py --sync              # 增量同步：获取自上次同步以来的所有变更
  python main.py --pull              # 拉取同步：获取最近50条，按 last_modified 比对同步
"""

import os
import sys
import argparse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(override=True)

from confluence_crawler import download_single_page, get_recently_changed_pages, get_latest_pages, get_trashed_pages_by_space
from weknora_client import create_knowledge_base, upload_file, delete_knowledge
from db import (
    init_db, get_kb_by_space, save_kb_mapping,
    get_page_mapping, save_page_mapping, delete_page_mapping,
    get_all_space_keys,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Confluence → WeKnora 自动同步工具"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "-p", "--page",
        type=str,
        help="指定页面（页面 ID 或 URL）"
    )
    group.add_argument(
        "--sync",
        action="store_true",
        help="增量同步：获取自 LAST_SYNC_TIME 以来的所有变更（含删除）"
    )
    group.add_argument(
        "--pull",
        action="store_true",
        help="拉取同步：获取最近50条页面，按 last_modified 比对后同步"
    )
    return parser.parse_args()


def ensure_knowledge_base(space_id: int, space_key: str, space_name: str) -> str:
    """
    确保 Confluence space 在 WeKnora 中有对应的知识库。
    如果 DB 中已有映射则直接返回 knowledge_base_id，
    否则创建新知识库并保存映射。
    """
    existing = get_kb_by_space(space_id)
    if existing:
        kb_id = existing["knowledge_base_id"]
        print(f"[映射] Space '{space_key}' (ID:{space_id}) 已有知识库: {kb_id}")
        return kb_id

    print(f"[新建] Space '{space_key}' (ID:{space_id}, {space_name}) 无对应知识库，正在创建...")
    result = create_knowledge_base(
        name=f"Confluence - {space_name}",
        description=f"从 Confluence Space '{space_key}' 自动同步",
    )
    kb_id = result.get("id", "")
    if not kb_id:
        raise RuntimeError(f"创建知识库失败，响应: {result}")

    save_kb_mapping(space_id, space_key, space_name, kb_id)
    print(f"[映射] 已保存: space {space_id} → kb {kb_id}")
    return kb_id


def sync_page_to_weknora(page_result: dict):
    """
    将单个爬取页面同步到 WeKnora：
    1. 确保 space 有对应知识库
    2. 检查是否已有旧映射，有则先删除
    3. 上传 PDF 到知识库
    4. 保存映射关系
    """
    page_id = page_result["page_id"]
    page_title = page_result["title"]
    space_id = page_result.get("space_id", 0)
    space_key = page_result.get("space_key", "")
    space_name = page_result.get("space", "未知空间")
    pdf_path = page_result.get("pdf_path", "")

    if not pdf_path:
        print(f"[跳过] 页面 '{page_title}' (ID: {page_id}) 无 PDF 文件")
        return

    print(f"\n{'='*50}")
    print(f"[同步] 页面: {page_title} (ID: {page_id})")
    print(f"{'='*50}")

    # 1. 确保知识库存在
    kb_id = ensure_knowledge_base(space_id, space_key, space_name)

    # 2. 检查是否已有旧映射，有则先删除 WeKnora 中的旧知识
    old_mapping = get_page_mapping(page_id)
    if old_mapping:
        old_kb_id = old_mapping["knowledge_base_id"]
        old_knowledge_id = old_mapping["knowledge_id"]
        print(f"[更新] 发现旧映射: knowledge_id={old_knowledge_id}，正在删除旧知识...")
        try:
            delete_knowledge(old_kb_id, old_knowledge_id)
            print(f"[更新] 旧知识已删除: knowledge_id={old_knowledge_id}")
            # WeKnora 删除成功后才删 DB 记录，避免产生孤儿数据
            delete_page_mapping(page_id)
        except Exception as e:
            print(f"[警告] 删除旧知识失败: {e}，保留 DB 记录等待下次重试")

    # 3. 上传 PDF 到 WeKnora
    print(f"[上传] 正在上传 PDF 到知识库 {kb_id}...")
    try:
        upload_result = upload_file(kb_id, pdf_path)
    except Exception as e:
        print(f"[错误] 上传失败: {e}")
        return

    knowledge_id = upload_result.get("id") or upload_result.get("knowledge_id") or ""
    if not knowledge_id:
        print(f"[警告] 上传成功但未返回 knowledge_id，响应: {upload_result}")
        knowledge_id = upload_result.get("file_name", "")

    # 4. 保存映射关系
    save_page_mapping(page_id, kb_id, knowledge_id, page_title,
                      last_modified=page_result.get("last_modified", ""))
    print(f"[完成] 映射已保存: page {page_id} → knowledge {knowledge_id}")

    # 5. 同步附件
    for att in page_result.get("attachments", []):
        att_path = att.get("local_path", "")
        if not att_path:
            continue
        att_name = att.get("filename", "")
        print(f"[附件] 正在上传附件: {att_name}")
        try:
            att_result = upload_file(kb_id, att_path)
            att_knowledge_id = att_result.get("id") or att_result.get("knowledge_id") or ""
            print(f"[附件] {att_name} 上传成功: knowledge_id={att_knowledge_id}")
        except Exception as e:
            print(f"[附件] {att_name} 上传失败: {e}")


def handle_trashed_page(page_info: dict) -> bool:
    """
    处理已删除（trashed）的页面：
    从 WeKnora 删除知识，从 DB 删除映射。
    返回 True 表示成功处理。
    """
    page_id = page_info["id"]
    page_title = page_info.get("title", "")

    mapping = get_page_mapping(page_id)
    if not mapping:
        print(f"[跳过] 页面 '{page_title}' (ID: {page_id}) 已在回收站，但 DB 中无映射")
        return True

    kb_id = mapping["knowledge_base_id"]
    knowledge_id = mapping["knowledge_id"]

    print(f"\n[删除] 页面 '{page_title}' (ID: {page_id}) 已在 Confluence 回收站")
    print(f"  WeKnora knowledge_id: {knowledge_id}")

    # 1. 从 WeKnora 删除知识
    try:
        delete_knowledge(kb_id, knowledge_id)
        print(f"  [WeKnora] 知识已删除")
    except Exception as e:
        print(f"  [WeKnora] 删除失败: {e}")

    # 2. 从 DB 删除映射
    delete_page_mapping(page_id)
    print(f"  [DB] 映射已删除")
    return True


def update_last_sync_time():
    """更新 .env 中的 LAST_SYNC_TIME 为当前时间"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

    lines = []
    found = False
    with open(env_path, "r") as f:
        for line in f:
            if line.startswith("LAST_SYNC_TIME="):
                lines.append(f"LAST_SYNC_TIME={now}\n")
                found = True
            else:
                lines.append(line)

    if not found:
        lines.append(f"\nLAST_SYNC_TIME={now}\n")

    with open(env_path, "w") as f:
        f.writelines(lines)

    print(f"[同步] LAST_SYNC_TIME 已更新为: {now}")


def run_sync():
    """
    增量同步模式：
    1. 读取 LAST_SYNC_TIME
    2. 查询 Confluence 自该时间以来的所有变更（含 trashed）
    3. 对 current 页面：下载并同步到 WeKnora
    4. 对 trashed 页面：从 WeKnora 和 DB 中删除
    5. 更新 LAST_SYNC_TIME
    """
    last_sync = os.getenv("LAST_SYNC_TIME", "").strip()
    if not last_sync:
        print("[ERROR] LAST_SYNC_TIME 未配置！")
        print("  请在 .env 中设置，例如: LAST_SYNC_TIME=2026-07-15 00:00")
        sys.exit(1)

    print(f"[同步] 上次同步时间: {last_sync}")

    # 1. 查询变更页面
    try:
        changed_pages = get_recently_changed_pages(since=last_sync)
    except Exception as e:
        print(f"[ERROR] 查询变更失败: {e}")
        sys.exit(1)

    if not changed_pages:
        print("[INFO] 自上次同步以来没有页面变更，退出。")
        return

    # 2. 分类处理
    trashed = [p for p in changed_pages if p["status"] == "trashed"]
    current = [p for p in changed_pages if p["status"] == "current"]

    # 2a. 先处理删除
    if trashed:
        print(f"\n{'#'*60}")
        print(f"  处理 {len(trashed)} 个已删除页面")
        print(f"{'#'*60}")
        for page_info in trashed:
            try:
                handle_trashed_page(page_info)
            except Exception as e:
                print(f"[ERROR] 处理删除页面 '{page_info.get('title')}' 失败: {e}")

    # 2b. 再处理新增/更新
    if current:
        print(f"\n{'#'*60}")
        print(f"  同步 {len(current)} 个活跃页面")
        print(f"{'#'*60}")
        success_count = 0
        for page_info in current:
            page_id = page_info["id"]
            try:
                results = download_single_page(page_id)
                for result in results:
                    sync_page_to_weknora(result)
                success_count += 1
            except Exception as e:
                print(f"[ERROR] 同步页面 '{page_info.get('title')}' (ID: {page_id}) 失败: {e}")

        print(f"\n[同步] 活跃页面同步完成: {success_count}/{len(current)}")

    # 3. 更新 LAST_SYNC_TIME
    update_last_sync_time()

    print(f"\n{'='*60}")
    print(f"  增量同步完成")
    print(f"  - 已删除: {len(trashed)} 个页面")
    print(f"  - 已同步: {len(current)} 个页面")
    print(f"{'='*60}")


def cleanup_trashed_pages():
    """
    清理回收站中的页面：
    遍历 DB 中所有 space_key，查询 Confluence 回收站，
    若 DB 中有映射的页面已在回收站，则从 WeKnora 和 DB 中同步删除。
    """
    space_keys = get_all_space_keys()
    if not space_keys:
        print("[清理] DB 中无已记录的 space，跳过回收站清理")
        return 0

    total_deleted = 0

    for space_key in space_keys:
        print(f"\n[清理] 正在检查 space '{space_key}' 的回收站...")
        try:
            trashed_pages = get_trashed_pages_by_space(space_key)
        except Exception as e:
            print(f"  [错误] 查询 space '{space_key}' 回收站失败: {e}")
            continue

        if not trashed_pages:
            print(f"  [清理] space '{space_key}' 回收站为空")
            continue

        print(f"  [清理] 发现 {len(trashed_pages)} 个回收站页面")

        for page_info in trashed_pages:
            page_id = str(page_info["id"])
            page_title = page_info.get("title", "")

            # 查询 DB 中是否有该页面的映射
            mapping = get_page_mapping(page_id)
            if not mapping:
                # DB 中没有记录，无需处理
                continue

            kb_id = mapping["knowledge_base_id"]
            knowledge_id = mapping["knowledge_id"]

            print(f"  [删除] 页面 '{page_title}' (ID: {page_id}) 已在回收站，正在清理...")

            # 1. 从 WeKnora 删除知识
            # 2. WeKnora 成功后才删 DB，避免产生孤儿数据
            try:
                delete_knowledge(kb_id, knowledge_id)
                print(f"    [WeKnora] 知识已删除: knowledge_id={knowledge_id}")
                delete_page_mapping(page_id)
                print(f"    [DB] 映射已删除: page_id={page_id}")
                total_deleted += 1
            except Exception as e:
                print(f"    [WeKnora] 删除失败 (knowledge_id={knowledge_id}): {e}，保留 DB 记录等待下次重试")

    return total_deleted


def run_pull():
    """
    拉取同步模式：
    1. 先清理回收站：遍历所有 space，删除已进入回收站的页面
    2. 从 Confluence 获取最近 N 条页面（按 lastModified 倒序）
    3. 逐条比对：
       - 已在本轮同步过 → 跳过
       - DB 无记录 或 last_modified 不同 → 同步
       - last_modified 一致（且非本轮同步的）→ 已同步到此处，结束
    """
    # 第一步：清理回收站
    print(f"\n{'#'*60}")
    print(f"  第一步：回收站清理")
    print(f"{'#'*60}")
    deleted_count = cleanup_trashed_pages()
    print(f"\n[清理] 共清理 {deleted_count} 个回收站页面")

    # 第二步：拉取同步
    pull_limit = int(os.getenv("PULL_LIMIT", "50"))
    print(f"\n{'#'*60}")
    print(f"  第二步：拉取同步（最近 {pull_limit} 条）")
    print(f"{'#'*60}")
    print(f"[拉取] 正在获取 Confluence 最近 {pull_limit} 条页面...")
    try:
        latest_pages = get_latest_pages(limit=pull_limit)
    except Exception as e:
        print(f"[ERROR] 查询失败: {e}")
        sys.exit(1)

    if not latest_pages:
        print("[INFO] 未获取到任何页面，退出。")
        return

    # 本轮实际同步过的 page ID（含递归子页面）
    synced_ids = set()

    for page_info in latest_pages:
        page_id = str(page_info["id"])
        page_title = page_info["title"]
        remote_last_modified = page_info["last_modified"]

        # 本轮已同步过（被递归子页面顺带同步了），跳过
        if page_id in synced_ids:
            print(f"\n[跳过] 页面 '{page_title}' (ID: {page_id}) 已在本轮同步过")
            continue

        # 比对 DB
        db_mapping = get_page_mapping(page_id)

        if db_mapping and db_mapping.get("last_modified") == remote_last_modified:
            # last_modified 一致，且不是本轮同步的，说明之前已同步到此处，结束
            print(f"\n[完成] 页面 '{page_title}' (ID: {page_id}) last_modified 一致，"
                  f"已同步到此处，提前结束。")
            break

        # 需要更新
        if db_mapping:
            print(f"\n[更新] 页面 '{page_title}' (ID: {page_id}) "
                  f"last_modified 已变更: {db_mapping.get('last_modified')} → {remote_last_modified}")
        else:
            print(f"\n[新增] 页面 '{page_title}' (ID: {page_id}) DB 中无记录，开始同步")

        try:
            results = download_single_page(page_id)
            for result in results:
                result_id = str(result.get("page_id", ""))
                sync_page_to_weknora(result)
                synced_ids.add(result_id)
        except Exception as e:
            print(f"[ERROR] 同步页面 '{page_title}' (ID: {page_id}) 失败: {e}")

    print(f"\n{'='*60}")
    print(f"  拉取同步完成")
    print(f"  - 回收站清理: {deleted_count} 个页面已删除")
    print(f"  - 新增/更新: 共同步 {len(synced_ids)} 个页面")
    print(f"{'='*60}")


def main():
    args = parse_args()

    print("=" * 60)
    print("  Confluence → WeKnora 自动同步工具")
    print("=" * 60)
    print()

    # 初始化数据库
    print("[初始化] 正在初始化数据库...")
    try:
        init_db()
    except Exception as e:
        print(f"[ERROR] 数据库初始化失败: {e}")
        sys.exit(1)

    if args.sync:
        # 增量同步模式
        run_sync()
    elif args.pull:
        # 拉取同步模式
        run_pull()
    else:
        # 单页面模式
        try:
            print(f"\n[爬取] 页面: {args.page}\n")
            results = download_single_page(args.page)
        except Exception as e:
            print(f"[ERROR] 爬取失败: {e}")
            sys.exit(1)

        if not results:
            print("[INFO] 未获取到任何页面，退出。")
            return

        print(f"\n{'#'*60}")
        print(f"  开始同步 {len(results)} 个页面到 WeKnora")
        print(f"{'#'*60}")

        success_count = 0
        for page_result in results:
            try:
                sync_page_to_weknora(page_result)
                success_count += 1
            except Exception as e:
                print(f"[ERROR] 同步页面 '{page_result.get('title')}' 失败: {e}")

        print(f"\n{'='*60}")
        print(f"  同步完成: {success_count}/{len(results)} 个页面成功")
        print(f"{'='*60}")


if __name__ == "__main__":
    main()

