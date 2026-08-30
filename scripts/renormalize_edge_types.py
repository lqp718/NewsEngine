"""历史数据重归一脚本：把 name='RELATES_TO' 的边按 fact 推断更具体的核心类型。

背景:
    历史写入中，LLM 产出的非核心关系类型（如 SUBSIDIARY_OF / CEO_OF /
    TRACKS / REPORTED）被旧版 normalize_edge_type() 兜底归一成通用
    RELATES_TO（约 150 条）。这些边的 fact 描述 100% 完整，仅类型缺失。
    本脚本基于 fact 关键词匹配（方案 A，零 LLM 成本）推断遗留类型名，
    再调用 normalize_edge_type() 归一到核心集，最后回写 Neo4j。

关键词规则（大小写不敏感，顺序即优先级）:
    1. 归属/所有权 (→ SUBSIDIARY_OF / OWNED_BY → PART_OF):
       subsidiary / parent / "owned by" / "owns the" / "part of" /
       wholly-owned / "a unit of"
    2. 主体参与/任职 (→ CEO_OF → INVOLVES):
       CEO / chairman / chair / president / director / analyst / ...
       以及 fact 直接出现 involve(s)
    3. 其余（含 tracks / reports / reported / states）保持 RELATES_TO
       —— 这些确实是通用关联，不做迁移。

    注意两类已收紧的泛化模式（CR 修复）:
    - "became" 必须带角色上下文（became the CEO/chairman/president/...），
      否则新闻高频状态变化句式 "X became the largest/top/first ..." 会被
      误归 INVOLVES（单向迁移不可自愈）
    - "partners" 限定名词语境（partnership / partner at|of|in），
      动词用法 "X partners with Y" 不再命中

两个阶段:
    阶段 1（迁移）: 处理 name='RELATES_TO' 的边，按 fact 推断更具体核心类型。
    阶段 2（修正）: 修正上一轮迁移中由过宽模式（裸 \\bbecame\\b /
      \\bpartners?\\b）误标为 INVOLVES 的边 —— 对这些 fact 用收紧后的规则
      重新推断；若不再是 INVOLVES 则改回正确类型（通常是 RELATES_TO）。

幂等性:
    阶段 1 只处理 name='RELATES_TO' 的边；迁移后 name 变为具体核心类型，
    重复运行不会再命中。阶段 2 只处理 name='INVOLVES' 且 fact 含
    became/partners 的边；修正后 name 改变，重跑不再命中。两次运行后
    两阶段 updated 均为 0。

用法:
    # 干跑（只统计，不写库）
    python scripts/renormalize_edge_types.py --dry-run

    # 实际执行（免交互确认，供自动化使用）
    python scripts/renormalize_edge_types.py --yes

    # 实际执行（交互式确认）
    python scripts/renormalize_edge_types.py

Neo4j 连接参数复用 src/core/config.py 的 settings（读取项目根 .env）。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

# 保证可从项目根导入 src.*（脚本位于 scripts/ 下）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.graphiti.episode_writer import normalize_edge_type  # noqa: E402


# ── fact 关键词 → 遗留类型名（再经 normalize_edge_type 归一） ──────────
#
# 每个模式用 (正则, 遗留类型名) 表示；正则均已做词边界/短语约束，
# 避免子串误伤（如 \bdirector\b 不会命中 "Directorate"）。

_PART_OF_PATTERNS: list[tuple[str, str]] = [
    # \b + (?:y|ies) 词边界约束，避免 "subsidiar" 裸前缀误伤（CR #2）
    (r"\bsubsidiar(?:y|ies)\b", "SUBSIDIARY_OF"),
    (r"\bparent\s+(company|firm|group)\b", "PARENT_OF"),
    (r"\bowned\s+by\b", "OWNED_BY"),
    (r"\bowns\s+the\b", "OWNED_BY"),            # X owns the Y project/operation
    (r"\bwholly[- ]?owned\b", "OWNED_BY"),
    (r"\ba\s+unit\s+of\b", "SUBSIDIARY_OF"),
    (r"\bpart\s+of\b", "PART_OF"),
]

# 角色词表: became 收紧模式与任职模式共用（CR #1 — "became" 必须带角色
# 上下文，否则 "X became the largest/top/first ..." 状态变化句式被误归）
_ROLE_WORDS = (
    "ceo|chief\\s+executive|chairman|chairwoman|chair(?:person)?|president|"
    "chief|head|directors?|ministers?|governors?|premiers?|secretary|"
    "spokespersons?|founders?|owners?|partners?|analysts?|economists?|"
    "professors?|strategists?|fellows?|anchors?|experts?"
)

_INVOLVES_PATTERNS: list[tuple[str, str]] = [
    (r"\binvolv(es|ed|ing)?\b", "INVOLVES"),    # fact 直述 involves
    (r"\bceo\b", "CEO_OF"),
    (r"\bchief\s+executive\b", "CEO_OF"),
    (r"\bchief\b", "CEO_OF"),                     # chief of / chief owner / CFO
    (r"chairman|chairwoman|\bco-?chairs?\b|\bchair\b", "CHAIRMAN_OF"),
    (r"\bpresident\b", "CEO_OF"),
    (r"\bdirectors?\b", "CEO_OF"),                # \b 防止命中 Directorate
    (r"\banalysts?\b", "CEO_OF"),
    (r"\beconomists?\b", "CEO_OF"),
    (r"\bprofessors?\b", "CEO_OF"),
    (r"\bfounders?\b", "CEO_OF"),
    (r"\bjoined\b", "EMPLOYED_BY"),
    # became 收紧为角色上下文（CR #1）: 仅命中 "became (the) CEO/chairman/..."
    # 状态变化句式 "became the largest/top/first ..." 不再命中
    (
        r"\bbecame\s+(the\s+)?(?:world'?s\s+)?"
        rf"(?:{_ROLE_WORDS})\b",
        "CEO_OF",
    ),
    (r"\bappoint(ed|ment)?\b", "CEO_OF"),
    (r"\bministers?\b", "CEO_OF"),
    (r"\bsecretary\b", "CEO_OF"),
    (r"\bgovernors?\b", "CEO_OF"),
    (r"\bpremiers?\b", "CEO_OF"),
    (r"\bspokespersons?\b", "CEO_OF"),
    (r"\bemployed\b", "EMPLOYED_BY"),
    (r"\bworks?\s+for\b", "WORKS_FOR"),
    (r"\bhead\s+of\b", "CEO_OF"),
    (r"\bmanaging\s+director\b", "CEO_OF"),
    (r"\bfellows?\b", "CEO_OF"),
    # partners 限定名词语境（CR #3）: partnership / partner at|of|in；
    # 动词用法 "X partners with Y" 不再命中
    (r"\bpartnership\b|\bpartners?\s+(at|of|in)\b", "CEO_OF"),
    (r"\bowners?\b", "CEO_OF"),
    (r"\bserv(es|ed)\s+as\b", "CEO_OF"),
    (r"\bstrategists?\b", "CEO_OF"),
    (r"\banchors?\b", "CEO_OF"),
    (r"\bexperts?\b", "CEO_OF"),
    (r"\bleadership\s+roles?\b", "CEO_OF"),
]

# 上一轮迁移中的过宽模式（裸 became / partners）。阶段 2 据此圈定需要
# 复核的 INVOLVES 边 —— 只有这些 fact 才可能由旧规则误标。
_MISLABELED_FACT_RE = re.compile(r"\bbecame\b|\bpartners?\b", re.IGNORECASE)


def infer_legacy_edge_type(fact: str | None) -> str | None:
    """从 fact 文本推断遗留类型名；无法推断时返回 None（保持 RELATES_TO）。

    归属关系（PART_OF 系）优先于任职/参与关系（INVOLVES 系），
    因结构性归属语义更具体；两者同时命中时取归属。
    """
    if not fact:
        return None
    text = fact.lower()
    for pattern, legacy in _PART_OF_PATTERNS:
        if re.search(pattern, text):
            return legacy
    for pattern, legacy in _INVOLVES_PATTERNS:
        if re.search(pattern, text):
            return legacy
    return None


def core_type_for_fact(fact: str | None) -> str:
    """fact → 核心类型（收紧规则推断 + 归一）；无法推断时返回 RELATES_TO。"""
    legacy = infer_legacy_edge_type(fact)
    if legacy is None:
        return "RELATES_TO"
    return normalize_edge_type(legacy)


def build_migration_plan(
    edges: list[tuple[str, str | None]],
) -> tuple[list[dict], Counter, int, dict]:
    """阶段 1: 对 name='RELATES_TO' 的边 (uuid, fact) 构建迁移计划。

    返回 (updates, transitions, kept, samples)。纯函数，便于单测。
    """
    transitions: Counter = Counter()
    samples: dict[str, list[str]] = {}
    updates: list[dict] = []
    kept = 0
    for uuid, fact in edges:
        core = core_type_for_fact(fact)
        if core == "RELATES_TO":
            kept += 1
            continue
        legacy = infer_legacy_edge_type(fact)
        transitions[(legacy, core)] += 1
        samples.setdefault(core, [])
        if len(samples[core]) < 3:
            samples[core].append(f"{uuid[:12]}: {(fact or '')[:90]}")
        updates.append({"uuid": uuid, "name": core})
    return updates, transitions, kept, samples


def build_fix_plan(edges: list[tuple[str, str | None]]) -> list[dict]:
    """阶段 2: 修正上一轮过宽模式误标为 INVOLVES 的边。

    入参为 name='INVOLVES' 的边 (uuid, fact)。只复核 fact 命中旧过宽模式
    （became/partners）的边；用收紧后的规则重新归一，若结果不再是
    INVOLVES 则生成修正更新（通常是改回 RELATES_TO）。

    幂等: 修正后 name != 'INVOLVES'，重跑不会再选中这些边；对仍是
    INVOLVES 的边（如 "became the CEO" 确实是任职）不产生更新。
    """
    updates: list[dict] = []
    for uuid, fact in edges:
        if not fact or not _MISLABELED_FACT_RE.search(fact):
            continue
        core = core_type_for_fact(fact)
        if core != "INVOLVES":
            updates.append({"uuid": uuid, "name": core})
    return updates


# ── Neo4j 查询 ──────────────────────────────────────────────────────────

_COUNT_BY_NAME_QUERY = """
MATCH ()-[r:RELATES_TO]->()
RETURN r.name AS name, count(*) AS cnt
"""

_FETCH_RELATES_TO_QUERY = """
MATCH ()-[r:RELATES_TO {name: 'RELATES_TO'}]->()
RETURN r.uuid AS uuid, r.fact AS fact
"""

_FETCH_INVOLVES_QUERY = """
MATCH ()-[r:RELATES_TO {name: 'INVOLVES'}]->()
RETURN r.uuid AS uuid, r.fact AS fact
"""

_UPDATE_FROM_RELATES_TO_QUERY = """
UNWIND $batch AS item
MATCH ()-[r:RELATES_TO {uuid: item.uuid, name: 'RELATES_TO'}]->()
SET r.name = item.name
RETURN count(r) AS updated
"""

_UPDATE_FROM_INVOLVES_QUERY = """
UNWIND $batch AS item
MATCH ()-[r:RELATES_TO {uuid: item.uuid, name: 'INVOLVES'}]->()
SET r.name = item.name
RETURN count(r) AS updated
"""


def _apply_updates(driver, query: str, updates: list[dict], batch_size: int = 200) -> int:
    """分批回写，返回实际更新条数。"""
    total = 0
    for i in range(0, len(updates), batch_size):
        batch = updates[i : i + batch_size]
        result = driver.execute_query(query, batch=batch)
        total += result.records[0]["updated"]
    return total


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 name='RELATES_TO' 的历史边按 fact 重归一到更具体的核心类型，"
        "并修正上一轮过宽模式误标为 INVOLVES 的边"
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过交互确认直接写库（防误操作：默认需要确认）",
    )
    args = parser.parse_args()

    from neo4j import GraphDatabase

    from src.core.config import get_settings

    settings = get_settings()

    try:
        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001 — 友好提示而非裸 traceback
        print(
            f"错误: 无法连接 Neo4j（{settings.neo4j_uri}）: {exc}\n"
            "请检查 .env 中 NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD 配置，"
            "并确认 Neo4j 服务已启动。",
            file=sys.stderr,
        )
        return 1

    try:
        with driver:
            # ── 归一前统计 ──
            before: Counter = Counter()
            result = driver.execute_query(_COUNT_BY_NAME_QUERY)
            for rec in result.records:
                before[rec["name"]] = rec["cnt"]

            print("=== 归一前 边 name 分布 ===")
            for name, cnt in sorted(before.items(), key=lambda kv: -kv[1]):
                print(f"  {name}: {cnt}")

            # ── 阶段 1: RELATES_TO 迁移计划 ──
            result = driver.execute_query(_FETCH_RELATES_TO_QUERY)
            relates_edges = [(rec["uuid"], rec["fact"]) for rec in result.records]
            print(f"\n[阶段 1] 待处理 RELATES_TO 边: {len(relates_edges)} 条")

            updates, transitions, kept, samples = build_migration_plan(relates_edges)

            print("=== 阶段 1 推断结果（遗留类型 → 核心类型） ===")
            for (legacy, core), cnt in sorted(
                transitions.items(), key=lambda kv: -kv[1]
            ):
                print(f"  {legacy} -> {core}: {cnt}")
            print(f"  保持 RELATES_TO: {kept}")
            print(f"  计划更新: {len(updates)} 条")
            for core, facts in samples.items():
                print(f"  [{core}] 样例:")
                for line in facts:
                    print(f"    - {line}")

            # ── 阶段 2: INVOLVES 误标修正计划 ──
            result = driver.execute_query(_FETCH_INVOLVES_QUERY)
            involves_edges = [(rec["uuid"], rec["fact"]) for rec in result.records]
            fixes = build_fix_plan(involves_edges)
            print(
                f"\n[阶段 2] 复核 name='INVOLVES' 边 {len(involves_edges)} 条"
                f"（仅 fact 含 became/partners 的旧过宽模式候选）"
            )
            print(f"  需修正: {len(fixes)} 条")
            for item in fixes[:10]:
                fact = next(
                    (f for u, f in involves_edges if u == item["uuid"]), ""
                )
                print(f"    - {item['uuid'][:12]}: -> {item['name']} | {fact[:80]}")

            if not updates and not fixes:
                print("\n无需更新（幂等，已是收敛状态）。")
                return 0

            if args.dry_run:
                print("\n[dry-run] 未写库。")
                return 0

            if not args.yes:
                answer = input(
                    f"即将写入 {len(updates) + len(fixes)} 条更新，确认执行？[y/N] "
                ).strip().lower()
                if answer not in ("y", "yes"):
                    print("已取消。")
                    return 0

            # ── 回写阶段 1（幂等: WHERE 含 name='RELATES_TO'） ──
            updated_total = _apply_updates(
                driver, _UPDATE_FROM_RELATES_TO_QUERY, updates
            )
            if updated_total != len(updates):
                print(
                    f"警告: 阶段 1 实际更新 {updated_total} 条 != 计划 {len(updates)} 条"
                    "（可能已被并发进程修改），请重跑 --dry-run 复核。",
                    file=sys.stderr,
                )

            # ── 回写阶段 2（幂等: WHERE 含 name='INVOLVES'） ──
            fixed_total = _apply_updates(
                driver, _UPDATE_FROM_INVOLVES_QUERY, fixes
            )
            if fixed_total != len(fixes):
                print(
                    f"警告: 阶段 2 实际修正 {fixed_total} 条 != 计划 {len(fixes)} 条"
                    "（可能已被并发进程修改），请重跑 --dry-run 复核。",
                    file=sys.stderr,
                )

            # ── 归一后统计 ──
            after: Counter = Counter()
            result = driver.execute_query(_COUNT_BY_NAME_QUERY)
            for rec in result.records:
                after[rec["name"]] = rec["cnt"]

            print("\n=== 归一后 边 name 分布 ===")
            for name, cnt in sorted(after.items(), key=lambda kv: -kv[1]):
                delta = cnt - before.get(name, 0)
                sign = f"+{delta}" if delta > 0 else str(delta)
                print(f"  {name}: {cnt} ({sign})")

            print(
                f"\n完成: 阶段 1 更新 {updated_total} 条，阶段 2 修正 {fixed_total} 条"
                "（幂等，可重复运行）。"
            )
            return 0
    except Exception as exc:  # noqa: BLE001
        print(f"错误: 执行失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
