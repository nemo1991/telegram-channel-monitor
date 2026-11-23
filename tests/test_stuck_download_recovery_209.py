"""2026-11-23 v2.0.9 hotfix — 卡死的 DOWNLOADING 启动自愈守卫。

背景:用户本机 SQLite 库有 64 条 `download_status='downloading'` 且无
`object_key` 的历史记录。DOWNLOADING 的下载任务只活在内存队列,进程关闭
后无人推进 → UI 永久画沙漏,下载/重试按钮都禁用(下载只认 None、重试只认
FAILED)。本文件守卫:

1. `reset_stuck_downloads()` 在 InMemory / SQLite 两后端语义一致:只复位
   DOWNLOADING 且无 object_key 的孤儿,有 key / 其他状态不碰,返回条数。
2. UI 取消沙漏:DOWNLOADING 与未下载统一显示「未下载」,下载按钮对两者
   都启用(兜住启动自愈前的那一瞬)。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import pytest_asyncio

from tests.conftest import InMemoryRepository, make_message
from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MediaType
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.storage.sqlite_repo import SqliteRepository


def _media(status: MediaDownloadStatus | None, key: str | None) -> MediaDTO:
    return MediaDTO(
        type=MediaType.PHOTO,
        download_status=status,
        object_key=key,
        file_size=1024,
    )


@pytest_asyncio.fixture(params=["inmemory", "sqlite"])
async def repo(request: pytest.FixtureRequest, tmp_path: Path) -> StorageRepository:
    if request.param == "inmemory":
        r: StorageRepository = InMemoryRepository()
    else:
        r = SqliteRepository(path=tmp_path / "stuck.db")
        await r.connect()
        await r.init_schema()
    yield r
    await r.close()


async def _seed(repo: StorageRepository) -> None:
    """4 条消息:2 卡死(无 key)/1 DOWNLOADING 有 key/1 DONE。"""
    await repo.save_message(
        make_message(
            channel_id=1,
            msg_id=1,
            media=[_media(MediaDownloadStatus.DOWNLOADING, None)],
        )
    )
    await repo.save_message(
        make_message(
            channel_id=1,
            msg_id=2,
            media=[
                dataclasses.replace(
                    _media(MediaDownloadStatus.DOWNLOADING, None),
                    download_error="旧错误",
                )
            ],
        )
    )
    await repo.save_message(
        make_message(
            channel_id=1,
            msg_id=3,
            media=[_media(MediaDownloadStatus.DOWNLOADING, "media/kept.jpg")],
        )
    )
    await repo.save_message(
        make_message(
            channel_id=1,
            msg_id=4,
            media=[_media(MediaDownloadStatus.DONE, "media/done.jpg")],
        )
    )


@pytest.mark.asyncio
async def test_reset_stuck_downloads_only_orphans(repo: StorageRepository) -> None:
    """只复位 DOWNLOADING + 无 object_key;有 key / DONE 不碰;错误清空。"""
    await _seed(repo)
    n = await repo.reset_stuck_downloads()
    assert n == 2

    m1 = await repo.get_message(1, 1)
    assert m1 is not None and m1.media[0].download_status is None
    m2 = await repo.get_message(1, 2)
    assert m2 is not None
    assert m2.media[0].download_status is None
    assert m2.media[0].download_error is None, "复位时旧错误必须清掉,否则 tooltip 显示噪音"

    m3 = await repo.get_message(1, 3)
    assert m3 is not None and m3.media[0].download_status == MediaDownloadStatus.DOWNLOADING
    m4 = await repo.get_message(1, 4)
    assert m4 is not None and m4.media[0].download_status == MediaDownloadStatus.DONE

    # 幂等:再跑一次不再复位任何行
    assert await repo.reset_stuck_downloads() == 0


@pytest.mark.asyncio
async def test_reset_stuck_downloads_empty_repo(repo: StorageRepository) -> None:
    assert await repo.reset_stuck_downloads() == 0


def test_bootstrap_calls_reset_stuck_downloads() -> None:
    """启动时无条件调用自愈,失败降级为日志不阻断启动。"""
    import inspect

    from tgmonitor.core import runtime

    src = inspect.getsource(runtime.bootstrap)
    assert "reset_stuck_downloads" in src
    assert "except Exception" in src, "自愈失败不应阻断启动"
