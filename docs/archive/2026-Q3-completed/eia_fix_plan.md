# EIA Adapter 整改方案 — WCRIMUS2 / WCREXUS2 修复

## 一、这两个数据源是什么？价值在哪？

### 数据定义
| 系列 ID | 含义 | 单位 |
|---------|------|------|
| WCRIMUS2 | 美国原油**周度进口量** | 千桶/日 (Mbbl/d) |
| WCREXUS2 | 美国原油**周度出口量** | 千桶/日 (Mbbl/d) |

来源：EIA Weekly Petroleum Status Report (WPSR)，每周三发布（美东 10:30）。

### 对 NewsEngine 的价值
1. **供需平衡核心指标**：净进口 = 进口 - 出口，直接反映美国国内原油供应松紧
2. **库存先行指标**：进口增 + 出口减 → 库存堆积 → 油价承压（反之亦然）。EIA 库存周报是原油市场每月最重要的事件之一
3. **地缘政治信号**：制裁、冲突（如红海危机）会立刻反映在贸易流突变上
4. **与已有数据的联动**：WCRSTUS1（库存）+ WCRFPUS2（产量）+ 进出口 = 完整的供需全景，比单一序列的信号强得多

**结论：这两个序列价值高，绝不能禁用。它们是"原油供需"主题的核心组成。**

---

## 二、我们错在哪里（对照 API 文档）

### 错误 1：路由是猜的，不符合 EIA API v2 真实结构

我们当前代码：
```python
"WCRIMUS2": {"route": "petroleum/imp/impw", ...}  # ❌ 404/400
"WCREXUS2": {"route": "petroleum/exp/expw", ...}  # ❌ 404/400
```

EIA API v2 文档（documentation.php）明确说明：
> "Datasets are arranged in a logical hierarchy. Member datasets may be discovered by querying their parent node."
> （数据集按层级组织，可通过查询父节点发现子数据集）

实测 API 自发现结果（GET /v2/petroleum）：
```
petroleum 子路由：
  - sum: Summary
  - pri: Prices
  - crd: Crude Reserves and Production
  - pnp: Refining and Processing
  - move: Imports/Exports and Movements   ← 进出口在这里！
  - stoc: Stocks
  - cons: Consumption/Sales
```

官方 EIAOpenData 库文档示例（pypi.org/project/EIAOpenData）：
> 选择 "Petroleum/Imports/Exports And Movements/**Weekly Imports & Exports**"
> URL: `.../petroleum/move/wkly?frequency=weekly&data=value;...`
> route = **`petroleum/move/wkly`**

**正确路由：`petroleum/move/wkly`**（周度进出口）
（注：`petroleum/move/imp`、`petroleum/move/exp` 也存在，但那是月度/更细粒度的移动数据）

### 错误 2：Facets 不对

我们当前代码：
```python
"facets": {"duoarea": "NUS", "product": "WST"}  # ❌ product=WST 不是原油
```

实测 move 数据的正确 facet 结构（来自成功的 API 响应）：
```json
{
  "period": "2026-05",
  "duoarea": "NUS-Z00",        // 或 R10-Z00 (PADD区域)
  "product": "EPC0",            // ← EPC0 = Crude Oil
  "process": "IM0",             // ← IM0 = Imports（进口）
  "process-name": "Imports",
  "series": "MAPIMP11",
  "value": "966",
  "units": "MBBL"
}
```

- `product=EPC0` = 原油（不是 WST！WST 是"Stock 系列"的产品代码，用在 stoc 路由）
- `process=IM0` = 进口；`process=EEX` = 出口
- `duoarea=NUS` 或 `NUS-Z00` = 美国全国

### 错误 3：没有指定频率

EIA API v2 支持 `frequency` 参数（weekly/monthly/annual/four-week-average）。
我们没传 → 默认可能不是 weekly。move/wkly 路由需要显式 `frequency=weekly`。

### 错误 4：系列 ID 命名误导

我们自定义的 `WCRIMUS2`/`WCREXUS2` 不是 EIA 标准 v1 系列 ID（seriesid 转换端点返回 404）。
EIA 标准的周度进出口 v1 系列 ID 是：
- `WTTIMUS2`（Weekly Total Imports US）
- `WTTEXUS2`（Weekly Total Exports US）

但没关系——用 API v2 路由 + facets 查询即可，不依赖 v1 ID。

---

## 三、整改方案（具体代码修改）

修改 `src/adapters/eia_adapter.py` 中的 `_EIA_SERIES`：

```python
_EIA_SERIES: dict[str, dict[str, Any]] = {
    # ... WCRSTUS1, WCRFPUS2, WGASUS1 保持不变 ...

    # Weekly US crude oil imports (thousand barrels/day)
    "WCRIMUS2": {
        "route": "petroleum/move/wkly",      # ✅ 正确路由（周度进出口）
        "facets": {
            "product": "EPC0",               # ✅ Crude Oil
            "process": "IM0",                # ✅ Imports
            "duoarea": "NUS",                # ✅ 全美
        },
        "frequency": "weekly",                # ✅ 显式周度
        "name": "Weekly U.S. Imports of Crude Oil",
        "units": "Thousand Barrels per Day",
        "topic": "Crude Oil Imports",
    },
    # Weekly US crude oil exports (thousand barrels/day)
    "WCREXUS2": {
        "route": "petroleum/move/wkly",      # ✅ 正确路由
        "facets": {
            "product": "EPC0",               # ✅ Crude Oil
            "process": "EEX",                # ✅ Exports
            "duoarea": "NUS",                # ✅ 全美
        },
        "frequency": "weekly",                # ✅ 显式周度
        "name": "Weekly U.S. Exports of Crude Oil",
        "units": "Thousand Barrels per Day",
        "topic": "Crude Oil Exports",
    },
}
```

### fetch() 方法同步修改

在 `fetch()` 中支持可选的 `frequency` 参数：

```python
params: dict[str, Any] = {
    "api_key": settings.eia_api_key,
    "data[]": "value",
    "sort[0][column]": "period",
    "sort[0][direction]": "desc",
    "length": str(_EIA_FETCH_LENGTH),
}
if cfg.get("frequency"):
    params["frequency"] = cfg["frequency"]
for facet, value in cfg["facets"].items():
    params[f"facets[{facet}][]"] = value
```

---

## 四、验证步骤

```bash
# 1. 单序列验证（绕过代理直连）
cd /Users/liuqipeng/Projects/MyWallet/NewsEngine
.venv/bin/python -c "
import os, httpx
os.chdir('.')
key = [l.split('=')[1].strip() for l in open('.env') if l.startswith('EIA_API_KEY=')][0]
c = httpx.Client(timeout=15, trust_env=False)
r = c.get('https://api.eia.gov/v2/petroleum/move/wkly/data/', params={
    'api_key': key, 'frequency': 'weekly', 'data[]': 'value',
    'facets[product][]': 'EPC0', 'facets[process][]': 'IM0', 'facets[duoarea][]': 'NUS',
    'sort[0][column]': 'period', 'sort[0][direction]': 'desc', 'length': 5})
print(r.status_code)
print(r.json()['response']['total'])
for rec in r.json()['response']['data'][:3]: print(rec)
"

# 2. 完整 adapter 验证
.venv/bin/python -m pytest tests/ -k eia -v
```

---

## 五、风险与回退

| 风险 | 应对 |
|------|------|
| `petroleum/move/wkly` 路由在高峰期响应慢 | fetch() 已有超时保护，失败仅 warn 不崩溃 |
| process facet 取值需最终确认（IM0/EEX） | 验证步骤 1 会打印实际返回记录，确认无误再合并 |
| EIA 限流（近期请求过多触发） | 等待冷却（文档说自动恢复），减少调试请求次数 |

---

## 六、需要用户决策的点

1. **是否同时修复 WCRSTUS1/WCRFPUS2 的频率显式化？**（它们当前能工作，但没显式指定频率，建议一并加 `"frequency": "weekly"`）
2. **验证后是否把 `petroleum/move/imp`（月度）也接入？**（可作为低频补充数据，但当前需求是周度，暂不必要）
