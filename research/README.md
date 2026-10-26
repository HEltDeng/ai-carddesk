# 公开站点调研工具

这里保存公开页面抓取、样本整理和本地报告渲染源码。仅使用 Python 标准库；报告交互脚本为原生 JavaScript。

- `fetch_public.py URL...`：读取指定公开页面，将提取文字和链接保存到本地 `evidence/`。
- `build_catalog.py`：从脚本中保留的 2026-10-22 公开观察快照生成 `sites-50.json` 和 `50站点逐项调研.md`。价格及商家声明是历史记录，运行脚本不会重新核实。
- `render_report.py`：读取本地 `sites-50.json` 和 `AI代充站调研报告.md`，生成 `AI代充站调研.html`。报告正文由本地提供，不随源码上传。
- `report-ui.js`：报告页的标签切换、搜索与分类筛选脚本。

在本目录运行 `python3 build_catalog.py`，准备本地报告正文后运行 `python3 render_report.py`。页面抓取证据、数据文件和报告产物保留在本地，已列入根目录 `.gitignore`。
