import json
import sys

from notmyfault.platform._run import default_runner, failed_detail
from notmyfault.platform.errors import BackendFailedError, BackendInvalidError


# SAPI 曾在播报完成后因堆损坏导致宿主进程崩溃，错误码 0xc0000374。
_TTS_HELPER = r'''
import json
import locale
import sys
import pythoncom
import win32com.client

payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
pythoncom.CoInitialize()
try:
    speaker = win32com.client.Dispatch("SAPI.SpVoice")
    requested = payload["voice"].casefold()
    if requested:
        selected = None
        for voice in speaker.GetVoices():
            try:
                name = str(voice.GetAttribute("Name"))
            except Exception:
                name = ""
            try:
                languages = [locale.windows_locale.get(int(value, 16), "").replace("_", "-").casefold()
                             for value in str(voice.GetAttribute("Language")).split(";") if value]
            except (ValueError, AttributeError):
                languages = []
            if requested in name.casefold() or requested in languages:
                selected = voice
                break
        if selected is None:
            print(json.dumps({"kind": "invalid", "message": "未找到语音: " + payload["voice"]}))
            sys.exit(2)
        speaker.Voice = selected
    speaker.Rate = payload["rate"]
    speaker.Volume = payload["volume"]
    speaker.Speak(payload["text"])
    speaker = None
finally:
    pythoncom.CoUninitialize()
'''


class SpeechBackend:
    def __init__(self, runner=None):
        self._runner = runner or default_runner

    def speak(self, text, rate: int = 0, volume: int = 100, voice: str = ""):
        if type(rate) is not int or not -10 <= rate <= 10:
            raise BackendInvalidError("rate 必须是 -10..10 的整数")
        if type(volume) is not int or not 0 <= volume <= 100:
            raise BackendInvalidError("volume 必须是 0..100 的整数")
        if not isinstance(text, str) or not text.strip():
            raise BackendInvalidError("没有文本可播报")
        if not isinstance(voice, str):
            raise BackendInvalidError("voice 必须是字符串")
        result = self._runner.run(
            [sys.executable, "-c", _TTS_HELPER], timeout=60,
            input=json.dumps(dict(text=text, rate=rate, volume=volume, voice=voice), ensure_ascii=False),
            encoding="utf-8", creationflags=0x08000000,
        )
        if result.returncode:
            try:
                error = json.loads(result.stdout)
            except (ValueError, TypeError):
                error = {}
            if error.get("kind") == "invalid":
                raise BackendInvalidError(error["message"])
            raise BackendFailedError(f"语音播报失败: {failed_detail(result)}")
