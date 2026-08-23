# NewsEngine P2 — 实体重复源头解决：修改说明

## 概述

本次修改通过 Adapter 层规范化 + Episode Body 约束注入，解决 966 个实体中大量重复（如 "Bougainville Copper" / "Bougainville Copper Ltd."）和 342/966 实体缺 entity_name 的问题。

## 新建文件

### 1. `src/utils/entity_canonical.py`
- **用途**: 实体名称规范化工具
- **核心函数**: `canonical_name(name, entity_type=None) -> str`
- **三阶段规范化**:
  1. 查找 EN_ZH_MAP（高频实体中英文映射）
  2. 去除公司后缀（Ltd, Inc, Corp, 控股, 有限公司等）
  3. 空白字符标准化
- **导出**:
  - `canonical_name()` - 单个名称规范化
  - `canonical_name_batch()` - 批量规范化
  - `is_canonical()` - 检查是否已是规范名称
  - `CORPORATE_SUFFIXES` - 公司后缀白名单
  - `EN_ZH_MAP` - 中英文映射表

### 2. `data/canonical_entities.yaml`
- **用途**: 高频实体 canonical name 映射配置
- **格式**: `canonical_name: [alias1, alias2, ...]`
- **覆盖范围**: Top 50+ 高频实体，分为 8 大类：
  - 科技巨头（腾讯/阿里/苹果/微软/谷歌/亚马逊/特斯拉/英伟达等）
  - 中国金融机构（平安/移动/联通/电信/四大行/券商等）
  - 工业制造（博世/西门子）
  - 国家/地区（美/中/港/日/韩/德/法/英/欧）
  - 大宗商品（黄金/白银/原油/天然气/铜/铁矿石/锂/比特币等）
  - 央行/政策（美联储/欧央行/人民银行/日央行）
  - 行业/主题（半导体/AI/电动车/可再生能源/太阳能/风电）

## 修改文件

### 3. `src/adapters/models.py`
- **修改内容**: 在 `EntityItem.__init__()` 中添加规范化逻辑
- **影响**: 所有通过 EntityItem 构造的实体自动规范化名称
- **代码**:
  ```python
  from src.utils.entity_canonical import canonical_name
  
  class EntityItem(BaseModel):
      # ... fields ...
      
      def __init__(self, **data):
          super().__init__(**data)
          # 规范化实体名称
          self.name = canonical_name(self.name, self.type)
  ```

### 4. `src/graphiti/episode_writer.py`
- **修改内容**: 重写 `_build_extended_body()` 函数，注入实体解析约束
- **影响**: LLM 在提取实体时被强制使用规范化后的名称
- **约束规则**:
  1. 严格使用列出的名称，逐字符匹配
  2. 不添加或删除后缀（Ltd, Inc, Corp, 控股等）
  3. 优先使用中文名称
  4. 同一实体出现不同名称时，使用第一个列出的名称
- **移除**: 不再使用 `build_entity_suffix()`（已删除导入）

### 5. `src/adapters/fred_adapter.py`
- **修改内容**: 导入 `canonical_name` 并在实体构造时调用
- **影响**: FRED 数据源的 "United States" 和 topic 实体名称被规范化
- **示例**: `"United States"` → `"美国"`

### 6. `src/adapters/cls_adapter.py`
- **修改内容**: 导入 `canonical_name` 并在 `_extract_entities_from_stock_list()` 中调用
- **影响**: CLS 财联社数据源的股票实体名称被规范化
- **示例**: `"腾讯"` → `"腾讯控股"`

### 7. `src/adapters/eastmoney_adapter.py`
- **修改内容**: 导入 `canonical_name` 并在实体构造时调用
- **影响**: EastMoney 数据源的股票实体名称被规范化
- **示例**: `"Apple Inc"` → `"苹果"`

## 验收标准检查

- [x] `entity_canonical.py` 创建完成
- [x] `canonical_entities.yaml` 创建骨架（Top 50+ 高频实体）
- [x] `models.py` 修改完成（EntityItem 自动规范化）
- [x] `episode_writer.py` 修改完成（注入 ENTITY RESOLUTION RULES）
- [x] 3 个 adapter 修改完成（fred/cls/eastmoney）
- [x] 代码无语法错误（py_compile 通过）
- [x] 生成修改说明文档（本文档）

## 工作原理

```
原始实体名称 (Adapter)
    ↓
canonical_name() 规范化
    ↓
EntityItem 构造 (models.py)
    ↓
_build_extended_body() 注入约束
    ↓
LLM 提取实体 (Graphiti)
    ↓
规范化后的实体进入知识图谱
```

## 预期效果

1. **减少重复**: "Tencent" / "Tencent Holdings" / "腾讯控股" 统一为 "腾讯控股"
2. **提高一致性**: 中英文实体统一使用中文规范名称
3. **LLM 约束**: 通过 ENTITY RESOLUTION RULES 强制 LLM 使用规范名称
4. **可扩展**: 新增实体只需更新 `canonical_entities.yaml` 和 `EN_ZH_MAP`

## 后续优化建议

1. **动态加载**: 从 `canonical_entities.yaml` 动态加载映射到 `EN_ZH_MAP`
2. **更多 Adapter**: 将规范化应用到其他 adapter（akshare/gdelt/rss/treasury 等）
3. **Neo4j 同步**: 定期从 Neo4j 提取高频实体，更新 `canonical_entities.yaml`
4. **监控指标**: 统计规范化前后的实体数量变化，评估去重效果

## 测试建议

```python
from src.utils.entity_canonical import canonical_name

# 测试中英文映射
assert canonical_name("Tencent Holdings Ltd.") == "腾讯控股"
assert canonical_name("Apple Inc") == "苹果"
assert canonical_name("United States") == "美国"

# 测试后缀去除
assert canonical_name("Bougainville Copper Ltd.") == "Bougainville Copper"
assert canonical_name("Some Company Inc") == "Some Company"

# 测试空白标准化
assert canonical_name("  Apple   Inc  ") == "苹果"
```

## 注意事项

1. **向后兼容**: 已入库的实体不会自动更新，需手动或通过脚本迁移
2. **性能影响**: `canonical_name()` 为 O(1) 查找 + O(n) 后缀去除，性能影响可忽略
3. **维护成本**: 需定期更新 `canonical_entities.yaml` 以覆盖新发现的高频实体

---

**修改日期**: 2026-08-21  
**修改人**: AI Agent (P2 实体重复源头解决任务)  
**版本**: v1.0
