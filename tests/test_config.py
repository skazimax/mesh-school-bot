import os
from unittest.mock import patch

import pytest

from app.config import Settings
from app.mesh.exceptions import MeshConfigError


@pytest.mark.parametrize("value,expected", [("", "text"), ("text", "text"), ("image", "image")])
def test_report_format_switch_and_backward_compatible_default(value: str, expected: str) -> None:
    with (
        patch("app.config.load_dotenv"),
        patch.dict(
            os.environ,
            {"WORKER_ONLY": "true", "REPORT_FORMAT": value},
            clear=True,
        ),
    ):
        assert Settings.load().report_format == expected


def test_report_format_rejects_typo() -> None:
    with (
        patch("app.config.load_dotenv"),
        patch.dict(
            os.environ,
            {"WORKER_ONLY": "true", "REPORT_FORMAT": "imgae"},
            clear=True,
        ),
        pytest.raises(MeshConfigError, match="REPORT_FORMAT"),
    ):
        Settings.load()
