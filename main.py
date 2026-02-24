#!/usr/bin/env python3
"""RSS-to-AI-RSS 自动化脚本。

功能概览：
1. 从 OPML 或 feeds.txt 读取 RSS 源。
2. 抓取并解析 RSS 条目。
3. 基于 history.json 仅处理未处理过的新文章。
4. 清洗 HTML 正文、截断长度后调用大模型生成中文要点总结。
5. 生成标准 RSS 2.0 output.xml，并与历史输出合并。
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape, unescape
from pathlib import Path
from time import struct_time
from typing import Any
from xml.etree import ElementTree as ET

import feedparser
import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from openai import OpenAI

# 默认读取你本机导出的 OPML 路径；在 GitHub Actions 可通过 OPML_PATH 覆盖。
DEFAULT_OPML_PATH = "/Users/a1/Downloads/feedbro-subscriptions-20260222-172113.opml"
DEFAULT_FEEDS_TXT = "feeds.txt"
DEFAULT_HISTORY_PATH = "history.json"
DEFAULT_OUTPUT_PATH = "output.xml"
DEFAULT_HTML_PATH = "index.html"

SYSTEM_PROMPT = (
    "你是一个高效的信息过滤助手。"
    "请用中文总结这篇文章，提取出 3-5 个最重要的核心要点（Bullet Points），"
    "直接输出要点，无需废话。"
)

USER_AGENT = "rss-to-ai-rss/1.0 (+https://github.com/)"


@dataclass
class Article:
    """待总结文章的统一结构。"""

    article_id: str
    title: str
    link: str
    published_at: datetime
    plain_text: str


@dataclass
class OutputItem:
    """输出到 output.xml 的条目结构。"""

    item_id: str
    title: str
    link: str
    description: str
    published_at: datetime


def get_env_int(name: str, default: int) -> int:
    """读取整数环境变量，异常时回退默认值。"""
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        logging.warning("环境变量 %s 不是合法整数，已回退默认值 %d", name, default)
        return default


def is_dry_run() -> bool:
    """判断是否启用 DRY_RUN。"""
    return os.getenv("DRY_RUN", "0").lower() in {"1", "true", "yes"}


def parse_opml_file(opml_path: Path) -> list[str]:
    """从 OPML 提取所有 xmlUrl。"""
    tree = ET.parse(opml_path)
    root = tree.getroot()

    urls: list[str] = []
    seen: set[str] = set()

    for outline in root.findall(".//outline"):
        xml_url = (outline.attrib.get("xmlUrl") or "").strip()
        if not xml_url:
            continue
        if xml_url in seen:
            continue
        seen.add(xml_url)
        urls.append(xml_url)

    return urls


def parse_feeds_txt(feeds_file: Path) -> list[str]:
    """从 feeds.txt 读取 RSS 列表（忽略空行和 # 注释行）。"""
    if not feeds_file.exists():
        return []

    urls: list[str] = []
    seen: set[str] = set()

    for raw_line in feeds_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            continue
        seen.add(line)
        urls.append(line)

    return urls


def load_feed_urls(opml_path: Path, feeds_file: Path) -> list[str]:
    """优先从 OPML 读取，失败时回退 feeds.txt。"""
    if opml_path.exists():
        try:
            urls = parse_opml_file(opml_path)
            if urls:
                logging.info("已从 OPML 读取 %d 个 RSS 源：%s", len(urls), opml_path)
                return urls
            logging.warning("OPML 文件存在但未解析到 rss 链接：%s", opml_path)
        except Exception as exc:  # noqa: BLE001
            logging.warning("解析 OPML 失败，回退 feeds.txt。原因：%s", exc)
    else:
        logging.warning("未找到 OPML 文件：%s，回退 feeds.txt", opml_path)

    urls = parse_feeds_txt(feeds_file)
    if urls:
        logging.info("已从 feeds.txt 读取 %d 个 RSS 源：%s", len(urls), feeds_file)
    return urls


def load_history(history_path: Path) -> set[str]:
    """读取已处理文章 ID 集合。"""
    if not history_path.exists():
        return set()

    try:
        payload = json.loads(history_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("history.json 格式非法，将以空历史重新开始：%s", history_path)
        return set()

    if isinstance(payload, list):
        return {str(item) for item in payload}

    if isinstance(payload, dict):
        items = payload.get("processed_ids", [])
        if isinstance(items, list):
            return {str(item) for item in items}

    logging.warning("history.json 结构不符合预期，将以空历史重新开始。")
    return set()


def save_history(history_path: Path, processed_ids: set[str]) -> None:
    """保存历史处理状态。"""
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "processed_ids": sorted(processed_ids),
    }
    history_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_struct_time(value: Any) -> datetime | None:
    """将 feedparser 的 struct_time 转为 UTC datetime。"""
    if not isinstance(value, struct_time):
        return None
    ts = calendar.timegm(value)
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def make_article_id(entry: dict[str, Any]) -> str:
    """为文章生成稳定 ID（优先 guid/link）。"""
    for key in ("id", "guid", "link"):
        value = (entry.get(key) or "").strip()
        if value:
            return value

    # 兜底策略：基于标题+发布时间哈希，避免无 ID 源导致重复处理。
    base = f"{entry.get('title', '')}|{entry.get('published', '')}|{entry.get('updated', '')}"
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()  # noqa: S324
    return f"sha1:{digest}"


def extract_html_from_entry(entry: dict[str, Any]) -> str:
    """从 RSS 条目中提取可用正文 HTML。"""
    if isinstance(entry.get("content"), list) and entry["content"]:
        first_content = entry["content"][0]
        if isinstance(first_content, dict):
            value = first_content.get("value")
            if isinstance(value, str) and value.strip():
                return value

    for key in ("summary", "description"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value

    return ""


def html_to_plain_text(html: str) -> str:
    """HTML 转纯文本，并进行空白归一化。"""
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # 去掉噪声节点，减少 token 消耗。
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    text = unescape(text)

    # 规整换行和空白。
    text = text.replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)

    return text.strip()


def fetch_feed_entries(feed_url: str, timeout: int) -> list[dict[str, Any]]:
    """抓取 RSS 源并返回 entry 列表。"""
    headers = {"User-Agent": USER_AGENT}

    response = requests.get(feed_url, headers=headers, timeout=timeout)
    response.raise_for_status()

    parsed = feedparser.parse(response.content)

    if parsed.bozo:
        logging.warning("RSS 解析异常（bozo=1）：%s", feed_url)

    return [entry for entry in parsed.entries if isinstance(entry, dict)]


def collect_new_articles(
    feed_urls: list[str],
    history_ids: set[str],
    timeout: int,
    max_content_chars: int,
) -> list[Article]:
    """抓取所有 RSS，并筛选历史中未出现的新文章。"""
    new_articles: list[Article] = []
    seen_in_this_run: set[str] = set()

    for feed_url in feed_urls:
        try:
            entries = fetch_feed_entries(feed_url, timeout)
            logging.info("RSS 抓取成功：%s（%d 条）", feed_url, len(entries))
        except requests.RequestException as exc:
            logging.warning("RSS 抓取失败，已跳过：%s，原因：%s", feed_url, exc)
            continue
        except Exception as exc:  # noqa: BLE001
            logging.warning("RSS 解析失败，已跳过：%s，原因：%s", feed_url, exc)
            continue

        for entry in entries:
            article_id = make_article_id(entry)
            if article_id in history_ids or article_id in seen_in_this_run:
                continue

            title = str(entry.get("title") or "(无标题)").strip()
            link = str(entry.get("link") or "").strip()

            published_at = (
                parse_struct_time(entry.get("published_parsed"))
                or parse_struct_time(entry.get("updated_parsed"))
                or datetime.now(timezone.utc)
            )

            raw_html = extract_html_from_entry(entry)
            plain_text = html_to_plain_text(raw_html)
            plain_text = plain_text[:max_content_chars].strip()

            if not plain_text:
                # 某些源只给标题和链接，避免空内容造成模型浪费。
                plain_text = f"标题：{title}\n链接：{link}\n正文为空。"

            new_articles.append(
                Article(
                    article_id=article_id,
                    title=title,
                    link=link,
                    published_at=published_at,
                    plain_text=plain_text,
                )
            )
            seen_in_this_run.add(article_id)

    # 优先处理最新文章，提升时效性。
    new_articles.sort(key=lambda x: x.published_at, reverse=True)
    return new_articles


def init_openai_client() -> OpenAI | None:
    """初始化 OpenAI 兼容客户端。"""
    if is_dry_run():
        logging.info("DRY_RUN=1：跳过真实 API 客户端初始化。")
        return None

    api_key = os.getenv("API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 API_KEY 环境变量，请在 GitHub Secrets 或本地环境中设置。")

    api_base_url = (
        os.getenv("API_BASE_URL", "").strip()
        or os.getenv("BASE_URL", "").strip()
        or "https://api.deepseek.com"
    )
    return OpenAI(api_key=api_key, base_url=api_base_url)


def summarize_article(client: OpenAI | None, model: str, article: Article) -> str:
    """调用大模型生成中文要点总结。"""
    if is_dry_run():
        return "- DRY_RUN 模式：跳过真实 API 调用。\n- 这是示例摘要。\n- 生产环境请关闭 DRY_RUN。"

    if client is None:
        return "- AI 客户端未初始化，无法生成摘要。"

    user_prompt = (
        f"文章标题：{article.title}\n"
        f"文章链接：{article.link or '无'}\n"
        f"文章正文（可能已截断）：\n{article.plain_text}"
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            stream=False,
            timeout=get_env_int("API_TIMEOUT", 60),
        )
        summary = (response.choices[0].message.content or "").strip()
        if summary:
            return summary
        return "- 模型返回空内容。"
    except Exception as exc:  # noqa: BLE001
        logging.warning("AI 总结失败：%s，文章：%s", exc, article.link or article.title)
        return f"- AI 总结失败：{exc}"


def compose_description(summary_text: str, original_text: str, preview_chars: int) -> str:
    """组装输出 RSS description 文本。"""
    preview = original_text[:preview_chars].strip()
    if not preview:
        preview = "（原文内容为空）"

    return (
        "【AI 总结】\n"
        f"{summary_text}\n\n"
        "【原文内容】\n"
        f"{preview}"
    )


def load_existing_output_items(output_path: Path) -> list[OutputItem]:
    """读取既有 output.xml，避免每次覆盖历史条目。"""
    if not output_path.exists():
        return []

    parsed = feedparser.parse(output_path.read_bytes())
    items: list[OutputItem] = []

    for entry in parsed.entries:
        if not isinstance(entry, dict):
            continue

        item_id = str(entry.get("id") or entry.get("guid") or entry.get("link") or "").strip()
        if not item_id:
            continue

        published_at = (
            parse_struct_time(entry.get("published_parsed"))
            or parse_struct_time(entry.get("updated_parsed"))
            or datetime.now(timezone.utc)
        )

        items.append(
            OutputItem(
                item_id=item_id,
                title=str(entry.get("title") or "(无标题)").strip(),
                link=str(entry.get("link") or "").strip(),
                description=str(entry.get("description") or "").strip(),
                published_at=published_at,
            )
        )

    return items


def merge_output_items(
    new_items: list[OutputItem],
    old_items: list[OutputItem],
    max_output_items: int,
) -> list[OutputItem]:
    """合并新旧条目并去重，保证输出稳定。"""
    merged: list[OutputItem] = []
    seen: set[str] = set()

    for item in [*new_items, *old_items]:
        if item.item_id in seen:
            continue
        seen.add(item.item_id)
        merged.append(item)

    merged.sort(key=lambda x: x.published_at, reverse=True)
    return merged[:max_output_items]


def generate_rss(items: list[OutputItem], output_path: Path) -> None:
    """生成 RSS 2.0 XML 文件。"""
    fg = FeedGenerator()

    feed_title = os.getenv("OUTPUT_FEED_TITLE", "AI 摘要聚合订阅")
    feed_description = os.getenv(
        "OUTPUT_FEED_DESCRIPTION",
        "自动抓取 RSS 并由 AI 生成摘要。",
    )

    # 这里是聚合源的唯一 ID；可在 Actions 中通过 OUTPUT_FEED_ID 覆盖成你的 Pages 链接。
    feed_id = os.getenv("OUTPUT_FEED_ID", "https://example.com/output.xml")
    fg.id(feed_id)
    fg.title(feed_title)
    fg.description(feed_description)
    fg.language("zh-CN")

    self_link = os.getenv("OUTPUT_FEED_SELF_LINK", feed_id)
    fg.link(href=self_link, rel="self")

    for item in items:
        entry = fg.add_entry(order="append")
        entry.id(item.item_id)
        entry.title(item.title)

        if item.link:
            entry.link(href=item.link)
            entry.guid(item.link, permalink=True)
        else:
            entry.guid(item.item_id, permalink=False)

        entry.description(item.description)
        entry.pubDate(item.published_at)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fg.rss_file(str(output_path), pretty=True)


def split_description(description: str) -> tuple[str, str]:
    """拆分 description 为 AI 总结与原文预览。"""
    marker = "【原文内容】"
    summary_part = description
    original_part = ""

    if marker in description:
        summary_part, original_part = description.split(marker, 1)

    summary_part = summary_part.replace("【AI 总结】", "", 1).strip()
    original_part = original_part.strip()
    return summary_part, original_part


def nl2br(text: str) -> str:
    """将纯文本安全转为 HTML 换行。"""
    lines = [escape(line) for line in text.splitlines()]
    return "<br>".join(lines)


def generate_web_page(items: list[OutputItem], html_path: Path, rss_path: Path) -> None:
    """生成可直接浏览的静态网页。"""
    page_title = os.getenv("OUTPUT_PAGE_TITLE", "AI RSS 每日简报")
    page_subtitle = os.getenv(
        "OUTPUT_PAGE_SUBTITLE",
        "自动聚合 RSS，并为每篇内容生成中文要点摘要。",
    )
    max_web_items = get_env_int("MAX_WEB_ITEMS", 80)
    show_items = items[:max_web_items]
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rss_link = os.getenv("OUTPUT_FEED_PUBLIC_URL", rss_path.name)

    cards_html: list[str] = []
    for item in show_items:
        summary_text, original_text = split_description(item.description)
        summary_html = nl2br(summary_text or "暂无摘要")
        original_html = nl2br((original_text or "暂无原文预览")[:1200])
        pub_text = item.published_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        title_text = escape(item.title or "(无标题)")
        link_text = escape(item.link or "#")

        cards_html.append(
            f"""
<article class="card">
  <h2><a href="{link_text}" target="_blank" rel="noopener noreferrer">{title_text}</a></h2>
  <p class="meta">发布时间：{pub_text}</p>
  <section>
    <h3>AI 总结</h3>
    <p>{summary_html}</p>
  </section>
  <section>
    <h3>原文预览</h3>
    <p>{original_html}</p>
  </section>
</article>
""".strip()
        )

    if not cards_html:
        cards_html.append(
            """
<article class="card empty">
  <h2>暂无文章</h2>
  <p>当前还没有可展示的摘要，等待下一次任务运行后会自动更新。</p>
</article>
""".strip()
        )

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escape(page_title)}</title>
  <style>
    :root {{
      --bg: #f4f6f8;
      --panel: #ffffff;
      --text: #16212b;
      --muted: #5b6772;
      --accent: #0a66c2;
      --border: #dfe5ea;
    }}
    body {{
      margin: 0;
      background: linear-gradient(180deg, #eef4ff 0%, var(--bg) 35%);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      line-height: 1.6;
    }}
    .container {{
      max-width: 920px;
      margin: 0 auto;
      padding: 28px 16px 48px;
    }}
    header {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 20px;
      margin-bottom: 16px;
    }}
    header h1 {{
      margin: 0 0 8px;
      font-size: 26px;
    }}
    header p {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }}
    .toolbar {{
      margin-top: 12px;
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .toolbar a {{
      color: var(--accent);
      text-decoration: none;
      font-weight: 600;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 18px;
      margin-bottom: 14px;
    }}
    .card h2 {{
      margin: 0 0 8px;
      font-size: 20px;
    }}
    .card h2 a {{
      color: var(--text);
      text-decoration: none;
    }}
    .card h2 a:hover {{
      color: var(--accent);
    }}
    .meta {{
      margin: 0 0 12px;
      color: var(--muted);
      font-size: 13px;
    }}
    .card h3 {{
      margin: 10px 0 4px;
      font-size: 14px;
      color: var(--accent);
    }}
    .card p {{
      margin: 0;
      font-size: 14px;
      white-space: normal;
      word-break: break-word;
    }}
    .empty {{
      text-align: center;
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <main class="container">
    <header>
      <h1>{escape(page_title)}</h1>
      <p>{escape(page_subtitle)}</p>
      <div class="toolbar">
        <a href="{escape(rss_link)}">订阅 RSS XML</a>
        <span>更新时间：{updated_at}</span>
        <span>展示条数：{len(show_items)}</span>
      </div>
    </header>
    {"".join(cards_html)}
  </main>
</body>
</html>
"""

    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_content, encoding="utf-8")


def main() -> None:
    """主流程。"""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    opml_path = Path(os.getenv("OPML_PATH", DEFAULT_OPML_PATH))
    feeds_file = Path(os.getenv("FEEDS_FILE", DEFAULT_FEEDS_TXT))
    history_path = Path(os.getenv("HISTORY_PATH", DEFAULT_HISTORY_PATH))
    output_path = Path(os.getenv("OUTPUT_XML_PATH", DEFAULT_OUTPUT_PATH))
    html_path = Path(os.getenv("OUTPUT_HTML_PATH", DEFAULT_HTML_PATH))

    max_content_chars = get_env_int("MAX_CONTENT_CHARS", 3000)
    max_new_items = get_env_int("MAX_NEW_ITEMS_PER_RUN", 20)
    max_output_items = get_env_int("MAX_OUTPUT_ITEMS", 300)
    preview_chars = get_env_int("ORIGINAL_PREVIEW_CHARS", 600)
    rss_timeout = get_env_int("RSS_TIMEOUT", 20)
    model_name = os.getenv("MODEL_NAME", "deepseek-chat")

    feed_urls = load_feed_urls(opml_path=opml_path, feeds_file=feeds_file)
    if not feed_urls:
        raise RuntimeError(
            "没有可用的 RSS 源。请检查 OPML_PATH 或 feeds.txt。"
        )

    history_ids = load_history(history_path)
    old_output_items = load_existing_output_items(output_path)

    new_articles = collect_new_articles(
        feed_urls=feed_urls,
        history_ids=history_ids,
        timeout=rss_timeout,
        max_content_chars=max_content_chars,
    )

    if len(new_articles) > max_new_items:
        logging.info(
            "检测到 %d 条新文章，本次仅处理最新 %d 条（控制 API 成本）。",
            len(new_articles),
            max_new_items,
        )
    new_articles = new_articles[:max_new_items]

    client = init_openai_client()
    new_output_items: list[OutputItem] = []

    for idx, article in enumerate(new_articles, start=1):
        logging.info("[%d/%d] 正在总结：%s", idx, len(new_articles), article.title)

        summary_text = summarize_article(
            client=client,
            model=model_name,
            article=article,
        )

        description = compose_description(
            summary_text=summary_text,
            original_text=article.plain_text,
            preview_chars=preview_chars,
        )

        new_output_items.append(
            OutputItem(
                item_id=article.article_id,
                title=article.title,
                link=article.link,
                description=description,
                published_at=article.published_at,
            )
        )

        # 即使 AI 调用失败也记入历史，避免同一坏数据反复消耗。
        history_ids.add(article.article_id)

    final_items = merge_output_items(
        new_items=new_output_items,
        old_items=old_output_items,
        max_output_items=max_output_items,
    )

    generate_rss(items=final_items, output_path=output_path)
    generate_web_page(items=final_items, html_path=html_path, rss_path=output_path)
    save_history(history_path=history_path, processed_ids=history_ids)

    logging.info(
        "执行完成：新增处理 %d 条，最终输出 %d 条，历史总量 %d。",
        len(new_output_items),
        len(final_items),
        len(history_ids),
    )


if __name__ == "__main__":
    main()
