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

需要 4 个 Secrets：`COS_SECRET_ID` / `COS_SECRET_KEY` / `COS_BUCKET` / `COS_REGION`。

## 三、本机配置（恒指同步，一次性+每日自动）

1. **确认本机 git 能推送**（脚本靠 git 把 hsi.json 送上去，不再用 coscmd）：
   ```bash
   cd atm_premium_monitor
   git push        # 能推成功即可；建议用 Git Credential Manager 记住凭据，避免计划任务卡在登录
   ```
2. **手动测试同步**（确保 OpenD 在运行）：
   ```bash
   python deploy/local/sync_hsi.py --upload
   ```
   今日非交割日会打印"本月已记录/跳过"——属正常；交割日（每月最后第二营业日）会写入并推送。
3. **Windows 计划任务**（每日 15:40 自动跑）：
   ```bat
   schtasks /create /tn "ATM-HSI-Sync" /tr "cmd /c cd /d E:\path\to\atm_premium_monitor && C:\path\to\python.exe deploy\local\sync_hsi.py --upload >> sync_hsi.log 2>&1" /sc daily /st 15:40
   ```
   （把路径换成实际 Python 与项目目录）

## 四、验证

| 验证项 | 方法 | 预期 |
|---|---|---|
| Actions 计算 | Actions 页 Run workflow | 日志显示"非交割日"或"已更新"，末尾打印"数据点变化: True/False" |
| COS 数据 | `curl -sS -o /dev/null -D - <桶域名>/hsi.json` | HTTP 200，`Content-Type: application/json` |
| 页面发布 | 托管产品构建日志 | 构建成功，输出站点 URL |
| 前端渲染 | 浏览器打开站点 URL | 三卡片+三图+明细表正常，hover 显示明细（**不再弹下载**） |
| 本机同步 | `python deploy/local/sync_hsi.py` | OpenD 在线时连接成功；交割日写入 |
| 数据更新 | 交割日后次日打开页面 | 最新月度日期变为当月交割日 |

## 五、日常维护

- **改前端**：改 `deploy/frontend/index.html` → push 到 main → 托管产品自动重新构建发布
- **改计算逻辑**：改 `build_data.py` → push 到 main → workflow 自动跑并传 COS
- **data.json 会被 Actions 回写进仓库**：交割日采集到新数据点后 workflow 会自动 commit 回 main
  （message 带 `[skip ci]`）。本地开发前先 `git pull`，否则容易与机器人提交冲突。
  机器人用 `GITHUB_TOKEN` 推送不会再次触发 workflow，不存在死循环。
- **workflow 只有一份**：`.github/workflows/daily.yml`，不要再往 `deploy/actions/` 放副本
- **本机关机影响**：仅恒指当日缺档，A股两个品种照常（Actions 在云端）
- **域名是唯一的长期依赖**：所有免费托管的默认域名都不可长期使用，买域名 + 备案是绕不开的一步

## 免责声明

本工具所载数据与计算仅供参考，不构成投资建议。市场有风险，投资需谨慎。
