"""CLI 与路径解析模块 (CLI & Path Manager)。

负责解析命令行参数，并根据运行平台推断默认的 Chrome ``History`` 数据库路径。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def default_history_path() -> Path:
    """根据 ``sys.platform`` 推断当前操作系统默认的 Chrome History 路径。"""
    home = Path.home()
    if sys.platform.startswith("win"):
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else home / "AppData" / "Local"
        return base / "Google" / "Chrome" / "User Data" / "Default" / "History"
    if sys.platform == "darwin":
        return (
            home
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
            / "Default"
            / "History"
        )
    # 其余情况按 Linux 处理
    return home / ".config" / "google-chrome" / "Default" / "History"


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="browser-history-analyzer",
        description="将本地 Chrome 浏览器历史记录转化为美观的纯静态网页报告。",
    )
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=None,
        help="自定义 Chrome History 数据库文件路径（默认自动识别当前系统路径）。",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("report.html"),
        help="生成的 HTML 报告路径（默认当前目录 report.html）。",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数，未指定 ``--input`` 时填充默认 History 路径。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.input is None:
        args.input = default_history_path()
    return args
