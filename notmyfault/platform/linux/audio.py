from __future__ import annotations

import json
import re

from notmyfault.platform._run import CommandRunner, default_runner, failed_detail
from notmyfault.platform.errors import (
    BackendFailedError,
    BackendInvalidError,
    BackendMissingError,
)


class AudioBackend:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def _run(self, args: list[str]) -> str:
        result = self._runner.run(args, timeout=5)
        if result.returncode != 0:
            raise BackendFailedError(failed_detail(result))
        return result.stdout

    def _wpctl(self) -> str:
        path = self._runner.which("wpctl")
        if not path:
            raise BackendMissingError("依赖缺失：音量控制需要 wpctl")
        return path

    def _volume(self, wpctl: str) -> tuple[float, bool]:
        output = self._run([wpctl, "get-volume", "@DEFAULT_AUDIO_SINK@"])
        match = re.fullmatch(r"Volume:\s*(\d+(?:\.\d+)?)\s*(\[MUTED\])?\s*", output.strip())
        if not match:
            raise BackendFailedError(f"无法解析音量回读: {output.strip()!r}")
        return float(match.group(1)), bool(match.group(2))

    def set_volume(self, percent: int) -> None:
        if type(percent) is not int or not 0 <= percent <= 100:
            raise BackendInvalidError("音量必须是 0 到 100 的整数")
        wpctl = self._wpctl()
        self._run([wpctl, "set-volume", "@DEFAULT_AUDIO_SINK@", f"{percent}%"])
        actual, _ = self._volume(wpctl)
        if abs(actual - percent / 100) > 0.01:
            raise BackendFailedError(f"音量设置未生效（期望 {percent}%，实际 {actual * 100:.0f}%）")

    def set_muted(self, muted: bool) -> None:
        if type(muted) is not bool:
            raise BackendInvalidError("静音状态必须是布尔值")
        wpctl = self._wpctl()
        self._run([wpctl, "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if muted else "0"])
        _, actual = self._volume(wpctl)
        if actual != muted:
            raise BackendFailedError("音频服务拒绝设置静音状态")

    def _query_tool(self) -> tuple[str, str]:
        for name in ("wpctl", "pactl"):
            if path := self._runner.which(name):
                return name, path
        raise BackendMissingError("依赖缺失：音频设备查询需要 wpctl 或 pactl")

    def _wpctl_snapshot(self, path: str) -> tuple[list[dict[str, str]], dict[str, str]]:
        output = self._run([path, "status"])
        devices = []
        defaults = {"render": "", "capture": ""}
        in_audio = False
        found_audio = False
        section = None
        for line in output.splitlines():
            label = line.strip().lstrip("│├└─┬ ").strip()
            if not label:
                continue
            if label in ("Sinks:", "Sources:"):
                section = "render" if label == "Sinks:" else "capture"
                continue
            if not line[:1].isspace():
                in_audio = label == "Audio"
                found_audio |= in_audio
                section = None
                continue
            if label.endswith(":"):
                section = None
                continue
            if not in_audio or section is None:
                continue
            match = re.fullmatch(r"(\*\s*)?(\d+)\.\s+(.+)", label)
            if not match:
                continue
            device_id = match.group(2)
            name = re.sub(r"\s*\[vol:.*\]\s*$", "", match.group(3)).strip()
            devices.append({"id": device_id, "name": name, "flow": section})
            if match.group(1):
                defaults[section] = device_id
        if not found_audio:
            raise BackendFailedError("wpctl 未返回 Audio 设备列表")
        return devices, defaults

    def _pactl_devices(self, path: str) -> list[dict[str, str]]:
        devices = []
        for flow, kind in (("render", "sinks"), ("capture", "sources")):
            output = self._run([path, "--format=json", "list", kind])
            try:
                entries = json.loads(output)
                if not isinstance(entries, list):
                    raise ValueError("设备列表不是数组")
                for entry in entries:
                    device_id = entry["name"]
                    if not isinstance(device_id, str) or not device_id:
                        raise ValueError("设备名称为空")
                    name = entry.get("description") or device_id
                    devices.append({"id": device_id, "name": str(name), "flow": flow})
            except (ValueError, KeyError, TypeError) as error:
                raise BackendFailedError("无法解析 pactl 音频设备列表") from error
        return devices

    def _pactl_defaults(self, path: str) -> dict[str, str]:
        return {
            flow: self._run([path, verb]).strip()
            for flow, verb in (("render", "get-default-sink"), ("capture", "get-default-source"))
        }

    def devices(self) -> list[dict[str, str]]:
        name, path = self._query_tool()
        if name == "wpctl":
            return self._wpctl_snapshot(path)[0]
        return self._pactl_devices(path)

    def default_devices(self) -> dict[str, str]:
        name, path = self._query_tool()
        if name == "wpctl":
            return self._wpctl_snapshot(path)[1]
        return self._pactl_defaults(path)

    def set_default(self, flow: str, device_id: str) -> dict:
        flow = str(flow or "").strip().lower()
        if flow not in ("render", "capture"):
            raise BackendInvalidError(f"无效的音频流向: {flow!r}（可选: render/capture）")
        target = str(device_id or "").strip()
        if not target:
            raise BackendInvalidError("必须填写目标音频设备 id 或名称")
        method, path = self._query_tool()
        if method == "wpctl":
            devices, defaults = self._wpctl_snapshot(path)
        else:
            devices = self._pactl_devices(path)
            defaults = self._pactl_defaults(path)
        candidates = [device for device in devices if device["flow"] == flow]
        matches = [device for device in candidates if device["id"] == target]
        if not matches:
            matches = [device for device in candidates if target.casefold() in device["name"].casefold()]
        if len(matches) != 1:
            raise BackendInvalidError(f"目标设备 {target!r} 匹配到 {len(matches)} 个{flow}设备，需要唯一设备 id 或名称")
        selected = matches[0]
        previous = defaults[flow]
        changed = previous != selected["id"]
        if changed:
            if method == "wpctl":
                self._run([path, "set-default", selected["id"]])
                actual = self._wpctl_snapshot(path)[1][flow]
            else:
                verb = "set-default-sink" if flow == "render" else "set-default-source"
                self._run([path, verb, selected["id"]])
                actual = self._pactl_defaults(path)[flow]
            if actual != selected["id"]:
                raise BackendFailedError(f"请求默认设备 {selected['id']!r}，回读为 {actual!r}")
        return {
            "device_id": selected["id"],
            "device_name": selected["name"],
            "previous_device_id": previous,
            "changed": changed,
            "method": method,
        }
