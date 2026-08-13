#!/usr/bin/env bash
# build_libtdjson.sh — 从 td/td 源码编译 libtdjson(TDLib JSON 接口动态库)
#
# 背景:aiotdlib 上游已归档,仓库改用自编译 libtdjson + ctypes 绑定
# (packages/tdlib_json)。TDLib 的发布**不创建 git tag**(只有 v1.8.0 一个
# tag),版本号写在 CMakeLists.txt 的 `project(TDLib VERSION ...)` 里,因此
# 这里按**锁定 commit** 拉取,保证可复现。
#
# 当前锁定:TDLib 1.8.46(commit b498497bbfd6b80c86f800b3546a0170206317d3,
#           2025-03-13,CMakeLists 版本号已核实)
#
# 产物:包内 `packages/tdlib_json/src/tdlib_json/tdlib/libtdjson_<plat>_<arch>.<ext>`,
# 命名规则必须与 tdlib_json/tdjson.py `_get_bundled_tdjson_lib_path()` 一致:
#   darwin → .dylib(arm64/amd64),linux → .so(arm64/amd64)
#
# 缓存:产物目录放一个 `.tdlib-version` manifest(TDLib commit + 平台)。
# 若产物存在且 manifest 匹配当前锁定 commit,直接跳过编译进 ctypes 验证,
# CI 里配合 actions/cache 持久化产物目录,避免每次发布重编 TDLib。
#
# 依赖:cmake、gperf、OpenSSL + zlib 开发头文件
#   - macOS:  brew install cmake gperf openssl@3
#   - Linux:  sudo apt install cmake gperf libssl-dev zlib1g-dev build-essential
set -euo pipefail

# ---- config ----
TDLIB_COMMIT=b498497bbfd6b80c86f800b3546a0170206317d3
TDLIB_VERSION=1.8.46
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$REPO_ROOT/.build/tdlib"

# tdjson.py 的架构别名:uname -m → 文件名里的 arch 段
system_name=$(uname -s | tr '[:upper:]' '[:lower:]')
machine=$(uname -m | tr '[:upper:]' '[:lower:]')
case "$machine" in
    x86_64|amd64)   arch=amd64 ;;
    arm64|aarch64)  arch=arm64 ;;
    *) echo "❌ 不支持的架构: $machine"; exit 1 ;;
esac
case "$system_name" in
    darwin) ext=dylib ;;
    linux)  ext=so ;;
    *) echo "❌ 不支持的系统: $system_name"; exit 1 ;;
esac

DEST_DIR="$REPO_ROOT/packages/tdlib_json/src/tdlib_json/tdlib"
DEST="$DEST_DIR/libtdjson_${system_name}_${arch}.${ext}"

# ---- 0. 产物缓存判断:commit/平台未变则跳过编译 ----
MANIFEST="$DEST_DIR/.tdlib-version"
CURRENT_MANIFEST="tdlib=$TDLIB_COMMIT $system_name/$arch"

if [[ -f "$DEST" && -f "$MANIFEST" && "$(cat "$MANIFEST")" == "$CURRENT_MANIFEST" ]]; then
    echo "==> 命中缓存产物 $DEST(TDLib $TDLIB_VERSION commit 未变),跳过编译"
else
    # ---- 0.5 工具检查 ----
    for tool in cmake gperf git; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            echo "❌ 缺 $tool — 请先安装:"
            echo "   macOS: brew install cmake gperf"
            echo "   Linux: sudo apt install cmake gperf"
            exit 1
        fi
    done

    # ---- 1. 拉取 td/td 源码(锁定 commit)----
    if [[ ! -d "$BUILD_DIR/src/.git" ]]; then
        mkdir -p "$BUILD_DIR"
        echo "==> clone td/td (浅克隆 HEAD,再 checkout 锁定 commit)"
        git clone --depth 1 https://github.com/tdlib/td.git "$BUILD_DIR/src"
        cd "$BUILD_DIR/src"
        git fetch --depth 1 origin "$TDLIB_COMMIT"
        git checkout "$TDLIB_COMMIT"
    else
        cd "$BUILD_DIR/src"
        echo "==> 复用已有源码 $BUILD_DIR/src"
    fi

    # 锁定校验:HEAD 必须是指定 commit(防远端历史改写 / 本地改乱)
    actual=$(git rev-parse HEAD)
    if [[ "$actual" != "$TDLIB_COMMIT" ]]; then
        echo "❌ 源码 HEAD($actual)≠ 锁定 commit($TDLIB_COMMIT),拒绝构建"
        exit 1
    fi
    echo "==> 源码锁定 OK:$TDLIB_COMMIT (TDLib $TDLIB_VERSION)"

    # ---- 2. cmake 配置 + 只编 tdjson target ----
    # OpenSSL:Homebrew 的 openssl@3 不自动进 CMake 搜索路径,显式给
    OPENSSL_ROOT_DIR=""
    if [[ -d /opt/homebrew/opt/openssl@3 ]]; then
        OPENSSL_ROOT_DIR=/opt/homebrew/opt/openssl@3
    elif [[ -d /usr/local/opt/openssl@3 ]]; then
        OPENSSL_ROOT_DIR=/usr/local/opt/openssl@3
    fi
    OPENSSL_FLAGS=()
    [[ -n "$OPENSSL_ROOT_DIR" ]] && OPENSSL_FLAGS+=("-DOPENSSL_ROOT_DIR=$OPENSSL_ROOT_DIR")

    cmake -S "$BUILD_DIR/src" -B "$BUILD_DIR/build" \
        -DCMAKE_BUILD_TYPE=Release \
        "${OPENSSL_FLAGS[@]+"${OPENSSL_FLAGS[@]}"}" \
        -DTD_ENABLE_JAVA=OFF \
        -DTD_ENABLE_DOTNET=OFF \
        -DTD_ENABLE_TD_CLI=OFF \
        -DTD_ENABLE_EXAMPLES=OFF

    # 并行度:macOS 用 sysctl,Linux 用 nproc
    jobs=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 2)
    cmake --build "$BUILD_DIR/build" --target tdjson -j"$jobs"

    # ---- 3. 拷贝产物到包内 tdlib/ ----
    # tdjson target 设了 SOVERSION(Linux 下是 libtdjson.so.1.8.46 + 软链),
    # 用 `-L` 解引用拿真实文件,命名成 tdjson.py 期待的文件名。
    # 注意:dylib/so 直接输出在 CMake build 根目录(macOS 实测),不是 build/tdjson/ 子目录。
    mkdir -p "$DEST_DIR"
    libfile=$(find "$BUILD_DIR/build" \( -name "libtdjson.so" -o -name "libtdjson.dylib" \) -print -quit)
    if [[ -z "$libfile" ]]; then
        echo "❌ 没找到编译产物 libtdjson (.so/.dylib),看 $BUILD_DIR/build/tdjson"
        exit 1
    fi
    cp -L "$libfile" "$DEST"
    echo "$CURRENT_MANIFEST" > "$MANIFEST"
    echo "==> 产物:$DEST"
    ls -lh "$DEST"
    file "$DEST"
fi

# ---- 4. 验证 ctypes 能加载(无论是否跳过编译)----
# 注意:验证通过后用 os._exit(0) 硬退出,跳过解释器 teardown —— TDLib
# 内部线程在进程退出时偶发抢 stdout 锁导致 Segfault(exit 139,CI macOS
# job 踩过),冒烟验证已通过,没必要再走解释器收尾。必须先 flush:
# CI 管道是块缓冲,直接 os._exit 会丢掉前面打印的输出。
# 若中间验证抛异常,会在 os._exit 前以非 0 退出,CI 仍能拦住失败。
cd "$REPO_ROOT"
uv run python -c "
import asyncio
import os
import sys
import tdlib_json
c = tdlib_json.TdlibJsonClient({'@type': 'setLogVerbosityLevel', 'new_verbosity_level': 0})
print('✅ libtdjson 加载 OK, client id =', c.tdjson_client.client_id)
asyncio.run(c.stop())
print('✅ 关闭 OK')
sys.stdout.flush()
os._exit(0)
"
echo "✅ 构建完成 — $DEST"
