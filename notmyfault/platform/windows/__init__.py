from __future__ import annotations

from notmyfault.platform.windows.audio import AudioBackend
from notmyfault.platform.windows.clipboard import ClipboardBackend
from notmyfault.platform.windows.desktop import DesktopBackend
from notmyfault.platform.windows.display import DisplayBackend
from notmyfault.platform.windows.keyboard import KeyboardBackend
from notmyfault.platform.windows.media import MediaBackend
from notmyfault.platform.windows.power import PowerBackend
from notmyfault.platform.windows.screen import ScreenBackend
from notmyfault.platform.windows.window import WindowBackend
from notmyfault.platform.windows.session import SessionBackend
from notmyfault.platform.windows.wifi import WifiBackend
from notmyfault.platform.windows.bluetooth import BluetoothBackend
from notmyfault.platform.windows.speech import SpeechBackend
from notmyfault.platform.windows.hotkey import HotkeyBackend

BACKENDS = {
    "clipboard": ClipboardBackend,
    "keyboard": KeyboardBackend,
    "audio": AudioBackend,
    "media": MediaBackend,
    "window": WindowBackend,
    "display": DisplayBackend,
    "screen": ScreenBackend,
    "power": PowerBackend,
    "desktop": DesktopBackend,
    "session": SessionBackend,
    "wifi": WifiBackend,
    "bluetooth": BluetoothBackend,
    "speech": SpeechBackend,
    "hotkey": HotkeyBackend,
}
