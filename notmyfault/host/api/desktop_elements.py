from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DesktopElementFailure(Exception):
    code: str
    message: str


class NativeDesktopElements:
    async def capture(self, delay_value: Any) -> dict[str, Any]:
        try:
            delay = float(delay_value or 3)
        except (TypeError, ValueError):
            delay = 3.0
        delay = min(max(delay, 1.0), 10.0)
        await asyncio.sleep(delay)
        from notmyfault.native.uia import (
            DesktopElementError,
            capture_element_under_cursor,
        )

        try:
            selector = await asyncio.to_thread(capture_element_under_cursor)
        except DesktopElementError as error:
            raise DesktopElementFailure(error.code, str(error)) from error
        return {"ok": True, "selector": selector}

    async def check(self, selector: Any) -> dict[str, Any]:
        from notmyfault.native.uia import DesktopElementError, check_selector

        try:
            return await asyncio.to_thread(check_selector, selector)
        except DesktopElementError as error:
            raise DesktopElementFailure(error.code, str(error)) from error
