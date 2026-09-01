import json
import sys

import pytest

from notmyfault.platform import capabilities
from notmyfault.platform.linux_support import session_type


@pytest.mark.linux_smoke
@pytest.mark.skipif(sys.platform != "linux", reason="仅在 Linux 桌面环境运行")
def test_linux_desktop_capability_report():
    session = session_type()
    report = capabilities.probe_capabilities()
    missing = {
        capability_id: entry["reason"]
        for capability_id, entry in report.items()
        if not entry["available"]
    }

    print(
        json.dumps(
            {"session": session, "missing": missing, "capabilities": report},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    assert session in {"x11", "wayland", "unknown"}
    assert set(report) == set(capabilities.CAPABILITY_IDS)
    assert all(
        set(entry) == {"available", "backend", "reason", "degraded"}
        for entry in report.values()
    )
