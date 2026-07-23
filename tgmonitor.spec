# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for tgmonitor — Stage D v1.0.0 release.

Cross-platform:
  - Linux  → onedir(dist/tgmonitor/),后续 scripts/build_appimage.sh 包成 AppImage
  - macOS  → .app bundle(BUNDLE),含 Info.plist + 资源

资源 collect 策略:
  - aiotdlib.tdlib 内置 TDLib native lib(.so / .dylib)— PyInstaller 默认
    不 collect,需要 `collect_data_files("aiotdlib")` + `hiddenimports=["aiotdlib.tdlib"]`
  - tgmonitor.resources / tgmonitor.ui.resources(SVG / QSS / icons)
    — 走 `importlib.resources.files()`,PyInstaller 跟 pkg 走,
    `collect_data_files` 显式保险

关键 spec 写法(踩过的坑):
  - EXE 必须 `exclude_binaries=True`,只输出 bootloader + scripts
  - COLLECT 集中收集 a.binaries / a.datas 到 dist/<name>/
  - 同时塞 EXE + COLLECT 会让 PyInstaller 把 EXE 阶段已写入的二进制当成
    source data 校验,ValueError 抛错
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# ---- resource collection ----
# aiotdlib 内置 TDLib native lib(命名:`libtdjson_<plat>_<arch>.<ext>`)
aiotdlib_data = collect_data_files("aiotdlib")
# 项目自身资源(SVG / icons / QSS)— 走 importlib.resources
tg_resources = collect_data_files("tgmonitor.resources")
ui_resources = collect_data_files("tgmonitor.ui.resources")

datas = []
# aiotdlib 的 native lib 在 pkg 内是 `aiotdlib/tdlib/libtdjson_*.dylib`。
# aiotdlib.tdjson loader 走 `pathlib.Path(__file__).parent / "tdlib" / binary_name`,
# 所以 destination 必须是 `aiotdlib/tdlib` —— PyInstaller 才把文件展到
# `<bundle>/Contents/Resources/aiotdlib/tdlib/libtdjson_*.dylib`,loader 找得到。
# (用 `aiotdlib` 会把 `tdlib/` 子目录抹平,loader 找不到 → smoke test 报错)
datas += [(src, "aiotdlib/tdlib") for src, _ in aiotdlib_data]
datas += [(src, "tgmonitor/resources") for src, _ in tg_resources]
datas += [(src, "tgmonitor/ui/resources") for src, _ in ui_resources]

# aiotdlib.tdlib 子包(__init__.py 负责 ctypes find_library)
hiddenimports = ["aiotdlib.tdlib"]

# App icon:macOS BUNDLE 只接受 .icns,我们目前只有 .svg(Pillow 也转不了)。
# v1.0.0 release 不强求 app icon — 用 None 让 PyInstaller fallback 到系统默认
# icon.app bundle 仍能跑(只是 dock / 窗口左上角没自定义图)。
# 后续 v1.0.1 / v1.1.0 再生成 .icns(用 iconutil 从多尺寸 PNG 合成)。
ICON = None

# ---- analysis ----
a = Analysis(
    ["src/tgmonitor/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    # 不让 PyInstaller 默认 hookspath;我们自己用 hooks/hook-aiotdlib.py
    # 当前 spec 里已显式 collect_data_files,不必走 hookspath(hook 是双保险,
    # D5 build.yml 里加 hookspath=[] 也行,先不开)
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],                # exclude_binaries=True → 不在这里输出 binaries
    exclude_binaries=True,
    name="tgmonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,       # macOS 上 strip 会破代码签名(我们不签,无所谓)
    upx=False,         # 不压 UPX — 影响 TDLib 启动速度
    console=False,     # GUI 应用,不开 console 窗口(Linux + macOS 都适用)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,    # 用户拍板不签 macOS
    entitlements_file=None,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="tgmonitor",
)

# ---- macOS .app bundle ----
# PyInstaller 6.x 的 BUNDLE 输出 macOS .app(LSMinimumSystemVersion 13.0 / Apple Silicon)。
# Linux AppImage 由 scripts/build_appimage.sh 后续包,不在 spec 范围。
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="tgmonitor.app",
        icon=ICON,
        bundle_identifier="com.github.forcetone.tgmonitor",
        info_plist={
            "CFBundleName": "tgmonitor",
            "CFBundleDisplayName": "Telegram Channel Monitor",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "13.0",
            # 不申请 Developer ID,Info.plist 不带 LSApplicationCategoryType,
            # 用户 Gatekeeper 手动允许
            "NSHumanReadableCopyright": "MIT License",
        },
    )