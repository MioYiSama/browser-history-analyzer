"""静态页面生成模块 (Jinja2 HTML Builder)。

把分析结果转换为 JSON，通过 Jinja2 注入到模板的 ``<script>`` 变量中，
输出单文件 HTML 报告。
"""

from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_TEMPLATE_NAME = "template.html"


def render(report: dict) -> str:
    """用分析结果渲染出完整 HTML 字符串。"""
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template(_TEMPLATE_NAME)
    # ensure_ascii=False 保留中文，便于直接嵌入页面调试。
    chart_data = json.dumps(report, ensure_ascii=False)
    return template.render(report=report, chart_data=chart_data)


def build(report: dict, output: Path) -> Path:
    """渲染并写入输出文件，返回写入路径。"""
    html = render(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return output
