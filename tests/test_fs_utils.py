"""_fs_utils 单测 — 2026-09-24 v1.8.3。

覆盖:
- `format_bytes`:0 / 1024 边界 / 各单位
- `dir_size`:空目录 / 不存在路径 / 嵌套 / OSError 单文件跳过
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tgmonitor.core._fs_utils import dir_size, format_bytes

# ===== format_bytes =====


def test_format_bytes_zero() -> None:
    """n==0 走「0 B」分支(不进循环)。"""
    assert format_bytes(0) == "0B"


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "1B"),
        (1023, "1023B"),
        (1024, "1.0KB"),
        (1536, "1.5KB"),
        (1024 * 1024, "1.0MB"),
        (1024 * 1024 * 1024, "1.0GB"),
        (1024 * 1024 * 1024 * 1024, "1.0TB"),
        (2 * 1024 * 1024 * 1024 * 1024, "2.0TB"),
    ],
)
def test_format_bytes_units(n: int, expected: str) -> None:
    """单位边界 1KB / 1MB / 1GB / 1TB。约定:<1KB 不带空格(clear_channel_preview_dialog 同款)。"""
    assert format_bytes(n) == expected


def test_format_bytes_below_1kb_no_decimal() -> None:
    """< 1KB 用 int 格式,不带 .0。"""
    assert format_bytes(512) == "512B"


# ===== dir_size =====


def test_dir_size_missing_returns_zero(tmp_path: Path) -> None:
    """不存在路径 → 0,不抛。"""
    assert dir_size(tmp_path / "nope") == 0


def test_dir_size_empty_dir_returns_zero(tmp_path: Path) -> None:
    """空目录 → 0。"""
    d = tmp_path / "empty"
    d.mkdir()
    assert dir_size(d) == 0


def test_dir_size_sums_top_level_files(tmp_path: Path) -> None:
    """顶层文件直接 sum。"""
    d = tmp_path / "x"
    d.mkdir()
    (d / "a").write_bytes(b"a" * 100)
    (d / "b").write_bytes(b"b" * 250)
    assert dir_size(d) == 350


def test_dir_size_sums_nested(tmp_path: Path) -> None:
    """递归进子目录。"""
    d = tmp_path / "x"
    d.mkdir()
    (d / "a").write_bytes(b"a" * 100)
    sub = d / "sub"
    sub.mkdir()
    (sub / "b").write_bytes(b"b" * 200)
    deeper = sub / "deeper"
    deeper.mkdir()
    (deeper / "c").write_bytes(b"c" * 50)
    assert dir_size(d) == 350


def test_dir_size_swallows_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """某个文件 stat() 抛 OSError → 跳过,继续累加其他文件。

    实际触发:Windows 下文件刚被删 / 权限抖。不能用真删 — 测试设个 flag
    monkeypatch `os.walk` 之外的 stat 调用,这里直接 patch Path.stat。
    """
    d = tmp_path / "x"
    d.mkdir()
    good = d / "good"
    good.write_bytes(b"x" * 100)
    bad = d / "bad"
    bad.write_bytes(b"y" * 999)

    real_stat = Path.stat

    def flaky_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        if self.name == "bad":
            raise OSError("simulated")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    # bad 抛错被吞,只算 good 的 100 bytes
    assert dir_size(d) == 100


def test_dir_size_skips_symlink_loops_silently(tmp_path: Path) -> None:
    """循环 symlink 走 `os.walk` 默认 followlinks=False,不会无限循环。

    这个测试不是严格必要的(`os.walk` 默认就安全),只是给未来读者一个
    显式 anchor:我们用 `os.walk(path)`(无 followlinks),不递归 symlink。
    """
    d = tmp_path / "x"
    d.mkdir()
    (d / "real").write_bytes(b"z" * 10)
    # symlink → 自己,followlinks=False 时不会被 walk 进入
    try:
        os.symlink(d, d / "loop", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this fs")
    # 不抛 + 至少算到 real
    assert dir_size(d) >= 10
