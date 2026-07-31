import json
import os
import subprocess
import sys

_SPEAK_TIMEOUT = 60


# SAPI + 音频驱动属于不可信的原生代码：曾出现播报完成后引擎进程整体
# 堆损坏静默崩溃（0xc0000374）。把 Windows TTS 放进独立子进程，SAPI 的
# 任何原生崩溃只影响该子进程，不会带走引擎/API/托盘。
_TTS_HELPER = r"""
import json
import sys
import pythoncom
import win32com.client

# 关键：必须用二进制读 stdin 再按 UTF-8 解码。Windows 中文环境文本模式
# stdin 是 GBK，直接 sys.stdin.read() 会把 UTF-8 中文读成乱码，SAPI 就会
# 念出"ting-shen"这类音。
payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
pythoncom.CoInitialize()
try:
    speaker = win32com.client.Dispatch("SAPI.SpVoice")
    voice_name = payload.get("voice") or ""
    if voice_name:
        for voice in speaker.GetVoices():
            try:
                name = str(voice.GetAttribute("Name"))
            except Exception:
                name = ""
            if voice_name.casefold() in name.casefold():
                speaker.Voice = voice
                break
    rate = int(payload.get("rate", 0) or 0)
    volume = int(payload.get("volume", 100) or 100)
    if rate != 0:
        speaker.Rate = max(-10, min(10, rate))
    if volume != 100:
        speaker.Volume = max(0, min(100, volume))
    # 子进程专职本次播报：同步 Speak 即可，卡死由父进程超时兜底
    speaker.Speak(payload.get("text", ""))
    speaker = None
finally:
    pythoncom.CoUninitialize()
"""


def _speak_windows(text: str, rate, volume, voice_name: str) -> None:
    payload = json.dumps({
        "text": text,
        "rate": rate,
        "volume": volume,
        "voice": voice_name,
    }, ensure_ascii=False).encode("utf-8")
    try:
        result = subprocess.run(
            [sys.executable, "-c", _TTS_HELPER],
            input=payload,
            capture_output=True,
            timeout=_SPEAK_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"语音播报超时（{_SPEAK_TIMEOUT}s）") from None
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"语音播报失败: {stderr[:200] or 'SAPI 错误'}")


def run(action_info, params):
    text = params.get("text", "").strip()
    rate = params.get("rate", 0)
    volume = params.get("volume", 100)
    voice_name = str(params.get("voice", "")).strip()

    if not text:
        raise ValueError("没有文本可播报")

    print(f"[Action:text_to_speech] 播报: {text[:60]}...")

    if os.name != "nt":
        command = ["spd-say", "--wait"]
        try:
            if rate:
                command.extend(["--rate", str(max(-100, min(100, int(rate) * 10)))])
            if volume != 100:
                command.extend(["--volume", str(max(-100, min(100, int(volume) - 100)))])
        except (TypeError, ValueError):
            raise ValueError(f"rate/volume 必须是数字，实际: rate={rate!r} volume={volume!r}") from None
        if voice_name:
            command.extend(["--language", voice_name])
        command.append(text)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "spd-say 播报失败")
        print("[Action:text_to_speech] 播报完成")
        return

    try:
        rate = int(rate)
        volume = int(volume)
    except (TypeError, ValueError):
        raise ValueError(f"rate/volume 必须是数字，实际: rate={rate!r} volume={volume!r}") from None

    _speak_windows(text, rate, volume, voice_name)
    print("[Action:text_to_speech] 播报完成")
