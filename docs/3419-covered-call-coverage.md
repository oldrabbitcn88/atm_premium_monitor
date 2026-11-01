# 3419.HK（Global X 恒指备兑 ETF）持仓下载与备兑覆盖比率统计

> 目标：拿到 3419 历史上**每日**的正股多头、恒指期货多头、卖出备兑认购期权持仓，
> 并统计每日「备兑期权名义 / 正股多头敞口」的覆盖比率。

---

## 0. 先说可行性结论

| 你想要的 | 能不能拿到 | 途径 | 回溯起点 |
|---|---|---|---|
| **每日**逐日全量持仓的历史存档 | **公开渠道没有** | —— | —— |
| 每日全量持仓，从今天起往后 | 能，且是唯一能得到真·逐日序列的办法 | 官网当日持仓文件 + 自建每日快照 | 你开始跑的那天 |
| **月度**覆盖率/行权价/权利金 | 能 | 官方《Covered Call Monthly Commentary》PDF | 2024-03 起（基金 2024-02-28 成立） |
| **半年度**逐张期权/期货明细（审计口径） | 能，最权威 | 中期报告 / 年度报告的「投资组合」表 | 首个报告期起 |
| 零散的历史日期 | 碰运气 | Internet Archive 快照 | 看爬虫抓过什么 |

**为什么没有每日历史**：SFC 对 ETF 的要求是「每日披露全部持仓」，但**不要求保留历史**。
官网那份持仓文件每天被**原地覆盖**，昨天的就没了。所以任何人想要逐日序列，
只有两条路：要么自己从某天起每天抓并存档（本仓库脚本做的事），要么买 Bloomberg / 万得 / Refinitiv 的历史持仓库。

> 一句话：**「下载过去所有」做不到，但「把能拿到的三个频率拼起来 + 从现在起自建逐日档」可以做到，
> 而且足够回答覆盖比率的问题** —— 因为该基金的覆盖比率由策略机械决定，月度/半年度点位已能刻画其水平与波动，
> 逐日档只是让展期那几天的细节看得清。

---

## 1. 这只基金的组合长什么样（决定了覆盖比率怎么定义）

3419 = Global X 恒生指数备兑认购主动型 ETF（曾用名 Global X HSI **Components** Covered Call Active ETF），
管理人 Mirae Asset Global Investments (Hong Kong)，2024-02-28 成立、02-29 上市，TER 0.75%，月度派息（不保证、可能来自资本）。

招募说明书的策略口径是三块：

1. **多头腿**：恒生指数**成分股正股** + **恒指 ETF**（如 2800 盈富基金）+ **恒指期货多头**
2. **空头腿**：**卖出恒生指数认购期权**（可在交易所或 OTC 写出）
3. 现金/保证金：期货与卖出期权都要交保证金

> 注意空头腿写的是**指数期权**，不是逐只成分股的个股期权。
> 所以覆盖比率必须把多头那三块先折算成**「指数等价名义」**，再和期权名义比——
> 这也是本仓库 `backend/coverage.py` 的核心逻辑。

另有同系列 3416（国指备兑），口径一致，脚本 `--code 3416` 即可复用。

---

## 2. 每日持仓怎么下载（A 层：从现在起的逐日档）

### 2.1 官网路径

基金详情页 → 「基金持仓 / Fund Holdings」区块 → 「查看全部持仓 / See all holdings」→ 下载链接（CSV 或 XLSX）。
页面上会标 **As of / 截至** 日期，通常是 **T-1**（前一交易日），不是当天。

### 2.2 关键障碍：持仓是前端异步加载的

Global X 香港站的持仓表多为 JS 异步取数后渲染，所以：

- 直接 `curl` 基金页的 HTML **拿不到持仓行**（HTML 里只有空壳）；
- 下载链接本身也可能是带签名/带日期参数的动态地址。

**找真实地址的做法**（一次性，找到后填进脚本就能长期自动化）：

1. Chrome 打开 `https://www.globalxetfs.com.hk/zh-hant/funds/3419/`（或 `/en/funds/3419/`）
2. F12 → **Network** → 勾 **Fetch/XHR** → 刷新页面
3. 在请求列表里找名字含 `holding` / `portfolio` / `position` / `constituent` 的请求，
   或者直接按 **Size 倒序**排——持仓是整页最大的那笔 JSON
4. 右键该请求 → **Copy → Copy as cURL**，就拿到了带全部必要 header 的可复现命令
5. 把 URL 填给脚本：`python -m backend.tools.gx_coverage fetch --url '<那个地址>'`
   （脚本本身也会自动从页面里扫候选地址，多数情况不用手工找）

### 2.3 自动化（本仓库已实现）

```bash
pip install requests openpyxl

# 抓当日持仓 → 存档 → 重算覆盖比率序列（恒指点位自动取）
python -m backend.tools.gx_coverage --code 3419 --hsi auto fetch
```

产出（默认 `deploy/data/gx3419/`）：

```
raw/2026-10-31.json   当日标准化持仓快照（含原始列，可复算、可审计）
coverage.json         覆盖比率时间序列
coverage.csv          同上，Excel/pandas 直接用
```

已配好 `.github/workflows/gx3419.yml`：每日两次（港时 09:30 / 18:30）自动抓并回写仓库。
**按 `as_of` 去重**，所以一天跑多次、事后补跑都不会重复落点。

### 2.4 兜底：手工下载也能进同一条流水线

官网加了人机校验、或你只是想先验证口径时：

```bash
# 手工把持仓文件下载到本地，csv/xlsx/json/html 都能吃
python -m backend.tools.gx_coverage --hsi 26013.5 local ~/Downloads/3419_holdings_20261031.csv
```

---

## 3. 历史怎么补（B / C / D 层）

### 3.1 B 层｜官方备兑月报 PDF（**性价比最高的历史来源**）

Mirae Asset 香港每月出一份备兑系列月报，里面通常直接给**当月写出的期权行权价、到期、
权利金、以及覆盖/备兑比例**——正是你要的东西，而且是官方口径，不用自己从持仓反算。

已确认存在的真实地址形如（注意命名不统一，EN/ENG、有无 `_Final` 都出现过）：

```
https://investments.miraeasset.com.hk/docs/ETF/24-08-Covered-call-monthly_EN_Final.pdf
https://investments.miraeasset.com.hk/docs/ETF/26-01-Covered-Call-monthly_ENG.pdf
https://investments.miraeasset.com.hk/docs/ETF/26-03-Covered-Call-monthly_ENG_Final.pdf
```

脚本会穷举候选命名并联网验证哪些真实存在：

```bash
python -m backend.tools.gx_coverage monthly-urls --start 2024-03 --check
```

### 3.2 C 层｜中期/年度报告的「投资组合」表（**最权威、可审计**）

基金法人实体是 **Global X Exchange Traded Funds Series OFC**（开放式基金型公司）。
中期（6/30）与年度（12/31）报告里的 *Investment Portfolio* / *Statement of Movements in Portfolio Holdings*
会**逐张**列出：

- 每只成分股的股数、市值、占净值比例
- **期货合约**：合约名、张数、名义金额、未实现盈亏
- **卖出的认购期权**：行权价、到期日、张数/名义、公允价值（负数=负债）

去哪找：

1. Mirae Asset 香港站的 **Fund Documents / 基金文件** 区块（年报、半年报、月报都在那）
2. HKEXnews 文件搜索 `https://www1.hkexnews.hk/search/titlesearch.xhtml`
   → 股份代号填 `3419`，或名称搜 `Global X Exchange Traded Funds Series OFC`
   → 文件类别选「财务报表/年报」
   （ETF/OFC 的报告有时只挂在管理人网站，HKEXnews 以公告为主，两边都要看）

这两份是**校验基准**：你自建的逐日档，在 6/30 和 12/31 这两天应该和报告对得上。
对不上就说明解析口径有问题——务必做这个对账。

### 3.3 D 层｜Internet Archive 补零散历史

```bash
python -m backend.tools.gx_coverage --code 3419 wayback --since 20240228
```

脚本用 CDX API 列出所有历史快照并逐个尝试解析。
**预期产出很稀疏**：爬虫不执行 JS，所以异步加载的持仓往往没被存下来。
这是方法的天花板，不是脚本的问题。能捞到几个日期算几个。

---

## 4. 覆盖比率怎么算（三种口径，都算，别只看一个）

### 4.1 分母：多头「指数等价名义」

```
L = Σ 正股市值
  + Σ 指数 ETF 市值（2800 等，等同指数多头）
  + Σ 期货张数 × 期货价 × 合约乘数
```

合约乘数（HKEX 官方条款，HKD/指数点）：恒指 **50**、小型恒指 MHI **10**、国指 HHI **50**、小型国指 MCH **10**。

### 4.2 分子与三种口径

| 口径 | 公式 | 读法 |
|---|---|---|
| **现货名义** `coverage_spot` | `Σ|空头张数| × 指数点位 × 乘数 / L` | 行业通称 *overwrite ratio*：组合有多少比例被备兑掉。**主口径** |
| **行权价名义** `coverage_strike` | `Σ|空头张数| × 行权价 × 乘数 / L` | 按行权价计的封顶敞口。价外时 > 现货口径，其差额≈让出去的那段上行空间 |
| **delta 调整** `coverage_delta_est` | `Σ|空头张数| × 指数点位 × 乘数 × Δ / L` | **真实让渡的上行参与度**。需假设波动率，属估算 |

说明：

- **买入的认购腿**（展期、价差结构）会**抵减**覆盖，脚本按净张数算。
- `coverage_spot ≈ 100%` 是满额备兑的设计结果，所以**有信息量的恰恰是偏离**：
  展期当天的空窗、分批阶梯、建仓/赎回造成的临时欠覆盖。
- delta 口径才是「上行还剩多少」的答案：满额备兑但深度价外时，
  `coverage_spot=100%` 而 `coverage_delta≈35%`，两者差别巨大。本仓库示例算例即如此。

### 4.3 延伸口径（按需）

- **beta 调整**：基金持的是成分股**篮子**而非指数本身，100% 名义覆盖 ≠ 100% 对冲。
  严格做法是 `L_beta = Σ(个股市值 × β_i) + ETF + 期货名义`。跟踪误差小的话差异有限。
- **接上本仓库的 HSICC 权利金率**：
  `组合层面权利金收入率 ≈ 覆盖比率 × ATM Premium Yield`。
  本仓库 `deploy/data/hsi.json` 已有恒指逐月 ATM 权利金率（HSICC 口径），
  两者相乘即可把「基金实际收了多少权利金」和「满额备兑理论上能收多少」拆开比。

---

## 5. 十个坑（都是会把数算错的）

1. **期货名义不能用持仓表的 Market Value**。表里期货那行的 MV 常常只是**浮动盈亏**（甚至 0 或负数）。
   必须 `张数 × 期货价 × 乘数` 自己算。少算的话分母偏小、覆盖比率虚高。
   （本仓库有专门的单元测试盯这条）
2. **卖出期权的 MV 是负数（负债），绝不能计入分母多头名义**。
3. **乘数别认错**：小型恒指 MHI 是 10 不是 50，且合约代码常与月码连写（`MHIX6`），
   正则要求右词边界会漏判。
4. **张数符号**：有的持仓表不给负号，而在类别列写 `Written` / `Short` / `賣出`。两种都要认。
5. **`As of` 是 T-1**，别当成抓取日。按 `as_of` 去重，不是按抓取时间。
6. **周期权**：恒指除月度期权还有**周期权**，混在一起时加权剩余期限会被拉短，分析时建议按到期分桶。
7. **到期日规则**：恒指期货/期权到期日是**该月最后营业日的前一个营业日**（倒数第二个营业日），
   遇香港公众假期会前移——脚本内置的推算只扣周末，精确到期日要以交易所列表为准。
8. **OTC 期权**：若写在场外，披露的是名义金额而非张数，行权价口径可能缺失。
   脚本会把该腿标进 `warnings`，现货口径仍可算。
9. **份额申赎**：规模变动当天，正股先到、期权后补（或反之），会出现**一两天的欠覆盖**，
   那不是策略变了，别当信号解读。
10. **净值对账**：`Σ正股 + ETF + 现金 + 期货浮盈 − 期权负债 ≈ 基金净资产`。
    每次解析后用这条校验，列名映射错了会立刻暴露。

---

## 6. 拿到序列之后统计什么

```python
import pandas as pd
df = pd.read_csv("deploy/data/gx3419/coverage.csv", parse_dates=["as_of"]).set_index("as_of")

df[["coverage_spot", "coverage_strike", "coverage_delta_est"]].describe()
df["coverage_spot"].plot()                                   # 覆盖率时间序列
df["wavg_moneyness"].plot()                                  # 行权价价外程度漂移
df[df.coverage_spot < 0.95]                                  # 欠覆盖的日子，逐个归因
df.groupby(df.index.to_period("M"))["coverage_spot"].agg(["mean", "min", "max"])
df.assign(dte_bucket=pd.cut(df.wavg_dte, [0, 7, 21, 45, 400])).groupby("dte_bucket")["coverage_spot"].mean()
```

重点看四件事：

1. **覆盖率水平与稳定性**：均值是否贴 100%，标准差多大
2. **展期行为**：按 `wavg_dte` 看是否月度展期、提前几天展、展期当天覆盖率掉多少
3. **行权价选择**：`wavg_moneyness` 是常年 ATM（≈0）还是价外（>0），以及是否随波动率调整
4. **真实上行让渡**：`coverage_delta_est` 的分布——这才是持有人实际放弃的参与度

---

## 7. 工具速查

```bash
# 抓当日 + 入档 + 重算（恒指点位自动取腾讯行情）
python -m backend.tools.gx_coverage --code 3419 --hsi auto fetch

# 手工下载的文件入档
python -m backend.tools.gx_coverage --hsi 26013.5 local ./3419_holdings_20261031.csv

# 只重算（不联网）；可用历史点位表修正现货口径
python -m backend.tools.gx_coverage report --index-levels hsi_closes.json --vol 0.22

# Internet Archive 回填
python -m backend.tools.gx_coverage --code 3419 wayback --since 20240228

# 官方备兑月报候选地址（带 --check 则联网验证哪些存在）
python -m backend.tools.gx_coverage monthly-urls --start 2024-03 --check

# 自测
python -m unittest discover -s tests
```

指数点位缺省时的降级链：`--hsi 显式值` → 快照里的 `index_level` → **期货价近似**（含基差，会在 `warnings` 标注）。
不给点位也能跑，但现货口径会有基差级别的偏差。

---

## 8. 本文档里哪些是已核实的、哪些要你再确认

**已核实**（公开资料可查）：基金策略三块腿的构成、2024-02-28 成立/02-29 上市、TER 0.75%、
管理人 Mirae Asset HK、月度派息、备兑月报 PDF 的真实命名样式、HKEX 合约乘数与到期日规则。

**需你在能联网时确认**（本次编写环境的出网被网络策略拦掉了，`www.globalxetfs.com.hk`
无法解析，所以这几项我没法替你验证）：

1. 官网持仓文件的**真实下载地址与格式**（CSV 还是 XLSX、列名具体叫什么）
   → 按 §2.2 用 F12 找一次即可；脚本的列名别名表已覆盖中英简繁常见写法，
   若有没覆盖到的列名，加进 `backend/data/globalx_adapter.py` 的 `COLUMN_ALIASES` 就行
2. **恒指点位接口** `qt.gtimg.cn/q=hkHSI` 的字段位（脚本已做 3 个字段位的容错 + 合理区间校验，
   首次运行时请核对一下取到的点位对不对）
3. 中期/年度报告在 **HKEXnews 还是只在管理人网站**（§3.2 两处都给了入口）

---

## 9. 口径与免责

本文方法与脚本仅做公开信息的下载与口径换算，不构成投资建议。
覆盖比率依赖官网持仓披露的准确性与及时性；delta 口径为假设波动率下的估算，
不等于基金真实持仓的风险敞口。半年度报告是唯一经审计的口径，请以其为准做对账。
