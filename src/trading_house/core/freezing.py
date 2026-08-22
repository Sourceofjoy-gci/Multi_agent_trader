"""Recursive immutability for JSON-shaped data crossing a model boundary."""

from typing import NoReturn, cast

from pydantic import JsonValue


class FrozenDict(dict[str, JsonValue]):
    """A dictionary that preserves the immutable model boundary recursively."""

    @staticmethod
    def _immutable(*_: object, **__: object) -> NoReturn:
        raise TypeError("value is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


class FrozenList(list[JsonValue]):
    """A list that preserves the immutable model boundary recursively."""

    @staticmethod
    def _immutable(*_: object, **__: object) -> NoReturn:
        raise TypeError("value is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return cast(JsonValue, FrozenDict({key: freeze_json(item) for key, item in value.items()}))
    if isinstance(value, list):
        return cast(JsonValue, FrozenList([freeze_json(item) for item in value]))
    return value
