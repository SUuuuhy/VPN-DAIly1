# 只照做清单：让日报网页每天自动更新

## 第一次部署

1. 下载 `vpn_daily_dashboard_auto.zip`。
2. 解压。
3. 打开 GitHub，新建仓库，名字建议：`vpn-daily-dashboard`。
4. 在仓库里点 `Add file` → `Upload files`。
5. 把解压后的全部文件夹拖进去：`.github`、`config`、`docs`、`scripts`、`requirements.txt`、`README_无代码部署.md`。
6. 点击绿色提交按钮。
7. 打开 `Settings` → `Actions` → `General`。
8. 找到 `Workflow permissions`，选择 `Read and write permissions`，保存。
9. 打开 `Settings` → `Pages`。
10. Source 选 `GitHub Actions`，保存。
11. 打开 `Actions` → `Daily VPN Dashboard Update` → `Run workflow`。
12. 等运行成功后，回到 `Settings` → `Pages`，复制页面地址。

## 之后会自动发生什么

每天新加坡时间 08:15 左右，GitHub 会自动：

1. 打开信息源清单。
2. 抓取公开网页、Reddit、媒体页、应用商店等来源。
3. 重新生成日报 JSON。
4. 重新生成 `docs/index.html`。
5. 直接部署到 GitHub Pages。
6. 同时把当日归档提交回仓库。

## 修改信息源

改这个文件：

`config/sources.csv`

保存后，下一次自动任务会按新来源抓取。

## 可选：让受限来源更完整

进入：

`Settings` → `Secrets and variables` → `Actions`

可以添加：

- Secret：`YOUTUBE_API_KEY`
- Secret：`X_BEARER_TOKEN`
- Secret：`SERPAPI_KEY`
- Secret：`OPENAI_API_KEY`
- Variable：`OPENAI_MODEL`

不添加也能运行；只是部分来源会显示“受限”。

## 手工补充私域/Discord/TikTok 线索

改这个文件：

`config/manual_inputs.csv`

把示例行的日期改成当天日期，或者留空日期，即可被下一次日报纳入。
