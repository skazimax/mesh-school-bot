"""Atomic private JSON files; rotating refresh credentials are persisted promptly."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.mesh.exceptions import MeshConfigError
from app.mesh.models import AuthState, RegistrationMetadata


def save_private_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".mesh-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class AuthStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> AuthState:
        try:
            return AuthState.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            raise MeshConfigError("Нет корректного файла авторизации. Выполните login.") from exc

    def save(self, state: AuthState) -> None:
        data = state.model_dump(mode="json")
        for key in ("client_id", "client_secret", "refresh_token", "mesh_access_token"):
            secret = getattr(state, key)
            data[key] = secret.get_secret_value() if secret else None
        save_private_json(self.path, data)


def load_registration(path: Path) -> RegistrationMetadata:
    try:
        return RegistrationMetadata.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise MeshConfigError("Нет metadata регистрации. Выполните prepare-login.") from exc
