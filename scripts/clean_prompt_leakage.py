"""清理 Neo4j 中的 prompt leakage 污染数据（一次性修复脚本）

背景:
    旧版 episode_writer._build_extended_body() 把 "ENTITY RESOLUTION RULES" /
    "CANONICAL ENTITY NAMES" 等指令文本追加到 episode_body 末尾，导致
    graphiti-core 的 LLM 在实体抽取时把指令文本当作实体数据，写入节点属性
    （典型症状: contact / summary 等属性出现 "Please wait We are optimizing
    your request..." 或 "ENTITY RESOLUTION RULES..." 文本）。

修复方式:
    扫描所有节点的字符串属性，凡包含泄漏标记的，从第一个标记位置截断:
    - 截断后仍有正文 → SET 为截断后的文本
    - 截断后为空     → REMOVE 该属性（纯污染数据）

    脚本幂等: 截断后属性中不再包含任何标记，重跑将匹配 0 条记录。

用法:
    NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=*** \
        python scripts/clean_prompt_leakage.py --dry-run   # 仅预览，不写库
    NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=*** \
        python scripts/clean_prompt_leakage.py             # 实际修复
"""
from __future__ import annotations

import argparse
import os
import re

from neo4j import GraphDatabase

URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
AUTH = (os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "password"))

# 泄漏标记（与旧版 _build_extended_body 追加的指令文本对应）。
# "Please wait..." 来自前端占位文本混入抓取内容后被当作实体数据提取的场景；
# 其余变体来自 LLM 把指令/推理过程整段写入节点属性的实际污染样本。
LEAK_MARKERS_PATTERN = (
    r"\[END OF CONTENT\]"
    r"|ENTITY RESOLUTION RULES"
    r"|CANONICAL ENTITY NAMES"
    r"|ENTITY NAME LANGUAGE RULE"
    r"|Use the canonical names listed below"
    r"|Do NOT add or remove suffixes"
    r"|Please wait,?\s*We are optimizing your request"
    r"|\(Wait, let.s refine"
    r"|Policy Check - Updated from Messages"
    r"|Policy/Act\u201d"
)

_PY_MARKER_RE = re.compile(LEAK_MARKERS_PATTERN, re.IGNORECASE)

# Neo4j 正则（Java 风格）: (?s) 使 . 匹配换行。
# 注意: Cypher 字符串用双引号包裹，模式内不得出现双引号。
_FIND_QUERY = f"""
MATCH (n)
WHERE any(key IN keys(n) WHERE n[key] IS NOT NULL AND n[key] =~ "(?s).*({LEAK_MARKERS_PATTERN}).*")
RETURN elementId(n) AS eid, labels(n) AS labels, properties(n) AS props
"""

_SET_QUERY = """
UNWIND $items AS item
MATCH (n) WHERE elementId(n) = item.eid
SET n[item.key] = item.value
"""

_REMOVE_QUERY = """
UNWIND $items AS item
MATCH (n) WHERE elementId(n) = item.eid
REMOVE n[item.key]
"""

_VERIFY_QUERY = f"""
MATCH (n)
WHERE any(key IN keys(n) WHERE n[key] IS NOT NULL AND n[key] =~ "(?s).*({LEAK_MARKERS_PATTERN}).*")
RETURN count(n) AS remaining
"""


def truncate_leaked(value: str) -> str:
    """从第一个泄漏标记位置截断；无标记则原样返回。

    截断后额外剥掉首尾残留的引号/空白碎片（如 `"` / `'`），
    使纯污染值归一为空串 → 上层据此 REMOVE 属性。
    """
    match = _PY_MARKER_RE.search(value)
    if not match:
        return value
    return value[: match.start()].strip(" \t\r\n\"'")


def build_plan(session) -> list[dict]:
    """扫描污染节点，返回修复计划 [{eid, key, labels, old, new}]。

    new 为 None 表示应 REMOVE 该属性（截断后为空）。
    """
    plan: list[dict] = []
    for record in session.run(_FIND_QUERY):
        eid = record["eid"]
        labels = record["labels"]
        props = record["props"]
        for key, value in props.items():
            if not isinstance(value, str) or not _PY_MARKER_RE.search(value):
                continue
            cleaned = truncate_leaked(value)
            plan.append(
                {
                    "eid": eid,
                    "key": key,
                    "labels": labels,
                    "old": value,
                    "new": cleaned if cleaned else None,
                }
            )
    return plan


def apply_plan(tx, plan: list[dict]) -> dict:
    """在一个写事务中应用修复计划，返回 {set, removed} 计数。"""
    set_items = [
        {"eid": p["eid"], "key": p["key"], "value": p["new"]}
        for p in plan
        if p["new"] is not None
    ]
    remove_items = [
        {"eid": p["eid"], "key": p["key"]} for p in plan if p["new"] is None
    ]
    if set_items:
        tx.run(_SET_QUERY, items=set_items)
    if remove_items:
        tx.run(_REMOVE_QUERY, items=remove_items)
    return {"set": len(set_items), "removed": len(remove_items)}


def _print_plan_sample(plan: list[dict], sample_limit: int = 5) -> None:
    """打印修复计划摘要与前若干条样例（属性值截断展示，避免刷屏）。"""
    affected_nodes = {p["eid"] for p in plan}
    remove_count = sum(1 for p in plan if p["new"] is None)
    print(
        f"Found {len(affected_nodes)} polluted node(s), "
        f"{len(plan)} polluted propertie(s) "
        f"({len(plan) - remove_count} to truncate, {remove_count} to remove)"
    )
    for item in plan[:sample_limit]:
        old_preview = item["old"].replace("\n", "\\n")
        if len(old_preview) > 120:
            old_preview = old_preview[:120] + "…"
        action = "REMOVE" if item["new"] is None else "TRUNCATE"
        print(
            f"  [{action}] {item['labels']} {item['key']!r}: {old_preview}"
        )
    if len(plan) > sample_limit:
        print(f"  ... and {len(plan) - sample_limit} more")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅扫描并打印修复计划，不写库",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=5,
        help="打印样例条数（默认 5）",
    )
    args = parser.parse_args()

    with GraphDatabase.driver(URI, auth=AUTH) as driver:
        with driver.session() as session:
            plan = build_plan(session)
            if not plan:
                print("No polluted data found — nothing to do.")
                return 0

            _print_plan_sample(plan, args.sample_limit)

            if args.dry_run:
                print("DRY-RUN: no changes written.")
                return 0

            counts = session.execute_write(apply_plan, plan)
            print(
                f"Fixed: {counts['set']} propertie(s) truncated, "
                f"{counts['removed']} propertie(s) removed."
            )

            remaining = session.run(_VERIFY_QUERY).single()["remaining"]
            print(f"Verify: remaining polluted nodes={remaining}")
            if remaining:
                print("WARNING: pollution still present — rerun the script.")
                return 1
            print("OK: no prompt-leakage markers left in Neo4j")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
