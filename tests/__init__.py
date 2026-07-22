# tests package marker — 让 `from tests.conftest import …` 在任何 pytest
# 入口形式(`pytest` binary / `python -m pytest` / IDE 单独跑文件)下都能
# 解析。pytest 的 rootdir 自动注入不再是被依赖的隐性机制。