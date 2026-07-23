def run(action_info, params):
    text = params.get("text", "").strip()
    rate = params.get("rate", 0)
    volume = params.get("volume", 100)
    voice_name = str(params.get("voice", "")).strip()

    if not text:
        print("[Action:text_to_speech] 没有文本可播报")
        return

    print(f"[Action:text_to_speech] 播报: {text[:60]}...")

    try:
        import win32com.client
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
            speaker.Rate = max(-10, min(10, int(rate)))
        if volume != 100:
            speaker.Volume = max(0, min(100, int(volume)))

        speaker.Speak(text)
        print(f"[Action:text_to_speech] 播报完成")

    except ImportError:
        print("[Action:text_to_speech] 需要 pywin32 库")
    except Exception as e:
        print(f"[Action:text_to_speech] 播报失败: {e}")
