"""程序入口：串联 CLI -> 提取 -> 分析 -> 生成报告。"""

from __future__ import annotations

import sys

from .analyzer import analyze
from .builder import build
from .cli import parse_args
from .extractor import extract


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print(f"📖 读取历史记录数据库: {args.input}")
    try:
        data = extract(args.input)
    except FileNotFoundError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        print(
            "   可通过 -i/--input 指定 Chrome History 文件路径。",
            file=sys.stderr,
        )
        return 1

    print(
        f"🔍 分析数据: {data.urls.height} 条网址, {data.visits.height} 条访问记录, "
        f"{data.searches.height} 条搜索词, {data.downloads.height} 条下载"
    )
    report = analyze(data.urls, data.visits, data.searches, data.downloads)

    output = build(report, args.output)
    print(f"✅ 报告已生成: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
