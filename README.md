# 🚀 Chrome 浏览器历史记录分析分析工具

## 一、 项目概述核心架构

本工具旨在将用户的本地 Chrome 浏览器历史记录，转化为直观、美观的纯静态网页报告。

- **输入流**：本地 SQLite3 数据库 (或通过 `-i` 参数指定)。
- **处理流**：Python 读取纯净数据 -> `Polars` 进行高性能聚合与时序分析 -> 生成基于字典的图表数据。
- **输出流**：`Jinja2` 注入数据 -> 结合 `ECharts` (CDN引入) 输出单文件 `index.html`。

## 二、 模块划分与详细要求

### 1. CLI 与路径解析模块 (CLI & Path Manager)

- **命令行参数解析**：使用内置的 `argparse` 库。
  - `-i` / `--input`：自定义 History 数据库文件路径。
  - `-o` / `--output`：生成的 HTML 报告路径（默认当前目录 `report.html`）。
- **默认路径自动识别**：根据 `sys.platform` 自动推断当前操作系统的默认 Chrome 保存路径：
  - `Windows`: `%LocalAppData%\Google\Chrome\User Data\Default\History`
  - `macOS`: `~/Library/Application Support/Google/Chrome/Default/History`
  - `Linux`: `~/.config/google-chrome/Default/History`

### 2. 数据库读取模块 (DB Extractor)

- **🚨 核心机制 (避坑必读)**：由于 Chrome 正在运行时会**锁定** `History` 文件，**千万不要直接读取**。
  - **策略**：必须在读取前，使用 `shutil.copy2` 将目标文件复制到系统的临时目录（如 `tempfile.gettempdir()`），读取临时文件，读取完成后删除。
- **目标表解析**：
  - `urls` 表：获取 `url`, `title`, `visit_count`, `typed_count`, `hidden`。
  - `visits` 表：获取 `visit_time` (访问时间)、`visit_duration` (真实停留时长，微秒)、`transition` (导航类型)、`from_visit` (来源访问)。
  - `keyword_search_terms` 表：Chrome 原生记录的搜索词（可选，缺失时降级为空）。
  - `downloads` 表：下载记录（可选）。

### 3. 数据清洗与分析模块 (Polars Data Engine)

使用 `Polars` 替代 Pandas，追求更高的内存效率和执行速度。

- **数据清洗规则**：
  - **时间戳转换**：Chrome 的时间戳是 WebKit 格式（从 **1601年1月1日** 起算的微秒数），需要使用 Polars 进行精准的日期推算。
  - **URL 解析**：提取 URL 中的 Domain (域名)，去除参数和具体路径，方便统计“最常访问的网站”。
- **真实停留时长还原**：`visit_duration` 为微秒级原始停留时长。统计「有效使用时长」时按 **30 分钟封顶**（capped engagement），剔除「开着页面离开」的挂机噪声，得到更接近真实的使用时间。
- **核心分析指标 (ECharts 图表需要的数据)**：
  1.  **总览 KPI**：总访问记录、有效使用时长、独立网址 / 域名、上网会话数、搜索次数、下载文件数，以及日均强度与数据时间区间。
  2.  **内容类别画像**：按域名归类（视频 / 开发技术 / AI / 搜索 / 社交 / 邮件办公 / 教育 / 新闻 / 购物 / 音乐 …），分别统计访问次数与有效时长（环形图 + 双指标柱状图）。
  3.  **网站排行**：Top 域名「按访问次数」与「按有效时长」双榜（条形图）。
  4.  **时间画像**：24 小时分布（折线）、周内分布（柱状，周末高亮）、每周 × 24 小时热力图、每日访问量与有效时长双轴趋势（可缩放）。
  5.  **行为模式**：单次停留时长分布（瞄一眼 vs 长读）、导航类型（链接 / 地址栏 / 联想 / 表单 …）、访问来源（直接访问 vs 页面内点击）、工作日 vs 周末日均对比、上网会话时长概况。
  6.  **搜索与下载**：高频原生搜索词 Top 30、最近下载文件列表。
  7.  **历史明细（全量表格）**：导出**每一条**访问记录（时间、标题、网址、域名、类别、停留时长、导航类型、来源），由前端 TanStack Table 驱动，支持全局搜索、按域名 / 类别 / 导航类型 / 来源 / 日期区间筛选、任意列排序、分页与**拖拽调整列宽**。数据以「列名 + 行数组」的紧凑列式结构注入，避免逐行重复字段名。

### 4. 静态页面生成模块 (Jinja2 HTML Builder)

- **模板设计**：`template.html` 文件预留 ECharts 所需的 `<div>` 图表容器和数据插槽。
- **前端依赖引入**（全部通过 CDN，输出单文件无需本地资源）：
  - ECharts 6：`<script src="https://cdn.jsdelivr.net/npm/echarts@6/dist/echarts.min.js"></script>`
  - jQuery 4：`<script src="https://cdn.jsdelivr.net/npm/jquery@4/dist/jquery.min.js"></script>`
  - Bootstrap 5：负责整体布局与卡片式 UI，让报告美观且自适应。
  - TanStack Table（`@tanstack/table-core@8`，以 ESM 形式按需引入）：驱动「历史明细」全量表格的筛选 / 排序 / 分页 / 列宽拖拽，由原生 JS 渲染，无需打包构建。
- **数据注入**：将 Polars 处理好的结果转换为 JSON 字符串，通过 Jinja2 注入到 HTML 的 `<script>` 标签内的 JavaScript 变量中，再由 jQuery 在 `ready` 后初始化各 ECharts 实例；明细表数据挂到 `window.REPORT` 供下方 ES module 读取。

## 三、 安装与使用

```bash
# 安装依赖（推荐使用 uv）
uv sync

# 自动识别当前系统的 Chrome History 路径并生成报告
uv run browser-history-analyzer

# 指定输入数据库与输出文件
uv run browser-history-analyzer -i /path/to/History -o report.html
```

> ⚠️ Chrome 运行时会锁定 `History` 文件，工具会先用 `shutil.copy2` 将其复制到系统临时目录再读取，读取完成后自动删除临时副本，因此无需关闭浏览器。

### 命令行参数

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `-i` / `--input` | 自定义 Chrome History 数据库文件路径 | 按操作系统自动识别 |
| `-o` / `--output` | 生成的 HTML 报告路径 | `report.html` |

## 四、 项目结构

```
src/browser_history_analyzer/
├── __main__.py        # 程序入口：CLI -> 提取 -> 分析 -> 生成
├── cli.py             # 命令行参数解析与默认路径识别
├── extractor.py       # 安全复制并读取 SQLite 数据库
├── analyzer.py        # Polars 数据清洗与聚合分析
├── builder.py         # Jinja2 渲染 HTML 报告
└── templates/
    └── template.html  # jQuery 4 + Bootstrap 5 + ECharts 6 + TanStack Table 模板
```
