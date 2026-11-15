# ATM Premium Yield 静态化部署（GitHub Actions 算数 + COS 存数据 + 静态托管跑页面）

三层拆分：**GitHub Actions 算数 → 数据落 COS → 页面由静态托管产品渲染**。

```
GitHub Actions（免费）              本机 OpenD（每日16:30计划任务）
  └─ 拉中金所IO/上交所510500   ┌──────┘ 抓恒指点位+恒指期权
  └─ 算 ATM PY → data.json     └──→ hsi.json ──git push──┐
        │                                                 │
        ├──── coscmd 上传 ────► 腾讯云 COS               │
        │                       ├── data.json            │
        │                       └── hsi.json ◄───────────┘
        │                              ▲ fetch()
        └──────► 仓库 main ──► 静态托管产品（EdgeOne Pages 等）
                                ├── index.html   ← 唯一需要被浏览器渲染的文件
                                └── vendor/echarts.min.js
```

## 为什么是这个拆法（重要，别再走回头路）

**COS 不能托管 HTML。** 2024-01-01 后创建的存储桶，走默认域名（**含静态网站域名
`*.cos-website.*`**）访问任意类型文件都会被强制下载，响应带平台注入的
`x-cos-force-download: true`，优先级高于对象自身的 `Content-Disposition: inline`，
`coscmd -H` 怎么设都无效。这是产品边界（COS 是对象存储，不是 web 托管），不是配置问题。

> 附带一个坑：`coscmd upload` **没有 `--metas` 参数**，设响应头要用 `-H`（`--headers`），
> 传 `--metas` 会因非法参数直接失败。早期 workflow 就栽在这里，而且失败得很安静。

**但 COS 完全可以放数据。** `fetch()` 和 `<script src>` 都**不读** `Content-Disposition`，
强制下载策略对它们无效。所以 `data.json` / `hsi.json` 走 COS 默认域名毫无问题，
额外好处是数据更新不需要重新构建站点。

**排查手法**：`curl -sS -o /dev/null -D - <url>`。
注意**必须用 GET**——COS 的 HEAD 响应不返回 `Content-Disposition`，
用 `curl -I` 自检会得到"一切正常"的假象。

## 大陆网络约束

- `github.io`、`cdn.jsdelivr.net` 裸网均不可用，故 GitHub Pages 托管 + jsDelivr CDN 这条路不成立；
  echarts 已自托管在 `deploy/frontend/vendor/`
- 开发机若挂了代理，**在本机做的"大陆能否访问"测试一律不可信**
  （代理 TUN 模式会把 DNS 解析到 `198.18.0.0/15` fake-IP 段），必须用手机流量验
- 各静态托管产品的默认域名都不能长期用：CloudBase 免费额度仅一个环境；
  EdgeOne Pages 默认域名只有 3 小时限时预览。**长期可用必须绑自己的域名**

## 目录结构

```
atm_premium_monitor/
├── .github/workflows/
│   └── daily.yml              # 唯一一份 workflow：计算 + 回写 + 发布 Pages
├── deploy/
│   ├── data/
│   │   ├── data.json          # CSI300 + CSI500 历史+最新（Actions 维护并自动回写仓库）
│   │   └── hsi.json           # HSI 历史+最新（本机同步维护并 git push）
│   ├── actions/
│   │   └── build_data.py      # A股计算脚本（Actions 运行，仅需 requests）
│   ├── local/
│   │   └── sync_hsi.py        # 本机恒指同步（OpenD + Windows 计划任务）
│   ├── frontend/
│   │   └── index.html         # 纯静态前端（同目录读 data.json + hsi.json）
│   ├── export_initial_json.py # 一次性：从 SQLite 导出基线 JSON（本机已跑过）
│   └── README-DEPLOY.md       # 本文档
```

## 站点地址

**https://atm-premium-yield-d2dab164c1808d-1341815007.tcloudbaseapp.com/**

> 默认域名 = `<envId>-<APPID>.tcloudbaseapp.com`，**带 APPID 后缀**，
> 不是纯 envId。用错域名会返回 `HTTP 418`（易被误判成"托管没开通"）。
> 腾讯云提示默认域名有访问频率限制、仅供开发测试；长期正式使用建议绑自定义域名。

## 一、静态托管（页面）

页面托管产品直接对接本仓库，检测到 push 后自行构建发布。以 EdgeOne Pages 为例，
项目配置：

| 项 | 值 |
|---|---|
| 生产分支 | `main` |
| 框架预设 | Other |
| 根目录 | `./` |
| **输出目录** | **`deploy/frontend`** |
| 构建命令 / 安装命令 | 留空（纯静态，无构建步骤） |

**域名**：默认域名只有 3 小时限时预览，长期可用必须绑自定义域名。
大陆节点加速要求域名已 ICP 备案；未备案域名可选"不含中国大陆"的加速区域，
走海外节点，大陆能访问但较慢。

## 二、GitHub Actions（算数 + 传 COS）

`.github/workflows/daily.yml` 触发时机：每个工作日北京时间 15:40（cron UTC 07:40）、
手动 Run workflow、以及 `deploy/**` 有 push 时。每次运行依次：

1. 跑 `build_data.py`：非交割日直接跳过不写数据；交割日（IO 第三个周五 / ETF 第四个周三，
   含 3 天顺延窗口）且当月未记录才追加数据点
2. **若数据点真的有变化**（`generated_at` 变化不算），把 `deploy/data/data.json`
   commit + push 回 main —— 这一步是关键：Actions 每次都是全新 checkout，
   不回写的话新数据点只存在于当次产物里，顺延窗口一过就会被基线覆盖丢失
3. 用 coscmd 把 `data.json`、`hsi.json` 传到 COS

4. 组装站点：把 `deploy/frontend/` 拷进 `_site/`，并按 `COS_BUCKET`/`COS_REGION`
   覆盖前端里的 `COS_BASE` —— **换 COS 桶只需改 secrets，不用改代码**
5. 用 CloudBase CLI 把 `_site/` 部署到静态托管
6. 用真实 HTTP 请求验证站点、echarts、COS 数据都可访问

> **不要给 `cloudbase hosting deploy` 加 `--verify`**：它上传后立刻查远端，
> 常因可见性延迟误报 `一致性校验失败: missing=/index.html,...`，而文件其实已经传上去了。
> 这种假失败会让 workflow 常态飘红，真出问题时反而不被当回事。第 6 步的
> HTTP 验证检的是用户实际拿到的东西，更可靠。

需要 4 个 Secrets：`COS_SECRET_ID` / `COS_SECRET_KEY` / `COS_BUCKET` / `COS_REGION`。
COS 与 CloudBase 在同一个腾讯云账号下，共用同一对密钥。

> **换账号/换桶时别忘了**：COS 桶必须配 CORS（`AllowedOrigin: *`，`GET`/`HEAD`），
> 否则页面（在 tcloudbaseapp.com 上）跨域 fetch COS 数据会被浏览器拦掉。
> 数据本身不用手工迁移——`data.json`/`hsi.json` 都在仓库里，workflow 会自动传到新桶。

## 三、本机配置（恒指采集）

恒指是唯一必须在本机采集的品种（富途 OpenD 是唯一数据源）。

1. **OpenD 登录**：打开图形版 `%APPDATA%\Futu_OpenD\Futu_OpenD.exe`，
   勾选**记住密码**和**自动登录**，登录一次。之后作业脚本拉起它就能无人值守登录。

   > 别用命令行版 `open-d\windows\FutuOpenD.exe`：它读 `FutuOpenD.xml` 里的
   > `login_pwd_md5`，而图形版并不往那里写凭据（实测为空），
   > 命令行版会因登录失败把监听端口关掉，表现为客户端 `WSAECONNREFUSED`。

2. **确认 git 能推送**（脚本靠 git 送 hsi.json，不用 coscmd）：
   ```
   git push
   ```
   建议用 Git Credential Manager 记住凭据，避免计划任务卡在登录。

3. **验证整条链路**：
   ```
   powershell -ExecutionPolicy Bypass -File deploy\local
un_hsi_job.ps1 -TestOnly
   ```
   看到 `[selftest] OK` 和恒指报价才算通过——**端口通不等于登录成功**。

4. **计划任务**（已注册，名为 `ATM-HSI-Sync`）：
   每天 16:30 触发（港股 16:00 收盘 + 16:10 收市竞价结束，故不能沿用 A 股的 15:40），
   开启了 **StartWhenAvailable**——关机错过了计划时间，下次开机后会尽快补跑。
   重建命令见 `run_hsi_job.ps1` 顶部注释。

### 恒指 PY 的口径（重要）

**权威值 = 港交所恒指期权日报里，交割日"下月 ATM 认购"的 O.Q.P.（官方结算价）÷ 当日恒指收盘。**
日报地址 `https://www.hkex.com.hk/eng/stat/dmstat/dayrpt/hsio{YYMMDD}.htm`，当晚发布，
**只保留约 11 个月**。由 `deploy/local/hsi_official.py` 读取。

2026-11 实证（交割日 O.Q.P. 对比各来源）：

| 月份 | 官方 PY | OpenD 实测 | 旧 CSV | Global X 3419 月报 |
|---|---|---|---|---|
| 2026-06 | 2.29%（528） | 528，相同 | 1.86% ✗ | 2.06% |
| 2026-07 | 2.35%（608） | 608，相同 | 1.97% ✗ | 2.60% |
| 2026-08 | 1.70%（434） | 434，相同 | — | 2.61% |
| 2026-01 | 2.18% | — | 1.76%（抄自 3419） | 1.76% |

> 2026-08 曾得出"OpenD 系统性高出港交所约 20%"的结论并据此以 CSV 为准——**这是错的**，
> 实际是 CSV 的 6、7 月填错。3419 月报也不能用：它是基金口径（期权金 ÷ 月底基金净值），
> 2026-01 起还混入了每周期权。

序列来源分段：2005-01~2025-03 来自带指数点位的历史工作簿；2025-04~2025-09 为 3419
月报近似值（官方日报已过保留期）；**2025-10 起全部为港交所官方结算价**。

数据字段约定：

| 字段 | 含义 |
|---|---|
| `premium_yield` | PY |
| `option_price` | 权利金（2025-10 起为官方结算价 O.Q.P.） |
| `source` | `hkex_oqp` = 已按官方结算价确定，不会再被改动 |
| `iv` | 日报给出的隐含波动率 |
| `premium_yield_provisional` | true = 当晚日报未出，暂用 OpenD 实测（次日自动转正） |
| `option_price_market` | OpenD 抓到的价格（存档） |
| `option_price_derived` | true = 由 PY×标的 反算（2025-09 及以前的月份，那时没有可取的结算价） |

### 采集与补采

`run_hsi_job.ps1` 每次运行依次：

1. `sync_hsi.py --missing --upload`：交割日当天用 OpenD 实时快照写入**临时值**；
   此前漏掉的月份也会尝试用 OpenD 历史日K补采（富途对退市合约历史保留很短，常补不到）
2. `hsi_official.py --upload`：对最近 330 天内**缺失或非官方口径**的月份读港交所日报，
   临时值转正、漏采月份补回**完整明细**（标的/行权价/结算价/合约）
3. 重推滞留本地的纯数据提交

所以关机错过交割日，只要 11 个月内开过机就能自动补齐。手动用法：

```
python deploy\local\hsi_official.py --since 2025-10        # 指定起始月重建
python deploy\local\sync_hsi.py --month 2026-09 --upload    # 用 OpenD 补指定月份
```

`import_hsi_csv.py` 已降为手工工具，只用于补超出日报保留期的月份（只有 PY，没有明细）。

## 四、验证

| 验证项 | 方法 | 预期 |
|---|---|---|
| Actions 计算 | Actions 页 Run workflow | 日志显示"非交割日"或"已更新"，末尾打印"数据点变化: True/False" |
| COS 数据 | `curl -sS -o /dev/null -D - <桶域名>/hsi.json` | HTTP 200，`Content-Type: application/json` |
| 页面发布 | workflow 的"验证站点"步 | 页面 200、echarts 200、COS 两个 json 200 |
| 前端渲染 | 浏览器打开站点 URL | 三卡片+三图+明细表正常，hover 显示明细（**不再弹下载**） |
| 本机同步 | `python deploy/local/sync_hsi.py --missing` | 先打印每个月的计划（skip/realtime/backfill）；OpenD 在线时补采写入 |
| 数据更新 | 交割日后次日打开页面 | 最新月度日期变为当月交割日 |

## 五、日常维护

- **改前端**：改 `deploy/frontend/index.html` → push 到 main → 托管产品自动重新构建发布
- **改计算逻辑**：改 `build_data.py` → push 到 main → workflow 自动跑并传 COS
- **data.json 会被 Actions 回写进仓库**：交割日采集到新数据点后 workflow 会自动 commit 回 main
  （message 带 `[skip ci]`）。本地开发前先 `git pull`，否则容易与机器人提交冲突。
  机器人用 `GITHUB_TOKEN` 推送不会再次触发 workflow，不存在死循环。
- **workflow 只有一份**：`.github/workflows/daily.yml`，不要再往 `deploy/actions/` 放副本
- **本机关机影响**：错过交割日当天则恒指该月只能补回费率、丢失明细；A股两个品种不受影响（Actions 在云端）
- **域名是唯一的长期依赖**：所有免费托管的默认域名都不可长期使用，买域名 + 备案是绕不开的一步

## 免责声明

本工具所载数据与计算仅供参考，不构成投资建议。市场有风险，投资需谨慎。
