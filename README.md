# ATM Premium Yield 月度监测面板

月度监测 **恒生指数 / 沪深300指数 / 中证500ETF** 的 ATM（平值）看涨期权权利金率（Premium Yield），
编制方法参考港交所恒生指数备兑期权指数（HSICC）：每月交割日卖出下月 ATM call，`Premium Yield = 权利金 / 标的收盘价`。

## 功能

- **月度交割日口径**：每品种每月仅记录 1 个数据点（交割日），不做盘中/每日高频采集
- 三个标的一屏监测：最新月度标的收盘价、ATM行权价、ATM Call权利金、Premium Yield、合约代码
- 三品种月度对比图 + 各品种历史走势（鼠标移动查看交割日/标的/ATM/权利金/合约明细）
- 月度明细表（每品种，可滚动）
- 后台每日 15:35 自动检查当日是否交割日，是则落库；另提供手动「执行交割日检查」按钮
- 历史数据已回溯导入：HSI 2005年起（港交所口径月度费率）、CSI300 2020年起（IO研究）、CSI500 2022年起（510500研究）

## 数据源（全部免费/自有，大陆可直连）

| 标的 | 标的价格 | 期权行情 | 来源 |
|---|---|---|---|
| 恒生指数 | 富途 OpenD（HK.800000） | 恒指期权（HKEX）实时行情 | 富途 OpenD 指数期权接口 |
| 沪深300 | 富途 OpenD（SH.000300） | 沪深300股指期权（IO）实时行情 | 中金所官网 quote_IO.txt |
| 中证500 | 富途 OpenD（SH.510500） | 中证500ETF期权（510500）实时行情 | 上交所 yunhq.sse.com.cn T型接口 |

> 富途 OpenD 需本机运行网关（端口11111）并开通港股期权行情权限；
> 中金所/上交所接口为官方免费实时行情，无需凭证。

## 目录结构

```
atm_premium_monitor/
├── backend/
│   ├── main.py            # FastAPI 入口（uvicorn backend.main:app，每日15:35交割日检查）
│   ├── config.py          # 配置（OpenD地址、品种定义）
│   ├── engine.py          # ATM strike 与 Premium Yield 计算引擎
│   ├── scheduler.py       # 月度交割日采集（HSI=最后第二营业日/IO=第三个周五/ETF=第四个周三，含顺延窗口）
│   ├── store.py           # SQLite 历史存储（按 key+月 去重，每月1点）
│   └── data/
│       ├── futu_adapter.py    # 富途 OpenD（恒指点位 + 恒指期权）
│       ├── cffex_adapter.py   # 中金所 quote_IO.txt（IO期权）
│       └── sse_adapter.py     # 上交所 yunhq（510500/510300 ETF期权）
└── frontend/
    └── index.html         # 月度监测面板（ECharts，hover查看历史明细）
```

## 本地运行

```bash
# 1. 依赖（Python 3.9+）
pip install -r requirements.txt

# 2. 确保富途 OpenD 已运行（默认 127.0.0.1:11111），并开通港股期权行情

# 3. 启动
cd atm_premium_monitor
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000

# 4. 访问
#   http://127.0.0.1:8000/        监测页面
#   http://127.0.0.1:8000/api/current   当前快照(JSON)
#   http://127.0.0.1:8000/api/history?key=HSI   历史序列
#   http://127.0.0.1:8000/api/refresh  手动刷新
```

环境变量（可选）：`FUTU_HOST`、`FUTU_PORT`。

## 数据口径说明（月度交割日）

- 每品种**每月仅记录1个数据点**，在当月交割日（收盘后15:35）抓取"下月ATM call"权利金并落库：
  - HSI：每月最后第二个营业日（以富途官方到期日列表为准）
  - CSI300（IO）：每月第三个周五；CSI500（ETF期权）：每月第四个周三
  - 若规则日遇节假日休市，其后3天内首个能成功抓取的交易日补记（顺延窗口）
- 同一月份重复抓取不会产生新点（同月覆盖），历史已导入的月度点不会被改动
- 富途 OpenD 未运行时，交割日检查快速跳过（不阻塞），其余接口（历史/面板）不受影响

## 部署到国内云服务器（腾讯云/阿里云轻量）

1. 云服务器需能访问富途 OpenD 网关。OpenD 需在**同一台服务器**上运行（或内网互通），
   并用 `-futu_open_d_port=11111` 启动、开通行情权限。
2. 将本项目上传至服务器（如 `/opt/atm_premium_monitor`），`pip install -r requirements.txt`。
3. 后台常驻运行：
   ```bash
   nohup python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 > /var/log/atm.log 2>&1 &
   ```
4. （可选）云厂商控制台放行 8000 端口；用 Nginx 反向代理 + 域名绑定（HTTP 即可，大陆直连）。
5. 若富途 OpenD 无法在服务器运行（如需要本地富途账号），可仅部署在本地 Windows 并配合
   内网穿透（如 frp/花生壳）映射到公网访问。

## ATM 规则（与之前研究一致）

- ATM strike = roundup(标的收盘 ÷ 行权价间距) × 行权价间距（HSICC CVC.3，xm=0%）
- 行权价间距动态取：恒指 20000点以上 200点；中金所 IO 50/100点；上交所 ETF 期权 0.25元（5-10元区间）
- 监测"下一个月度到期日"合约（距今天≥10天），即 HSICC 展期卖出的下月 ATM call
- ETF 期权若因标的分红被除息调整（合约简称带 A/B），自动取行权价≥现货且最接近的 call

## 免责声明

本工具所载数据与计算仅供参考，不构成投资建议。市场有风险，投资需谨慎。
