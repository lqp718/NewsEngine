"""临时研究脚本: Graphiti 数据质量分析 (RELATES_TO / 孤立实体 / group 字段)。

用法: python scripts/_research_graph_quality.py [task]
task: edges | orphans | group | summary | all
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

driver = GraphDatabase.driver(
    os.getenv("NEO4J_URI", "bolt://localhost:7687"),
    auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "")),
)


def q(session, cypher, **params):
    return list(session.run(cypher, **params))


def analyze_edges(s):
    print("=== 1. 边 name 分布 ===")
    for rec in q(s, "MATCH ()-[r:RELATES_TO]->() RETURN r.name AS n, count(*) AS c ORDER BY c DESC LIMIT 15"):
        print(f"  {rec['n']!r}: {rec['c']}")

    print("\n=== 2. RELATES_TO fact 抽样 (30条, 含端点标签) ===")
    rows = q(s, """
        MATCH (a:Entity)-[r:RELATES_TO {name: 'RELATES_TO'}]->(b:Entity)
        RETURN labels(a) AS la, a.name AS an, r.fact AS fact, labels(b) AS lb, b.name AS bn
        LIMIT 30
    """)
    for i, rec in enumerate(rows, 1):
        print(f"  [{i}] {rec['an']} ({','.join(l for l in rec['la'] if l!='Entity')}) "
              f"-RELATES_TO-> {rec['bn']} ({','.join(l for l in rec['lb'] if l!='Entity')})")
        print(f"      fact: {rec['fact']}")

    print("\n=== 3. RELATES_TO 端点标签对分布 ===")
    for rec in q(s, """
        MATCH (a:Entity)-[r:RELATES_TO {name: 'RELATES_TO'}]->(b:Entity)
        WITH [l IN labels(a) WHERE l <> 'Entity'][0] AS la,
             [l IN labels(b) WHERE l <> 'Entity'][0] AS lb
        RETURN la, lb, count(*) AS c ORDER BY c DESC LIMIT 15
    """):
        print(f"  {rec['la']} -> {rec['lb']}: {rec['c']}")


def analyze_orphans(s):
    print("=== 4. 孤立实体统计 ===")
    rec = q(s, """
        MATCH (n:Entity)
        WHERE NOT (n)--(:Entity)
        RETURN count(n) AS c
    """)[0]
    print(f"  无任何与其他 Entity 相连的边: {rec['c']}")

    print("\n=== 5. 孤立实体按标签分布 ===")
    for rec in q(s, """
        MATCH (n:Entity) WHERE NOT (n)--(:Entity)
        WITH [l IN labels(n) WHERE l <> 'Entity'][0] AS lab
        RETURN lab, count(*) AS c ORDER BY c DESC
    """):
        print(f"  {rec['lab']}: {rec['c']}")

    print("\n=== 6. 孤立实体抽样 (15个) ===")
    for rec in q(s, """
        MATCH (n:Entity) WHERE NOT (n)--(:Entity)
        RETURN n.name AS name, labels(n) AS labs,
               substring(coalesce(n.summary,''),0,160) AS summary,
               n.created_at AS created
        LIMIT 15
    """):
        labs = ','.join(l for l in rec['labs'] if l != 'Entity')
        print(f"  [{labs}] {rec['name']}")
        print(f"      summary: {rec['summary']}")

    print("\n=== 6b. 孤立实体是否曾出现在 episode 中(被提取但无边) ===")
    for rec in q(s, """
        MATCH (n:Entity) WHERE NOT (n)--(:Entity)
        WITH n LIMIT 15
        OPTIONAL MATCH (ep:Episodic)-[:MENTIONS]->(n)
        RETURN n.name AS name, count(ep) AS mentions
        LIMIT 15
    """):
        print(f"  {rec['name']}: mentions={rec['mentions']}")


def analyze_group(s):
    print("=== 7. group 字段现状 ===")
    for rec in q(s, """
        MATCH (n:Entity)
        RETURN n.group IS NULL AS is_null, count(*) AS c
    """):
        print(f"  group IS NULL = {rec['is_null']}: {rec['c']}")
    for rec in q(s, """
        MATCH (n:Entity) WHERE n.group IS NOT NULL
        RETURN n.group AS g, count(*) AS c LIMIT 5
    """):
        print(f"  non-null group: {rec['g']}: {rec['c']}")

    print("\n=== 8. 实体标签分布 ===")
    for rec in q(s, """
        MATCH (n:Entity)
        WITH [l IN labels(n) WHERE l <> 'Entity'][0] AS lab
        RETURN lab, count(*) AS c ORDER BY c DESC
    """):
        print(f"  {rec['lab']}: {rec['c']}")


def main():
    task = sys.argv[1] if len(sys.argv) > 1 else "all"
    with driver.session() as s:
        if task in ("edges", "all"):
            analyze_edges(s)
        if task in ("orphans", "all"):
            analyze_orphans(s)
        if task in ("group", "all"):
            analyze_group(s)
    driver.close()


if __name__ == "__main__":
    main()
