"""单频道 jsonl 文件 — 内存索引 + 行级锁。

`ChannelFile` 是 `JsonlFileStore` 的单频道视图:每条消息一行 JSON,
内存里维护 `telegram_msg_id -> line_no` 的 index 用于快速 upsert。

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
        self.path = path
        # telegram_msg_id -> 内存行号(0-based)
        self.index: dict[int, int] = {}
        # 内存行:list[dict]
        self.rows: list[dict] = []
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        if not self.path.exists():
            return
        # 文件可能极大,目前一次性 load;后续可改为 mmap
        text = self.path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.rows.append(d)
            mid = int(d.get("telegram_msg_id", 0))
            if mid:
                self.index[mid] = i

    async def upsert(self, msg_dict: dict) -> int:
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
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".part")
            with tmp.open("w", encoding="utf-8") as f:
                for r in self.rows:
                    f.write(json.dumps(r, ensure_ascii=False, default=str))
                    f.write("\n")
            tmp.replace(self.path)