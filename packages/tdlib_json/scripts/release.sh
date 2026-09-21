#!/usr/bin/env bash
# release.sh — 本地手动发布 tdlib-json-client 到 GitHub Release
#
# 用法:
#   1. 编辑 packages/tdlib_json/pyproject.toml bump version (0.1.0 → 0.1.1)
#   2. 编译当前平台的 libtdjson:
#        bash scripts/build_libtdjson.sh
#   3. 跑本脚本:
#        bash packages/tdlib_json/scripts/release.sh
#
# 脚本会:
#   - 读 pyproject.toml 的 version
#   - 在 packages/tdlib_json/dist/ 打 wheel
#   - 用 gh CLI 创建/更新 GitHub Release tdlib-json-client/v<version>
#     并上传 wheel
#
# **跨平台 wheel 必须在 GitHub Actions 上发布**(本脚本只产当前平台 wheel):
# 在 Actions 页面手动 Run workflow on tdlib_json.yml,3 平台并行编译 +
# Release job 把全部 wheels 整合到一个 release。
#
# **前置**:
#   - gh CLI 已认证:`gh auth status`
#   - 当前分支 main 且 up-to-date
#   - 仓库有 contents: write 权限
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKG_DIR="${REPO_ROOT}/packages/tdlib_json"

# ---- 0. 校验 ----
if ! command -v gh >/dev/null 2>&1; then
    echo "❌ 缺 gh CLI:brew install gh" >&2
    exit 1
fi
if [[ "$(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD)" != "main" ]]; then
    echo "❌ 必须在 main 分支发布" >&2
    exit 1
fi
if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain)" ]]; then
    echo "❌ 工作区不干净,提交或 stash 后再发" >&2
    exit 1
fi

# ---- 1. 读 version ----
version=$(grep -E '^version = ' "${PKG_DIR}/pyproject.toml" | sed -E 's/version = "(.*)"/\1/')
tag="tdlib-json-client/v${version}"
echo "==> 发布版本: ${version} (tag: ${tag})"

# ---- 2. 校验 native lib 产物 ----
# tdjson.py 路径规则:packages/tdlib_json/src/tdlib_json/tdlib/libtdjson_<sys>_<arch>.<ext>
sys=$(uname -s | tr '[:upper:]' '[:lower:]')
machine=$(uname -m)
case "$machine" in
    x86_64|amd64) arch=amd64 ;;
    arm64|aarch64) arch=arm64 ;;
    *) echo "❌ 不支持的架构: $machine"; exit 1 ;;
esac
case "$sys" in
    darwin) ext=dylib ;;
    linux)  ext=so ;;
    *) echo "❌ 不支持的系统: $sys(本脚本只支持 Linux/macOS;Windows 走 CI)"; exit 1 ;;
esac

lib="${PKG_DIR}/src/tdlib_json/tdlib/libtdjson_${sys}_${arch}.${ext}"
if [[ ! -f "$lib" ]]; then
    echo "❌ 缺 native lib: $lib"
    echo "   先跑: bash scripts/build_libtdjson.sh"
    exit 1
fi
ls -lh "$lib"
echo "✅ native lib 就绪"

# ---- 3. 打 wheel ----
echo "==> 打 wheel"
(cd "${PKG_DIR}" && uv build --wheel --out-dir dist/)
ls -lh "${PKG_DIR}/dist/"

# ---- 4. 检查 Release 是否已存在 ----
if gh release view "${tag}" >/dev/null 2>&1; then
    echo "==> Release ${tag} 已存在,上传/覆盖 wheel"
    gh release upload "${tag}" "${PKG_DIR}"/dist/*.whl --clobber
else
    echo "==> 创建 Release ${tag}"
    gh release create "${tag}" \
        --generate-notes \
        --title "tdlib-json-client v${version}" \
        "${PKG_DIR}"/dist/*.whl
fi

echo
echo "✅ 完成:https://github.com/$(gh repo view --json nameWithOwner -q .nameWithOwner)/releases/tag/${tag}"
echo
echo "下一步:"
echo "  - 验证 wheel 链接正确:gh release view ${tag} --json assets"
echo "  - CI 会自动在 main 上 build 其他平台的 wheel;若想立即让全平台就绪,"
echo "    去 Actions 页面 Run workflow on tdlib_json.yml"
