# ATM Premium Yield 静态化部署（GitHub Actions + 腾讯云 COS）

零服务器架构：**GitHub Actions 每日定时计算 → COS 存数据 + 静态前端 → 浏览器直接读取渲染**。
月成本 ≈ ¥0.1-0.5（COS 按量），富途 OpenD 留在本机（仅每日 1 次抓恒指）。

```
GitHub Actions（免费）                本机 OpenD（每日15:40计划任务）
  └─ 拉中金所IO/上交所510500    ┌──────┘ 抓恒指点位+恒指期权
  └─ 算 ATM PY → data.json      └──→ hsi.json
        │                              │
        ▼                              ▼
          腾讯云 COS（静态网站托管）
          ├── data.json   （沪深300/中证500）
          ├── hsi.json    （恒生指数）
          └── index.html  （前端，读以上JSON）
                    ▲
       用户浏览器直接访问 COS 静态页
```

## 目录结构

```
atm_premium_monitor/
├── deploy/
│   ├── data/
│   │   ├── data.json          # CSI300 + CSI500 历史+最新（Actions维护）
│   │   └── hsi.json           # HSI 历史+最新（本机同步维护）
│   ├── actions/
│   │   └── build_data.py      # A股计算脚本（Actions运行，仅需requests）
│   ├── local/
│   │   └── sync_hsi.py        # 本机恒指同步（OpenD + Windows计划任务）
│   ├── frontend/
│   │   └── index.html         # 纯静态前端（读 data.json + hsi.json）
│   ├── export_initial_json.py # 一次性：从SQLite导出基线JSON（本机已跑过）
│   └── README-DEPLOY.md       # 本文档
```

## 一、腾讯云 COS 配置（一次性，约10分钟）

1. **创建存储桶**：控制台 → 对象存储 COS → 创建存储桶
   - 名称如 `atm-premium-{你的id}`，地域选离你最近的（如 `ap-guangzhou`），**公有读私有写**
2. **开启静态网站**：存储桶 → 基础配置 → 静态网站 → 开启，索引文档填 `index.html`，错误文档留空
3. **CORS 配置**：存储桶 → 安全 → CORS 规则 → 添加：
   ```json
   {"AllowedOrigin": "*", "AllowedMethod": ["GET", "HEAD"], "AllowedHeader": ["*"],
    "ExposeHeader": ["ETag"], "MaxAgeSeconds": 600}
   ```
4. **访问地址**：静态网站域名形如 `https://atm-premium-xxx.cos-website.ap-guangzhou.myqcloud.com`（大陆直连，无需备案即可通过COS域名访问）
5. **密钥**：访问管理 CAM → API密钥管理 → 新建密钥，得到 `SecretId` / `SecretKey`

## 二、GitHub 仓库配置（一次性，约10分钟）

1. **创建公开仓库**（如 `atm-premium-monitor`），上传本项目（含 `deploy/`）
2. **workflow 位置**：唯一一份在 `.github/workflows/daily.yml`（改这一份即可，不要再放副本）
3. **配置 Secrets**：仓库 → Settings → Secrets and variables → Actions → New repository secret，添加4个：
   - `COS_SECRET_ID` = 你的SecretId
   - `COS_SECRET_KEY` = 你的SecretKey
   - `COS_BUCKET` = `atm-premium-{你的id}`
   - `COS_REGION` = `ap-guangzhou`
4. **首次手动触发**：Actions 页 → daily-atm-premium → Run workflow（验证能跑通并上传）
5. 之后自动：**每个交易日 15:40（北京时间）自动运行**，非交割日自动跳过不写数据

## 三、本机配置（恒指同步，一次性+每日自动）

1. **安装 coscmd**（本机，用于上传 hsi.json）：
   ```bash
   pip install coscmd
   coscmd config -a <SecretId> -s <SecretKey> -b atm-premium-{你的id} -r ap-guangzhou
   ```
2. **手动测试同步**（确保OpenD在运行）：
   ```bash
   cd atm_premium_monitor
   python deploy/local/sync_hsi.py --upload
   ```
   今日非交割日会打印"跳过"——属正常；交割日（每月最后第二营业日）会写入并上传。
3. **Windows 计划任务**（每日 15:40 自动跑）：
   ```bat
   schtasks /create /tn "ATM-HSI-Sync" /tr "cmd /c cd /d E:\path\to\atm_premium_monitor && C:\path\to\python.exe deploy\local\sync_hsi.py --upload >> sync_hsi.log 2>&1" /sc daily /st 15:40
   ```
   （把路径换成实际 Python 与项目目录）

## 四、验证

| 验证项 | 方法 | 预期 |
|---|---|---|
| Actions 计算 | Actions 页 Run workflow | 日志显示"非交割日"或"已更新"，data.json 上传成功 |
| 前端渲染 | 浏览器打开 COS 静态域名 | 三卡片+三图+明细表正常，hover显示明细 |
| 本机同步 | `python deploy/local/sync_hsi.py` | OpenD在线时连接成功；交割日写入 |
| 数据更新 | 交割日后次日打开页面 | 最新月度日期变为当月交割日 |

## 五、日常维护

- **改前端/计算逻辑**：改 `deploy/frontend/index.html` 或 `build_data.py` → push 到 GitHub → Actions 自动上传（前端改动需手动触发一次 Run workflow 或等下次定时）
- **COS 上传必须带 `--metas`**：HTML 不带 `Content-Type: text/html` + `Content-Disposition: inline`，
  COS 默认返回 `attachment`，浏览器会强制下载而不是渲染。workflow 与 `sync_hsi.py` 均已带上，
  新增上传命令时别漏。另外**必须用静态网站域名** `*.cos-website.<region>.myqcloud.com` 访问，
  COS 原始域名 `*.cos.<region>.myqcloud.com` 永远触发下载。
- **data.json 会被 Actions 回写进仓库**：交割日采集到新数据点后，workflow 会自动 commit 回 main
  （commit message 带 `[skip ci]`）。本地开发前先 `git pull`，否则容易与机器人提交冲突。
- **COS 密钥泄露风险**：SecretKey 只存 GitHub Secrets 与本机 coscmd 配置，勿提交到代码
- **本机关机影响**：仅恒指当日缺档，A股两个品种照常（Actions 在云端）

## 免责声明

本工具所载数据与计算仅供参考，不构成投资建议。市场有风险，投资需谨慎。
