# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for tgmonitor — Stage D v1.5.0 release.

Cross-platform:
  - Linux  → onedir(dist/tgmonitor/),后续 scripts/build_appimage.sh 包成 AppImage
  - macOS  → .app bundle(BUNDLE),含 Info.plist + 资源

资源收集策略(显式写死 datas,不用 collect_data_files,详见下方 datas 段):
  - tgmonitor.resources / tgmonitor.ui.resources(SVG / QSS / icons)
    — 运行时走 `importlib.resources.files()`,destination 分别对应
    `<bundle>/tgmonitor/resources/` 与 `<bundle>/tgmonitor/ui/resources/`,
    `icons/` 等子目录必须原样保留(否则 Windows 启动即报 nav_live.svg 缺失)。
  - ~~core/storage/schema.sql~~ 2026-10-12 plan-batch3 目标 3 step 3.1 删除
    文件并 inline 到 `postgres_repo.py:SCHEMA_SQL` 字符串,免去冷启动
    `Path(__file__).parent / "schema.sql"` 路径解析(frozen 下 __file__ 不可靠)。
    spec 不再单独打包 schema.sql。

关键 spec 写法(踩过的坑):
  - EXE 必须 `exclude_binaries=True`,只输出 bootloader + scripts
  - COLLECT 集中收集 a.binaries / a.datas 到 dist/<name>/
  - 同时塞 EXE + COLLECT 会让 PyInstaller 把 EXE 阶段已写入的二进制当成
    source data 校验,ValueError 抛错
"""
import sys
from pathlib import Path

block_cipher = None

# ---- resource collection:显式写死 datas,三平台统一 ----
# SPECPATH 是 PyInstaller 注入的全局变量:spec 文件所在目录 = 仓库根。
_SPEC_DIR = Path(SPECPATH)

# (源目录, 目标目录)。PyInstaller 对目录型 (src, dest) 会递归拷贝整棵树,
# 子目录结构原样保留。目标目录必须与运行时定位逻辑一致:
#   - icon.py:    `importlib.resources.files("tgmonitor.resources")`
#   - theme.py:   `importlib.resources.files("tgmonitor.ui.resources")`
_DATA_DIRS = [
    (_SPEC_DIR / "src/tgmonitor/resources", "tgmonitor/resources"),
    (_SPEC_DIR / "src/tgmonitor/ui/resources", "tgmonitor/ui/resources"),
    # 2026-09-02 v1.5.3 PR #D3:i18n 翻译文件(.*.qm — pyside6-lrelease 编译产物)
    (_SPEC_DIR / "src/tgmonitor/i18n", "tgmonitor/i18n"),
]

datas = []
for src_dir, dest in _DATA_DIRS:
    if src_dir.is_dir():
        datas.append((str(src_dir), dest))
    else:
        print(f"[spec] WARNING: 跳过缺失目录 {src_dir}")

# 2026-10-12 plan-batch3 目标 3 step 3.1:`schema.sql` 已删,SQL inline 到
# `postgres_repo.py:SCHEMA_SQL` 字符串,frozen 启动直接 `await conn.execute(SCHEMA_SQL)`。
# spec 不再需要 copy 一个不存在的单文件。

# aioboto3(S3 对象存储)通过 `session.client("s3")` 用**字符串
# lazy import** 注册服务子模块(如 `'aioboto3.s3.inject.inject_s3_transfer_methods'`),
# PyInstaller 静态分析只能看到 `import aioboto3`,漏掉全部子模块 → 打包产物
# 运行时 `no module named aioboto3.s3`(v1.0.18 Windows 产物报错)。collect_submodules
# 把 aioboto3 / aiobotocore(client 类同样运行时才构造)整包收集,彻底兜底。
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("aioboto3") + collect_submodules("aiobotocore")

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
    upx=False,         # 不压 UPX — 启动更快,体积换速度
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
            "CFBundleShortVersionString": "1.6.7",
            "CFBundleVersion": "1.6.7",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "13.0",
            # 不申请 Developer ID,Info.plist 不带 LSApplicationCategoryType,
            # 用户 Gatekeeper 手动允许
            "NSHumanReadableCopyright": "MIT License",
        },
    )
