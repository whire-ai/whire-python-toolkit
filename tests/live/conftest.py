"""The live scenarios build on each other (shared payer, mandate and recorded ids), so they run in file order
even when a plugin such as pytest-randomly shuffles the rest of the suite."""

from __future__ import annotations

import pytest


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    live = [item for item in items if "tests/live/" in item.nodeid.replace("\\", "/")]
    if not live:
        return
    ordered = iter(sorted(live, key=lambda item: (str(item.path), item.location[1])))
    items[:] = [next(ordered) if item in live else item for item in items]
