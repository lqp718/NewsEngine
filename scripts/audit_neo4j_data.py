#!/usr/bin/env python3
"""Neo4j 数据质量审计脚本"""

import os
import sys
from collections import defaultdict
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

# 媒体黑名单
MEDIA_BLACKLIST = [
    "CLS", "Cailisi", "CLS News", "Reuters", "Bloomberg", "Xinhua", "CCTV",
    "Eastmoney", "财联社", "新华社", "央视", "路透社", "彭博社"
]

def run_query(session, query, params=None):
    """执行查询并返回结果"""
    result = session.run(query, params or {})
    return [record.data() for record in result]

def audit_node_stats(session):
    """节点统计"""
    print("\n" + "="*60)
    print("1. 节点统计")
    print("="*60)
    
    # 总节点数
    result = run_query(session, "MATCH (n) RETURN count(n) as total")
    total = result[0]["total"]
    print(f"总节点数: {total}")
    
    # 按类型分布
    result = run_query(session, "MATCH (n) RETURN labels(n)[0] as type, count(n) as count ORDER BY count DESC")
    print("\n节点类型分布:")
    for r in result:
        print(f"  {r['type']}: {r['count']}")
    
    return total

def audit_relationship_stats(session):
    """关系统计"""
    print("\n" + "="*60)
    print("2. 关系统计")
    print("="*60)
    
    # 总关系数
    result = run_query(session, "MATCH ()-[r]->() RETURN count(r) as total")
    total = result[0]["total"]
    print(f"总关系数: {total}")
    
    # 按类型分布
    result = run_query(session, "MATCH ()-[r]->() RETURN type(r) as type, count(r) as count ORDER BY count DESC")
    print("\n关系类型分布:")
    for r in result:
        print(f"  {r['type']}: {r['count']}")

def audit_ticker_coverage(session):
    """Ticker 覆盖率"""
    print("\n" + "="*60)
    print("3. Ticker 覆盖率 (Stock 节点)")
    print("="*60)
    
    # 总 Stock 数
    result = run_query(session, "MATCH (s:Stock) RETURN count(s) as total")
    total = result[0]["total"]
    
    # 有 ticker 的 Stock 数
    result = run_query(session, "MATCH (s:Stock) WHERE s.ticker IS NOT NULL AND s.ticker <> '' RETURN count(s) as with_ticker")
    with_ticker = result[0]["with_ticker"]
    
    coverage = (with_ticker / total * 100) if total > 0 else 0
    print(f"Stock 节点总数: {total}")
    print(f"有 ticker 的: {with_ticker}")
    print(f"覆盖率: {coverage:.1f}%")
    
    # 列出有 ticker 的
    if with_ticker > 0:
        result = run_query(session, "MATCH (s:Stock) WHERE s.ticker IS NOT NULL AND s.ticker <> '' RETURN s.name as name, s.ticker as ticker LIMIT 10")
        print("\n有 ticker 的 Stock (前10):")
        for r in result:
            print(f"  {r['name']}: {r['ticker']}")
    
    # 列出没有 ticker 的
    result = run_query(session, "MATCH (s:Stock) WHERE s.ticker IS NULL OR s.ticker = '' RETURN s.name as name LIMIT 10")
    print("\n没有 ticker 的 Stock (前10):")
    for r in result:
        print(f"  {r['name']}")

def audit_media_contamination(session):
    """媒体名称入图情况"""
    print("\n" + "="*60)
    print("4. 媒体名称入图 (污染)")
    print("="*60)
    
    # 查找名称匹配媒体黑名单的节点
    media_pattern = "|".join(MEDIA_BLACKLIST)
    query = f"""
    MATCH (n)
    WHERE n.name =~ '(?i).*({media_pattern}).*'
    RETURN labels(n)[0] as type, n.name as name, count(n) as count
    ORDER BY count DESC
    """
    result = run_query(session, query)
    
    if result:
        print(f"发现 {len(result)} 个媒体相关节点:")
        for r in result:
            print(f"  [{r['type']}] {r['name']}: {r['count']}")
    else:
        print("未发现媒体名称入图 ✅")

def audit_part_of_issues(session):
    """PART_OF 语义混乱"""
    print("\n" + "="*60)
    print("5. PART_OF 关系分析")
    print("="*60)
    
    # 总 PART_OF 数
    result = run_query(session, "MATCH ()-[r:PART_OF]->() RETURN count(r) as total")
    total = result[0]["total"]
    print(f"PART_OF 关系总数: {total}")
    
    # 按目标节点类型分布
    result = run_query(session, """
    MATCH (a)-[r:PART_OF]->(b)
    RETURN labels(b)[0] as target_type, count(r) as count
    ORDER BY count DESC
    """)
    print("\nPART_OF 目标节点类型分布:")
    for r in result:
        print(f"  → {r['target_type']}: {r['count']}")
    
    # 采样查看
    result = run_query(session, """
    MATCH (a)-[r:PART_OF]->(b)
    RETURN a.name as source, labels(a)[0] as source_type,
           b.name as target, labels(b)[0] as target_type,
           r.fact as fact
    LIMIT 10
    """)
    print("\nPART_OF 采样 (前10):")
    for r in result:
        print(f"  {r['source']} [{r['source_type']}] → {r['target']} [{r['target_type']}]")
        if r['fact']:
            print(f"    fact: {r['fact'][:80]}...")

def audit_bidirectional_edges(session):
    """双向矛盾边"""
    print("\n" + "="*60)
    print("6. 双向矛盾边")
    print("="*60)
    
    # 查找 A→B 和 B→A 的同类型边
    query = """
    MATCH (a)-[r1]->(b)
    MATCH (b)-[r2]->(a)
    WHERE type(r1) = type(r2)
    AND id(a) < id(b)  // 避免重复
    RETURN a.name as node_a, labels(a)[0] as type_a,
           b.name as node_b, labels(b)[0] as type_b,
           type(r1) as rel_type,
           r1.fact as fact_ab,
           r2.fact as fact_ba
    LIMIT 20
    """
    result = run_query(session, query)
    
    if result:
        print(f"发现 {len(result)} 对双向边:")
        for r in result:
            print(f"\n  {r['node_a']} [{r['type_a']}] ↔ {r['node_b']} [{r['type_b']}]")
            print(f"  关系类型: {r['rel_type']}")
            if r['fact_ab']:
                print(f"  A→B: {r['fact_ab'][:60]}...")
            if r['fact_ba']:
                print(f"  B→A: {r['fact_ba'][:60]}...")
    else:
        print("未发现双向矛盾边 ✅")

def audit_episode_duplication(session):
    """Episode 重复情况"""
    print("\n" + "="*60)
    print("7. Episode 重复分析")
    print("="*60)
    
    # 总 Episode 数
    result = run_query(session, "MATCH (e:Episodic) RETURN count(e) as total")
    total = result[0]["total"]
    print(f"Episode 总数: {total}")
    
    # 按 name 后缀（hash[:12]）分组，找重复
    query = """
    MATCH (e:Episodic)
    WITH e, 
         CASE WHEN e.name CONTAINS '_' 
              THEN substring(e.name, size(e.name) - 12)
              ELSE e.name
         END as hash_suffix
    WITH hash_suffix, count(e) as dup_count, collect(e.name) as names
    WHERE dup_count > 1
    RETURN hash_suffix, dup_count, names
    ORDER BY dup_count DESC
    LIMIT 20
    """
    result = run_query(session, query)
    
    if result:
        total_dup_groups = len(result)
        total_dup_episodes = sum(r['dup_count'] for r in result)
        print(f"\n发现 {total_dup_groups} 组重复 Episode")
        print(f"涉及 {total_dup_episodes} 条 Episode")
        
        print("\n重复最严重的 (前10组):")
        for r in result[:10]:
            print(f"\n  Hash: {r['hash_suffix']} (重复 {r['dup_count']} 次)")
            for name in r['names'][:3]:
                print(f"    - {name}")
            if len(r['names']) > 3:
                print(f"    ... 还有 {len(r['names']) - 3} 条")
    else:
        print("未发现重复 Episode ✅")

def audit_entity_aliases(session):
    """实体别名未合并"""
    print("\n" + "="*60)
    print("8. 实体别名分析 (疑似重复节点)")
    print("="*60)
    
    # 查找名称相似的节点（简单启发式：包含相同关键词）
    # 这里用贵州茅台作为示例
    keywords = ["茅台", "Moutai", "腾讯", "Tencent"]
    
    for kw in keywords:
        query = f"""
        MATCH (n)
        WHERE n.name =~ '(?i).*{kw}.*'
        RETURN labels(n)[0] as type, n.name as name
        ORDER BY type, name
        """
        result = run_query(session, query)
        
        if len(result) > 1:
            print(f"\n关键词 '{kw}' 发现 {len(result)} 个疑似重复节点:")
            for r in result:
                print(f"  [{r['type']}] {r['name']}")

def main():
    print("Neo4j 数据质量审计报告")
    print(f"连接: {NEO4J_URI}")
    
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    
    try:
        driver.verify_connectivity()
        print("连接成功 ✅\n")
    except Exception as e:
        print(f"连接失败: {e}")
        sys.exit(1)
    
    with driver.session() as session:
        audit_node_stats(session)
        audit_relationship_stats(session)
        audit_ticker_coverage(session)
        audit_media_contamination(session)
        audit_part_of_issues(session)
        audit_bidirectional_edges(session)
        audit_episode_duplication(session)
        audit_entity_aliases(session)
    
    driver.close()
    print("\n" + "="*60)
    print("审计完成")
    print("="*60)

if __name__ == "__main__":
    main()
