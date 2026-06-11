"""数据库读取模块 (DB Extractor)。

Chrome 运行时会锁定 ``History`` 文件，直接读取会失败。本模块在读取前先把
数据库复制到系统临时目录，读取完成后清理临时文件。

除核心的 ``urls`` / ``visits`` 表外，还会尽量读取 ``keyword_search_terms``
（原生搜索词）与 ``downloads``（下载记录）。这两张表在部分 Chrome 版本或
精简数据库中可能缺失，因此读取失败时返回空表而非抛错。
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import polars as pl

# urls 表：每个 URL 的标题、累计访问次数、地址栏键入次数、是否隐藏。
_URLS_QUERY = """
    SELECT id, url, title, visit_count, typed_count, hidden
    FROM urls
"""

# visits 表：每次访问的时间戳、停留时长、导航类型与来源访问。
#   - visit_duration：微秒，真实停留时长（DB 独有）。
#   - transition：导航类型，低 8 位为核心类型（link/typed/generated…）。
#   - from_visit：来源 visit id，0 表示无来源（直接访问 / 新标签 / 启动）。
_VISITS_QUERY = """
    SELECT url AS url_id, visit_time, visit_duration, transition, from_visit
    FROM visits
"""

# keyword_search_terms 表：Chrome 原生记录的搜索词。
_SEARCH_QUERY = """
    SELECT term
    FROM keyword_search_terms
    WHERE term IS NOT NULL AND term != ''
"""

# downloads 表：下载记录。target_path 为最终保存路径。
_DOWNLOADS_QUERY = """
    SELECT target_path, total_bytes, start_time, tab_url, mime_type
    FROM downloads
"""


@dataclass
class HistoryData:
    """从 History 数据库提取出的全部原始表。"""

    urls: pl.DataFrame
    visits: pl.DataFrame
    searches: pl.DataFrame
    downloads: pl.DataFrame


@contextmanager
def _temp_copy(source: Path) -> Iterator[Path]:
    """将 ``source`` 复制到临时目录并在退出时删除。

    使用 ``shutil.copy2`` 保留元数据，避免 Chrome 运行时的文件锁问题。
    """
    if not source.exists():
        raise FileNotFoundError(f"未找到 History 数据库文件: {source}")

    tmp_dir = Path(tempfile.gettempdir())
    tmp_path = tmp_dir / f"chrome_history_{uuid.uuid4().hex}.sqlite"
    shutil.copy2(source, tmp_path)
    try:
        yield tmp_path
    finally:
        tmp_path.unlink(missing_ok=True)


def _read_table(conn: sqlite3.Connection, query: str, schema: dict) -> pl.DataFrame:
    """用 Polars 原生 API 执行查询并构建 DataFrame。

    ``schema_overrides`` 直接交给 Polars 完成类型转换，无需手动取游标 / fetchall。
    空结果集时 Polars 也会按 schema 返回正确列类型的空表。
    """
    return pl.read_database(query, conn, schema_overrides=schema)


def _read_optional(
    conn: sqlite3.Connection, query: str, schema: dict
) -> pl.DataFrame:
    """读取可能不存在的表，失败时返回带有正确 schema 的空表。"""
    try:
        return _read_table(conn, query, schema)
    except sqlite3.Error:
        return pl.DataFrame(schema=schema)


def extract(source: Path) -> HistoryData:
    """读取 Chrome History 数据库，返回 :class:`HistoryData`。"""
    with _temp_copy(source) as tmp_path:
        # 以只读模式打开临时副本。用 Path.as_uri() 生成合法的 file URI
        # （正斜杠 + 百分号转义），避免 Windows 路径里的反斜杠 / 盘符 / 空格
        # 导致 SQLite 找不到文件。
        uri = f"{tmp_path.as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            urls = _read_table(
                conn,
                _URLS_QUERY,
                {
                    "id": pl.Int64,
                    "url": pl.Utf8,
                    "title": pl.Utf8,
                    "visit_count": pl.Int64,
                    "typed_count": pl.Int64,
                    "hidden": pl.Int64,
                },
            )
            visits = _read_table(
                conn,
                _VISITS_QUERY,
                {
                    "url_id": pl.Int64,
                    "visit_time": pl.Int64,
                    "visit_duration": pl.Int64,
                    "transition": pl.Int64,
                    "from_visit": pl.Int64,
                },
            )
            searches = _read_optional(conn, _SEARCH_QUERY, {"term": pl.Utf8})
            downloads = _read_optional(
                conn,
                _DOWNLOADS_QUERY,
                {
                    "target_path": pl.Utf8,
                    "total_bytes": pl.Int64,
                    "start_time": pl.Int64,
                    "tab_url": pl.Utf8,
                    "mime_type": pl.Utf8,
                },
            )
        finally:
            conn.close()

    return HistoryData(
        urls=urls, visits=visits, searches=searches, downloads=downloads
    )
