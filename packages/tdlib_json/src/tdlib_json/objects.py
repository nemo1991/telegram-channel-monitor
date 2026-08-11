from __future__ import annotations

from typing import Any


class TDLibObject(dict):
    """TDLib 响应对象的轻量 dict 包装。

    设计:
    - 继承 `dict`,与 raw JSON 双向兼容(可直接再序列化);
    - `__getattr__` 让 `obj.field` 等价 `obj["field"]`,缺失返回 None —
      业务代码(`TdlibTelegramClient` / `ChannelsApi`)沿用 `resp.id` /
      `resp.title` 的属性写法;
    - `type_name` 把 `@type` 首字母大写(PascalCase),如 `messagePhoto` →
      `MessagePhoto`,供内容类型派发使用。

    注意:`__getattr__` 只在常规属性查找失败时触发;dunder 走类型级查找,
    不受影响。
    """

    def __getattr__(self, name: str) -> Any:
        return self.get(name)

    @property
    def type_name(self) -> str:
        """`@type` 首字母大写;缺失返回空串。"""
        type_str = self.get("@type", "")
        if not type_str:
            return ""
        return type_str[:1].upper() + type_str[1:]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TDLibObject:
        """递归包装嵌套 dict / list 为 `TDLibObject`。"""
        return cls(_wrap(data))


def _wrap(value: Any) -> Any:
    """递归:dict → 新 dict(值递归包装),list → 新 list,其它原样返回。"""
    if isinstance(value, dict):
        return {key: _wrap(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_wrap(item) for item in value]
    return value
