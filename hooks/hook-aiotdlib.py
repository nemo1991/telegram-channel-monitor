"""PyInstaller hook for aiotdlib — Stage D v1.0.0.

PyInstaller 6.x 没有官方 aiotdlib hook。本 hook 做两件事:

1. 显式 collect aiotdlib.tdlib 子包的数据文件(`libtdjson_<plat>_<arch>.<ext>`)。
   aiotdlib 0.27.x 用 ctypes find_library 在 pkg 目录里找 native lib,
   PyInstaller 默认不会跟子包的 data file,所以要在这里 collect。
   注:这是 spec 里 `collect_data_files("aiotdlib")` 的子 hook 版本,放
   在这里以防 spec 改动时漏掉。

2. 显式 hidden import `aiotdlib.tdlib`(同 spec 的 hiddenimports)。
"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# aiotdlib.tdlib 子包 + 任何深层子模块(防御未来 aiotdlib 拆分)
hiddenimports = collect_submodules("aiotdlib")

# aiotdlib.tdlib 内置 TDLib native lib + 任何 aiotdlib 自带数据
datas = collect_data_files("aiotdlib")