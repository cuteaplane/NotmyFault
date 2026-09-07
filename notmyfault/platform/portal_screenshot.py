"""通过 XDG Desktop Portal 在 Wayland 下截图"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from contextlib import suppress
from pathlib import Path
from urllib.parse import unquote, urlparse

from dbus_next import Message, MessageType, Variant
from dbus_next.aio import MessageBus


async def _take_screenshot(output_path: str, interactive: bool, timeout: float = 120) -> None:
    bus = await MessageBus().connect()
    request_path = ""
    completed = False
    try:
        portal_name = "org.freedesktop.portal.Desktop"
        portal_path = "/org/freedesktop/portal/desktop"
        token = "nmf_" + uuid.uuid4().hex
        sender = bus.unique_name.removeprefix(":").replace(".", "_")
        request_path = f"{portal_path}/request/{sender}/{token}"
        response_future = asyncio.get_running_loop().create_future()
        match_rule = (
            "type='signal',"
            "sender='org.freedesktop.portal.Desktop',"
            "interface='org.freedesktop.portal.Request',"
            "member='Response',"
            f"path='{request_path}'"
        )

        def on_message(message: Message) -> bool:
            if (
                message.message_type == MessageType.SIGNAL
                and message.path == request_path
                and message.interface == "org.freedesktop.portal.Request"
                and message.member == "Response"
                and not response_future.done()
            ):
                response_future.set_result(tuple(message.body))
                return True
            return False

        bus.add_message_handler(on_message)
        match_reply = await bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[match_rule],
            )
        )
        if match_reply.message_type == MessageType.ERROR:
            raise RuntimeError("无法监听截图 Portal 响应")
        async with asyncio.timeout(timeout):
            reply = await bus.call(Message(
                destination=portal_name,
                path=portal_path,
                interface="org.freedesktop.portal.Screenshot",
                member="Screenshot",
                signature="sa{sv}",
                body=["", {"handle_token": Variant("s", token),
                           "interactive": Variant("b", interactive),
                           "modal": Variant("b", False)}],
            ))
            if reply.message_type == MessageType.ERROR:
                raise RuntimeError("截图 Portal 调用失败: " + " ".join(map(str, reply.body)))
            if reply.body[0] != request_path:
                request_path = reply.body[0]
                raise RuntimeError("截图 Portal 返回了不匹配的请求路径")
            response, results = await response_future
        completed = True
        if response != 0:
            raise RuntimeError(f"截图请求被取消或拒绝（portal code={response}）")
        uri_variant = results.get("uri")
        uri = uri_variant.value if uri_variant else ""
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise RuntimeError("截图 Portal 未返回本地文件")
        source_path = Path(unquote(parsed.path))
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination)
    finally:
        if request_path and not completed:
            with suppress(Exception):
                await asyncio.wait_for(bus.call(Message(
                    destination="org.freedesktop.portal.Desktop",
                    path=request_path,
                    interface="org.freedesktop.portal.Request",
                    member="Close",
                )), timeout=2)
        bus.disconnect()


def take_screenshot(output_path: str, interactive: bool = False) -> None:
    asyncio.run(_take_screenshot(output_path, interactive))
