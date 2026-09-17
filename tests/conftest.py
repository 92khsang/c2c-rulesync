"""Keep the developer's own hook settings out of the tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def hermetic_environment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Unset the developer's hook settings and keep hook state in a temporary directory.

    Hook processes started by the tests inherit ``os.environ``. Without this, a
    developer's ``CLAUDE_CONFIG_DIR`` would add their own rules to the output,
    and a payload without state settings would sweep their real state directory.
    """
    with pytest.MonkeyPatch.context() as patch:
        for name in list(os.environ):
            if name in ("CLAUDE_CONFIG_DIR", "XDG_STATE_HOME") or name.startswith("C2C_RULESYNC_"):
                patch.delenv(name)
        patch.setenv("C2C_RULESYNC_STATE_DIR", str(tmp_path_factory.mktemp("state")))
        yield
