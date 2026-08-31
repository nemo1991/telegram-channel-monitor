"""共用 fixtures 子包 — 2026-08-31 v1.5.0 PR #A6。

按域拆分原 tests/conftest.py(542 行)为多个子模块:
  - `_settings.py`       : `settings` fixture
  - `_storage.py`        : `InMemoryRepository` 测试替身 + `JsonlFileStore` 工厂 +
                           `repo_backend` parametrize(inmemory + jsonl)
  - `_objectstore.py`    : `objectstore` fixture(LocalObjectStore)
  - `_bus_client.py`     : `bus` / `client` fixtures(EventBus + FakeTelegramClient)
  - `_factories.py`      : `make_message` / `make_photo` 测试数据工厂
  - `_tdlib_stub.py`     : `stub_tdlib_init` fixture(Windows / CI 不编译 libtdjson)
  - `_monitor_app.py`    : `monitor` / `app` fixtures(完整链路)

公开 API:`tests.fixtures.<X>` 直接 import 子模块;`tests/conftest.py` 保留
backward-compat re-export 让现有 20 个 `from tests.conftest import ...`
不动也能用。
"""
