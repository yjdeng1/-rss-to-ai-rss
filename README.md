# RSS-to-AI-RSS（GitHub Actions 零服务器版）

这是一个默认仅支持手动运行的项目：
- 读取你提供的 RSS 源（优先从 OPML 读取）。
- 抓取新文章，调用大模型生成中文要点总结。
- 生成新的 `output.xml`（可直接订阅）。
- 生成 `index.html` 网页，可在手机/电脑直接浏览 AI 摘要。
- 可通过 GitHub Actions 手动运行并自动提交结果。

## 1. 项目文件说明

- `main.py`：核心逻辑（抓取、去重、摘要、生成 RSS、写入历史）
- `requirements.txt`：Python 依赖
- `.github/workflows/rss_generator.yml`：手动运行工作流（已暂停定时触发）
- `feeds.txt`：OPML 不可用时的备用 RSS 列表
- `history.json`：文章处理历史（首次运行会自动创建）
- `output.xml`：最终可订阅的 AI RSS（首次运行会自动创建）
- `index.html`：网页版 AI 摘要日报（首次运行会自动创建）

## 2. 新手一步步部署（GitHub）

1. 在 GitHub 新建一个仓库（例如：`rss-to-ai-rss`）。
2. 把本项目文件上传到仓库根目录。
3. 推荐把你的 OPML 文件也放到仓库根目录，文件名例如：
   `feedbro-subscriptions-20260222-172113.opml`
4. 进入仓库 `Settings` -> `Secrets and variables` -> `Actions`。
5. 点击 `New repository secret`，至少添加：
   - `API_KEY`：你的模型平台 API Key
6. 可选再加：
   - `API_BASE_URL`：例如 DeepSeek 用 `https://api.deepseek.com`
   - `BASE_URL`：与 `API_BASE_URL` 等价（兼容字段）
7. 提交后进入 `Actions` 页面，手动运行一次 `RSS to AI RSS`（`Run workflow`）验证。

## 3. DeepSeek API 获取方式（示例）

1. 打开 DeepSeek 开发者平台并登录。
2. 创建 API Key。
3. 把这个 Key 填到 GitHub Secret `API_KEY`。
4. 将 `API_BASE_URL` 设为：
   `https://api.deepseek.com`
   （如果你平台文档写的是 `BASE_URL`，也可以把同样地址填到 `BASE_URL` secret）

> 安全提醒：API Key 不要写进代码，不要提交到仓库。

## 4. OPML 与 feeds.txt 的优先级

`main.py` 读取逻辑：

1. 优先读取 `OPML_PATH` 指向的 OPML 文件。
2. 如果 OPML 不存在或解析失败，自动回退到 `feeds.txt`。

默认 `OPML_PATH` 是：
`/Users/a1/Downloads/feedbro-subscriptions-20260222-172113.opml`

这条默认路径适用于你本机本地运行；在 GitHub Actions 里建议将 OPML 文件提交到仓库并在工作流里设置：

```yaml
env:
  OPML_PATH: feedbro-subscriptions-20260222-172113.opml
```

## 5. 工作流状态说明（北京时间）

当前仓库已经暂停 GitHub Actions 的定时触发，只保留手动运行（`workflow_dispatch`）。

如果后续需要恢复每天 2 次自动运行，可在 `.github/workflows/rss_generator.yml` 中重新加入：

```yaml
- cron: "0 0,10 * * *"
```

## 6. 如何在手机/电脑随时打开（网页 + RSS）

你有两种常见方式：

### 方式 A：Raw 链接（最简单）

格式：

```text
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/<分支名>/output.xml
```

例如：

```text
https://raw.githubusercontent.com/yourname/rss-to-ai-rss/main/output.xml
```

### 方式 B：GitHub Pages 链接（推荐，手机/电脑都可直接打开）

1. 打开仓库 `Settings` -> `Pages`
2. `Source` 选 `Deploy from a branch`
3. 分支选 `main`，目录选 `/ (root)`
4. 保存后等待发布

发布后链接通常是：

```text
https://<你的用户名>.github.io/<仓库名>/
```

网页日报地址通常是：

```text
https://<你的用户名>.github.io/<仓库名>/index.html
```

RSS 地址通常是：

```text
https://<你的用户名>.github.io/<仓库名>/output.xml
```

你也可以把这个地址写到工作流环境变量：
- `OUTPUT_FEED_ID`
- `OUTPUT_FEED_SELF_LINK`
- `OUTPUT_FEED_PUBLIC_URL`

## 7. 本地运行（可选）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export API_KEY="你的Key"
export API_BASE_URL="https://api.deepseek.com"
python main.py
```

运行后会生成或更新：
- `output.xml`
- `history.json`
- `index.html`

## 8. 成本控制机制（已内置）

- `history.json` 去重：只处理从未处理过的新文章。
- 正文清洗+截断：默认最多 3000 字符。
- 当 RSS 正文为空时，自动尝试抓取原网页正文后再总结（失败时才回退为空提示）。
- 对前端渲染站点（静态 HTML 无正文）可启用 `r.jina.ai` 二级回填，提高可读文本获取成功率。
- 每次最多处理 `MAX_NEW_ITEMS_PER_RUN`（默认 20）条新文章。
- API 失败时不中断全流程，单篇文章会写入失败提示并继续处理其他文章。

## 9. 常用环境变量

- `API_KEY`：必填
- `API_BASE_URL`：可选，默认 `https://api.deepseek.com`
- `BASE_URL`：可选，与 `API_BASE_URL` 等价
- `MODEL_NAME`：默认 `deepseek-chat`
- `OPML_PATH`：默认 `/Users/a1/Downloads/feedbro-subscriptions-20260222-172113.opml`
- `FEEDS_FILE`：默认 `feeds.txt`
- `HISTORY_PATH`：默认 `history.json`
- `OUTPUT_XML_PATH`：默认 `output.xml`
- `MAX_CONTENT_CHARS`：默认 `3000`
- `MAX_NEW_ITEMS_PER_RUN`：默认 `20`
- `MAX_OUTPUT_ITEMS`：默认 `300`
- `ORIGINAL_PREVIEW_CHARS`：默认 `600`
- `RSS_TIMEOUT`：默认 `20`
- `ENABLE_WEB_FALLBACK`：默认 `1`（RSS 无正文时抓原网页）
- `WEB_FETCH_TIMEOUT`：默认 `20`
- `ENABLE_JINA_FALLBACK`：默认 `1`（网页正文过短时走 `r.jina.ai` 二级回填）
- `JINA_TIMEOUT`：默认 `25`
- `API_TIMEOUT`：默认 `60`

## 10. 首次运行建议

1. 先手动触发一次 Actions。
2. 打开 `output.xml` 确认内容可读。
3. 用任意 RSS 阅读器订阅 `output.xml` 链接测试。
4. 再观察次日自动任务是否按时产出。
