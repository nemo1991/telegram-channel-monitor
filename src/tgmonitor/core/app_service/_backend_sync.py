"""AppService 后端 swap 同步辅助 — 2026-10-12 plan-batch3 目标 2 step 2.1。

消 SLF001:`AppService.objects.setter` / `storage.setter` 直接写
`_sub._storage` / `_media._storage` / `_media._objects` 私有属性的
`noqa: SLF001` 注释集中到本模块,facade setter 只调一行 helper。

设计:
- plain module-level function(非 class)
- 第一参数 `app: AppService`(走 `app._xxx` 引用,避免 stale 副本)
- 失败语义与原 setter 一致:objects=None → 早返(历史兼容 aclose 后置 None);
  storage=None 不接受(property type 已声明非 None)

不在本模块范围:
- `_rebuild_storage` / `_rebuild_objects`(实质 swap 流程)— 留 step 2.5 跟
  `_reconfigure.py` 一起拆,跟 `validate_backends` 共享建 / 关新 backend 模板
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService


def set_objects(app: AppService, value: ObjectStore | None) -> None:
    """swap objectstore 时同步给 MediaService。

    触发场景:
    - 测试 `app.objects = fake_s3`
    - reconfigure / _rebuild_objects 末尾 `app.objects = new_objects`

    `value=None` 是历史兼容路径(`aclose()` 后置 None)— 不写回,
    避免 `_media._objects` 类型不匹配。
    """
    if value is None:
        return
    app._objects = value
    app._media._objects = value


def set_storage(app: AppService, value: StorageRepository) -> None:
    """swap storage 时同步给 SubscriptionService + MediaService。

    触发场景:
    - 测试 `app.storage = fake_repo`
    - reconfigure / _rebuild_storage 末尾 `app.storage = new_storage`
    """
    app._storage = value
    app._sub._storage = value
    app._media._storage = value
