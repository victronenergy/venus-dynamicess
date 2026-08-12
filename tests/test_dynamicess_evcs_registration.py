import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "ext", "aiovelib"))

try:
    import dbus  # noqa: F401
except ImportError:
    # python-dbus is only present on the actual Venus device, not in this
    # dev/test environment. dynamicess.py imports it (unused directly, kept
    # for on-device side effects), so stub it out purely to make the module
    # importable here.
    import types
    sys.modules.setdefault("dbus", types.ModuleType("dbus"))

import helper  # noqa: E402  (must be imported before globals/dynamicess -- circular import)
import dynamicess  # noqa: E402
from dynamicess import DynamicEss  # noqa: E402


class FakeService:
    """Minimal stand-in for aiovelib.client.Service."""

    def __init__(self, name, owner=":1.99"):
        self.name = name
        self.owner = owner

    def get_value(self, path):
        return None


class FakeItem:
    def __init__(self):
        self.value = None

    def get_item(self, path):
        return self

    def set_local_value(self, value):
        self.value = value


class FakeDelegate:
    """Stand-in for EVCSDelegate that just records lifecycle calls."""

    def __init__(self, service, instance, monitor, dess, init_disabled=False):
        self.service = service
        self.instance = instance
        self.begin_calls = 0
        self.end_calls = 0

    async def begin(self, init_disabled=False):
        self.begin_calls += 1

    async def end(self, by_dess=True, dbus_disconnect=False):
        self.end_calls += 1


class SleepGate:
    """Replaces asyncio.sleep so a test can pause a coroutine at the
    'connect in 10 seconds' point and resume it deterministically, instead
    of waiting on a real timer."""

    def __init__(self):
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, seconds):
        self.reached.set()
        await self.release.wait()


class EvcsRegisterRemoveRaceTest(unittest.IsolatedAsyncioTestCase):
    def _make_dess(self):
        dess = DynamicEss(bus_type=None)
        dess._aiomonitor = object()  # only needs to be non-None
        dess._dbusservice = FakeItem()
        return dess

    async def test_removal_during_connect_delay_does_not_crash(self):
        """A charger that disconnects again during the 10s post-registration
        grace period must not crash _on_service_added with a KeyError when
        it wakes back up and tries to begin() the (by then removed) delegate."""
        dess = self._make_dess()
        name = "com.victronenergy.evcharger.ttyUSB0"
        service = FakeService(name)

        gate = SleepGate()
        with patch.object(dynamicess, "EVCSDelegate", FakeDelegate), \
             patch.object(dynamicess.asyncio, "sleep", gate):
            added_task = asyncio.ensure_future(dess._on_service_added(name, 40, service))

            await asyncio.wait_for(gate.reached.wait(), timeout=1)
            self.assertIn("40", dess._evcs_delegates)

            # charger disconnects again while we're still waiting to connect.
            await dess._on_service_removed(name, 40, service)
            self.assertNotIn("40", dess._evcs_delegates)

            gate.release.set()
            await asyncio.wait_for(added_task, timeout=1)  # must not raise KeyError

        self.assertNotIn("40", dess._evcs_delegates)

    async def test_registration_without_removal_still_connects(self):
        """Sanity check: when nothing removes the charger during the grace
        period, begin() is still called as before."""
        dess = self._make_dess()
        name = "com.victronenergy.evcharger.ttyUSB0"
        service = FakeService(name)

        with patch.object(dynamicess, "EVCSDelegate", FakeDelegate), \
             patch.object(dynamicess.asyncio, "sleep", AsyncMock(return_value=None)):
            await dess._on_service_added(name, 40, service)

        delegate = dess._evcs_delegates["40"]
        self.assertEqual(delegate.begin_calls, 1)


if __name__ == "__main__":
    unittest.main()
