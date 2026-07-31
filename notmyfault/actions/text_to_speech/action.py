import os
import subprocess
import time

# SAPI SpeechRunState: SRSEDone = 1, SRSEIsSpeaking = 2
_SAPI_DONE = 1
_SPEAK_TIMEOUT = 60


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

    import win32com.client

    try:
        rate = int(rate)
        volume = int(volume)
    except (TypeError, ValueError):
        raise ValueError(f"rate/volume 必须是数字，实际: rate={rate!r} volume={volume!r}") from None

    speaker = win32com.client.Dispatch("SAPI.SpVoice")

    if voice_name:
        matched_voice = None
        for voice in speaker.GetVoices():
            try:
                name = str(voice.GetAttribute("Name"))
            except Exception:
                name = ""
            if voice_name.casefold() in name.casefold():
                matched_voice = voice
                break
        if matched_voice is None:
            print(f"[Action:text_to_speech] 未找到声音: {voice_name}，使用系统默认声音")
        else:
            speaker.Voice = matched_voice

    if rate != 0:
        speaker.Rate = max(-10, min(10, rate))
    if volume != 100:
        speaker.Volume = max(0, min(100, volume))

    # SAPI 的 ISpeechVoice 没有 SpeakAsync 方法：异步播报用 Speak 的
    # SVSFlagsAsync（=1）标志。轮询 RunningState，避免长文本/卡死的
    # TTS 永久阻塞工作流。
    speaker.Speak(text, 1)
    deadline = time.time() + _SPEAK_TIMEOUT
    while time.time() < deadline:
        try:
            if int(speaker.Status.RunningState) == _SAPI_DONE:
                break
        except Exception:
            break  # COM 状态读取失败时按完成处理，不阻塞工作流
        time.sleep(0.2)
    else:
        raise RuntimeError(f"语音播报超时（{_SPEAK_TIMEOUT}s）")
    print("[Action:text_to_speech] 播报完成")
