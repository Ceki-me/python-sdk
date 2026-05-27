"""US keyboard layout mapping for CDP Input.dispatchKeyEvent."""

from __future__ import annotations

_SHIFTED_DIGITS: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
}

# (code, key, windowsVirtualKeyCode, needsShift)
_KEYMAP: dict[str, tuple[str, str, int, bool]] = {}

# a-z
for _c in range(ord("a"), ord("z") + 1):
    _ch = chr(_c)
    _KEYMAP[_ch] = (f"Key{_ch.upper()}", _ch, ord(_ch.upper()), False)

# A-Z
for _c in range(ord("A"), ord("Z") + 1):
    _ch = chr(_c)
    _KEYMAP[_ch] = (f"Key{_ch}", _ch, ord(_ch), True)

# 0-9
for _c in range(ord("0"), ord("9") + 1):
    _ch = chr(_c)
    _KEYMAP[_ch] = (f"Digit{_ch}", _ch, ord(_ch), False)

# Special keys
_KEYMAP[" "] = ("Space", " ", 32, False)
_KEYMAP["\n"] = ("Enter", "Enter", 13, False)
_KEYMAP["\t"] = ("Tab", "Tab", 9, False)
_KEYMAP["\b"] = ("Backspace", "Backspace", 8, False)

# Shifted digits: !@#$%^&*()
for _shifted, _base in _SHIFTED_DIGITS.items():
    _KEYMAP[_shifted] = (f"Digit{_base}", _shifted, ord(_base), True)

# Punctuation (unshifted)
_KEYMAP["-"] = ("Minus", "-", 189, False)
_KEYMAP["="] = ("Equal", "=", 187, False)
_KEYMAP["["] = ("BracketLeft", "[", 219, False)
_KEYMAP["]"] = ("BracketRight", "]", 221, False)
_KEYMAP["\\"] = ("Backslash", "\\", 220, False)
_KEYMAP[";"] = ("Semicolon", ";", 186, False)
_KEYMAP["'"] = ("Quote", "'", 222, False)
_KEYMAP[","] = ("Comma", ",", 188, False)
_KEYMAP["."] = ("Period", ".", 190, False)
_KEYMAP["/"] = ("Slash", "/", 191, False)
_KEYMAP["`"] = ("Backquote", "`", 192, False)

# Punctuation (shifted)
_KEYMAP["_"] = ("Minus", "_", 189, True)
_KEYMAP["+"] = ("Equal", "+", 187, True)
_KEYMAP["{"] = ("BracketLeft", "{", 219, True)
_KEYMAP["}"] = ("BracketRight", "}", 221, True)
_KEYMAP["|"] = ("Backslash", "|", 220, True)
_KEYMAP[":"] = ("Semicolon", ":", 186, True)
_KEYMAP['"'] = ("Quote", '"', 222, True)
_KEYMAP["<"] = ("Comma", "<", 188, True)
_KEYMAP[">"] = ("Period", ">", 190, True)
_KEYMAP["?"] = ("Slash", "?", 191, True)
_KEYMAP["~"] = ("Backquote", "~", 192, True)


def keymap_for_char(char: str) -> tuple[str, str, int, bool] | None:
    """Return (code, key, windowsVirtualKeyCode, needsShift) for a character.

    Returns None if the character is not in the US keyboard map (non-ASCII fallback).
    """
    return _KEYMAP.get(char)
