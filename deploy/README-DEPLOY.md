# ATM Premium Yield 静态化部署（GitHub Actions + GitHub Pages）

零服务器架构：**GitHub Actions 每日定时计算 → 站点文件发布到 GitHub Pages → 浏览器直接读取渲染**。
成本 ¥0（Actions 与 Pages 对公开仓库免费），富途 OpenD 留在本机（仅每日 1 次抓恒指）。

```
GitHub Actions（免费）                本机 OpenD（每日15:40计划任务）
  └─ 拉中金所IO/上交所510500    ┌──────┘ 抓恒指点位+恒指期权
  └─ 算 ATM PY → data.json      └──→ hsi.json ──git push──┐
        │                                                  │
        └──────────────► 仓库 main 分支 ◄──────────────────┘
                              │
                              ▼
                   GitHub Pages（同一个 workflow 发布）
                   ├── index.html  （前端，读以下两个 json）
                   ├── data.json   （沪深300/中证500）
                   └── hsi.json    （恒生指数）
                              ▲
              用户浏览器访问 https://<用户名>.github.io/<仓库名>/
```

> **为什么不用腾讯云 COS**：2024-01-01 之后创建的 COS 存储桶，通过默认域名
> （**包括静态网站域名 `*.cos-website.*`**）访问任意类型文件都会被强制下载，
> 响应带平台注入的 `x-cos-force-download: true`，其优先级高于对象自身的
> `Content-Disposition: inline`，`coscmd --metas` 无论怎么设都救不回来。
> 唯一解法是绑定自定义域名，而大陆地域的自定义域名还要求 ICP 备案。
> 排查方法：`curl -sSD - <url> -o /dev/null`，看到 `x-cos-force-download: true`
> 就别再折腾元数据了。（注意 HEAD 请求不返回 `Content-Disposition`，必须用 GET。）

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

## 一、开启 GitHub Pages（一次性，约 1 分钟）

1. 确认仓库是 **Public**（免费版 Pages 只支持公开仓库）
2. 仓库 → **Settings → Pages → Build and deployment → Source** 选 **GitHub Actions**
   （不要选 "Deploy from a branch"）
3. 站点地址：`https://<用户名>.github.io/<仓库名>/`，首次发布后在 Actions 运行日志的
   deploy 步骤里也能直接看到

无需任何 Secrets——Pages 用内置的 `GITHUB_TOKEN`，workflow 里已声明所需 `permissions`。

## 二、workflow 做了什么

`.github/workflows/daily.yml` 触发时机：每个工作日北京时间 15:40（cron UTC 07:40）、
手动 Run workflow、以及 `deploy/**` 有 push 时。每次运行依次：

1. 跑 `build_data.py`：非交割日直接跳过不写数据；交割日（IO 第三个周五 / ETF 第四个周三，
   含 3 天顺延窗口）且当月未记录才追加数据点
2. **若数据点真的有变化**（`generated_at` 变化不算），把 `deploy/data/data.json`
   commit + push 回 main —— 这一步是关键：Actions 每次都是全新 checkout，
   不回写的话新数据点只存在于当次产物里，顺延窗口一过就会被基线覆盖丢失
3. 把 `index.html` + `data.json` + `hsi.json` 复制到 `_site/` 并发布到 Pages

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
| Pages 发布 | 同一次运行的 deploy 作业 | 输出站点 URL，状态 success |
| 前端渲染 | 浏览器打开站点 URL | 三卡片+三图+明细表正常，hover 显示明细（**不再弹下载**） |
| 本机同步 | `python deploy/local/sync_hsi.py` | OpenD 在线时连接成功；交割日写入 |
| 数据更新 | 交割日后次日打开页面 | 最新月度日期变为当月交割日 |

## 五、日常维护

- **改前端/计算逻辑**：改 `deploy/frontend/index.html` 或 `build_data.py` → push 到 main
  → workflow 自动重新发布 Pages（`deploy/**` 路径已在 push 触发范围内）
- **data.json 会被 Actions 回写进仓库**：交割日采集到新数据点后 workflow 会自动 commit 回 main
  （message 带 `[skip ci]`）。本地开发前先 `git pull`，否则容易与机器人提交冲突。
  机器人用 `GITHUB_TOKEN` 推送不会再次触发 workflow，不存在死循环。
- **workflow 只有一份**：`.github/workflows/daily.yml`，不要再往 `deploy/actions/` 放副本
- **本机关机影响**：仅恒指当日缺档，A股两个品种照常（Actions 在云端）
- **大陆访问 github.io 时快时慢**：如果以后需要稳定直连，路径是买域名 + ICP 备案 + 绑定
  COS/CDN 自定义域名；届时把 COS 上传步骤从 git 历史里捡回来即可（commit 73e577d）

## 免责声明

本工具所载数据与计算仅供参考，不构成投资建议。市场有风险，投资需谨慎。
