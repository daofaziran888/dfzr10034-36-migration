"""差异记录类型映射：为差异采集流水线提供可区分类型的值表示。

旧版差异采集流水线只存文本：写入时一律 str()，读取时文本零 "0"、
数值零 0 与缺列互相冲突。本模块定义带类型标签的值映射（类型映射），
作为迁移、采集、确认与回放共同的交换格式：

- 采集侧按生产者的真实类型打标：字符串 "7" 永远是 text，整数 7 永远是
  int，缺列是 missing，显式空值是 null，互不混淆；
- 存量迁移只保留原文并标记 legacy，绝不凭字符外观猜测类型；
- 回放只依赖落库的类型映射与原文，与读取路径是否回滚无关。
"""
from __future__ import annotations

from typing import Any

# 类型标签
TYPE_TEXT = "text"        # 文本（含文本零 "0"、前导零 "0007" 等）
TYPE_INT = "int"          # 整数
TYPE_FLOAT = "float"      # 浮点数
TYPE_BOOL = "bool"        # 布尔
TYPE_NULL = "null"        # 显式空值：列存在但值为空
TYPE_MISSING = "missing"  # 缺列：记录里根本没有这一列
TYPE_LEGACY = "legacy"    # 存量迁移值：只保留原文，不猜类型

# 歧义类型：旧读法下无法确定真实类型，只能保留原文
AMBIGUOUS_TYPES = {TYPE_LEGACY}


def typed_value(value: Any, *, missing: bool = False) -> dict[str, Any]:
    """把采集端送来的值包装成带类型的值映射。

    类型只取生产侧的真实类型，绝不对字符串内容做外观推断：
    字符串 "7" 是 text，整数 7 是 int，二者落库后依然可分。
    """
    if missing:
        return {"type": TYPE_MISSING, "text": "", "present": False}
    if value is None:
        return {"type": TYPE_NULL, "text": "", "present": True}
    if isinstance(value, bool):
        return {"type": TYPE_BOOL, "text": "true" if value else "false", "present": True}
    if isinstance(value, int):
        return {"type": TYPE_INT, "text": str(value), "present": True}
    if isinstance(value, float):
        return {"type": TYPE_FLOAT, "text": repr(value), "present": True}
    return {"type": TYPE_TEXT, "text": str(value), "present": True}


def legacy_value(text: Any) -> dict[str, Any]:
    """存量记录的值映射：原文原样保留，类型标记 legacy（不猜）。"""
    return {"type": TYPE_LEGACY, "text": "" if text is None else str(text), "present": True}


def looks_ambiguous(text: Any) -> bool:
    """判断原文在旧读法下是否有歧义（仅用于标记与统计，不用于转换）。

    数字外观的字符串（"0"、"0007"、"7.0"）、空串、布尔/空值字样在旧
    读法里都可能被错读成别的类型；迁移遇到这些值时保留原始字段和值。
    """
    s = "" if text is None else str(text)
    if s == "":
        return True
    try:
        float(s)
        return True
    except ValueError:
        pass
    return s.casefold() in {"true", "false", "null", "none"}


def is_ambiguous(typed: dict[str, Any]) -> bool:
    """值映射是否带歧义（legacy 一律视为歧义，只能保留原文）。"""
    return typed.get("type") in AMBIGUOUS_TYPES
