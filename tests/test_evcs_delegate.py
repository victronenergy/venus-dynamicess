import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "ext", "aiovelib"))

import helper  # noqa: E402  (must be imported before globals/evcs_delegate -- circular import)
from globals import EvcsGxFlags, C_DISABLE_EVCS_CONTROL  # noqa: E402
from evcs_delegate import EVCSDelegate  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
