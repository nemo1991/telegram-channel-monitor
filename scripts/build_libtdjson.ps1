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
    [string]$Triplet = "x64-windows"
)

$ErrorActionPreference = "Stop"

# ---- config ----
$RepoRoot = Split-Path -Parent $PSScriptRoot
$DestDir = Join-Path $RepoRoot "packages\tdlib_json\src\tdlib_json\tdlib"
$DestName = "libtdjson_windows_amd64.dll"

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
& $VcpkgExe install "tdlib:$Triplet"
if ($LASTEXITCODE -ne 0) {
    Write-Error "vcpkg install tdlib 失败 (exit $LASTEXITCODE)"
    exit $LASTEXITCODE
}

# ---- 2. 拷贝产物到包内 tdlib/ ----
$VcpkgBin = Join-Path $VcpkgRoot "installed\$Triplet\bin"
if (-not (Test-Path $VcpkgBin)) {
    # VCPKG_BUILD_TYPE=release 时产物目录带 -release 后缀
    $VcpkgBin = Join-Path $VcpkgRoot "installed\$Triplet-release\bin"
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
$verify = @'
import asyncio
import tdlib_json
c = tdlib_json.TdlibJsonClient({'@type': 'setLogVerbosityLevel', 'new_verbosity_level': 0})
print('OK libtdjson loaded, client id =', c.tdjson_client.client_id)
asyncio.run(c.stop())
print('OK close')
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
