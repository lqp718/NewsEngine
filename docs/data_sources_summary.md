# NewsEngine 数据源全景报告

**扫描日期**: 2026-08-15  
**代码版本**: 当前 main 分支  
**调度器**: `IngestionScheduler` — 每 15 分钟一个周期

---

## 一、总览

| # | 数据源 | SOURCE_TYPE | 数据类型 | 频率 | API/网站 | 状态 | 业务价值 |
|---|--------|-------------|----------|------|----------|------|----------|
| 1 | GDELT | `gdelt_csv` / `gdelt_events` | 全球事件（战争/抗议/外交/灾害） | 15 分钟 | data.gdeltproject.org (CSV) | ✅ 正常 | 地缘政治风险信号、全球宏观事件驱动 |
| 2 | RSS | `rss` | 金融新闻（Reuters/FT/WSJ 等） | 15 分钟 | feeds.content.dowjones.io, ft.com | ✅ 正常 | 权威财经新闻补充、英文源覆盖 |
| 3 | CLS 财联社 | `cls_telegraph` | 实时财经快讯（A 股为主） | 15 分钟 | cls.cn | ✅ 正常 | **A 股主力新闻源**（V6.1 Phase 1） |
| 4 | EastMoney 搜索 | `eastmoney` | 个股新闻（按股票名搜索） | 15 分钟 | search-api-web.eastmoney.com | ⚠️ 降级为 fallback | CLS 失败时的备选 |
| 5 | AkShare | `akshare` | 个股新闻（按股票代码） | 15 分钟 | akshare 库 (东方财富 API) | ⚠️ 降级为 fallback | EastMoney 也失败时的兜底 |
| 6 | CNInfo 巨潮 | `cninfo_announcement` | 上市公司官方公告（PDF） | 15 分钟 | cninfo.com.cn | ✅ 正常 | 法定信息披露（V6.2 Phase 2） |
| 7 | EastMoney 研报 | `eastmoney_research` | 分析师研报（PDF） | 15 分钟 | reportapi.eastmoney.com | ✅ 正常 | 机构观点/目标价/评级（V6.3 Phase 3） |
| 8 | FRED | `fred` | 美国宏观经济（GDP/CPI/失业率/联邦基金利率/PPI） | 15 分钟 | api.stlouisfed.org | ✅ 正常（需 API Key） | 美国宏观基本面 |
| 9 | BLS | `bls` | 美国就业数据（非农/失业率/时薪/CPI） | 15 分钟 | api.bls.gov | ✅ 正常（无需 Key） | 就业市场 + 通胀交叉验证 |
| 10 | EIA | `eia` | 美国能源数据（原油库存/产量/进出口/汽油价） | 15 分钟 | api.eia.gov | ✅ 正常（需 API Key） | 原油供需全景 |
| 11 | ACLED | `acled` | 武装冲突事件（战场/爆炸/抗议/暴乱） | 15 分钟 | acleddata.com | ❌ 已禁用 | 地缘冲突量化（账户被锁） |
| 12 | Sanctions | `sanctions` | 制裁名单（OFAC SDN + OpenSanctions） | 15 分钟 | api.opensanctions.org + OFAC CSV | ✅ 正常（无需 Key） | 制裁合规 + 地缘信号 |
| 13 | Treasury | `treasury` | 美国国债收益率曲线 | 15 分钟 | home.treasury.gov | ⚠️ 骨架实现 | 收益率曲线倒挂信号（未接入） |

**统计**: 13 个数据源，10 个正常/可用，1 个已禁用，1 个降级为 fallback，1 个骨架未接入。

---

## 二、按业务线分类

### 2.1 宏观数据管线（Macro Pipeline）

| 数据源 | 覆盖领域 | 具体数据 | 发布频率 | 对 NewsEngine 的作用 |
|--------|----------|----------|----------|---------------------|
| **GDELT** | 全球事件 | 战争、外交、抗议、灾害、恐怖袭击 | 实时（15 分钟更新） | 地缘政治风险早期预警；事件驱动新闻（如"叙利亚停火协议签署"） |
| **RSS** | 英文财经新闻 | Reuters/Bloomberg/FT/WSJ/CNBC 头条 | 实时（15 分钟更新） | 权威英文源覆盖；与 GDELT 互补 |
| **FRED** | 美国宏观经济 | GDP、CPI（消费者物价）、UNRATE（失业率）、DFF（联邦基金利率）、PPI（生产者物价） | 月度/季度/日度 | 宏观经济基本面跟踪；利率决策分析 |
| **BLS** | 美国就业市场 | 非农就业、失业率、平均时薪、CPI-U | 月度 | 就业数据交叉验证 FRED；工资通胀分析 |
| **EIA** | 美国能源 | 原油库存、原油产量、原油进口、原油出口、汽油零售价 | 周度（周三发布） | 原油供需全景；库存异常检测；能源价格预测 |
| **ACLED** | 武装冲突 | 战场事件、爆炸、抗议、暴乱、 fatalities | 日度 | ❌ 已禁用（账户锁定） |
| **Sanctions** | 制裁合规 | OFAC SDN 名单 + OpenSanctions 全球制裁名单 | 不定期 | 制裁事件实时检测；合规风险预警 |
| **Treasury** | 美国国债 | 收益率曲线（1M~30Y） | 日度 | ⚠️ 骨架未接入；潜在价值：收益率曲线倒挂预警 |

### 2.2 股票新闻管线（Stock Pipeline）

| 数据源 | 覆盖领域 | 具体数据 | 优先级 | 对 NewsEngine 的作用 |
|--------|----------|----------|--------|---------------------|
| **CLS 财联社** | A 股实时快讯 | 财联社电报（7x24 实时财经快讯） | 🥇 主力 | A 股新闻主源；覆盖公告、政策、行业动态 |
| **EastMoney 搜索** | A 股个股新闻 | 按股票名称搜索东方财富新闻 | 🥈 备选 | CLS 失败时的 fallback |
| **AkShare** | A 股个股新闻 | 按股票代码获取东方财富新闻 | 🥉 兜底 | 前两者都失败时的最终兜底 |
| **CNInfo 巨潮** | 上市公司公告 | 法定信息披露（年报/季报/临时公告，PDF 全文提取） | 🥇 主力 | 官方公告 = 最权威信息源；IPO/增发/分红/重组 |
| **EastMoney 研报** | 分析师研报 | 券商研报 PDF（目标价/评级/盈利预测） | 🥇 主力 | 机构观点跟踪；行业评级变化检测 |

---

## 三、数据源详细说明

### 3.1 GDELT（Global Database of Events, Language, and Tone）

- **SOURCE_TYPE**: `gdelt_csv`（GKG 管线）/ `gdelt_events`（Events 管线）
- **API**: `http://data.gdeltproject.org/gkg/` + `http://data.gdeltproject.org/events/`
- **认证**: 无需认证
- **数据量**: 每 15 分钟 ~数千条事件
- **过滤机制**:
  - Events-first 架构：Events CSV 为主源，GKG 为安全网
  - 三阶段过滤：CAMEO 事件代码白名单 → Goldstein 评分 ≥ 4.0 → NumMentions ≥ 1
  - Plan D：权威媒体域名无条件通过
- **频率**: 15 分钟（跟随调度器周期）
- **业务价值**: 全球地缘政治风险的"雷达"。能在主流媒体之前捕捉到冲突、政变、自然灾害等事件。

### 3.2 RSS（金融新闻聚合）

- **SOURCE_TYPE**: `rss`
- **API**: RSS 2.0 / Atom feeds
- **认证**: 无需认证
- **默认源**: Dow Jones MarketWatch Top Stories, Financial Times
- **配置**: `data/rss_feeds.json`（可动态增删）
- **过滤**: 内容相关性过滤（关键词白名单 + 域名黑名单）
- **频率**: 15 分钟
- **业务价值**: 英文权威财经新闻覆盖，与 GDELT 互补。

### 3.3 CLS 财联社（A 股主力新闻源）

- **SOURCE_TYPE**: `cls_telegraph`
- **API**: `cls.cn/v1/roll/get_roll_list`
- **认证**: 本地签名计算（md5(sha1(sorted query string))）
- **数据**: 7x24 实时财经快讯全文
- **频率**: 15 分钟
- **业务价值**: **A 股新闻的第一来源**。财联社快讯是 A 股交易员最常看的信息源，覆盖公告、政策、行业动态、突发事件。

### 3.4 EastMoney 搜索（A 股备选）

- **SOURCE_TYPE**: `eastmoney`
- **API**: `search-api-web.eastmoney.com/search/jsonp`
- **认证**: 无需认证（使用 curl_cffi 绕过反爬）
- **数据**: 按股票名称搜索，每只股票最多 20 条
- **频率**: 15 分钟
- **业务价值**: CLS 的 fallback。当 CLS 不可用时自动切换。

### 3.5 AkShare（A 股兜底）

- **SOURCE_TYPE**: `akshare`
- **API**: akshare 库封装的东方财富 API
- **认证**: 无需认证
- **数据**: 按股票代码获取新闻，每只股票最多 10 条
- **频率**: 15 分钟
- **业务价值**: 最终兜底。有速率限制（`akshare_request_interval_sec`）。

### 3.6 CNInfo 巨潮资讯（上市公司公告）

- **SOURCE_TYPE**: `cninfo_announcement`
- **API**: `cninfo.com.cn/new/hisAnnouncement/query`
- **认证**: 无需认证
- **数据**: 上市公司官方公告 PDF → PyMuPDF 全文提取 → Markdown
- **频率**: 15 分钟
- **业务价值**: **法定信息披露平台**。年报/季报/临时公告 = 最权威的公司信息源。对投资决策至关重要。

### 3.7 EastMoney 研报（分析师研报）

- **SOURCE_TYPE**: `eastmoney_research`
- **API**: `reportapi.eastmoney.com/report/list`
- **认证**: 无需认证
- **数据**: 券商研报 PDF → PyMuPDF 全文提取 → Markdown（最多 15 页）
- **频率**: 15 分钟
- **业务价值**: 机构观点跟踪。目标价/评级/盈利预测变化是重要的交易信号。

### 3.8 FRED（美联储经济数据）

- **SOURCE_TYPE**: `fred`
- **API**: `api.stlouisfed.org/fred/series/observations`
- **认证**: 需要 API Key（免费注册）
- **数据序列**:
  | 序列 ID | 含义 | 发布频率 |
  |---------|------|----------|
  | `GDP` | 美国 GDP | 季度 |
  | `CPIAUCSL` | 消费者物价指数 | 月度 |
  | `UNRATE` | 失业率 | 月度 |
  | `DFF` | 联邦基金利率 | 日度 |
  | `PPIACO` | 生产者物价指数 | 月度 |
- **频率**: 15 分钟轮询，90 天回溯
- **业务价值**: 美国宏观经济基本面。GDP 增长、通胀、就业、利率是决定市场方向的核心变量。

### 3.9 BLS（美国劳工统计局）

- **SOURCE_TYPE**: `bls`
- **API**: `api.bls.gov/publicAPI/v2/timeseries/data/`
- **认证**: 无需认证
- **数据序列**:
  | 序列 ID | 含义 | 发布频率 |
  |---------|------|----------|
  | `CES0000000001` | 非农就业人数 | 月度 |
  | `LNS14000000` | 失业率 | 月度 |
  | `CES0500000003` | 平均时薪 | 月度 |
  | `CUUR0000SA0` | CPI-U（全部商品） | 月度 |
- **频率**: 15 分钟轮询
- **业务价值**: 就业市场深度数据。与 FRED 交叉验证失业率；工资增长是通胀领先指标。

### 3.10 EIA（美国能源信息管理局）

- **SOURCE_TYPE**: `eia`
- **API**: `api.eia.gov/v2`
- **认证**: 需要 API Key（免费注册）
- **数据序列**:
  | 序列 ID | 含义 | 路由 | 频率 |
  |---------|------|------|------|
  | `WCRSTUS1` | 原油库存（千桶） | petroleum/stoc/wstk | weekly |
  | `WCRFPUS2` | 原油产量（千桶/日） | petroleum/crd/crpdn | weekly |
  | `WCRIMUS2` | 原油进口（千桶/日） | petroleum/move/wkly | weekly |
  | `WCREXUS2` | 原油出口（千桶/日） | petroleum/move/wkly | weekly |
  | `WGASUS1` | 汽油零售价（美元/加仑） | petroleum/pri/gnd | weekly |
- **频率**: 15 分钟轮询（数据本身是周度发布）
- **业务价值**: 原油供需全景。库存 + 产量 + 进出口 = 完整的供需平衡表。周三 EIA 周报是原油市场最重要的定期事件。

### 3.11 ACLED（武装冲突位置与事件数据）

- **SOURCE_TYPE**: `acled`
- **API**: `acleddata.com/api/acled/read`
- **认证**: OAuth2（用户名 + 密码）
- **状态**: ❌ **已禁用** — 2026-08-14 OAuth2 迁移后反复登录导致 `flood_user_blocked`
- **配置**: `.env` 中 `ACLED_USERNAME` / `ACLED_PASSWORD` 已注释
- **业务价值**: 武装冲突量化数据（战场/爆炸/抗议/暴乱/死亡人数）。理论上对地缘政治风险量化极有价值，但当前不可用。

### 3.12 Sanctions（制裁名单）

- **SOURCE_TYPE**: `sanctions`
- **API**: `api.opensanctions.org` + OFAC SDN CSV
- **认证**: OpenSanctions 需要 API Key（付费）；OFAC 无需认证
- **降级策略**: OpenSanctions 失败 → 自动回退到 OFAC SDN CSV
- **数据**: 全球制裁名单（个人/实体/船舶/飞机），~17,000 条记录
- **频率**: 15 分钟
- **业务价值**: 制裁事件实时检测。新增制裁 = 强信号（severity=high）。

### 3.13 Treasury（美国国债收益率曲线）

- **SOURCE_TYPE**: `treasury`
- **API**: `home.treasury.gov`（计划中）
- **状态**: ⚠️ **骨架实现** — 只有辅助函数（`_detect_inversion`, `_build_yield_curve_body`），fetch() 返回空
- **潜在价值**: 收益率曲线倒挂（2Y > 10Y）是经典衰退信号。

---

## 四、调度架构

```
IngestionScheduler (每 15 分钟)
├── Macro Pipeline（宏观管线）
│   ├── GDELT Adapter → 全球事件
│   ├── RSS Adapter → 英文财经新闻
│   ├── FRED Adapter → 美国宏观经济
│   ├── BLS Adapter → 美国就业数据
│   ├── EIA Adapter → 美国能源数据
│   ├── Sanctions Adapter → 制裁名单
│   ├── ACLED Adapter → 武装冲突（已禁用）
│   └── Treasury Adapter → 国债收益率（骨架）
│
├── Stock Pipeline（股票管线）
│   ├── CLS Adapter → A 股快讯（主力）
│   ├── EastMoney Adapter → 个股新闻（备选）
│   ├── AkShare Adapter → 个股新闻（兜底）
│   ├── CNInfo Adapter → 上市公司公告
│   └── EastMoney Research Adapter → 分析师研报
│
├── Briefing Aggregator → 行业简报聚合
└── TTL Cleanup → 每日清理过期数据
```

---

## 五、数据源覆盖矩阵

| 维度 | 覆盖情况 | 缺口 |
|------|----------|------|
| **地缘政治** | GDELT（全球事件）+ Sanctions（制裁） | ACLED 已禁用；缺少情报类源 |
| **美国宏观** | FRED（GDP/CPI/利率）+ BLS（就业）+ EIA（能源） | Treasury 未接入（缺收益率曲线） |
| **A 股新闻** | CLS（快讯）+ EastMoney（搜索）+ AkShare（兜底） | 三层降级，覆盖充分 |
| **A 股公告** | CNInfo（法定披露） | 覆盖充分 |
| **A 股研报** | EastMoney Research | 覆盖充分 |
| **英文财经** | RSS（Reuters/FT/WSJ） | 源数量可扩展 |
| **原油/能源** | EIA（5 个周度序列） | 覆盖充分 |
| **中国宏观** | ❌ 缺失 | 无中国 GDP/CPI/PMI 等数据源 |
| **外汇/利率** | ❌ 缺失 | 无汇率/央行利率数据源 |
| **加密货币** | ❌ 缺失 | 无 BTC/ETH 等数据源 |

---

## 六、关键配置

### API Keys（`.env`）

| Key | 数据源 | 状态 |
|-----|--------|------|
| `FRED_API_KEY` | FRED | ✅ 已配置 |
| `EIA_API_KEY` | EIA | ✅ 已配置 |
| `OPEN_SANCTIONS_API_KEY` | Sanctions | ❌ 未配置（降级到 OFAC） |
| `ACLED_USERNAME` / `ACLED_PASSWORD` | ACLED | ❌ 已注释（账户锁定） |

### 调度参数

| 参数 | 值 | 说明 |
|------|-----|------|
| `ingestion_interval_sec` | 900 (15 分钟) | 调度周期 |
| `news_max_age_days` | 14 | 数据有效窗口 |
| `_EIA_MAX_AGE_DAYS` | 60 | EIA 专用窗口（覆盖月度数据） |
| `_BLS_MAX_AGE_DAYS` | 60 | BLS 专用窗口（覆盖月度数据） |
| `_FRED_FETCH_LOOKBACK_DAYS` | 90 | FRED 回溯窗口 |

---

## 七、总结

**NewsEngine 当前拥有 13 个数据源**，覆盖三大业务线：

1. **宏观数据管线**（8 个源）：GDELT + RSS + FRED + BLS + EIA + Sanctions + ACLED(禁用) + Treasury(骨架)
2. **股票新闻管线**（5 个源）：CLS + EastMoney + AkShare + CNInfo + EastMoney Research
3. **辅助模块**：Authoritative Media Domains（~30 个权威财经域名白名单）

**核心优势**：
- A 股新闻三层降级（CLS → EastMoney → AkShare），可靠性高
- 美国宏观数据覆盖全面（GDP/CPI/就业/能源/制裁）
- EIA 原油供需 5 序列刚刚修复，数据完整

**主要缺口**：
- 中国宏观数据（GDP/CPI/PMI）完全缺失
- Treasury 收益率曲线未接入
- ACLED 武装冲突数据不可用
- 无外汇/加密货币数据源
