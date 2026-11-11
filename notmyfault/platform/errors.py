from __future__ import annotations

KINDS = frozenset({"unsupported", "unavailable", "permission_denied", "invalid", "failed"})


class PlatformServiceError(RuntimeError):
    def __init__(self, capability: str, kind: str, reason: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"未知错误类别: {kind!r}")
        self.capability = capability
        self.kind = kind
        self.reason = reason
        super().__init__(f"{capability}: {reason}")

    def to_dict(self) -> dict[str, str]:
        return {"capability": self.capability, "kind": self.kind, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict) -> "PlatformServiceError":
        return cls(str(data["capability"]), str(data["kind"]), str(data["reason"]))


class BackendError(RuntimeError):
    kind = "failed"


class BackendUnsupportedError(BackendError):
    kind = "unsupported"


class BackendMissingError(BackendError):
    kind = "unavailable"


class BackendPermissionDeniedError(BackendError):
    kind = "permission_denied"


class BackendInvalidError(BackendError, ValueError):
    kind = "invalid"


class BackendFailedError(BackendError):
    kind = "failed"
