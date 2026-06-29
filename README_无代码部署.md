# VPN UK 信息源观察面板｜每日自动更新版

这是一套可直接上传到 GitHub 的自动日报网页包。上传后，GitHub Actions 会每天定时运行 `scripts/update_dashboard.py`，抓取 `config/sources.csv` 中的信息源，生成并覆盖 `docs/index.html`。GitHub Pages 会把 `docs/index.html` 发布成一个网页。

## 你需要做的一次性操作

### 1. 下载并解压本包

解压后不要只上传压缩包本身，要上传解压出来的全部文件夹和文件：

- `.github/workflows/daily-update.yml`
- `config/`
- `docs/`
- `scripts/`
- `requirements.txt`
- `README_无代码部署.md`

### 2. 在 GitHub 新建一个仓库

仓库名建议用：

`vpn-daily-dashboard`

仓库可以是 Public。若你要用 GitHub Free 发布 GitHub Pages，Public 仓库最省事。

### 3. 上传文件

进入新仓库后，点击：

`Add file` → `Upload files`

把解压后的所有文件和文件夹拖进去，然后点击底部绿色按钮提交。

### 4. 给 GitHub Actions 写入权限

进入：

`Settings` → `Actions` → `General` → `Workflow permissions`

选择：

`Read and write permissions`

保存。

这一步用于让定时任务自动把每天生成的新 HTML/JSON 提交回仓库。

### 5. 开启 GitHub Pages

进入：

`Settings` → `Pages`

在 `Build and deployment` 里选择：

- Source：`GitHub Actions`

保存。

这套包已经内置了 `actions/configure-pages`、`actions/upload-pages-artifact` 和 `actions/deploy-pages`，每天生成 `docs/index.html` 后会直接部署到 GitHub Pages。

保存后，GitHub 会给你一个网页地址，通常长这样：

`https://你的用户名.github.io/vpn-daily-dashboard/`

### 6. 手动跑第一次

进入：

`Actions` → `Daily VPN Dashboard Update` → `Run workflow`

运行完成后，刷新 GitHub Pages 网页即可看到自动版面板。

之后每天新加坡时间 08:15 左右会自动更新并自动部署一次。

## 可选增强：让日报整合更像“分析师”

这套包即使不配置任何 Key，也可以用规则引擎自动抓取和打分。

如果你希望它对大量条目做更像分析师的二次整合，可以添加：

- Repository Secret：`OPENAI_API_KEY`
- Repository Variable：`OPENAI_MODEL`

建议把 `OPENAI_MODEL` 设置成你当前可用的轻量模型。没有设置时，脚本会自动回退到规则引擎，不会中断日报。

进入路径：

`Settings` → `Secrets and variables` → `Actions`

添加 Secret 或 Variable。

## 可选增强：接入更多受限来源

这些来源公开网页经常受限，脚本已预留 Key：

- `YOUTUBE_API_KEY`：用于 YouTube 搜索结果
- `X_BEARER_TOKEN`：用于 X/Twitter Recent Search
- `SERPAPI_KEY`：用于 Google UK 搜索结果

不配置也可以运行，只是对应来源会显示“受限”。

## 如何修改信息源

编辑：

`config/sources.csv`

保持列名不变即可。最重要的列是：

- `来源名称`
- `URL/入口`
- `平台`
- `追踪频率`
- `备注`
- `监控优先级分`
- `日报层级`

保存后，下一次 Actions 运行会自动使用新来源。

## 每天会生成什么

- `docs/index.html`：网页面板
- `docs/data/latest.json`：最新结构化数据
- `docs/archive/YYYY-MM-DD.json`：每日归档
- `docs/reports/YYYY-MM-DD.md`：每日 Markdown 摘要
- `docs/status/last_run.json`：最后一次运行状态

## 目前自动抓取能力

可自动抓取：

- Reddit subreddit / search
- 普通网页和媒体页
- Apple App Store UK 搜索
- Google Play 搜索，依赖 `google-play-scraper`
- YouTube，配置 API Key 后更稳
- X/Twitter，配置 Bearer Token 后可用
- Google UK SERP，配置 SerpApi 后可用

受限但不会中断：

- Discord 群
- TikTok 搜索页
- 需要登录的评论区
- 反爬严格的搜索页和评测页

## 故障排查

如果 Actions 红了：

1. 点进失败的那次运行。
2. 打开 `Generate dashboard` 步骤。
3. 看错误信息。
4. 通常是某个网页临时拒绝访问；脚本会尽量跳过失败来源，正常不应导致整个任务失败。

如果网页没更新：

1. 看 Actions 是否运行成功。
2. 看仓库里 `docs/status/last_run.json` 是否更新。
3. 看 Settings → Pages 是否选择了 `GitHub Actions`。
4. 点进 Actions 运行记录，确认 `Deploy to GitHub Pages` 步骤是否成功。
5. 等待 Pages 发布完成后刷新页面。

## 重要说明

我已经把自动化代码和定时任务配置放进这个包里。真正“每天运行”的执行环境需要在你自己的 GitHub 仓库中开启，因为我不能替你登录账户或在本对话外持续运行后台任务。
