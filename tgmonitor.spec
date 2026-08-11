# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for tgmonitor — Stage D v1.0.0 release.

Cross-platform:
  - Linux  → onedir(dist/tgmonitor/),后续 scripts/build_appimage.sh 包成 AppImage
  - macOS  → .app bundle(BUNDLE),含 Info.plist + 资源

资源 collect 策略:
  - tdlib_json 内置 libtdjson native lib(.so / .dylib)— 由
    scripts/build_libtdjson.sh 从 td/td 源码编译后放入包内 `tdlib/` 目录。
    PyInstaller 默认不 collect data,需要 `collect_data_files("tdlib_json")`。
    加载器(tdjson.py)用 `Path(__file__).parent / "tdlib" / <binary>` 定位,
    所以 destination 必须是 `tdlib_json/tdlib`。
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

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# ---- resource collection ----
# tdlib_json 内置 libtdjson native lib(命名:`libtdjson_<plat>_<arch>.<ext>`,
# 由 scripts/build_libtdjson.sh 生成;未编译时 collect 为空,属正常 dev 状态)
tdlib_json_data = collect_data_files("tdlib_json")
# 项目自身资源(SVG / icons / QSS)— 走 importlib.resources
tg_resources = collect_data_files("tgmonitor.resources")
ui_resources = collect_data_files("tgmonitor.ui.resources")

datas = []
# tdlib_json 的 native lib 在 pkg 内是 `tdlib_json/tdlib/libtdjson_*.dylib`。
# tdlib_json.tdjson loader 走 `pathlib.Path(__file__).parent / "tdlib" / binary_name`,
# 所以 destination 必须是 `tdlib_json/tdlib` —— PyInstaller 才把文件展到
# `<bundle>/tdlib_json/tdlib/libtdjson_*.dylib`,loader 找得到。
# (用 `tdlib_json` 会把 `tdlib/` 子目录抹平,loader 找不到 → smoke test 报错)
for src, dest in tdlib_json_data:
    datas.append((src, f"tdlib_json/{dest}" if dest else "tdlib_json"))
datas += [(src, "tgmonitor/resources") for src, _ in tg_resources]
datas += [(src, "tgmonitor/ui/resources") for src, _ in ui_resources]

# tdlib_json 是纯 Python + data,无延迟导入的 C 扩展,不需要 hiddenimports
hiddenimports = []

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
    # 旧 hooks/hook-aiotdlib.py 已随 aiotdlib 迁移删除;libtdjson 数据收集
    # 由上面显式 collect_data_files 完成,不再需要 hookspath
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
