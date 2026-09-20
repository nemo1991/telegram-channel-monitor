"""单频道 jsonl 文件 — 内存索引 + 行级锁。

`ChannelFile` 是 `JsonlFileStore` 的单频道视图:每条消息一行 JSON,
内存里维护 `telegram_msg_id -> 内存行号`(即 `rows` 下标,不是文件行号;
`load` 会 skip 坏行,两者不等)的 index 用于快速 upsert。

文件布局(由 caller 提供 path):
```
<root>/messages/<channel_id>.jsonl
    {"telegram_msg_id": 1, "text": "...", ...}
    {"telegram_msg_id": 2, ...}
    ...
```

upsert 语义:`(telegram_msg_id)` 重复时**原地覆盖**(行号不变);
flush 时全文件重写,所以行长度变化不影响索引。

线程安全:每实例有独立 `asyncio.Lock`,跨频道串行由 `JsonlFileStore._write_lock`
保证(同一频道并发安全,跨频道亦有序)。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path


class ChannelFile:
    """单频道 jsonl 文件的内存索引 + 锁。"""

    def __init__(self, path: Path) -> None:
        """`path` = 单频道 jsonl 文件路径(由 JsonlFileStore 计算好传入)。"""
        self.path = path
        # telegram_msg_id -> 内存行号(0-based)
        self.index: dict[int, int] = {}
        # 内存行:list[dict]
        self.rows: list[dict] = []
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """一次性读全文进内存(后续可改 mmap),构建 telegram_msg_id -> 内存行号索引。

        损坏行 skip(不抛);不存在文件等同空文件。

        2026-09-20 修 3 个 bug — 由 `tests/property/test_jsonl_corruption_recovery.py`
        的 property test 发现(此前一直 `xfail` 锁着):

        1. **索引错位(静默返错消息)** — 原 `for i, line in enumerate(...)` 把
           `i`(文件行号)存进 `self.index`,但 `rows` 只装解析成功的行。跳过任意
           一行后两者错位,`index[mid]` 指向**别的消息**,`get_message` 静默返回
           错 DTO。改用 `rows` 的真实下标。
        2. **合法 JSON 但非 dict 崩 load** — `[1,2,3]` / `42` / `null` 能过
           `json.loads`,append 进 rows 后 `d.get(...)` 抛 AttributeError,整个
           load 挂掉(用户手改文件可能留下这种行)。加 isinstance 守卫。
        3. **`splitlines()` 静默丢数据** — `str.splitlines()` 还会在 NEL(\\x85)/
           \\v / \\f / \\u2028 / \\u2029 处断行。`flush` 用
           `ensure_ascii=False`,这些字符在 JSON 字符串值里不会被转义 → 一行被
           劈成几段,每段都 JSONDecodeError 被 skip → 整条消息丢失。
           改 `split("\\n")`:flush 只写 \\n,读也只按 \\n 断。
        """
        if not self.path.exists():
            return
        # 文件可能极大,目前一次性 load;后续可改为 mmap
        text = self.path.read_text(encoding="utf-8")
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue  # 合法 JSON 但非对象(数组/数字/null)→ 跳过不崩
            try:
                mid = int(d.get("telegram_msg_id", 0))
            except (TypeError, ValueError):
                mid = 0  # 手改文件可能留下非数字 mid;数据保留但不可寻址
            self.rows.append(d)
            if mid:
                # 必须用 rows 下标 —— 上面 continue 掉的坏行不入 rows,
                # 用 enumerate 行号会错位(查到别的消息)
                self.index[mid] = len(self.rows) - 1

    async def upsert(self, msg_dict: dict) -> int:
        """幂等 upsert:`(telegram_msg_id)` 重复时原地覆盖,行号不变。

        返回 DB 内部 id(若 dict 里给了 id 就用 dict 的,否则用 telegram_msg_id)。
        """
        async with self._lock:
            mid = int(msg_dict["telegram_msg_id"])
            if mid in self.index:
                # 原地覆盖(行号不变);行长度可能变,后续 flush 全文件重写
                self.rows[self.index[mid]] = msg_dict
            else:
                self.index[mid] = len(self.rows)
                self.rows.append(msg_dict)
            # 同步 id(若调用方分配)
            return int(msg_dict.get("id", mid))

    async def delete(self, telegram_msg_id: int) -> None:
        """删单条消息;不存在 idempotent 不抛。删后重建 index(行号位移)。"""
        async with self._lock:
            if telegram_msg_id not in self.index:
                return
            idx = self.index.pop(telegram_msg_id)
            self.rows.pop(idx)
            # 重建 index(行号位移)
            for k, v in list(self.index.items()):
                if v > idx:
                    self.index[k] = v - 1

    async def flush(self) -> None:
        """全文件重写 + 原子 rename(.part 中转);锁内执行,保证读一致性。"""
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".part")
            with tmp.open("w", encoding="utf-8") as f:
                for r in self.rows:
                    f.write(json.dumps(r, ensure_ascii=False, default=str))
                    f.write("\n")
            tmp.replace(self.path)
