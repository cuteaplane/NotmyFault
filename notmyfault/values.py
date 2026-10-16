from __future__ import annotations

import tempfile
import threading
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4


BLOB_THRESHOLD = 64 * 1024
_allowed_secrets = ContextVar("allowed_secrets", default=())


def _readonly(*args, **kwargs):
    raise TypeError("运行数据只读，需要修改时请复制")


class FrozenDict(dict):
    __slots__ = ("_secret_ids", "__weakref__")
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _readonly
    __setattr__ = __delattr__ = _readonly

    def __init__(self, values=(), *, blobs=True):
        super().__init__((key, hold_value(value, blobs=blobs)) for key, value in dict(values).items())
        object.__setattr__(self, "_secret_ids", _collect_secret_ids(self.values()))

    def __deepcopy__(self, memo):
        return self


class FrozenList(list):
    __slots__ = ("_secret_ids", "__weakref__")
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _readonly
    __setattr__ = __delattr__ = _readonly

    def __init__(self, values=(), *, blobs=True):
        super().__init__(hold_value(value, blobs=blobs) for value in values)
        object.__setattr__(self, "_secret_ids", _collect_secret_ids(self))

    def __deepcopy__(self, memo):
        return self


class Secret(str):
    __slots__ = ("_value", "_secret_ids")

    def __new__(cls, value):
        result = str.__new__(cls, "***")
        object.__setattr__(result, "_value", hold_value(value))
        object.__setattr__(result, "_secret_ids", frozenset({id(result)}) | _secret_ids(result._value))
        return result

    __setattr__ = __delattr__ = _readonly

    def __str__(self):
        return "***"

    def __repr__(self):
        return "Secret(***)"

    def __deepcopy__(self, memo):
        return self

    def __eq__(self, other):
        return self._value == secret_value(other)

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = None

    def reveal(self):
        if not any(id(self) in group for group in _allowed_secrets.get()):
            raise PermissionError("只能读取传给当前动作参数的敏感值")
        return self._value


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class Blob:
    _value: str | bytes = field(repr=False)
    id: str = field(default_factory=lambda: uuid4().hex, init=False)
    size: int = field(init=False)
    type: str = field(init=False)
    _path: Path | None = field(default=None, init=False, repr=False)
    _path_lock: object = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self):
        if not isinstance(self._value, (str, bytes)):
            raise TypeError("Blob 只接受文本或 bytes")
        object.__setattr__(self, "type", "text" if isinstance(self._value, str) else "bytes")
        object.__setattr__(self, "size", len(self._value.encode("utf-8")) if self.type == "text" else len(self._value))

    def __repr__(self):
        return f"Blob(type={self.type!r}, size={self.size})"

    def __deepcopy__(self, memo):
        return self

    def __bytes__(self):
        return self._value.encode("utf-8") if self.type == "text" else self._value

    def text(self, encoding="utf-8"):
        return self._value if self.type == "text" else self._value.decode(encoding)

    def as_path(self):
        with self._path_lock:
            if self._path is None:
                with tempfile.NamedTemporaryFile(prefix="notmyfault-blob-", delete=False) as file:
                    file.write(bytes(self))
                    path = Path(file.name)
                object.__setattr__(self, "_path", path)
                weakref.finalize(self, path.unlink, missing_ok=True)
            return self._path

    def descriptor(self):
        return {"blob_id": self.id, "size": self.size, "type": self.type}


def hold_value(value, *, blobs=True):
    if isinstance(value, (Secret, Blob, FrozenDict, FrozenList)):
        return value
    if blobs and isinstance(value, (str, bytes)) and len(value) >= BLOB_THRESHOLD:
        return Blob(value)
    if isinstance(value, dict):
        return FrozenDict(value, blobs=blobs)
    if isinstance(value, list):
        return FrozenList(value, blobs=blobs)
    return value


def secret_value(value):
    return value._value if isinstance(value, Secret) else value


def protect(value):
    return value if isinstance(value, Secret) else Secret(hold_value(value))


def contains_secret(value):
    if isinstance(value, Secret):
        return True
    if isinstance(value, (FrozenDict, FrozenList)):
        return bool(value._secret_ids)
    if isinstance(value, dict):
        return any(contains_secret(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_secret(item) for item in value)
    return False


def _secret_ids(value):
    return value._secret_ids if isinstance(value, (Secret, FrozenDict, FrozenList)) else frozenset()


def _collect_secret_ids(values):
    result = set()
    for value in values:
        result.update(_secret_ids(value))
    return frozenset(result)


def plain_value(value):
    if isinstance(value, Secret):
        return plain_value(value._value)
    if isinstance(value, Blob):
        return value._value
    if isinstance(value, dict):
        return {key: plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_value(item) for item in value]
    return value


@contextmanager
def allow_secrets(params):
    allowed = []

    def visit(value):
        if isinstance(value, (Secret, FrozenDict, FrozenList)):
            if value._secret_ids:
                allowed.append(value._secret_ids)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(params)
    token = _allowed_secrets.set(tuple(allowed))
    try:
        yield
    finally:
        _allowed_secrets.reset(token)


def action_values(value):
    if isinstance(value, Secret):
        return action_values(value.reveal())
    if isinstance(value, Blob):
        return value.text() if value.type == "text" else bytes(value)
    if isinstance(value, dict):
        return {key: action_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [action_values(item) for item in value]
    return value
