"""数据清洗与分析模块 (Polars Data Engine)。

使用 Polars 完成时间戳转换、域名解析、停留时长还原与各项聚合指标计算，
输出可直接交给 ECharts 渲染的纯字典 / 列表结构。

相比最初版本，新增了以下维度（大多依赖 ``visits`` 表里被忽略的字段）：

- **真实停留时长**：``visit_duration``（微秒），并按 30 分钟封顶得到「有效
  使用时长」（capped engagement），剔除「开着页面离开」的挂机噪声。
- **域名时长榜**：不仅看访问次数，还看实际停留小时数。
- **内容类别**：按域名归类（视频 / 开发 / AI / 搜索 / 社交 …），分别按次数与
  时长统计。
- **停留时长分布**：把每次访问的时长分桶，刻画「瞄一眼就走」与「长读」。
- **导航类型**：``transition`` 低 8 位（link / typed / generated …）。
- **导航来源**：``from_visit`` 是否为 0，区分「直接访问」与「页面内点击」。
- **搜索词**：``keyword_search_terms`` 原生记录的高频搜索词。
- **会话分析**：以 30 分钟间隔切分上网会话，统计时长分布。
- **工作日 / 周末**：对比两类日期的强度。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import cast

import polars as pl

# WebKit 时间戳：从 1601-01-01 起算的微秒数。
# 1601-01-01 与 Unix 纪元 1970-01-01 相差 11644473600 秒。
_WEBKIT_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000

# 从 URL 中提取域名（去除协议、用户信息、端口、路径与参数）。
_DOMAIN_PATTERN = r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*://)?(?:[^@/]+@)?([^:/?#]+)"

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 单次停留封顶阈值（秒）：超过此值视为「页面挂着人离开」，计入有效时长时封顶。
_CAP_SECONDS = 1800

# 会话切分间隔（秒）：相邻两次访问间隔超过此值即视为新会话。
_SESSION_GAP_SECONDS = 1800

# transition 低 8 位 → 人类可读名称。
_TRANSITION_NAMES = {
    0: "链接点击",
    1: "地址栏键入",
    2: "自动书签",
    3: "自动子框架",
    4: "手动子框架",
    5: "地址栏联想",
    6: "起始页",
    7: "表单提交",
    8: "重新加载",
    9: "搜索关键字",
    10: "搜索联想",
}

# 内容类别规则：按顺序匹配域名子串，命中即归类。顺序很重要（先具体后宽泛）。
_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("AI", (
        "chatgpt", "openai.com", "claude.ai", "anthropic", "gemini.google",
        "bard.google", "openrouter", "perplexity", "midjourney", "lobehub",
        "lobe", "huggingface", "deepseek", "kimi", "comfyui", "civitai",
        "x.ai", "grok", "ollama",
    )),
    ("视频", (
        "bilibili", "youtube", "youtu.be", "netflix", "twitch", "iqiyi",
        "youku", "douyin", "tiktok", "vimeo", "ixigua", "v.qq.com",
    )),
    ("开发/技术", (
        "github", "gitlab", "gitee", "bitbucket", "stackoverflow",
        "stackexchange", "npmjs", "pypi", "crates.io", "readthedocs",
        "developer.", "docs.", "devdocs", "mdn", "developer.mozilla",
        "rust-lang", "golang", "go.dev", "python.org", "kaggle", "vite",
        "tailwindcss", "react.dev", "vuejs", "localhost", "127.0.0.1",
    )),
    ("搜索", (
        "google.com/search", "www.google.", "bing.com", "duckduckgo",
        "baidu.com", "so.com", "sogou", "search.",
    )),
    ("社交", (
        "twitter", "x.com", "facebook", "instagram", "reddit", "weibo",
        "zhihu", "qzone", "user.qzone", "linkedin", "telegram", "discord",
        "t.me", "tieba",
    )),
    ("邮件/办公", (
        "mail.", "gmail", "outlook", "feishu", "lark", "notion",
        "docs.google", "office.com", "slack", "calendar.google", "docs.qq",
        "yuque", "atlassian", "trello",
    )),
    ("教育", (
        "coursera", "edx.org", "udemy", "netacad", "mooc", "canvas",
        "khanacademy", "uestc.edu", "nus.edu", ".edu.", "instructure",
    )),
    ("新闻", (
        "news.", "bbc.", "cnn.", "nytimes", "reuters", "36kr", "thepaper",
        "guancha", "ifeng",
    )),
    ("购物", (
        "amazon", "taobao", "jd.com", "tmall", "ebay", "aliexpress",
        "pinduoduo", "1688",
    )),
    ("音乐/音频", (
        "spotify", "music.", "soundcloud", "y.qq.com", "kuwo", "kugou",
    )),
]


def _local_offset_us() -> int:
    """返回本地时区相对 UTC 的偏移量（微秒），用于把 UTC 转为本地墙钟时间。"""
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return 0
    return int(offset.total_seconds() * 1_000_000)


def _categorize_expr() -> pl.Expr:
    """构建一个把 ``domain`` 映射到类别名的 Polars 表达式。"""
    expr = pl.lit("其他")
    # 逆序构造 when/then，使靠前规则优先级更高。
    for name, needles in reversed(_CATEGORY_RULES):
        cond = pl.col("domain").str.contains_any(list(needles))
        expr = pl.when(cond).then(pl.lit(name)).otherwise(expr)
    return expr.alias("category")


def _prepare(urls: pl.DataFrame, visits: pl.DataFrame) -> pl.DataFrame:
    """把 urls / visits 连接成一张「每次访问」宽表，并附带派生列。

    产出列：``ts``（本地时间）, ``domain``, ``category``, ``dur_s``（原始秒）,
    ``capped_s``（封顶秒）, ``transition``, ``from_visit``。
    """
    offset_us = _local_offset_us()
    urls = urls.with_columns(
        pl.col("url")
        .str.extract(_DOMAIN_PATTERN, 1)
        .str.to_lowercase()
        .alias("domain")
    ).filter(pl.col("domain").is_not_null() & (pl.col("domain") != ""))

    visits = visits.filter(pl.col("visit_time") > 0).with_columns(
        (
            (pl.col("visit_time") - _WEBKIT_EPOCH_OFFSET_US + offset_us)
            .cast(pl.Datetime("us"))
        ).alias("ts"),
        (pl.col("visit_duration").fill_null(0) / 1_000_000).alias("dur_s"),
    )

    joined = visits.join(
        urls.select("id", "url", "title", "domain"),
        left_on="url_id",
        right_on="id",
        how="inner",
    )
    return joined.with_columns(
        pl.col("dur_s").clip(0, _CAP_SECONDS).alias("capped_s"),
        # transition 低 8 位为核心类型
        (pl.col("transition") % 256).alias("trans_core"),
        _categorize_expr(),
    )


def _overview(
    urls: pl.DataFrame, events: pl.DataFrame, downloads: pl.DataFrame
) -> dict:
    n_days = max(events.get_column("ts").dt.date().n_unique(), 1)
    capped_hours = events.get_column("capped_s").sum() / 3600
    dates = events.get_column("ts").dt.date()
    return {
        "total_visits": int(events.height),
        "total_urls": int(urls.height),
        "total_domains": int(events.get_column("domain").n_unique()),
        "active_days": int(n_days),
        "avg_visits_per_day": round(events.height / n_days, 1),
        "engaged_hours": round(capped_hours, 1),
        "avg_hours_per_day": round(capped_hours / n_days, 1),
        "search_count": 0,  # 由 analyze() 覆盖
        "download_count": int(downloads.height),
        "date_start": cast(date, dates.min()).isoformat() if events.height else "",
        "date_end": cast(date, dates.max()).isoformat() if events.height else "",
    }


def _top_domains_by_visits(events: pl.DataFrame, limit: int = 12) -> dict:
    agg = (
        events.group_by("domain")
        .agg(pl.len().alias("count"))
        .sort("count", descending=True)
        .head(limit)
    )
    return {
        "domains": agg.get_column("domain").to_list(),
        "counts": [int(x) for x in agg.get_column("count").to_list()],
    }


def _top_domains_by_hours(events: pl.DataFrame, limit: int = 12) -> dict:
    """Top N 域名（按有效停留小时数）。"""
    agg = (
        events.group_by("domain")
        .agg((pl.col("capped_s").sum() / 3600).alias("hours"))
        .sort("hours", descending=True)
        .head(limit)
    )
    return {
        "domains": agg.get_column("domain").to_list(),
        "hours": [round(x, 1) for x in agg.get_column("hours").to_list()],
    }


def _categories(events: pl.DataFrame) -> dict:
    """各内容类别的访问次数与有效时长。"""
    agg = (
        events.group_by("category")
        .agg(
            pl.len().alias("count"),
            (pl.col("capped_s").sum() / 3600).alias("hours"),
        )
        .sort("hours", descending=True)
    )
    return {
        "names": agg.get_column("category").to_list(),
        "counts": [int(x) for x in agg.get_column("count").to_list()],
        "hours": [round(x, 1) for x in agg.get_column("hours").to_list()],
    }


def _hourly_distribution(events: pl.DataFrame) -> list[int]:
    counts = dict(
        events.with_columns(pl.col("ts").dt.hour().alias("hour"))
        .group_by("hour")
        .agg(pl.len().alias("count"))
        .iter_rows()
    )
    return [int(counts.get(h, 0)) for h in range(24)]


def _weekday_distribution(events: pl.DataFrame) -> dict:
    """周内分布：每个工作日的访问次数。"""
    counts = dict(
        events.with_columns((pl.col("ts").dt.weekday() - 1).alias("wd"))
        .group_by("wd")
        .agg(pl.len().alias("count"))
        .iter_rows()
    )
    return {
        "weekdays": _WEEKDAYS,
        "counts": [int(counts.get(wd, 0)) for wd in range(7)],
    }


def _weekday_hour_heatmap(events: pl.DataFrame) -> dict:
    grouped = (
        events.with_columns(
            pl.col("ts").dt.hour().alias("hour"),
            (pl.col("ts").dt.weekday() - 1).alias("weekday"),
        )
        .group_by("weekday", "hour")
        .agg(pl.len().alias("count"))
    )
    lookup = {(wd, h): c for wd, h, c in grouped.iter_rows()}
    data = []
    max_count = 0
    for wd in range(7):
        for h in range(24):
            c = int(lookup.get((wd, h), 0))
            max_count = max(max_count, c)
            data.append([h, wd, c])
    return {
        "hours": [f"{h}时" for h in range(24)],
        "weekdays": _WEEKDAYS,
        "data": data,
        "max": max_count,
    }


def _daily_trend(events: pl.DataFrame) -> dict:
    """每日访问量与有效时长（覆盖完整数据区间）。"""
    if events.height == 0:
        return {"dates": [], "counts": [], "hours": []}

    daily = (
        events.with_columns(pl.col("ts").dt.date().alias("date"))
        .group_by("date")
        .agg(
            pl.len().alias("count"),
            (pl.col("capped_s").sum() / 3600).alias("hours"),
        )
    )
    first_day = cast(date, daily.get_column("date").min())
    last_day = cast(date, daily.get_column("date").max())
    full = pl.DataFrame(
        {"date": pl.date_range(first_day, last_day, interval="1d", eager=True)}
    )
    merged = (
        full.join(daily, on="date", how="left")
        .with_columns(pl.col("count").fill_null(0), pl.col("hours").fill_null(0))
        .sort("date")
    )
    return {
        "dates": [d.isoformat() for d in merged.get_column("date").to_list()],
        "counts": [int(x) for x in merged.get_column("count").to_list()],
        "hours": [round(x, 2) for x in merged.get_column("hours").to_list()],
    }


def _duration_distribution(events: pl.DataFrame) -> dict:
    """停留时长分桶（用原始时长，刻画长尾）。"""
    labels = ["0秒", "1-4秒", "5-29秒", "30-59秒", "1-4分", "5-14分", "15-59分", "1时+"]
    bounds = [
        (pl.col("dur_s") <= 0),
        (pl.col("dur_s") > 0) & (pl.col("dur_s") < 5),
        (pl.col("dur_s") >= 5) & (pl.col("dur_s") < 30),
        (pl.col("dur_s") >= 30) & (pl.col("dur_s") < 60),
        (pl.col("dur_s") >= 60) & (pl.col("dur_s") < 300),
        (pl.col("dur_s") >= 300) & (pl.col("dur_s") < 900),
        (pl.col("dur_s") >= 900) & (pl.col("dur_s") < 3600),
        (pl.col("dur_s") >= 3600),
    ]
    bucket = pl.lit(len(bounds) - 1)
    for i, cond in reversed(list(enumerate(bounds))):
        bucket = pl.when(cond).then(pl.lit(i)).otherwise(bucket)
    counts = dict(
        events.with_columns(bucket.alias("b"))
        .group_by("b")
        .agg(pl.len().alias("c"))
        .iter_rows()
    )
    return {
        "labels": labels,
        "counts": [int(counts.get(i, 0)) for i in range(len(labels))],
    }


def _transitions(events: pl.DataFrame) -> dict:
    """导航类型分布（transition 核心类型）。"""
    agg = (
        events.group_by("trans_core")
        .agg(pl.len().alias("count"))
        .sort("count", descending=True)
    )
    items = [
        {
            "name": _TRANSITION_NAMES.get(int(t), f"类型{int(t)}"),
            "value": int(c),
        }
        for t, c in agg.iter_rows()
    ]
    return {"items": items}


def _navigation_source(events: pl.DataFrame) -> dict:
    """直接访问（from_visit=0）vs 页面内点击。"""
    direct = int(events.filter(pl.col("from_visit") == 0).height)
    referred = int(events.height - direct)
    return {"direct": direct, "referred": referred}


def _search_terms(searches: pl.DataFrame, limit: int = 30) -> dict:
    """高频原生搜索词。"""
    if searches.height == 0:
        return {"total": 0, "terms": [], "counts": []}
    agg = (
        searches.with_columns(pl.col("term").str.strip_chars().str.to_lowercase())
        .filter(pl.col("term") != "")
        .group_by("term")
        .agg(pl.len().alias("count"))
        .sort("count", descending=True)
        .head(limit)
    )
    return {
        "total": int(searches.height),
        "terms": agg.get_column("term").to_list(),
        "counts": [int(x) for x in agg.get_column("count").to_list()],
    }


def _sessions(events: pl.DataFrame) -> dict:
    """以 30 分钟间隔切分会话，统计会话数与时长分布（分钟）。"""
    if events.height == 0:
        return {"count": 0, "avg_min": 0, "median_min": 0, "max_min": 0}
    ts = events.select("ts").sort("ts").with_columns(
        (pl.col("ts").diff().dt.total_seconds()).alias("gap")
    )
    ts = ts.with_columns(
        (pl.col("gap").fill_null(_SESSION_GAP_SECONDS + 1) > _SESSION_GAP_SECONDS)
        .cum_sum()
        .alias("session")
    )
    sess = ts.group_by("session").agg(
        (
            (pl.col("ts").max() - pl.col("ts").min()).dt.total_seconds() / 60
        ).alias("minutes")
    )
    minutes = sess.get_column("minutes")
    return {
        "count": int(sess.height),
        "avg_min": round(cast(float, minutes.mean()), 1),
        "median_min": round(cast(float, minutes.median()), 1),
        "max_min": round(cast(float, minutes.max()), 1),
    }


def _weekday_weekend(events: pl.DataFrame) -> dict:
    """工作日 vs 周末的日均访问与日均有效时长对比。"""
    df = events.with_columns((pl.col("ts").dt.weekday() >= 6).alias("is_weekend"))
    out = {}
    for label, flag in (("工作日", False), ("周末", True)):
        sub = df.filter(pl.col("is_weekend") == flag)
        days = max(sub.get_column("ts").dt.date().n_unique(), 1) if sub.height else 1
        out[label] = {
            "avg_visits": round(sub.height / days, 0) if sub.height else 0,
            "avg_hours": round(sub.get_column("capped_s").sum() / 3600 / days, 1)
            if sub.height
            else 0,
        }
    return out


def _records(events: pl.DataFrame) -> dict:
    """全量访问明细，供前端 TanStack Table 做展示 / 筛选 / 排序 / 搜索。

    以「列名 + 行数组」的紧凑列式结构输出（避免每行重复字段名），
    前端再映射回对象。时间用本地墙钟字符串，按字典序即等价于时间序，
    便于客户端排序与按日期区间过滤。
    """
    fields = ["ts", "title", "url", "domain", "category", "dur", "trans", "src"]
    if events.height == 0:
        return {"fields": fields, "rows": []}

    df = events.select(
        pl.col("ts").dt.strftime("%Y-%m-%d %H:%M:%S").alias("ts"),
        pl.col("title").fill_null("").alias("title"),
        pl.col("url"),
        pl.col("domain"),
        pl.col("category"),
        pl.col("dur_s").round(0).cast(pl.Int64).alias("dur"),
        pl.col("trans_core")
        .replace_strict(_TRANSITION_NAMES, default="其他", return_dtype=pl.Utf8)
        .alias("trans"),
        pl.when(pl.col("from_visit") == 0)
        .then(pl.lit("直接访问"))
        .otherwise(pl.lit("页面内点击"))
        .alias("src"),
    ).sort("ts", descending=True)

    return {"fields": fields, "rows": df.rows()}


def _downloads_summary(downloads: pl.DataFrame, limit: int = 15) -> dict:
    """下载概况：总数、总大小，以及最近的几条记录。"""
    if downloads.height == 0:
        return {"count": 0, "total_mb": 0, "recent": []}
    total_bytes = int(downloads.get_column("total_bytes").fill_null(0).sum())
    recent = (
        downloads.sort("start_time", descending=True)
        .head(limit)
        .with_columns(
            pl.col("target_path")
            .str.extract(r"([^/\\]+)$", 1)
            .alias("name"),
            (pl.col("total_bytes").fill_null(0) / 1_048_576).round(2).alias("mb"),
        )
    )
    items = [
        {"name": n or "(未知)", "mb": float(m)}
        for n, m in recent.select("name", "mb").iter_rows()
    ]
    return {
        "count": int(downloads.height),
        "total_mb": round(total_bytes / 1_048_576, 1),
        "recent": items,
    }


def analyze(
    urls: pl.DataFrame,
    visits: pl.DataFrame,
    searches: pl.DataFrame | None = None,
    downloads: pl.DataFrame | None = None,
) -> dict:
    """执行全部分析，返回供模板注入的字典。"""
    if searches is None:
        searches = pl.DataFrame(schema={"term": pl.Utf8})
    if downloads is None:
        downloads = pl.DataFrame(
            schema={
                "target_path": pl.Utf8,
                "total_bytes": pl.Int64,
                "start_time": pl.Int64,
                "tab_url": pl.Utf8,
                "mime_type": pl.Utf8,
            }
        )

    events = _prepare(urls, visits)
    search = _search_terms(searches)

    overview = _overview(urls, events, downloads)
    overview["search_count"] = search["total"]

    return {
        "overview": overview,
        "top_domains": _top_domains_by_visits(events),
        "top_domains_hours": _top_domains_by_hours(events),
        "categories": _categories(events),
        "hourly": _hourly_distribution(events),
        "weekday": _weekday_distribution(events),
        "heatmap": _weekday_hour_heatmap(events),
        "trend": _daily_trend(events),
        "duration_dist": _duration_distribution(events),
        "transitions": _transitions(events),
        "nav_source": _navigation_source(events),
        "search": search,
        "sessions": _sessions(events),
        "weekday_weekend": _weekday_weekend(events),
        "downloads": _downloads_summary(downloads),
        "records": _records(events),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
