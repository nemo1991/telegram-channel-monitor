"""tdlib_json `TDLibObject` 嵌套包装回归测试。

2026-08-11:业务代码 `chat.type.supergroup_id` 报
`AttributeError: 'dict' object has no attribute 'supergroup_id'` — 根因是
`TDLibObject.from_dict` 只包顶层,嵌套 dict 是普通 dict,属性访问在嵌套层
失效(`iter_chat_history` 的 `_map_message` 也因此把内容派发成
"[service: dict]")。修复后嵌套 dict 递归包成 TDLibObject。
"""

from __future__ import annotations

import json

from tdlib_json import TDLibObject


def test_nested_dicts_are_wrapped_recursively() -> None:
    """嵌套 dict 也必须是 TDLibObject,否则 `chat.type.supergroup_id` 崩。"""
    obj = TDLibObject.from_dict(
        {
            "@type": "chat",
            "id": 42,
            "title": "频道",
            "type": {
                "@type": "chatTypeSupergroup",
                "is_channel": True,
                "supergroup_id": 123,
            },
        }
    )
    assert obj.id == 42
    assert obj.title == "频道"
    # 修复前:嵌套层是普通 dict → AttributeError
    assert obj.type.supergroup_id == 123
    assert obj.type.is_channel is True
    assert obj.type.get("@type") == "chatTypeSupergroup"


def test_nested_lists_are_wrapped() -> None:
    """list 元素里的 dict 也要能属性访问(如 resp.messages / Photo.sizes)。"""
    obj = TDLibObject.from_dict({"messages": [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}]})
    assert obj.messages[0].id == 1
    assert obj.messages[1].text == "b"


def test_type_name_on_nested_content() -> None:
    """`_map_message` 派发依赖嵌套 content 的 type_name(修复前落到 "[service: dict]")。"""
    msg = TDLibObject.from_dict(
        {"id": 1, "content": {"@type": "messageText", "text": {"text": "hi"}}}
    )
    assert msg.content.type_name == "MessageText"
    assert msg.content.text.text == "hi"


def test_missing_attribute_returns_none() -> None:
    """缺失字段走 `__getattr__` → None,不抛(业务代码大量 `x.field or ...`)。"""
    obj = TDLibObject.from_dict({"a": 1})
    assert obj.a == 1
    assert obj.missing is None


def test_dict_equality_and_json_serialization_preserved() -> None:
    """TDLibObject 仍是 dict 子类:与 plain dict 内容相等、可直接 json 序列化。"""
    obj = TDLibObject.from_dict({"a": {"b": 1}})
    assert obj == {"a": {"b": 1}}
    assert json.dumps(obj) == '{"a": {"b": 1}}'
