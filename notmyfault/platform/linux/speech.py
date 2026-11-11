import re

from notmyfault.platform._run import default_runner, failed_detail
from notmyfault.platform.errors import BackendFailedError, BackendInvalidError, BackendMissingError


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
        executable = self._runner.which("spd-say")
        if not executable:
            raise BackendMissingError("语音播报需要 spd-say")
        command = [executable, "--wait", "--rate", str(rate * 10), "--volume", str(volume * 2 - 100)]
        if voice:
            result = self._runner.run([executable, "--list-synthesis-voices"], timeout=10)
            if result.returncode:
                raise BackendFailedError(failed_detail(result))
            selected = None
            for line in result.stdout.splitlines():
                fields = re.split(r"\s{2,}|\t+", line.strip())
                if len(fields) < 2:
                    fields = line.split()
                if len(fields) < 2 or fields[0].casefold() == "name":
                    continue
                name, language = fields[:2]
                if voice.casefold() in name.casefold():
                    selected = ["--synthesis-voice", name]
                    break
                if voice.casefold() == language.replace("_", "-").casefold():
                    selected = ["--language", language.replace("_", "-")]
                    break
            if selected is None:
                raise BackendInvalidError(f"未找到语音: {voice}")
            command.extend(selected)
        result = self._runner.run([*command, "--", text], timeout=120)
        if result.returncode:
            raise BackendFailedError(failed_detail(result))
