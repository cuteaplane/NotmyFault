from __future__ import annotations

import functools

import notmyfault.platform
from notmyfault.platform import capabilities as ids
from notmyfault.platform._run import default_runner
from notmyfault.platform.errors import BackendError, PlatformServiceError
from notmyfault.platform.models import CapabilityStatus

_UNSUPPORTED = "当前平台暂未提供此能力"


def _op(capability, handle_methods=()):
    def wrap(fn):
        @functools.wraps(fn)
        def call(self, *args, **kwargs):
            result = self._platform._invoke(self.name, fn.__name__, capability, args, kwargs)
            return _BackendHandle(result, capability, handle_methods) if handle_methods else result

        call.capability = capability
        return call

    return wrap


def _backend_call(capability, call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except PlatformServiceError:
        raise
    except BackendError as error:
        raise PlatformServiceError(capability, error.kind, str(error)) from error
    except ImportError as error:
        raise PlatformServiceError(capability, "unavailable", str(error)) from error
    except (TypeError, ValueError) as error:
        raise PlatformServiceError(capability, "invalid", str(error)) from error
    except PermissionError as error:
        raise PlatformServiceError(capability, "permission_denied", str(error)) from error
    except Exception as error:
        raise PlatformServiceError(capability, "failed", str(error)) from error


class _BackendHandle:
    def __init__(self, backend, capability, methods):
        self._backend = backend
        self._capability = capability
        self._methods = methods

    def __getattr__(self, name):
        if name not in self._methods:
            raise AttributeError(name)
        return functools.partial(_backend_call, self._capability, getattr(self._backend, name))


class _Domain:
    name: str

    def __init__(self, platform: "Platform") -> None:
        self._platform = platform


class Clipboard(_Domain):
    name = "clipboard"

    @_op(ids.CLIPBOARD_READ)
    def read(self, max_chars: int | None = None) -> str | None:
        """读取剪贴板文本，剪贴板里没有文本时返回 None"""

    @_op(ids.CLIPBOARD_WRITE)
    def write(self, text: str) -> None:
        """把文本写入剪贴板"""

    @_op(ids.CLIPBOARD_WRITE)
    def clear(self) -> None:
        """清空剪贴板"""


class Keyboard(_Domain):
    name = "keyboard"

    @_op(ids.INPUT_SEND)
    def type_text(self, text: str) -> None:
        """向当前焦点窗口逐字输入文本"""

    @_op(ids.INPUT_SEND)
    def send_hotkey(self, keys: list[str]) -> None:
        """按下一组组合键，keys 如 ["ctrl", "c"]"""


class Audio(_Domain):
    name = "audio"

    @_op(ids.AUDIO_CONTROL)
    def set_volume(self, percent: int) -> None:
        """设置默认播放设备音量，percent 为 0 到 100"""

    @_op(ids.AUDIO_CONTROL)
    def set_muted(self, muted: bool) -> None:
        """静音或取消静音默认播放设备"""

    @_op(ids.AUDIO_DEVICE_QUERY)
    def devices(self) -> list[dict[str, str]]:
        """列出音频设备的 id、name 和 flow"""

    @_op(ids.AUDIO_DEVICE_QUERY)
    def default_devices(self) -> dict[str, str]:
        """返回 render 和 capture 的默认设备 id，没有默认设备的流向返回空字符串"""

    @_op(ids.AUDIO_DEVICE_QUERY)
    def set_default(self, flow: str, device_id: str) -> dict:
        """按 id 或名称切换默认设备，返回设备名称和切换结果"""



class Media(_Domain):
    name = "media"

    @_op(ids.MEDIA_CONTROL)
    def send(self, command: str) -> None:
        """向媒体播放器发送控制命令"""

class Window(_Domain):
    name = "window"

    @_op(ids.WINDOW_ENUMERATE)
    def list(self) -> list[dict]:
        """列出窗口，window_id 为不透明字符串"""

    @_op(ids.WINDOW_ENUMERATE)
    def titles(self) -> dict[str, str]:
        """返回窗口 id 到标题的映射"""

    @_op(ids.WINDOW_ENUMERATE)
    def foreground(self) -> str | None:
        """返回前台窗口 id，没有前台窗口时返回 None"""

    @_op(ids.WINDOW_ENUMERATE)
    def info(self, window_id: str) -> dict | None:
        """读取窗口信息，窗口已关闭时返回 None"""

    @_op(ids.WINDOW_ENUMERATE)
    def monitors(self) -> list[dict]:
        """列出显示器及其工作区域"""

    @_op(ids.WINDOW_CONTROL)
    def show(self, window_id: str, state: str) -> None:
        """设置窗口显示状态"""

    @_op(ids.WINDOW_CONTROL)
    def activate(self, window_id: str) -> None:
        """请求将窗口切换到前台"""

    @_op(ids.WINDOW_CONTROL)
    def move(self, window_id: str, rect: dict) -> None:
        """移动窗口并设置尺寸"""

    @_op(ids.WINDOW_CONTROL)
    def set_opacity(self, window_id: str, percent: int) -> None:
        """设置窗口透明度，percent 为 0 到 100"""

    @_op(ids.WINDOW_CONTROL)
    def close(self, window_id: str) -> None:
        """请求窗口关闭"""

    @_op(ids.WINDOW_PIN)
    def set_pinned(self, action: str, target: str, title: str = "") -> dict:
        """置顶、取消置顶或切换窗口，返回 state 和 window_id"""


class Display(_Domain):
    name = "display"

    @_op(ids.DISPLAY_BRIGHTNESS)
    def set_brightness(self, percent: int) -> dict:
        """设置并回读亮度，返回 brightness、methods 和 warnings"""

    @_op(ids.DISPLAY_POWER)
    def set_power(self, action: str) -> dict:
        """开关显示器并返回 methods 和 warnings"""


class Screen(_Domain):
    name = "screen"

    @_op(ids.SCREEN_CAPTURE)
    def capture(self, output_path: str, mode: str, fmt: str) -> str:
        """截图保存到 output_path 并返回最终路径"""

    @_op(ids.SCREEN_OCR)
    def ocr(self, image_path: str, language: str) -> str:
        """识别图片中的文字"""


class Power(_Domain):
    name = "power"

    @_op(ids.POWER_PROFILE)
    def set_profile(self, plan: str, custom_guid: str = "") -> str:
        """切换电源计划并返回平台档位名或 GUID"""

    @_op(ids.POWER_STATE)
    def battery(self) -> dict:
        """返回 on_battery 和 percent，无电池时 percent 为 None"""

    @_op(ids.POWER_STATE, ("start", "consume_resume", "stop"))
    def resume_monitor(self):
        """返回提供 start、consume_resume 和 stop 的恢复监听对象"""

    @_op(ids.POWER_SYSTEM)
    def system(self, action: str, force: bool = False) -> None:
        """执行 shutdown、restart、sleep、hibernate 或 logoff"""


class Session(_Domain):
    name = "session"

    @_op(ids.SESSION_LOCK)
    def lock(self) -> None:
        """锁定当前会话"""

    @_op(ids.SESSION_STATE)
    def is_locked(self) -> bool:
        """查询当前会话是否锁定，查询失败抛错"""

    @_op(ids.IDLE_QUERY)
    def idle_seconds(self) -> float:
        """返回自最后输入起的空闲秒数"""

    @_op(ids.SESSION_STATE)
    def session_id(self) -> str | None:
        """返回当前会话 id，无会话时返回 None"""


class Wifi(_Domain):
    name = "wifi"

    @_op(ids.WIFI_QUERY)
    def current_ssid(self, interface: str | None = None) -> str | None:
        """查询当前 SSID，未连接时返回 None"""

    @_op(ids.WIFI_CONTROL)
    def connect(self, ssid: str, password: str | None = None, interface=None) -> dict:
        """连接网络并返回 ssid、connected 和 method"""

    @_op(ids.WIFI_CONTROL)
    def disconnect(self, interface=None) -> dict:
        """断开网络并返回 ssid、connected 和 method"""


class Bluetooth(_Domain):
    name = "bluetooth"

    @_op(ids.BLUETOOTH_CONTROL)
    def state(self) -> str:
        """返回 on、off 或 unavailable"""

    @_op(ids.BLUETOOTH_CONTROL)
    def set(self, enabled: bool) -> dict:
        """设置蓝牙状态并返回回读结果"""


class Speech(_Domain):
    name = "speech"

    @_op(ids.TTS)
    def speak(self, text, rate: int = 0, volume: int = 100, voice: str = "") -> None:
        """播报文本，rate 为 -10 到 10，volume 为 0 到 100"""


class Hotkey(_Domain):
    name = "hotkey"

    @_op(ids.INPUT_GLOBAL_HOTKEY, ("poll", "close"))
    def register(self, keys: list[str]):
        """返回提供 poll 和 close 的热键监听对象"""

    @_op(ids.INPUT_GLOBAL_HOTKEY)
    def capture(self, timeout: float) -> list[str]:
        """捕获统一键名列表，取消或超时返回空列表"""

class Desktop(_Domain):
    name = "desktop"

    @_op(ids.FILE_LOCK)
    def file_in_use(self, path: str) -> bool:
        """检查文件读取及占用状态，Linux 扫描编辑进程打开的文档"""

    @_op(ids.DESKTOP_WALLPAPER)
    def set_wallpaper(self, path, style) -> None:
        """设置壁纸，style 为 fill、fit、stretch、center、tile 或 span"""

    @_op(ids.DESKTOP_SHORTCUT)
    def create_shortcut(self, target, location, name, arguments="", icon="") -> dict:
        """创建快捷方式并返回 shortcut_path"""


_DOMAINS = (
    Clipboard, Keyboard, Audio, Media, Window, Display, Screen, Power, Session,
    Wifi, Bluetooth, Speech, Desktop, Hotkey,
)


class Platform:
    def __init__(self) -> None:
        self.clipboard = Clipboard(self)
        self.keyboard = Keyboard(self)
        self.audio = Audio(self)
        self.media = Media(self)
        self.window = Window(self)
        self.display = Display(self)
        self.screen = Screen(self)
        self.power = Power(self)
        self.desktop = Desktop(self)
        self.session = Session(self)
        self.wifi = Wifi(self)
        self.bluetooth = Bluetooth(self)
        self.speech = Speech(self)
        self.hotkey = Hotkey(self)

    @property
    def name(self) -> str:
        return notmyfault.platform.current()

    def capability(self, capability: str) -> CapabilityStatus:
        if capability not in ids.CAPABILITY_IDS:
            raise PlatformServiceError(capability, "invalid", f"未知能力 id: {capability!r}")
        backends = self._backends()
        provided = any(
            hasattr(backends.get(domain.name), method)
            for domain, method, cap in _operations()
            if cap == capability
        )
        if not provided:
            return CapabilityStatus(
                id=capability, available=False, backend=None,
                reason=_UNSUPPORTED, degraded=False,
            )
        value = ids.probe_capability(capability)
        return CapabilityStatus(
            id=capability,
            available=bool(value["available"]),
            backend=value.get("backend"),
            reason=value.get("reason"),
            degraded=bool(value.get("degraded")),
        )

    def _backends(self) -> dict:
        name = notmyfault.platform.current()
        if name == "linux":
            from notmyfault.platform.linux import BACKENDS
        elif name == "windows":
            from notmyfault.platform.windows import BACKENDS
        else:
            return {}
        return BACKENDS

    def _invoke(self, domain: str, method: str, capability: str, args, kwargs):
        cls = self._backends().get(domain)
        if cls is None or not hasattr(cls, method):
            raise PlatformServiceError(capability, "unsupported", _UNSUPPORTED)
        status = self.capability(capability)
        if not status.available:
            raise PlatformServiceError(
                capability, "unavailable", status.reason or "当前系统不可用",
            )
        return _backend_call(
            capability, lambda: getattr(cls(default_runner), method)(*args, **kwargs),
        )


def _operations() -> list[tuple[type, str, str]]:
    found = []
    for domain in _DOMAINS:
        for method, value in vars(domain).items():
            capability = getattr(value, "capability", None)
            if capability is not None:
                found.append((domain, method, capability))
    return found


def operations() -> list[tuple[str, str, str]]:
    return [(domain.name, method, capability) for domain, method, capability in _operations()]


platform = Platform()
