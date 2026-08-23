"""清洗 Neo4j 中的控制字符（tab \\x09, backspace \\x08, etc.）

一次性修复脚本：遍历所有节点与关系，将字符串属性中的 C0 控制字符
（\\x00-\\x08, \\x0B, \\x0C, \\x0E-\\x1F；保留 \\n \\r \\t）移除。

与 src/graphiti/episode_writer.py 中的 _clean_text 过滤钩子共用同一规则，
防止未来写入时再次污染。

用法:
    NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=xxx \
        python scripts/clean_control_chars.py
"""
from __future__ import annotations

import os

from neo4j import GraphDatabase

URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
AUTH = (os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "password"))

# 保留 \t(0x09) \n(0x0A) \r(0x0D)，其余 C0 控制字符全部移除
_PATTERN = r".*[\x00-\x08\x0B\x0C\x0E-\x1F].*"
_CTRL_CHARS = [chr(c) for c in range(0x00, 0x20) if c not in (0x09, 0x0A, 0x0D)]

# 用 n[k] =~ $pattern 直接匹配字符串属性（regex 对非字符串返回 null，天然类型安全），
# 避免 toString() 把 list/int 也纳入匹配后 SET 成字符串导致属性类型被破坏。
_NODE_CLEAN_QUERY = """
MATCH (n)
WHERE any(key IN keys(n) WHERE n[key] =~ $pattern)
WITH n, keys(n) AS ks
UNWIND ks AS k
WITH n, k
WHERE n[k] =~ $pattern
SET n[k] = reduce(s = n[k], c IN $ctrl_chars | replace(s, c, ''))
RETURN count(DISTINCT n) AS cleaned
"""

_REL_CLEAN_QUERY = """
MATCH ()-[r]->()
WHERE any(key IN keys(r) WHERE r[key] =~ $pattern)
WITH r, keys(r) AS ks
UNWIND ks AS k
WITH r, k
WHERE r[k] =~ $pattern
SET r[k] = reduce(s = r[k], c IN $ctrl_chars | replace(s, c, ''))
RETURN count(DISTINCT r) AS cleaned
"""

_VERIFY_NODE_QUERY = """
MATCH (n)
WHERE any(key IN keys(n) WHERE n[key] =~ $pattern)
RETURN count(n) AS remaining
"""

_VERIFY_REL_QUERY = """
MATCH ()-[r]->()
WHERE any(key IN keys(r) WHERE r[key] =~ $pattern)
RETURN count(r) AS remaining
"""


def clean(tx) -> dict:
    """在一个写事务中清洗所有节点与关系，返回各类清洗计数。"""
    node_result = tx.run(_NODE_CLEAN_QUERY, pattern=_PATTERN, ctrl_chars=_CTRL_CHARS).single()
    rel_result = tx.run(_REL_CLEAN_QUERY, pattern=_PATTERN, ctrl_chars=_CTRL_CHARS).single()
    return {
        "nodes": node_result["cleaned"] if node_result else 0,
        "relationships": rel_result["cleaned"] if rel_result else 0,
    }


def main() -> int:
    with GraphDatabase.driver(URI, auth=AUTH) as driver:
        with driver.session() as session:
            counts = session.execute_write(clean)
            print(
                f"Cleaned {counts['nodes']} nodes, "
                f"{counts['relationships']} relationships with control characters"
            )

            # 验证：清洗后不应再有残留
            remaining_nodes = session.run(
                _VERIFY_NODE_QUERY, pattern=_PATTERN
            ).single()["remaining"]
            remaining_rels = session.run(
                _VERIFY_REL_QUERY, pattern=_PATTERN
            ).single()["remaining"]
            print(
                f"Verify: remaining control-char nodes={remaining_nodes}, "
                f"relationships={remaining_rels}"
            )
            if remaining_nodes or remaining_rels:
                print("WARNING: control characters still present!")
                return 1
            print("OK: no control characters left in Neo4j")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
