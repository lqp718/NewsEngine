"""单元测试: episode_writer.normalize_edge_type — 边类型归一规则。

覆盖:
- 既有规则: TRIGGERS / AFFECTS / INVOLVES / HAPPENED_IN / PART_OF
- P1-3: INVESTS_IN / EXPOSED_TO 已废弃 —— 归一回落 RELATES_TO，
  含存量降级计划 build_retirement_plan 与 main() 阶段 3 分支
- P3-1: _clean_text 增加 HTML 实体解码（html.unescape）
- 新增规则: SUBSIDIARY/OWNED_BY/PARENT_OF → PART_OF；
  CEO_OF/EMPLOYED_BY/WORKS_FOR/CHAIRMAN_OF/CHAIR_OF/CHAIRPERSON/PRESIDENT_OF
  → INVOLVES；TRACKS/REPORTS/REPORTED_BY/STATES 保持 RELATES_TO
- "IN" 前缀收窄: "INFLUENCES" 不再被误归为 HAPPENED_IN（→ AFFECTS）；
  "INVOLVES" 不会被误归为 HAPPENED_IN
- TRIGGERS 盲区修复: TRIGGERED_BY → TRIGGERS（"TRIGGER" 前缀）
- None / 空输入 → RELATES_TO
- 重归一脚本的 fact 关键词推断（scripts/renormalize_edge_types.py），
  含误伤反例（状态变化句式 became / 动词 partners）、
  误标修正阶段（build_fix_plan）与 dry-run / 写库分支（fake driver）、
  方案 C 新增规则（AFFECTS / TRIGGERS / HAPPENED_IN 关键词 + 收紧策略 + 优先级）
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.graphiti.episode_writer import (
    CORE_EDGE_TYPES,
    _clean_text,
    _normalized_edge_types,
    normalize_edge_type,
)


def _load_renormalize_module():
    """按路径加载 scripts/renormalize_edge_types.py（scripts 非包）。"""
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "renormalize_edge_types.py"
    )
    spec = importlib.util.spec_from_file_location(
        "renormalize_edge_types", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestNormalizeEdgeTypeExistingRules:
    """既有规则保持不变。"""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("CAUSES", "TRIGGERS"),
            ("CAUSED", "TRIGGERS"),
            ("CAUSED_BY", "TRIGGERS"),
            ("TRIGGERS", "TRIGGERS"),
            ("AFFECTS", "AFFECTS"),
            ("IMPACTS", "AFFECTS"),
            ("MITIGATES", "AFFECTS"),
            ("INVOLVES", "INVOLVES"),
            ("ACTOR", "INVOLVES"),
            # P1-3: EXPOSED_TO 已废弃 → 回落 RELATES_TO
            ("EXPOSED", "RELATES_TO"),
            ("EXPOSED_TO", "RELATES_TO"),
            ("HAPPENED_IN", "HAPPENED_IN"),
            ("IN", "HAPPENED_IN"),
            ("IN_REGION", "HAPPENED_IN"),
            ("PART_OF", "PART_OF"),
            # P1-5.4: BELONGS_TO 独立于结构性 PART_OF（板块/行业/概念分类归属）
            ("BELONGS_TO", "BELONGS_TO"),
            ("TRADED_ON", "TRADED_ON"),
            ("LISTED_ON", "TRADED_ON"),
            # P1-3: INVESTS_IN 已废弃 → 回落 RELATES_TO
            ("INVESTS_IN", "RELATES_TO"),
            ("INVESTED_IN", "RELATES_TO"),
            ("LOCATED_IN", "PART_OF"),
            ("RELATES_TO", "RELATES_TO"),
        ],
    )
    def test_existing_rules(self, raw, expected):
        assert normalize_edge_type(raw) == expected

    def test_empty_and_unknown_fall_back_to_relates_to(self):
        assert normalize_edge_type("") == "RELATES_TO"
        assert normalize_edge_type("SOME_RANDOM_TYPE") == "RELATES_TO"

    def test_none_falls_back_to_relates_to(self):
        # CR #5: None 输入不应抛异常，归一到默认兜底
        assert normalize_edge_type(None) == "RELATES_TO"

    def test_case_insensitive_and_strip(self):
        assert normalize_edge_type("  subsidiary_of ") == "PART_OF"
        assert normalize_edge_type("ceo_of") == "INVOLVES"

    def test_result_always_in_core_set(self):
        for raw in (
            "CAUSES", "SUBSIDIARY_OF", "CEO_OF", "TRACKS", "INFLUENCES",
            "HAPPENED_IN", "whatever", "",
        ):
            assert normalize_edge_type(raw) in CORE_EDGE_TYPES


class TestNormalizeEdgeTypeNewRules:
    """新增映射规则。"""

    @pytest.mark.parametrize(
        "raw",
        ["SUBSIDIARY_OF", "SUBSIDIARY", "OWNED_BY", "PARENT_OF"],
    )
    def test_affiliation_maps_to_part_of(self, raw):
        assert normalize_edge_type(raw) == "PART_OF"

    @pytest.mark.parametrize(
        "raw",
        ["CEO_OF", "EMPLOYED_BY", "WORKS_FOR", "CHAIRMAN_OF"],
    )
    def test_role_maps_to_involves(self, raw):
        assert normalize_edge_type(raw) == "INVOLVES"

    @pytest.mark.parametrize(
        "raw",
        # CR #4: CHAIR 前缀覆盖 CHAIRMAN_OF/CHAIRPERSON/CHAIR_OF；
        # PRESIDENT 前缀覆盖 PRESIDENT_OF（消除写时/迁移不对称）
        ["CHAIR_OF", "CHAIRPERSON", "CHAIR", "PRESIDENT_OF", "PRESIDENT"],
    )
    def test_chair_president_maps_to_involves(self, raw):
        assert normalize_edge_type(raw) == "INVOLVES"

    @pytest.mark.parametrize(
        "raw",
        # CR #4: TRIGGERED_BY 不中 startswith("TRIGGERS")，用 "TRIGGER" 前缀覆盖
        ["TRIGGERED_BY", "TRIGGER", "TRIGGERED"],
    )
    def test_triggered_by_maps_to_triggers(self, raw):
        assert normalize_edge_type(raw) == "TRIGGERS"

    @pytest.mark.parametrize(
        "raw",
        ["TRACKS", "REPORTS", "REPORTED_BY", "STATES"],
    )
    def test_generic_relation_stays_relates_to(self, raw):
        assert normalize_edge_type(raw) == "RELATES_TO"


class TestNormalizeEdgeTypeInPrefixBugfix:
    """'IN' 前缀收窄后不再误伤其他 IN* 名称。"""

    def test_involves_not_happened_in(self):
        # INVOLV 规则先于 IN 规则，INVOLVES 必须归到 INVOLVES
        assert normalize_edge_type("INVOLVES") == "INVOLVES"

    def test_influences_not_happened_in(self):
        # 旧规则 startswith("IN") 会把 INFLUENCES 误归为 HAPPENED_IN；
        # 修复后走 INFLUENC → AFFECTS（影响本质）
        assert normalize_edge_type("INFLUENCES") == "AFFECTS"

    def test_generic_in_star_names_no_longer_happened_in(self):
        # 非 HAPPENED/IN_/精确 IN 的 IN* 名称落到默认兜底
        assert normalize_edge_type("INITIATES") == "RELATES_TO"

    def test_happened_and_in_prefix_still_work(self):
        assert normalize_edge_type("HAPPENED_IN") == "HAPPENED_IN"
        assert normalize_edge_type("IN_CITY") == "HAPPENED_IN"
        assert normalize_edge_type("IN") == "HAPPENED_IN"


class TestRenormalizeFactInference:
    """重归一脚本的 fact 关键词推断（方案 A）。"""

    @pytest.fixture(scope="class")
    def mod(self):
        return _load_renormalize_module()

    @pytest.mark.parametrize(
        "fact",
        [
            "MMG is a subsidiary of state-owned China Minmetals Corp.",
            "Lundin Mining owns the Caserones operation.",
            "The Afipsky refinery is owned by the privately-held firm ForteInvest",
            "X is a part of Y group.",
        ],
    )
    def test_affiliation_facts_infer_part_of(self, mod, fact):
        legacy = mod.infer_legacy_edge_type(fact)
        assert legacy is not None
        assert normalize_edge_type(legacy) == "PART_OF"

    @pytest.mark.parametrize(
        "fact",
        [
            "Brandon Craig became the CEO of BHP Group in July 2026",
            "Bernardo Fontaine is the Chairman of Codelco",
            "Haytham Hodaly joined Wheaton Precious Metals in January 2012",
            "James Steel is the chief precious metals analyst at HSBC",
            "Terry Bowen has been appointed as an independent non-executive director at Northern Star Resources",
            "enforcement action with SouthPoint Bancshares, Inc. involves SouthPoint Bancshares, Inc.",
            "Scott Bessent is the U.S. Treasury Secretary.",
        ],
    )
    def test_role_facts_infer_involves(self, mod, fact):
        legacy = mod.infer_legacy_edge_type(fact)
        assert legacy is not None
        assert normalize_edge_type(legacy) == "INVOLVES"

    @pytest.mark.parametrize(
        "fact",
        [
            "Ministry of Energy and Mines data tracks Las Bambas mine production",
            "Kyodo reports that Japan debt-servicing cost is expected to rise",
            "World Gold Council reported gold-backed ETFs added $7 billion",
            "",
            None,
        ],
    )
    def test_generic_facts_stay_relates_to(self, mod, fact):
        legacy = mod.infer_legacy_edge_type(fact)
        if legacy is None:
            return  # 保持 RELATES_TO
        assert normalize_edge_type(legacy) == "RELATES_TO"

    def test_directorate_not_false_positive(self, mod):
        # "Directorate" 不应命中 \bdirector\b
        fact = "Norwegian Offshore Directorate reports oil output could collapse"
        legacy = mod.infer_legacy_edge_type(fact)
        # 该 fact 含 reports 且无任职关键词 → 保持 RELATES_TO
        assert legacy is None or normalize_edge_type(legacy) == "RELATES_TO"

    def test_affiliation_takes_priority_over_role(self, mod):
        # 同时命中归属与任职关键词时，结构性归属优先
        fact = "The subsidiary's CEO resigned; X is a subsidiary of Y."
        legacy = mod.infer_legacy_edge_type(fact)
        assert normalize_edge_type(legacy) == "PART_OF"

    # ── CR 误伤反例 ────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "fact",
        [
            # CR #1: 状态变化句式不应命中 became（旧裸 \bbecame\b 会误归）
            "China became the world's largest gold producer",
            "The firm became the top supplier of copper cathodes",
            "Indonesia became the first country to ban nickel ore exports",
            "The project became the biggest mining investment in the region",
        ],
    )
    def test_status_change_became_not_involves(self, mod, fact):
        # 无角色上下文的 became → 不命中任何模式 → 保持 RELATES_TO
        legacy = mod.infer_legacy_edge_type(fact)
        if legacy is None:
            return
        assert normalize_edge_type(legacy) == "RELATES_TO"

    @pytest.mark.parametrize(
        "fact",
        [
            # CR #1: became 带角色上下文仍应命中（收紧后保留真任职）
            "Brandon Craig became the CEO of BHP Group in July 2026",
            "Haytham Hodaly became CEO of Wheaton Precious Metals",
            "She became chairman of the board last year",
            "He became president of the company in 2020",
        ],
    )
    def test_became_with_role_still_involves(self, mod, fact):
        legacy = mod.infer_legacy_edge_type(fact)
        assert legacy is not None
        assert normalize_edge_type(legacy) == "INVOLVES"

    @pytest.mark.parametrize(
        "fact",
        [
            # CR #3: partners 动词用法不应命中（旧 \bpartners?\b 会误归）
            "Anglo American partners with Teck on the Quebrada Blanca project",
            "The two companies partners with local suppliers",
        ],
    )
    def test_partners_verb_not_involves(self, mod, fact):
        legacy = mod.infer_legacy_edge_type(fact)
        if legacy is None:
            return
        assert normalize_edge_type(legacy) == "RELATES_TO"

    @pytest.mark.parametrize(
        "fact",
        [
            # CR #3: partners 名词语境仍应命中（收紧后保留真任职/合伙）
            "Jane Doe is a partner at Goldman Sachs",
            "He became a managing partner of the fund",
            "The firm announced a new partnership with Y",  # partnership 名词语境不算任职，但若仅出现 partnership 且无角色词 → 不命中（允许）
        ],
    )
    def test_partners_noun_context(self, mod, fact):
        legacy = mod.infer_legacy_edge_type(fact)
        # partnership 单独特例：若无角色关键词可不命中；命中则必为 INVOLVES
        if legacy is not None:
            assert normalize_edge_type(legacy) == "INVOLVES"

    def test_subsidiary_word_boundary(self, mod):
        # CR #2: 词边界约束 —— 裸前缀不再误伤，但真实 subsidiary 仍命中
        fact = "MMG is a subsidiary of China Minmetals Corp."
        legacy = mod.infer_legacy_edge_type(fact)
        assert normalize_edge_type(legacy) == "PART_OF"

    # ── "part of" 收紧（2026-08-31）: 归属句式命中，"as part of" 习语不命中 ──
    # 背景: 裸 \bpart\s+of\b 把 42 条命中中 37 条 "as part of" 习语误归 PART_OF。

    @pytest.mark.parametrize(
        "fact",
        [
            # 正向: 归属句式（X is/are/form(s)/remain(s) (a/an) [修饰词] part of Y）
            "The mine forms part of the Eagle project",
            "Abu Dhabi is part of the UAE",
            "The plant is an integral part of the investment",
            "The business remains a key part of the portfolio",
        ],
    )
    def test_attribution_part_of_infers_part_of(self, mod, fact):
        legacy = mod.infer_legacy_edge_type(fact)
        assert legacy is not None
        assert normalize_edge_type(legacy) == "PART_OF"

    @pytest.mark.parametrize(
        "fact",
        [
            # 反向: "as part of ..." 习语 —— "part of" 前无系动词，不命中归属模式；
            # 且不应被其他模式（INVOLVES/AFFECTS/...）误归 → 保持 RELATES_TO
            "Lindor will oversee the mine as part of Codelco's northern operations",
            "The U.S. imposed sanctions as part of 'Operation Economic Fury'",
            "Applicants should consider master's programmes as part of the Chevening Scholarship",
            # 反向: 隐喻参与习语 "are part of the (ongoing) race" —— 有系动词，
            # 由负向前瞻（抽象隐喻中心词）排除，不归一为 PART_OF
            "Nvidia's chips are part of the ongoing race to build AI",
        ],
    )
    def test_idiomatic_part_of_stays_relates_to(self, mod, fact):
        legacy = mod.infer_legacy_edge_type(fact)
        if legacy is None:
            return  # 保持 RELATES_TO
        assert normalize_edge_type(legacy) == "RELATES_TO"

    # ── 方案 C 新增规则: AFFECTS / TRIGGERS / HAPPENED_IN ────────────────

    @pytest.mark.parametrize(
        "fact",
        [
            "Rising copper prices threaten the profitability of smaller miners",
            "The stimulus package boosts the gold sector",
            "A stronger dollar pressures metal prices",
            "The strike impacts production at the Las Bambas mine",
            "The scandal undermines investor confidence",
            "Lower ore grades hurt output at the copper mine",
            "The tax break benefits domestic smelters",
            "The government supports the mining ban",
            "The opposition party opposes the new royalty tax",
            "The fund withdrew its support for the takeover bid",
            "The embargo harms Chilean copper exports",
            "The spill damages the river ecosystem",
            "Higher output strengthens the company's balance sheet",
            "Rising costs weaken margins at the refinery",
        ],
    )
    def test_affects_facts_infer_affects(self, mod, fact):
        legacy = mod.infer_legacy_edge_type(fact)
        assert legacy is not None
        assert normalize_edge_type(legacy) == "AFFECTS"

    @pytest.mark.parametrize(
        "fact",
        [
            "The strike caused a halt in copper production",
            "The rate cut led to a rally in gold prices",
            "The power outage triggered a supply squeeze",
            "The announcement sparked selling in mining stocks",
            "The furnace fire resulted in the closure of the smelter",
            "The sanctions precipitated a liquidity crisis",
        ],
    )
    def test_triggers_facts_infer_triggers(self, mod, fact):
        legacy = mod.infer_legacy_edge_type(fact)
        assert legacy is not None
        assert normalize_edge_type(legacy) == "TRIGGERS"

    @pytest.mark.parametrize(
        "fact",
        [
            "The accident happened in the Atacama region",
            "The earthquake occurred in northern Chile",
            "The summit took place in Geneva",
            "The refinery is located in Texas",
            "The smelter was located in Guangdong province",
        ],
    )
    def test_happened_in_facts_infer_happened_in(self, mod, fact):
        legacy = mod.infer_legacy_edge_type(fact)
        assert legacy is not None
        assert normalize_edge_type(legacy) == "HAPPENED_IN"

    @pytest.mark.parametrize(
        "fact",
        [
            # impacts 名词用法（后接 of）→ 不归一（收紧策略）
            "the environmental impacts of mining",
            # impacts 名词用法（have impacts on ...）→ 不归一（抽样发现）
            "The project will have significant impacts on medicine",
            # supports 名词复数用法（后接介词）→ 不归一（收紧策略）
            "technical supports for the project",
            # cause 名词用法（非 caused 动词）→ 不归一（收紧策略）
            "the cause of the accident",
            # support 技术分析名词用法（support at $X）→ 不归一（抽样发现）
            "Crude Oil WTI is above the SuperTrend support at $83.32",
            # support 名词用法（fiscal support during ...）→ 不归一（抽样发现）
            "The ECB released research on fiscal support during the pandemic",
        ],
    )
    def test_noun_usage_stays_relates_to(self, mod, fact):
        legacy = mod.infer_legacy_edge_type(fact)
        if legacy is None:
            return  # 保持 RELATES_TO
        assert normalize_edge_type(legacy) == "RELATES_TO"

    def test_part_of_takes_priority_over_affects(self, mod):
        # 优先级: 同时命中 PART_OF 与 AFFECTS 时取 PART_OF（结构性归属优先）
        fact = "The subsidiary supports the group's overseas expansion"
        legacy = mod.infer_legacy_edge_type(fact)
        assert normalize_edge_type(legacy) == "PART_OF"

    def test_involves_takes_priority_over_triggers(self, mod):
        # 优先级: 同时命中 INVOLVES 与 TRIGGERS 时取 INVOLVES（参与 > 因果）
        fact = "The chairman caused controversy with the restructuring plan"
        legacy = mod.infer_legacy_edge_type(fact)
        assert normalize_edge_type(legacy) == "INVOLVES"

    def test_affects_takes_priority_over_happened_in(self, mod):
        # 优先级: 同时命中 AFFECTS 与 HAPPENED_IN 时取 AFFECTS（影响 > 地点）
        fact = "The storm that happened in Chile threatens copper output"
        legacy = mod.infer_legacy_edge_type(fact)
        assert normalize_edge_type(legacy) == "AFFECTS"


class TestRenormalizeFixPlan:
    """阶段 2：误标 INVOLVES 边的修正计划（build_fix_plan）。"""

    @pytest.fixture(scope="class")
    def mod(self):
        return _load_renormalize_module()

    def test_fix_plan_demotes_status_change_became(self, mod):
        # 旧裸 became 误标的状态变化句式 → 修正回 RELATES_TO
        edges = [
            ("u1", "China became the world's largest gold producer"),
            ("u2", "The firm became the top copper supplier"),
        ]
        fixes = mod.build_fix_plan(edges)
        assert len(fixes) == 2
        assert all(item["name"] == "RELATES_TO" for item in fixes)

    def test_fix_plan_keeps_genuine_role_became(self, mod):
        # 真任职（became the CEO）仍是 INVOLVES → 不产生修正（幂等）
        edges = [
            ("u1", "Brandon Craig became the CEO of BHP Group"),
            ("u2", "Haytham Hodaly became CEO of Wheaton Precious Metals"),
        ]
        fixes = mod.build_fix_plan(edges)
        assert fixes == []

    def test_fix_plan_ignores_unrelated_facts(self, mod):
        # fact 不含 became/partners → 不在复核范围，不产生修正
        edges = [
            ("u1", "James Steel is the chief precious metals analyst at HSBC"),
            ("u2", "enforcement action involves SouthPoint Bancshares"),
            ("u3", None),
        ]
        fixes = mod.build_fix_plan(edges)
        assert fixes == []

    def test_fix_plan_demotes_partners_verb(self, mod):
        # 动词 "partners with" 误标 → 修正回 RELATES_TO
        edges = [("u1", "Anglo American partners with Teck on the project")]
        fixes = mod.build_fix_plan(edges)
        assert len(fixes) == 1
        assert fixes[0]["name"] == "RELATES_TO"

    def test_fix_plan_keeps_partners_noun_role(self, mod):
        # 名词 "partner at" 真任职 → 仍是 INVOLVES，不修正
        edges = [("u1", "Jane Doe is a partner at Goldman Sachs")]
        fixes = mod.build_fix_plan(edges)
        assert fixes == []

    def test_fix_plan_idempotent_after_apply(self, mod):
        # 模拟修正后（name 已非 INVOLVES）：再次传入同一 fact 仍会产生计划，
        # 但真实运行中 WHERE name='INVOLVES' 会拦住 —— 此处验证纯函数稳定性：
        # 同一输入多次调用结果一致（确定性）。
        edges = [("u1", "China became the world's largest gold producer")]
        assert mod.build_fix_plan(edges) == mod.build_fix_plan(edges)


class TestRenormalizeMainCli:
    """重归一脚本 main() 的 dry-run / 写库 / 幂等分支（fake Neo4j driver）。"""

    @pytest.fixture()
    def fake_env(self, monkeypatch, tmp_path):
        """用 fake driver + fake settings 替换 neo4j 与配置，返回状态容器。"""
        import sys

        mod = _load_renormalize_module()

        state = {"edges": {}, "updates": [], "connect_error": None}

        class _FakeResult:
            def __init__(self, records):
                self.records = records

        class _FakeDriver:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def verify_connectivity(self):
                if state["connect_error"]:
                    raise state["connect_error"]

            def execute_query(self, query, **kw):
                q = query.upper()
                if "UNWIND" in q:
                    batch = kw["batch"]
                    if "ITEM.FROM_NAME" in q:
                        # 阶段 3（P1-3）: 废弃类型降级，源 name 由 from_name 逐项给出
                        updated = 0
                        for item in batch:
                            bucket = state["edges"].setdefault(
                                item["from_name"], {}
                            )
                            if item["uuid"] in bucket:
                                fact = bucket.pop(item["uuid"])
                                state["edges"].setdefault(item["name"], {})[
                                    item["uuid"]
                                ] = fact
                                updated += 1
                                state["updates"].append(
                                    (item["from_name"], item)
                                )
                        return _FakeResult([{"updated": updated}])
                    # 根据 WHERE 子句判断源 name
                    src = "INVOLVES" if "'INVOLVES'" in query else "RELATES_TO"
                    updated = 0
                    for item in batch:
                        bucket = state["edges"].setdefault(src, {})
                        if item["uuid"] in bucket:
                            fact = bucket.pop(item["uuid"])
                            state["edges"].setdefault(item["name"], {})[
                                item["uuid"]
                            ] = fact
                            updated += 1
                            state["updates"].append((src, item))
                    return _FakeResult([{"updated": updated}])
                if "COUNT(*)" in q:
                    return _FakeResult(
                        [
                            {"name": name, "cnt": len(bucket)}
                            for name, bucket in state["edges"].items()
                        ]
                    )
                if "$RETIRED" in q:
                    # 阶段 3 fetch（P1-3）: 按 retired 参数返回废弃类型边
                    rows = []
                    for name in kw.get("retired", []):
                        rows.extend(
                            {"uuid": uuid, "name": name, "fact": fact}
                            for uuid, fact in state["edges"]
                            .get(name, {})
                            .items()
                        )
                    return _FakeResult(rows)
                src = "INVOLVES" if "'INVOLVES'" in query else "RELATES_TO"
                bucket = state["edges"].get(src, {})
                return _FakeResult(
                    [
                        {"uuid": uuid, "fact": fact}
                        for uuid, fact in bucket.items()
                    ]
                )

        class _FakeGraphDatabase:
            @staticmethod
            def driver(uri, auth=None):
                return _FakeDriver()

        class _FakeSettings:
            neo4j_uri = "bolt://fake:7687"
            neo4j_user = "neo4j"
            neo4j_password = "fake"

        import neo4j as neo4j_mod

        monkeypatch.setattr(neo4j_mod, "GraphDatabase", _FakeGraphDatabase)
        import src.core.config as config_mod

        monkeypatch.setattr(config_mod, "get_settings", lambda: _FakeSettings())

        return mod, state

    def _seed(self, state):
        """预置典型语料：含会被旧规则误伤的句式和真任职句式。"""
        state["edges"]["RELATES_TO"] = {
            "u-rel-role": "Brandon Craig became the CEO of BHP Group",
            "u-rel-status": "China became the world's largest gold producer",
            "u-rel-generic": "Kyodo reports that debt costs will rise",
        }
        state["edges"]["INVOLVES"] = {
            "u-inv-role": "Haytham Hodaly became CEO of Wheaton Precious Metals",
            "u-inv-status": "Indonesia became the first country to ban nickel exports",
            "u-inv-verb": "Anglo American partners with Teck on the project",
            "u-inv-clean": "James Steel is the chief analyst at HSBC",
        }
        # P1-3: 废弃类型存量边 —— 持股事实 / 误抽事实 / 风险影响事实
        state["edges"]["INVESTS_IN"] = {
            "u-inv-stake": "X holds a stake in the Caserones mine",
            "u-inv-bad": "Y donated $1M to the university",
        }
        state["edges"]["EXPOSED_TO"] = {
            "u-exp-risk": "Rising copper prices threaten profitability",
        }

    def test_dry_run_writes_nothing(self, fake_env, monkeypatch, capsys):
        mod, state = fake_env
        self._seed(state)
        monkeypatch.setattr("sys.argv", ["renormalize", "--dry-run"])
        rc = mod.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "[dry-run]" in out
        assert state["updates"] == []  # 未写库
        # 阶段 1 应命中真任职（CEO），不命中状态变化句式（largest）
        assert "u-rel-role" in state["edges"]["RELATES_TO"]

    def test_dry_run_detects_status_change_not_migrated(self, fake_env, monkeypatch, capsys):
        mod, state = fake_env
        state["edges"]["RELATES_TO"] = {
            "u-status": "China became the world's largest gold producer",
        }
        state["edges"]["INVOLVES"] = {}
        monkeypatch.setattr("sys.argv", ["renormalize", "--dry-run"])
        rc = mod.main()
        out = capsys.readouterr().out
        assert rc == 0
        # 无应更新项 → 打印幂等收敛信息，计划更新为 0 条
        assert "计划更新: 0 条" in out

    def test_write_migrates_role_and_fixes_mislabels(self, fake_env, monkeypatch, capsys):
        mod, state = fake_env
        self._seed(state)
        monkeypatch.setattr("sys.argv", ["renormalize", "--yes"])
        rc = mod.main()
        out = capsys.readouterr().out
        assert rc == 0
        # 阶段 1：真任职 RELATES_TO → INVOLVES；状态变化/通用 保持 RELATES_TO
        assert "u-rel-role" in state["edges"]["INVOLVES"]
        assert "u-rel-status" in state["edges"]["RELATES_TO"]
        assert "u-rel-generic" in state["edges"]["RELATES_TO"]
        # 阶段 2：误标的状态变化/动词 partners → 改回 RELATES_TO；真任职保留
        assert "u-inv-status" in state["edges"]["RELATES_TO"]
        assert "u-inv-verb" in state["edges"]["RELATES_TO"]
        assert "u-inv-role" in state["edges"]["INVOLVES"]
        assert "u-inv-clean" in state["edges"]["INVOLVES"]
        # 阶段 3（P1-3）: 废弃类型降级 —— 持股事实 → PART_OF；
        # 无关键词误抽事实 → RELATES_TO；风险影响事实 → AFFECTS
        assert "u-inv-stake" in state["edges"]["PART_OF"]
        assert "u-inv-bad" in state["edges"]["RELATES_TO"]
        assert "u-exp-risk" in state["edges"]["AFFECTS"]
        assert not state["edges"].get("INVESTS_IN")
        assert not state["edges"].get("EXPOSED_TO")
        assert "完成" in out

    def test_idempotent_second_run_no_updates(self, fake_env, monkeypatch, capsys):
        mod, state = fake_env
        self._seed(state)
        monkeypatch.setattr("sys.argv", ["renormalize", "--yes"])
        mod.main()
        first_update_count = len(state["updates"])
        assert first_update_count > 0
        capsys.readouterr()
        # 第二次运行：已收敛，应无任何更新（幂等）
        rc = mod.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert len(state["updates"]) == first_update_count  # 无新增写库
        assert "无需更新" in out

    def test_connect_error_friendly_message(self, fake_env, monkeypatch, capsys):
        mod, state = fake_env
        state["connect_error"] = RuntimeError("Connection refused")
        monkeypatch.setattr("sys.argv", ["renormalize", "--yes"])
        rc = mod.main()
        err = capsys.readouterr().err
        assert rc == 1
        assert "无法连接" in err
        assert "Connection refused" in err


class TestP13RetiredTypes:
    """P1-3: INVESTS_IN / EXPOSED_TO 从核心集移除（~60% 错误率，零下游消费）。"""

    def test_retired_types_not_in_core_set(self):
        assert "INVESTS_IN" not in CORE_EDGE_TYPES
        assert "EXPOSED_TO" not in CORE_EDGE_TYPES

    @pytest.mark.parametrize(
        "raw",
        ["INVESTS_IN", "INVESTED_IN", "INVESTS", "EXPOSED_TO", "EXPOSED"],
    )
    def test_retired_names_fall_back_to_relates_to(self, raw):
        # 废弃类型名（含 LLM 偶发再产出）统一由默认兜底收敛到 RELATES_TO
        assert normalize_edge_type(raw) == "RELATES_TO"

    def test_normalized_edge_types_exclude_retired(self):
        from src.graphiti.relation_types import EDGE_TYPES

        normalized = _normalized_edge_types(EDGE_TYPES)
        assert "INVESTS_IN" not in normalized
        assert "EXPOSED_TO" not in normalized
        # 补齐后的 schema 键仍全部落在核心集内
        assert set(normalized) <= CORE_EDGE_TYPES

    def test_extraction_instructions_no_longer_offer_retired_types(self):
        from src.adapters.models import EntityItem, NormalizedEpisode
        from src.graphiti.episode_writer import _build_extraction_instructions
        from datetime import datetime, timezone

        episode = NormalizedEpisode(
            name="test",
            episode_body="body",
            source_description="src",
            source_url="https://example.com",
            valid_at=datetime.now(timezone.utc),
            content_hash="h",
            source_type="rss",
            entities=[EntityItem(type="stock", name="X", ticker="0700.HK")],
        )
        instructions = _build_extraction_instructions(episode)
        # 不再提供 INVESTS_IN 精确定义，且显式禁止再创造废弃类型
        assert "INVESTS_IN:" not in instructions
        assert "do NOT invent INVESTS_IN/EXPOSED_TO" in instructions


class TestRenormalizeRetirementPlan:
    """阶段 3（P1-3）: build_retirement_plan — 废弃类型存量边降级计划。"""

    @pytest.fixture(scope="class")
    def mod(self):
        return _load_renormalize_module()

    def test_stake_fact_demotes_to_part_of(self, mod):
        updates, transitions = mod.build_retirement_plan(
            [("u1", "INVESTS_IN", "X holds a stake in the Caserones mine")]
        )
        assert updates == [
            {"uuid": "u1", "name": "PART_OF", "from_name": "INVESTS_IN"}
        ]
        assert transitions[("INVESTS_IN", "PART_OF")] == 1

    def test_acquired_fact_demotes_to_part_of(self, mod):
        updates, _ = mod.build_retirement_plan(
            [("u1", "INVESTS_IN", "X acquired a 51% stake in Y")]
        )
        assert updates[0]["name"] == "PART_OF"

    def test_risk_fact_demotes_to_affects(self, mod):
        updates, _ = mod.build_retirement_plan(
            [("u1", "EXPOSED_TO", "Rising copper prices threaten profitability")]
        )
        assert updates[0]["name"] == "AFFECTS"
        assert updates[0]["from_name"] == "EXPOSED_TO"

    def test_uninferable_fact_demotes_to_relates_to(self, mod):
        # 误抽事实（捐赠/学历类，INVESTS_IN ~60% 错误率的主体）无关键词
        # → 回落 RELATES_TO，而非扩散进 PART_OF/AFFECTS
        updates, _ = mod.build_retirement_plan(
            [
                ("u1", "INVESTS_IN", "Y donated $1M to the university"),
                ("u2", "INVESTS_IN", None),
                ("u3", "EXPOSED_TO", "Z is exposed to currency risk"),
            ]
        )
        assert [u["name"] for u in updates] == [
            "RELATES_TO",
            "RELATES_TO",
            "RELATES_TO",
        ]

    def test_plan_targets_are_core_types_only(self, mod):
        updates, _ = mod.build_retirement_plan(
            [
                ("u1", "INVESTS_IN", "X invested in Y"),
                ("u2", "EXPOSED_TO", "The embargo harms copper exports"),
            ]
        )
        for u in updates:
            assert u["name"] in CORE_EDGE_TYPES
            assert u["name"] not in ("INVESTS_IN", "EXPOSED_TO")

    def test_plan_is_deterministic(self, mod):
        edges = [("u1", "INVESTS_IN", "X holds a stake in Y")]
        assert mod.build_retirement_plan(edges) == mod.build_retirement_plan(edges)


class TestCleanTextP31:
    """P3-1: _clean_text — HTML 实体解码 + 控制字符清除。"""

    def test_named_entity_decoded(self):
        # 诊断报告实例: "China&rsquo;s" 泄漏进 Sector 名
        assert _clean_text("China&rsquo;s economy") == "China\u2019s economy"

    def test_amp_and_numeric_entities(self):
        assert _clean_text("A &amp; B") == "A & B"
        assert _clean_text("it&#39;s") == "it's"
        assert _clean_text("it&#x27;s") == "it's"
        assert _clean_text("&lt;tag&gt;") == "<tag>"

    def test_single_pass_no_double_unescape(self):
        # html.unescape 单遍不递归: "&amp;rsquo;" 只解一层
        assert _clean_text("&amp;rsquo;") == "&rsquo;"

    def test_control_chars_still_removed(self):
        assert _clean_text("a\x00b\x1fc") == "abc"
        # \n \r \t 保留
        assert _clean_text("line1\nline2\tend\r") == "line1\nline2\tend\r"

    def test_entity_decoded_control_char_removed(self):
        # 先解码后清控制字符: 数字实体解码产生的控制字符也被清除
        assert _clean_text("a&#x1;b") == "ab"

    def test_non_string_passthrough(self):
        assert _clean_text(None) is None
        assert _clean_text(42) == 42

    def test_plain_text_unchanged(self):
        assert _clean_text("贵州茅台发布年报") == "贵州茅台发布年报"
