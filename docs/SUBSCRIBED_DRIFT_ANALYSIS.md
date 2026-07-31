# `_subscribed` set 漂移风险专项分析

> 时间: 2026-07-31
> 触发: REVIEW.md M7 列出 `core/app_service.py` 4 个 site(`_subscribed`)
>       跟 storage 之间在 reload / 退订失败 / reconfigure 路径可能漏同步
> 范围: 只**分析**,**不动代码** — 分析报告留此,改动留后续专门 PR

## TL;DR

| # | 风险等级 | site | 描述 | 当前是否真有 bug |
|---|---|---|---|---|
| A | 🔴 **Critical** | `app_service.py:180` | `unsubscribe_channel` 异常被吞,`_subscribed.discard` 仍执行 → 与 storage drift | 是 |
| B | 🟠 **High** | `app_service.py:225` | `list_messages(None)` 走 `_subscribed` 而 `list_subscribed_channels()` 走 storage — 同一服务两套真理 | 是(导出 vs 拉消息不一致) |
| C | 🔴 **Critical** | `app_service.py:303-304` | reconfigure 时 `new_db_ids = list_channels()`(全频道)而**不是** `list_subscribed_channels()`(已订频道),union 出 `_subscribed = 全频道 ∪ 旧订` — 一句配置改错,所有频道都被"自动订阅" | 是(语义错) |
| D | 🟡 **Medium** | bootstrap (L96-102) | 启动时一次性 load 持久化订阅 — 之后每次 `set_whitelist(...)` 从 VM 走,**AppService._subscribed 跟 VM 独立维护** | 否(轻微 drift,可接受) |
| E | 🟢 **Low** | `ChannelWidget._subscribed_ids` / `MonitorService._whitelist` | UI 端各自有 cache,跟 storage 同步依赖 VM 推送 | 否(标准 UI cache pattern) |

## 关键事实(2026-07-31 复核)

### 数据源分层

```
+--------------------+    +----------------------+
|  storage           |    |  AppService._subscribed|
|  (truth,持久)      |    |  (in-memory cache)|  ←── D 主战场
|  is_subscribed=True|<-→|  set[int]           |
+--------------------+    +----------------------+
                                    │
                                    ▼
                    +------------------------------+
                    |  MonitorViewModel             |
                    |  via list_subscribed_channels|
                    |  (AppService L158-162)        |
                    +------------------------------+
                                    │ (set_whitelist / set_subscribed)
                                    ▼
                    +------------------------------+
                    |  MonitorService._whitelist    |  ←  UI 实际订阅源
                    |  + ChannelWidget._subscribed_ids|
                    +------------------------------+
                                    │
                                    ▼
                          dashboard / main window / export dialog
```

### `AppService._subscribed` 5 个使用点

| site | 代码 | 跟 storage 关系 | drift 风险 |
|---|---|---|---|
| L66 | `self._subscribed: set[int] = set()` | `__init__` 初始化 | 无 |
| L102 | `self._subscribed = {c.id for c in persisted}` | **读** storage.list_subscribed_channels | 启动时 — 没接 EventBus 推送,VM bootstrap 后才会 reload |
| L169 | `self._subscribed.add(channel.id)` | `subscribe_channel` 内 — 紧跟 `set_channel_subscribed(True)` | 跟 C 有关,但单点成功路径没 drift |
| L180 | `self._subscribed.discard(channel_id)` | `unsubscribe_channel` 内 — **storage.set_channel_subscribed(False) 是 try/except 包裹** | **A:drift** |
| L225 | `ids = list(self._subscribed)` | `list_messages(channel_ids=None)` fallback | **B:drift**(与 L160-162 真理不一致) |

### 真相

`AppService.list_subscribed_channels()` 在 **L160-162** 已显式改成读 storage,且自述:

```python
async def list_subscribed_channels(self) -> list[ChannelDTO]:
    # 单一来源:storage.is_subscribed=True 的频道。
    # 不再以 `self._subscribed` in-memory set 为主 — 否则与 storage 漂移
    # 时 UI 与 monitor 不同步。
    return await self.storage.list_subscribed_channels()
```

但 **`list_messages(None)` 跟 `_subscribed` 是 fallback 真理** — 同一个 AppService 内 **两套真理并存**。

---

## A. Critical — `unsubscribe_channel` 异常吞 → drift

**位置**: `src/tgmonitor/core/app_service.py:172-181`

```python
async def unsubscribe_channel(self, channel_id: int) -> None:
    try:
        await self.storage.set_channel_subscribed(channel_id, False)
    except Exception:  # noqa: BLE001
        log.exception("set_channel_subscribed(%s, False) failed", channel_id)
    self._subscribed.discard(channel_id)  # ← 不论 storage 成功失败都 discard
    await self.bus.publish(ChannelUnsubscribed(channel_id=channel_id))
```

**问题**: storage 写失败的 `log.exception` 是诊断信号,**对用户来说**退订失败 ≠ 退订成功。当前实现:
- UI 发布 `ChannelUnsubscribed` → UI 立刻把这个 channel 从「已监听」栏移除
- 但 storage 里 `is_subscribed=True` 还在 → 下次进程重启 / reload,bind 到 `_subscribed`(L102)
- **ChannelWidget 的 `_subscribed_ids` set 也跟着 discard**(走 bus)→ 用户视觉上一致
- **但 storage 持记录 + monitor 白名单持有 → 重启后被还原**

另外 `ChannelUnsubscribed` 也没有附 success / failure 信息,UI 无法判断要不要恢复画布。

**Severity**: 🔴 — 跟 durable storage 不一致,影响 long-running session 跟下次启动。

**最小修复(建议放未来 PR,scope 控制)**:
1. storage 失败时**返**(`bool` 或 raise),不静默 discard
2. UI 已经 emit `ChannelUnsubscribed`,那 channel 实际应该真的退了 — 应该**先**走 `_subscribed.discard`,**storage 失败再 emit 一个 ErrorOccurred 让 UI 回滚视觉效果**
3. 或:加一行 **一致性 checker**(`assert_storage_and_cache_match()`),启动时 + 退订后跑,drift 时 log CRITICAL。

---

## B. High — `list_messages(None)` vs `list_subscribed_channels()` 真理不一致

**位置**: `src/tgmonitor/core/app_service.py:225`

```python
async def list_messages(self, channel_ids=None, ..., limit=200):
    ids = channel_ids if channel_ids is not None else list(self._subscribed)  # ← cache
    if not ids:
        return []
    return await self.storage.list_messages(ids, ...)
```

vs L158-162:

```python
async def list_subscribed_channels(self) -> list[ChannelDTO]:
    return await self.storage.list_subscribed_channels()  # ← storage truth
```

**问题**: 同一个 AppService 上,「列出已订阅频道」走 storage(L158),「拉最近消息时取所有订阅 id」走 `_subscribed` cache(L225)。

**实际受害者** — `ExportService` / `dashboard_widget.py` 等调 `list_subscribed_channels()` 时拿全真理,但 `list_messages(channel_ids=None)` 拿 cache 后 subset:
- 假设 A 场景触发(A 退订时 storage 失败但 cache 已 discard):用户 UI 看不到这个 channel,但 `list_messages()` 仍会拉它的消息 — 这是 **错方向**(用户期望"我看的是已订频道"),但实际表现是「它没在白名单里」所以不应该被拉。
- 反方向:VM bootstrap 期间 `_subscribed` 是空,直到 L102 赋值 — 这窗口内若 `list_messages(None)` 触发返 `[]`(本身无害,但用户视角像是「消息忽然没了」)。

**Severity**: 🟠 — 偶发不一致,UI 一致(都跟 cache 走),只是 export 跟消息流之间有 gap。

**最小修复**: `list_messages(None)` 改为:

```python
ids = channel_ids
if ids is None:
    ids = [c.id for c in await self.storage.list_subscribed_channels()]
```

即跟 `list_subscribed_channels()` 走同一真理,代价是每次 list_messages 多一次 storage IO;但 export/dashboard 不是 hot path,无 perf 关注。

---

## C. Critical — reconfigure 把全频道记成已订

**位置**: `src/tgmonitor/core/app_service.py:300-304`

```python
# 同步已订阅频道集合 — 用 union 而不是 intersection:
# 用户之前的订阅应该被保留(在新存储里没有的就是缺数据,
# 也不能默默从内存里抹掉)。
new_db_ids = {c.id for c in await new_storage.list_channels()}
self._subscribed = (new_db_ids | self._subscribed)
```

**bug**:`new_storage.list_channels()` 返**所有** channel(`is_subscribed` 不论真假),不是 `list_subscribed_channels()`(只返 `is_subscribed=True`)。

实际触发场景:用户在 Settings 切了 `db_backend`(JSONL → postgres),`AppService._apply_settings_changes` 触发 storage 重建:
- 新 storage 是空的(`list_channels()` 返 `[]`)
- `new_db_ids | self._subscribed` = `set() | self._subscribed` — 这是预期行为
- 但若 postgres 路径下 storage 已经有旧的 channel rows(共用 DB 不同 table),new_db_ids 就是**全部 channel**,union 出 `_subscribed = 全部 channel`(包括 unsubscribed!)
- 之后 `list_messages(None)` 拉所有 channel 消息(因为 `_subscribed` 包含全部)
- 用户看见 dashboard 频道数 = 总频道数,不等于「已监听」

注释里写的"用户之前的订阅应该被保留"是合理的语义,但实现里把 unsubscribed 也并进来,语义错。

**Severity**: 🔴 — 一次切 db_backend 后所有频道都算已订,违反 explicit-unsubscribe 的语义。

**最小修复**:

```python
new_db_subscribed = {
    c.id for c in await new_storage.list_subscribed_channels()
}
self._subscribed = (
    new_db_subscribed | (self._subscribed - new_db_subscribed)
)
# 简化:取 「新 storage 已订 + 旧 cache 不在新 storage 的」(后者保留等待用户决策)
```

或更安全的:

```python
# Re-read from storage as source of truth.
new_subscribed = {
    c.id for c in await new_storage.list_subscribed_channels()
}
preserved_from_cache = self._subscribed - {
    c.id for c in await new_storage.list_channels()
}
self._subscribed = new_subscribed | preserved_from_cache
```

即"新 storage 已订 + 旧 cache 中新 storage 完全没有的 channel(保留等下次 reload)"。含义清晰。

---

## D. Medium — VM bootstrap 之前的窗口期

**位置**: `AppService.__init__`(L66)+ `bootstrap`(L96-102)

VM 在 `bootstrap_ui` 时调 `app.list_subscribed_channels()` 拿列表推到 `MonitorService.set_whitelist(...)`。期间:
- AppService._subscribed = 空(L66 init 顺序)
- bootstrap() 第一行就 set(L102)— 同步发生在 VM 调之前
- VM bootstrap 拉 → cache 已经被填 — OK

但若 VM bootstrap 走 `set_whitelist` 之前有人 fire-and-forget 调 `_subscribed` 的路径(实际目前没有,只是 future risk)— drift。

**Severity**: 🟡 — 当前代码路径没真触发,但未来加 `app.subscribe_channel()` 早期 hook 时容易踩。

**最小修复**: 加 `AppService.subscribe_channel` / `unsubscribe_channel` 内 `await self.bus.publish(ChannelSubscribed/Unsubscribed)` 已是实现的,**MonitorService 监听 ChannelSubscribed 重新 set_whitelist 是补救路线** — 给未来加新 subscriber 的人留注释。

---

## E. Low — UI cache 跟 storage 同步

**位置**:`src/tgmonitor/ui/widgets/channel_widget.py:286-410`(_subscribed_ids)+ `src/tgmonitor/core/monitor/service.py:42-73`(_whitelist)

这两个 UI / service cache 跟 `AppService._subscribed` 同形 — 都是 memory cache。靠 EventBus 推 + `set_subscribed(channels)` 全量 reload 维护一致。

具体场景:`VM.set_subscribed(...)` 走 `list_subscribed_channels()`(L158),**走 storage 真理**,所以这两个 cache 跟 storage 一致,反而跟 `AppService._subscribed` cache 可能不一致 — 但这俩 cache 是 UI 唯一真理(实际不读 `_subscribed`)。

**Severity**: 🟢 — 没真 drift,只是多套内存副本。

**注意**:UI 端的 cache 是必要的(高频读),设计是合理的;`AppService._subscribed` 这个多余的 cache 才是问题 root。

---

## 修复优先级建议(供后续 PR 排序)

| 优先级 | 项 | 工作量 | 影响 | 单元测试路径 |
|---|---|---|---|---|
| P0 | **C reconfigure 全频道已订 bug** | 30 min | 🔴 一次切 db_backend 就出 | 模拟 storage 切换 + 验 `_subscribed` 只含 `is_subscribed=True` |
| P0 | **A unsubscribe 异常吞 → drift** | 1 h | 🔴 静默失败 | storage mock 抛 + 验 `_subscribed` 没 discard(且 emit ErrorOccurred) |
| P1 | **B list_messages 真理统一** | 15 min | 🟠 偶发不一致 | 退订失败 mock + 调 list_messages 验它仍然拉(因为 storage 还在订) |
| P2 | **D VM bootstrap 窗口期文档** | 10 min | 🟡 未来风险 | 写一段 docstring 注释 |

### 备选:删 `AppService._subscribed` 整体(L66 / L102 / L169 / L180 / L225 / L304 全删)

- 600+ 行 cleanup,一次 commit
- `_subscribed` 当前 5 个 use site 全部改成 call `await self.storage.list_subscribed_channels()`
- L304 reconfigure 改写逻辑跟 C 修一起做
- L225 list_messages 跟 B 一起做
- L180 unsubscribe 失败 discard 改为 emit ErrorOccurred 不 discard
- 之后 AppService 只剩 bootstrap 时一次性 cache(L102)做预热,VM 推一遍即可

**工时**:1-2 h(改 + 测 + 加回归测试)。

---

## 结论(不动代码结论)

1. **AppService._subscribed 是冗余 cache,设计不安全** — 真理已在 storage.is_subscribed。
2. **当前代码有 ≥2 个真 bug**(A、C),其中 C 是隐藏雷(用户切 db_backend 才会触发,但运行代码 search 看注释没查出来 — 注释里"用户之前的订阅应该被保留"是合理语义,但实现是错的)。
3. **B 是设计不一致**(两套真理并存),不是 bug。
4. **D / E 当前没真问题**,是「未来风险」类。

**后续 PR 推荐**:把 `AppService._subscribed` 整体删掉 + 修 C + 修 A,合并成 1 个 1-2 h 的 PR。要不要做等你拍板。
