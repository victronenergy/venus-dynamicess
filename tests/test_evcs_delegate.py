import json
import os
import sys
import unittest
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "ext", "aiovelib"))

import helper  # noqa: E402  (must be imported before globals/evcs_delegate -- circular import)
from globals import EvcsGxFlags, C_DISABLE_EVCS_CONTROL, C_EVCS_VRM_FLAGS  # noqa: E402
from evcs_delegate import EVCSDelegate  # noqa: E402
from dynamicess_window import DynamicEssWindow  # noqa: E402
from s2python.common import ControlType  # noqa: E402


_UNSET = object()


class FakeService:
    """Minimal stand-in for aiovelib.client.Service: only get_value/name/owner,
    which is all EVCSDelegate actually touches on it."""

    def __init__(self, name="com.victronenergy.evcharger.fake", owner=":1.99"):
        self.name = name
        self.owner = owner
        self._values = {"/Mode": 1, "/Status": 2}

    def get_value(self, path):
        return self._values.get(path)

    def set(self, path, value):
        self._values[path] = value


class FakeMonitor:
    """Minimal stand-in for helper.AioMonitor. KeepAlive always acks True,
    modelling an RM whose KeepAlive method doesn't verify session identity."""

    def __init__(self):
        self.keepalive_calls = 0

    async def dbus_call(self, service_name, path, method, signature, *args, **kwargs):
        if method == "KeepAlive":
            self.keepalive_calls += 1
        return [True]

    async def add_message_handler(self, *args, **kwargs):
        pass

    async def remove_message_handler(self, *args, **kwargs):
        pass


class DummyDess:
    active = True


class EvcsS2ActiveLivenessTest(unittest.IsolatedAsyncioTestCase):
    def _make_delegate(self, active_value=_UNSET):
        C_DISABLE_EVCS_CONTROL.current_value = 0  # control not administratively disabled
        service = FakeService()
        monitor = FakeMonitor()
        delegate = EVCSDelegate(service, 40, monitor, DummyDess())
        delegate.gx_flags = EvcsGxFlags.GX_AUTO_ACQUIRED | EvcsGxFlags.CONTROLLABLE
        if active_value is not _UNSET:
            service.set("/S2/0/Active", active_value)
        return delegate

    async def test_dead_rm_session_is_detected_even_though_keepalive_still_acks(self):
        """Session was confirmed alive at least once, then /S2/0/Active flips
        to 0 while still latched -> must be dropped so the retry-connect
        logic can re-acquire it."""
        delegate = self._make_delegate(active_value=1)
        await delegate._check_conditions()  # observes Active=1, arms the check

        result = await delegate._aiomonitor.dbus_call(
            delegate.service.name, delegate.s2rmpath, "KeepAlive", "s", "CEM")
        self.assertEqual(result, [True])

        delegate.service.set("/S2/0/Active", 0)
        await delegate._check_conditions()

        self.assertNotIn(EvcsGxFlags.CONTROLLABLE, delegate.gx_flags)

    async def test_healthy_session_is_left_alone(self):
        """/S2/0/Active=1 -> no false-positive disconnect."""
        delegate = self._make_delegate(active_value=1)
        before = delegate.gx_flags

        await delegate._check_conditions()

        self.assertEqual(delegate.gx_flags, before)

    async def test_missing_active_path_is_not_treated_as_dead(self):
        """Charger that doesn't publish /S2/0/Active at all (get_value ->
        None) must not be wrongly disconnected."""
        delegate = self._make_delegate(active_value=_UNSET)
        before = delegate.gx_flags

        await delegate._check_conditions()

        self.assertEqual(delegate.gx_flags, before)

    async def test_freshly_connected_session_is_not_dropped_before_rm_publishes_active(self):
        """Right after begin(), /S2/0/Active is read from aiovelib's locally
        cached Item (see ext/aiovelib/aiovelib/client.py Service.get_value) --
        it isn't a live read, and the RM hasn't necessarily published (or we
        haven't yet received) Active=1 by the time the very next
        _check_conditions() tick runs. A session that has never yet been
        observed as active must not be torn down just because it hasn't
        confirmed itself alive yet."""
        delegate = self._make_delegate(active_value=0)
        before = delegate.gx_flags

        await delegate._check_conditions()

        self.assertEqual(delegate.gx_flags, before)

        # once the RM does publish Active=1, the session must still be
        # usable -- confirm the check arms itself rather than staying stuck.
        delegate.service.set("/S2/0/Active", 1)
        await delegate._check_conditions()
        self.assertEqual(delegate.gx_flags, before)

        delegate.service.set("/S2/0/Active", 0)
        await delegate._check_conditions()
        self.assertNotIn(EvcsGxFlags.CONTROLLABLE, delegate.gx_flags)


class EvcsScheduledSetpointTest(unittest.IsolatedAsyncioTestCase):
    """Scheduled kWh in window.to_ev must be converted to average W over window.duration (seconds)."""

    def _make_delegate(self):
        C_DISABLE_EVCS_CONTROL.current_value = 0
        C_EVCS_VRM_FLAGS.current_value = "{}"
        delegate = EVCSDelegate(FakeService(), 40, FakeMonitor(), DummyDess())
        delegate.gx_flags = EvcsGxFlags.CONTROLLABLE
        delegate.active_control_type = ControlType.OPERATION_MODE_BASED_CONTROL
        delegate.ombc_active_operation_mode = object()
        delegate.ombc_system_description = object()

        async def _noop():
            pass

        delegate._approach_setpoint = _noop
        return delegate

    def _make_window(self, kwh, duration):
        return DynamicEssWindow(
            datetime.now(timezone.utc), duration, 50, 50, True, 0, 0, 0, 0,
            json.dumps({"40": kwh}))

    async def _run(self, kwh, duration):
        delegate = self._make_delegate()
        await delegate.loop(self._make_window(kwh, duration), datetime.now(timezone.utc))
        return delegate

    async def test_15min_window(self):
        delegate = await self._run(kwh=1.0, duration=900)
        self.assertAlmostEqual(delegate.power_setpoint, 4000)
        self.assertIn(EvcsGxFlags.CHARGING, delegate.gx_flags)
        self.assertIn(EvcsGxFlags.SCHEDULED, delegate.gx_flags)

    async def test_60min_window(self):
        delegate = await self._run(kwh=2.0, duration=3600)
        self.assertAlmostEqual(delegate.power_setpoint, 2000)

    async def test_30min_window(self):
        delegate = await self._run(kwh=1.5, duration=1800)
        self.assertAlmostEqual(delegate.power_setpoint, 3000)

    async def test_zero_duration_does_not_divide_by_zero(self):
        delegate = await self._run(kwh=1.0, duration=0)
        self.assertEqual(delegate.power_setpoint, 0)

    async def test_zero_energy_stops_charging(self):
        delegate = self._make_delegate()
        delegate.add_flag(EvcsGxFlags.CHARGING)
        delegate.power_setpoint = 4000
        await delegate.loop(self._make_window(0, 900), datetime.now(timezone.utc))
        self.assertEqual(delegate.power_setpoint, 0)
        self.assertNotIn(EvcsGxFlags.CHARGING, delegate.gx_flags)


if __name__ == "__main__":
    unittest.main()
