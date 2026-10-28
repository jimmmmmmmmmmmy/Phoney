"""Secret values that must not appear in repr, logs, or assertion text."""

from __future__ import annotations


class Secret:
    """A private env value. ``repr`` and ``str`` never include the value."""

    def __init__(self, env_name: str, value: str):
        self.env_name = str(env_name)
        self._value = "" if value is None else str(value)

    def __repr__(self) -> str:
        return f"Secret(env={self.env_name}, configured={self.configured})"

    def __str__(self) -> str:
        return repr(self)

    def __bool__(self) -> bool:
        return self.configured

    @property
    def configured(self) -> bool:
        return bool(self._value)

    def leaked_into(self, text: str) -> bool:
        if not self._value or len(self._value) < 4 or not isinstance(text, str):
            return False
        return self._value in text

    def reveal(self) -> str:
        """The raw value, for the live model instruction only."""
        return self._value


def assert_no_leak(text: str, secrets: list[Secret]) -> None:
    """Fail with env names only, never with the secret or the scanned text."""
    leaked = [secret.env_name for secret in secrets if secret.leaked_into(text)]
    if leaked:
        raise AssertionError("configured secrets leaked: " + ", ".join(leaked))


def load_secret(environ: dict, env_name: str) -> Secret:
    raw = environ.get(env_name, "")
    return Secret(env_name, "" if raw is None else str(raw).strip())
