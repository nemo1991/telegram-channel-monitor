"""hatchling custom build hook — 给 wheel 打 platform-specific tag。

2026-09-20+ 解耦:tdlib-json-client wheel 含 native binary(由
scripts/build_libtdjson.{sh,ps1} 编译后放到 tdlib/),但 hatchling 不知道
这是 platform-specific(它只认 C 扩展为 platform-specific;我们走 ctypes 加载,
不算 C 扩展)。默认会打成 `py3-none-any` tag — pip install 时会装到所有
平台,但 Linux 装了带 macOS .dylib 的 wheel 就崩。

本 hook 在 initialize() 阶段设 {infer_tag: True, pure_python: False} →
hatchling 走 `packaging.tags.sys_tags()` 推出当前平台的 platform tag:
  - linux x86_64  → manylinux2014_x86_64(可能含更老的兼容层)
  - linux aarch64 → manylinux2014_aarch64
  - macos arm64   → macosx_<当前主版本>_0_arm64(本地 26.0;CI macos-14 ≈ 14.0)
  - macos amd64   → macosx_<当前主版本>_0_x86_64
  - windows x64   → win_amd64

build 输出文件名形如 `tdlib_json_client-0.1.1-cp313-cp313-<plat>.whl`,
与 platform tag 一一对应。
"""
from __future__ import annotations

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """标记 wheel 为 platform-specific(非 pure_python)。"""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        # 默认 {infer_tag: False, pure_python: True} → tag = "py3-none-any"
        # 这意味着 wheel 装到所有平台 —— 但 native lib 只对当前平台有效,
        # 其他平台装了启动就崩。
        # 解:infer_tag=True → hatchling 调 packaging.tags.sys_tags() 推
        # 当前平台 tag(macosx_11_0_arm64 / manylinux2014_x86_64 / win_amd64 / 等),
        # 同时 pure_python=False(双开关:tag 计算 + Wheel metadata Root-Is-Purelib 字段)。
        build_data["infer_tag"] = True
        build_data["pure_python"] = False
