"""路径锚点：包根/项目根相对源码位置解析，重构搬迁后仍然成立"""

import os
from pathlib import Path

import pytest

import notmyfault
from notmyfault.host import alert
from notmyfault.application_paths import ApplicationPaths
from notmyfault.platform.platform_support import get_config_dir
from notmyfault.security import security as security_mod
from notmyfault.security import signing
from notmyfault.security import signing_keys


def test_package_and_project_roots_survive_subpackage_moves():
    pkg_root = security_mod._PKG_ROOT
    project_root = security_mod._PROJECT_ROOT
    # security.py 位于 notmyfault/security/ 下，两级 parents 必须分别锚到包根和项目根
    assert pkg_root == Path(notmyfault.__file__).resolve().parent
    assert project_root == pkg_root.parent
    assert (pkg_root / "config.py").is_file()
    assert (pkg_root / "core" / "engine.py").is_file()
    assert (project_root / "NOTMYFAULT.pyw").is_file()


def test_signing_uses_project_private_directory():
    assert signing.PRIVATE_KEY_FILE == (
        security_mod._PROJECT_ROOT / ".private" / "signing_private_key.pem"
    )


def test_application_paths_keep_existing_physical_locations():
    paths = ApplicationPaths.default()
    config_dir = Path(get_config_dir())
    assert paths.config_file == config_dir / "config.json"
    assert paths.rules_file == config_dir / "rules.json"
    assert paths.config_secret_file == config_dir / ".config_secret"
    assert paths.api_token_file == config_dir / ".api_token"
    assert paths.ai_api_key_file == config_dir / ".ai_api_key"
    assert paths.logs_dir == config_dir / "logs"
    assert paths.user_plugins_dir == config_dir / "plugins"
    assert paths.plugin_private_key_file == signing.PRIVATE_KEY_FILE
    assert paths.plugin_public_key_file == Path(signing_keys._USER_PUBLIC_KEY_PATH)


def test_source_dashboard_entry_is_resolved_from_project_root():
    expected = os.path.join(str(security_mod._PROJECT_ROOT), "dashboard.pyw")
    dashboard_path = alert._dashboard_pyw_path()
    assert os.path.realpath(dashboard_path) == os.path.realpath(expected)
    assert os.path.exists(dashboard_path)


@pytest.mark.skipif(os.name != "nt", reason="源码托盘当前仅导入 Windows 模块")
def test_source_tray_assets_are_resolved_from_project_root():
    from notmyfault.host import tray

    assert os.path.realpath(tray.PROJECT_ROOT) == os.path.realpath(
        str(security_mod._PROJECT_ROOT)
    )
    assert os.path.realpath(tray.ICON_PATH) == os.path.realpath(
        os.path.join(str(security_mod._PROJECT_ROOT), "logo.ico")
    )
    assert os.path.exists(tray.ICON_PATH)
