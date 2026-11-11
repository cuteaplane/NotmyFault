from __future__ import annotations

from notmyfault.platform.errors import BackendInvalidError

MODIFIERS = frozenset({"ctrl", "shift", "alt", "super"})
SPECIAL_KEYS = frozenset({
    "enter", "return", "tab", "space", "backspace", "delete", "escape", "esc",
    "home", "end", "pgup", "pgdn", "up", "down", "left", "right", "capslock",
    "insert", "minus", "equal", "comma", "dot", "slash", "semicolon",
    "apostrophe", "grave", "backslash", "leftbrace", "rightbrace",
    *{f"f{index}" for index in range(1, 25)},
})
_PUNCTUATION = dict(zip("-=,./;'`\\[]", (
    "minus", "equal", "comma", "dot", "slash", "semicolon", "apostrophe",
    "grave", "backslash", "leftbrace", "rightbrace",
)))


def parse_hotkey(keys: list[str]) -> list[str]:
    if not isinstance(keys, list) or not keys:
        raise BackendInvalidError("热键必须是非空按键列表")
    parts = []
    main_key_seen = False
    for token in keys:
        if not isinstance(token, str) or not token.strip():
            raise BackendInvalidError("按键名称必须是非空文本")
        key = token.strip().lower()
        key = {
            "win": "super", "control": "ctrl", "return": "enter", "esc": "escape",
            "del": "delete", "ins": "insert", "pageup": "pgup", "pagedown": "pgdn",
            "page_up": "pgup", "page_down": "pgdn",
        }.get(key, _PUNCTUATION.get(key, key))
        if key in MODIFIERS:
            if main_key_seen:
                raise BackendInvalidError("组合键中只允许修饰键在前")
        else:
            if key not in SPECIAL_KEYS and not (len(key) == 1 and key in "abcdefghijklmnopqrstuvwxyz0123456789"):
                raise BackendInvalidError(f"无法识别的按键: {token!r}")
            if main_key_seen:
                raise BackendInvalidError("组合键只能包含一个主键")
            main_key_seen = True
        parts.append(key)
    if not main_key_seen:
        raise BackendInvalidError("组合键必须包含一个主键")
    return parts
