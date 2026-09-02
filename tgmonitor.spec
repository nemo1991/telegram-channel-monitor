# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for tgmonitor — Stage D v1.5.0 release.

Cross-platform:
  - Linux  → onedir(dist/tgmonitor/),后续 scripts/build_appimage.sh 包成 AppImage
  - macOS  → .app bundle(BUNDLE),含 Info.plist + 资源

资源收集策略(显式写死 datas,不用 collect_data_files,详见下方 datas 段):
  - tdlib_json 内置 libtdjson native lib(.so / .dylib / .dll)— 由
    scripts/build_libtdjson.sh(macOS / Linux)或
    scripts/build_libtdjson.ps1(Windows,vcpkg)编译后放入包内 `tdlib/` 目录。
    加载器(tdjson.py)用 `Path(__file__).parent / "tdlib" / <binary>` 定位,
    所以 destination 必须是 `tdlib_json/tdlib`。
  - tgmonitor.resources / tgmonitor.ui.resources(SVG / QSS / icons)
    — 运行时走 `importlib.resources.files()`,destination 分别对应
    `<bundle>/tgmonitor/resources/` 与 `<bundle>/tgmonitor/ui/resources/`,
    `icons/` 等子目录必须原样保留(否则 Windows 启动即报 nav_live.svg 缺失)。
  - core/storage/schema.sql — 单文件(非目录),走 datas 单文件条目,
    destination 必须是 `tgmonitor/core/storage/`(与 postgres_repo.py 的
    `Path(__file__).parent / "schema.sql"` 定位一致);漏掉则配置 PG 后
    init_schema() 直接 FileNotFoundError(v1.0.12 Windows 产物报错根因)。

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
#   - tdjson.py:  `Path(__file__).parent / "tdlib" / <binary>` →
#                 frozen 下 __file__ = <bundle>/tdlib_json/tdjson.pyc
#   - icon.py:    `importlib.resources.files("tgmonitor.resources")`
#   - theme.py:   `importlib.resources.files("tgmonitor.ui.resources")`
#
# 为什么不用 collect_data_files:它的 dest 由 find_spec 解析 editable workspace
# 包决定,跨平台不一致 —— Windows CI 上 `.pth` 展开的 `packages/tdlib_json/src`
# 在 sys.path,dest 会翻倍嵌套成 `tdlib_json/tdlib_json/tdlib`(v1.0.7 / v1.0.8
# Windows 产物 dll 两层嵌套、启动崩的根因)。写死源目录与目标目录,三平台
# 行为完全一致、可预期。
_DATA_DIRS = [
    (_SPEC_DIR / "packages/tdlib_json/src/tdlib_json/tdlib", "tdlib_json/tdlib"),
    (_SPEC_DIR / "src/tgmonitor/resources", "tgmonitor/resources"),
    (_SPEC_DIR / "src/tgmonitor/ui/resources", "tgmonitor/ui/resources"),
]

datas = []
for src_dir, dest in _DATA_DIRS:
    if src_dir.is_dir():
        datas.append((str(src_dir), dest))
    else:
        print(f"[spec] WARNING: 跳过缺失目录 {src_dir}")

# schema.sql 是单文件,不走目录递归;PyInstaller datas 支持 (file, dest_dir),
# 目标目录必须与 postgres_repo.py 的 `Path(__file__).parent / "schema.sql"` 一致。
_SCHEMA_SQL = _SPEC_DIR / "src/tgmonitor/core/storage/schema.sql"
if _SCHEMA_SQL.is_file():
    datas.append((str(_SCHEMA_SQL), "tgmonitor/core/storage"))
else:
    print(f"[spec] WARNING: 缺失 schema.sql {_SCHEMA_SQL}")

# tdlib_json 是纯 Python + data,无延迟导入的 C 扩展,不需要 hiddenimports。
# aioboto3(S3 对象存储)例外:它通过 `session.client("s3")` 用**字符串
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
    # 旧 hooks/hook-aiotdlib.py 已随 aiotdlib 迁移删除;libtdjson 数据收集
    # 由上面显式 datas 完成,不再需要 hookspath
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
            "CFBundleShortVersionString": "1.5.2",
            "CFBundleVersion": "1.5.2",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "13.0",
            # 不申请 Developer ID,Info.plist 不带 LSApplicationCategoryType,
            # 用户 Gatekeeper 手动允许
            "NSHumanReadableCopyright": "MIT License",
        },
    )
