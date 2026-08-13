# build_libtdjson.ps1 — Windows 编译 libtdjson(TDLib JSON 接口动态库, vcpkg 方式)
#
# 背景:aiotdlib 上游已归档,仓库改用自编译 libtdjson + ctypes 绑定
# (packages/tdlib_json)。Linux / macOS 走 scripts/build_libtdjson.sh 从
# td/td 源码编译;Windows 上 TDLib 官方推荐 vcpkg,本脚本用
# `vcpkg install tdlib` 安装并拷贝产物到包内 `tdlib/` 目录。
#
# 产物:包内 `packages/tdlib_json/src/tdlib_json/tdlib/libtdjson_windows_amd64.dll`,
# 命名规则必须与 tdlib_json/tdjson.py `_get_bundled_tdjson_lib_path()` 一致
# (windows → .dll)。vcpkg 产出的 `tdjson.dll` 会被重命名为该名字;
# 依赖 dll(openssl / zlib)一并拷贝到同目录,ctypes 加载时经
# `os.add_dll_directory` 同目录解析。
#
# 依赖:vcpkg + Visual Studio C++ 工具链(GitHub Actions windows-latest 自带;
# 本地首次需装 vcpkg,见 https://vcpkg.io/en/getting-started)
param(
    # vcpkg 根目录;默认读 $env:VCPKG_INSTALLATION_ROOT / $env:VCPKG_ROOT,
    # 再退到 C:\vcpkg(GitHub windows-latest 预装路径)
    [string]$VcpkgRoot = "",
    [string]$Triplet = "x64-windows",
    # 编译工作根目录:MSVC 中间文件(%TMP%)+ vcpkg buildtrees/downloads/install/packages
    # 全挪这里。GitHub windows-latest 的 C: 盘在链接期会爆(C1088/LNK1180),
    # D: 盘有 ~70GB 空闲,默认 D:\a\vcpkg-work(本地跑可传别的盘符)
    [string]$WorkRoot = "D:\a\vcpkg-work"
)

$ErrorActionPreference = "Stop"

# ---- config ----
$RepoRoot = Split-Path -Parent $PSScriptRoot
$DestDir = Join-Path $RepoRoot "packages\tdlib_json\src\tdlib_json\tdlib"
$DestName = "libtdjson_windows_amd64.dll"

# ---- 0.5 临时目录与 vcpkg 缓存根统一到 $WorkRoot ----
# MSVC 编译器中间文件(_CL_*.tmp)写 %TMP%;vcpkg 编译中间产物写 buildtrees。
# 两者都在 C: 的话,tdlib 全量编译(~80min)会把 C: 挤爆,挪到 D: 后 C: 只
# 剩 checkout + venv,安全。目录先建好,子进程(cl.exe / nmake)会继承。
# vcpkg 二进制缓存(binary cache)也放 D::二次 install 命中存档时直接解压,
# 不再重编 tdlib(GitHub Actions 里配合 actions/cache 持久化,发布不用等全量编译)。
$env:TMP = Join-Path $WorkRoot "tmp"
$env:TEMP = Join-Path $WorkRoot "tmp"
New-Item -ItemType Directory -Force -Path $env:TMP | Out-Null
$VcpkgInstallRoot = Join-Path $WorkRoot "installed"
$env:VCPKG_DEFAULT_BINARY_CACHE = Join-Path $WorkRoot "binary-cache"
New-Item -ItemType Directory -Force -Path $env:VCPKG_DEFAULT_BINARY_CACHE | Out-Null

# ---- 0. 定位 vcpkg ----
if (-not $VcpkgRoot) {
    # GitHub windows-latest 预装 vcpkg,环境变量是 VCPKG_INSTALLATION_ROOT
    if ($env:VCPKG_INSTALLATION_ROOT) {
        $VcpkgRoot = $env:VCPKG_INSTALLATION_ROOT
    } elseif ($env:VCPKG_ROOT) {
        $VcpkgRoot = $env:VCPKG_ROOT
    } elseif (Test-Path "C:\vcpkg") {
        $VcpkgRoot = "C:\vcpkg"
    } else {
        Write-Error "找不到 vcpkg:请装 https://vcpkg.io 并设 VCPKG_ROOT 或传 -VcpkgRoot"
        exit 1
    }
}
$VcpkgExe = Join-Path $VcpkgRoot "vcpkg.exe"
if (-not (Test-Path $VcpkgExe)) {
    # 预装 vcpkg 可能只带源码,先跑官方 bootstrap 脚本编译 vcpkg.exe
    $Bootstrap = Join-Path $VcpkgRoot "bootstrap-vcpkg.bat"
    if (-not (Test-Path $Bootstrap)) {
        Write-Error "vcpkg.exe 不存在且无 bootstrap-vcpkg.bat: $VcpkgRoot"
        exit 1
    }
    Write-Host "==> bootstrapping vcpkg ..."
    & $Bootstrap -disableMetrics
    if ($LASTEXITCODE -ne 0) {
        Write-Error "bootstrap-vcpkg.bat 失败 (exit $LASTEXITCODE)"
        exit $LASTEXITCODE
    }
}
Write-Host "==> vcpkg: $VcpkgRoot"

# ---- 1. vcpkg install tdlib(首次编译较久,后续增量缓存) ----
# GitHub windows-latest runner 内存有限:tdlib Debug 配置用 MSVC 编译
# shareddialog.cpp 会 OOM(C1002),且 Debug PDB 超 4GB(LNK1140)。
# 只构建 Release,并限制编译并发,压低内存峰值。
$env:VCPKG_BUILD_TYPE = "release"
$env:VCPKG_MAX_CONCURRENCY = "2"
Write-Host "==> vcpkg install tdlib:$Triplet (build_type=$env:VCPKG_BUILD_TYPE) ..."
Write-Host "==> work root: $WorkRoot (tmp=$env:TMP, install=$VcpkgInstallRoot)"
# 缓存/中间目录全指到 $WorkRoot(D:),避免 C: 盘在链接期爆掉
$VcpkgArgs = @(
    "install", "tdlib:$Triplet",
    "--x-buildtrees-root=$(Join-Path $WorkRoot 'buildtrees')",
    "--x-downloads-root=$(Join-Path $WorkRoot 'downloads')",
    "--x-install-root=$VcpkgInstallRoot",
    "--x-packages-root=$(Join-Path $WorkRoot 'packages')"
)
& $VcpkgExe @VcpkgArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "vcpkg install tdlib 失败 (exit $LASTEXITCODE)"
    exit $LASTEXITCODE
}

# ---- 2. 拷贝产物到包内 tdlib/ ----
$VcpkgBin = Join-Path $VcpkgInstallRoot "$Triplet\bin"
if (-not (Test-Path $VcpkgBin)) {
    # VCPKG_BUILD_TYPE=release 时产物目录带 -release 后缀
    $VcpkgBin = Join-Path $VcpkgInstallRoot "$Triplet-release\bin"
}
$TdjsonDll = Join-Path $VcpkgBin "tdjson.dll"
if (-not (Test-Path $TdjsonDll)) {
    Write-Error "没找到 vcpkg 编译产物 tdjson.dll: $VcpkgBin"
    exit 1
}
New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
$Dest = Join-Path $DestDir $DestName
Copy-Item $TdjsonDll $Dest -Force
Write-Host "==> 产物:$Dest"

# 依赖 dll(openssl / zlib)— 与主库同目录,ctypes 加载时能解析
Get-ChildItem $VcpkgBin -Filter *.dll | Where-Object { $_.Name -ne "tdjson.dll" } | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $DestDir $_.Name) -Force
    Write-Host "==> 依赖:$($_.Name)"
}
Get-ChildItem $DestDir | Select-Object Name, Length | Format-Table -AutoSize

# ---- 3. 验证 ctypes 能加载 ----
# 注意:stop() 后 TDLib 内部线程可能仍在收尾,解释器 shutdown 时若它们
# 回调 __log_message_callback 写 stdout,会触发 Fatal Python error
# (_enter_buffered_busy,Windows 常见)导致非零退出。冒烟测试输出已 flush,
# 直接 os._exit(0) 跳过解释器 finalize,避免 daemon 线程抢 stdout 锁。
$verify = @'
import asyncio
import os
import sys
import tdlib_json
c = tdlib_json.TdlibJsonClient({'@type': 'setLogVerbosityLevel', 'new_verbosity_level': 0})
print('OK libtdjson loaded, client id =', c.tdjson_client.client_id, flush=True)
asyncio.run(c.stop())
print('OK close', flush=True)
sys.stdout.flush()
os._exit(0)
'@
Push-Location $RepoRoot
try {
    $verify | uv run python -
    if ($LASTEXITCODE -ne 0) {
        Write-Error "ctypes 加载验证失败 (exit $LASTEXITCODE)"
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}
Write-Host "OK Windows libtdjson build done"
