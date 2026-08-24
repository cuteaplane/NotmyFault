from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from notmyfault.platform.platform_support import get_config_dir


@dataclass(frozen=True, slots=True)
class ApplicationPaths:
    config_dir: Path
    package_root: Path
    project_root: Path

    @classmethod
    def default(cls) -> "ApplicationPaths":
        package_root = Path(__file__).resolve().parent
        return cls(
            config_dir=Path(get_config_dir()),
            package_root=package_root,
            project_root=package_root.parent,
        )

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def rules_file(self) -> Path:
        return self.config_dir / "rules.json"

    @property
    def config_secret_file(self) -> Path:
        return self.config_dir / ".config_secret"

    @property
    def api_token_file(self) -> Path:
        return self.config_dir / ".api_token"

    @property
    def ai_api_key_file(self) -> Path:
        return self.config_dir / ".ai_api_key"

    @property
    def run_history_file(self) -> Path:
        return self.config_dir / "run-events.jsonl"

    @property
    def plugin_manifest_file(self) -> Path:
        return self.config_dir / "plugin_manifest.json"

    @property
    def logs_dir(self) -> Path:
        return self.config_dir / "logs"

    @property
    def user_plugins_dir(self) -> Path:
        return self.config_dir / "plugins"

    @property
    def private_dir(self) -> Path:
        return self.project_root / ".private"

    @property
    def plugin_private_key_file(self) -> Path:
        return self.private_dir / "signing_private_key.pem"

    @property
    def plugin_public_key_file(self) -> Path:
        return self.private_dir / "signing_public.pem"
