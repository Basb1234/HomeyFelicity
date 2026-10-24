"""Make the app importable without a Homey runtime.

``homey`` is only present inside Homey Pro, so the tests stand in a minimal
stub with the same shape: enough of `App`, `Driver`, `Device` and `PairSession`
for ``app.py``, ``driver.py`` and ``device.py`` to import and for their pure
logic to be exercised.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Logging:
    def log(self, *args, **kwargs): ...
    def debug(self, *args, **kwargs): ...
    def error(self, *args, **kwargs): ...


class StubApp(_Logging):
    async def on_init(self): ...
    async def on_uninit(self): ...


class StubDriver(_Logging):
    async def on_init(self): ...
    async def on_pair_list_devices(self, view_data): return []


class StubDevice(_Logging):
    """Records capability writes so tests can assert on them."""

    def __init__(self):
        self.state: dict[str, object] = {}
        self.capabilities: set[str] = set()
        self.store: dict[str, object] = {}
        self.settings: dict[str, object] = {}

    async def on_init(self): ...
    async def on_uninit(self): ...

    def has_capability(self, id): return id in self.capabilities
    def get_capability_value(self, id): return self.state.get(id)
    async def set_capability_value(self, id, value): self.state[id] = value
    async def add_capability(self, id): self.capabilities.add(id)
    async def remove_capability(self, id): self.capabilities.discard(id)

    def get_store(self): return dict(self.store)
    async def set_store_value(self, key, value): self.store[key] = value

    def get_settings(self): return dict(self.settings)
    def get_setting(self, key): return self.settings.get(key)
    async def set_settings(self, settings): self.settings.update(settings)

    async def set_available(self): self.available = True
    async def set_unavailable(self, message=None): self.available = False


class StubPairSession:
    def __init__(self):
        self.handlers = {}

    def set_handler(self, event, listener):
        self.handlers[event] = listener
        return self

    async def done(self): ...


def _install_stub() -> None:
    homey = types.ModuleType("homey")
    sys.modules["homey"] = homey

    for name, attrs in (
        ("homey.app", {"App": StubApp}),
        ("homey.device", {"Device": StubDevice}),
        ("homey.driver", {"Driver": StubDriver, "ListDeviceProperties": dict}),
        ("homey.pair_session", {"PairSession": StubPairSession}),
    ):
        module = types.ModuleType(name)
        for attr, value in attrs.items():
            setattr(module, attr, value)
        sys.modules[name] = module
        setattr(homey, name.split(".", 1)[1], module)


_install_stub()
