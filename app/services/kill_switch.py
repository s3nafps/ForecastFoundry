from dataclasses import dataclass


class KillSwitchError(RuntimeError):
    pass


@dataclass
class KillSwitch:
    active: bool = True
    reason: str = "startup safety default"

    def activate(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("kill-switch reason is required")
        self.active = True
        self.reason = reason

    def clear(self) -> None:
        self.active = False
        self.reason = ""

    def assert_clear(self) -> None:
        if self.active:
            raise KillSwitchError(f"kill switch is active: {self.reason}")
