"""Errors deliberately contain neither response bodies nor credentials."""


class MeshError(Exception):
    """Base error safe to show in the CLI."""


class MeshAuthError(MeshError):
    """Authentication failed or requires manual intervention."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class MeshAPIError(MeshError):
    """HTTP, network or response-shape error."""


class MeshConfigError(MeshError):
    """Missing or invalid local configuration."""
