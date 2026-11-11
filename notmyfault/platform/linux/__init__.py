from __future__ import annotations

from notmyfault.platform.linux.desktop import DesktopBackend, desktop_environment, session_type
from notmyfault.platform.linux.audio import AudioBackend
from notmyfault.platform.linux.clipboard import ClipboardBackend
from notmyfault.platform.linux.display import DisplayBackend
from notmyfault.platform.linux.keyboard import KeyboardBackend
from notmyfault.platform.linux.media import MediaBackend
from notmyfault.platform.linux.power import PowerBackend
from notmyfault.platform.linux.screen import ScreenBackend
from notmyfault.platform.linux.window import WindowBackend
from notmyfault.platform.linux.session import SessionBackend
from notmyfault.platform.linux.wifi import WifiBackend
from notmyfault.platform.linux.bluetooth import BluetoothBackend
from notmyfault.platform.linux.speech import SpeechBackend
from notmyfault.platform.linux.hotkey import HotkeyBackend

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
