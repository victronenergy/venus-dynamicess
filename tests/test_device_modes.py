"""
Unit tests for the charge / discharge / idle / self_consume control methods of
VebusDevice and MultiRsDevice.

The devices only talk to the outside world via:
  * AioMonitor.get_value / set_value_async   (VebusDevice + shared EssDevice properties)
  * Service.get_value / set_value_async       (MultiRsDevice)
  * a few attributes on DynamicEss            (oneway_efficiency, charge power capability, ...)
  * global Configurables                      (grid / battery limits)

All of them are faked here, so each test feeds a combination of DC-PV, AC-PV,
consumption and requested rate and asserts the dbus values written.

Unless stated otherwise the one-way efficiency used is 0.925 (= 85% system efficiency).
"""
import os
import sys
import unittest
from dataclasses import dataclass, field
from typing import Any, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "ext", "aiovelib"))

import helper  # noqa: E402,F401  (must be imported before globals -- circular import)
from constants import SYSTEM_SERVICE, SETTINGS_SERVICE, HUB4_SERVICE, MAX_FEEDIN_VALUE  # noqa: E402
from globals import (  # noqa: E402
	Flags, Restrictions,
	C_BATTERY_CHARGE_LIMIT, C_BATTERY_DISCHARGE_LIMIT, C_GRID_EXPORT_LIMIT, C_GRID_IMPORT_LIMIT,
)
from vebus_device import VebusDevice  # noqa: E402
from multirs_device import MultiRsDevice  # noqa: E402

EFF = 0.925
SOLARCHARGER = "com.victronenergy.solarcharger.ttyS1"

# --------------------------------------------------------------------------- cases

class _NotSet:
	def __repr__(self):
		return "<not set>"

NOT_SET: Any = _NotSet()


@dataclass(frozen=True, kw_only=True)
class Case:
	"""One input scenario plus the values a test expects to be written / returned.

	Inputs default to "nothing happening"; expectations default to NOT_SET. A test only
	checks the expectations it is about -- but if it checks one the case did not set,
	assertExpected() fails instead of silently passing."""
	name: str

	# --- inputs
	dcpv: float = 0
	acpv: float = 0
	consumption: float = 0
	rate: Optional[float] = 0
	allow_feedin: Optional[bool] = True
	restrictions: Restrictions = Restrictions.NONE
	flags: Flags = Flags.NONE
	acpv_split: bool = False          # spread AC-PV over PvOnGrid/PvOnOutput L1..L3
	consumption_split: bool = False   # spread consumption over L1..L3

	# --- expectations
	expected_setpoint: Any = NOT_SET              # RS: /Ess/AcPowerSetpoint, VE.Bus: /Overrides/Setpoint
	expected_rate: Any = NOT_SET                  # value returned by the method
	expected_max_charge_power: Any = NOT_SET      # VE.Bus: /Overrides/MaxChargePower
	expected_max_discharge_power: Any = NOT_SET   # VE.Bus: /Overrides/MaxDischargePower


@dataclass(frozen=True, kw_only=True)
class FeedinLimitCase:
	name: str
	local_limit: float          # /Settings/CGwacs/MaxFeedInPower (W, -1 = none)
	dess_limit: Optional[float] # C_GRID_EXPORT_LIMIT (kW)
	expected: float


@dataclass(frozen=True, kw_only=True)
class ExternalMpptCase:
	name: str
	chargers: dict = field(default_factory=dict)  # service -> /Yield/Power
	expected_disabled: int = NOT_SET


# --------------------------------------------------------------------------- fakes

class FakeMonitor:
	"""Stand-in for helper.AioMonitor: a dict of (service, path) -> value; writes are recorded."""

	def __init__(self, values=None):
		self.values = dict(values or {})
		self.writes = {}

	def get_value(self, service, path):
		return self.values.get((service, path))

	def set_value_async(self, service, path, value):
		self.writes[(service, path)] = value


class FakeService:
	"""Stand-in for aiovelib.client.Service (the device's own service)."""

	def __init__(self, values=None):
		self.values = dict(values or {})
		self.writes = {}

	def get_value(self, path):
		return self.values.get(path)

	def set_value_async(self, path, value):
		self.writes[path] = value


class FakeDess:
	def __init__(self, oneway_efficiency=EFF, charge_power_capability=None, external_solarchargers=()):
		self.oneway_efficiency = oneway_efficiency
		self._charge_power_capability = charge_power_capability
		self._external_solarcharger_services = list(external_solarchargers)

	def get_charge_power_capability(self):
		return self._charge_power_capability


def system_values(dcpv=0, acpv=0, consumption=0, acpv_split=False, consumption_split=False):
	"""Build monitor values for com.victronenergy.system. Optionally spread AC-PV over
	grid/output and consumption over three phases, to exercise the summing properties."""
	v = {(SYSTEM_SERVICE, '/Dc/Pv/Power'): dcpv}
	if acpv_split:
		share = acpv / 6.0
		for loc in ('PvOnGrid', 'PvOnOutput'):
			for ph in ('L1', 'L2', 'L3'):
				v[(SYSTEM_SERVICE, f'/Ac/{loc}/{ph}/Power')] = share
	else:
		v[(SYSTEM_SERVICE, '/Ac/PvOnGrid/L1/Power')] = acpv
	if consumption_split:
		for ph in ('L1', 'L2', 'L3'):
			v[(SYSTEM_SERVICE, f'/Ac/Consumption/{ph}/Power')] = consumption / 3.0
	else:
		v[(SYSTEM_SERVICE, '/Ac/Consumption/L1/Power')] = consumption
	return v


class _DeviceTestBase(unittest.TestCase):
	"""Saves/restores the global limit Configurables around every test, plus shared asserts."""

	_CONFIGURABLES = (C_BATTERY_CHARGE_LIMIT, C_BATTERY_DISCHARGE_LIMIT, C_GRID_EXPORT_LIMIT, C_GRID_IMPORT_LIMIT)

	def setUp(self):
		self._saved = [c.current_value for c in self._CONFIGURABLES]
		# Generous defaults (kW) so limits only bite in tests that want them to.
		C_GRID_EXPORT_LIMIT.current_value = 50.0
		C_GRID_IMPORT_LIMIT.current_value = 50.0
		C_BATTERY_CHARGE_LIMIT.current_value = 50.0
		C_BATTERY_DISCHARGE_LIMIT.current_value = 50.0

	def tearDown(self):
		for c, v in zip(self._CONFIGURABLES, self._saved):
			c.current_value = v

	def assertExpected(self, actual, expected, what=""):
		if expected is NOT_SET:
			self.fail(f"case does not define an expectation for {what or 'this value'}")
		if expected is None or actual is None:
			self.assertEqual(actual, expected, what)
		else:
			self.assertAlmostEqual(actual, expected, places=3, msg=what)

	def make_for(self, case: Case, **kw):
		return self.make(dcpv=case.dcpv, acpv=case.acpv, consumption=case.consumption,
			acpv_split=case.acpv_split, consumption_split=case.consumption_split, **kw)


# =========================================================================== Multi RS

class MultiRsTestBase(_DeviceTestBase):
	def make(self, dcpv=0, acpv=0, consumption=0, eff=EFF, external_solarchargers=None, **kw):
		values = system_values(dcpv, acpv, consumption, **kw)
		for svc, power in (external_solarchargers or {}).items():
			values[(svc, '/Yield/Power')] = power
		self.monitor = FakeMonitor(values)
		self.service = FakeService({'/Settings/Ess/Mode': 1})
		dess = FakeDess(oneway_efficiency=eff, external_solarchargers=(external_solarchargers or {}).keys())
		return MultiRsDevice(dess, self.monitor, self.service)

	def setpoint(self):
		return self.service.writes['/Ess/AcPowerSetpoint']

	def assertDisableFeedin(self, allow_feedin):
		self.assertEqual(self.service.writes['/Ess/DisableFeedIn'], 0 if allow_feedin in (True, None) else 1)


class MultiRsChargeTest(MultiRsTestBase):
	G2B = Restrictions.GRID2BAT
	CASES = [
		# --- grid charging, no solar
		Case(
			name="grid only",
			rate=3000,
			expected_setpoint=3000 / EFF,
			expected_rate=3000
		),
		Case(
			name="grid only + consumption",
			consumption=500,
			rate=3000,
			expected_setpoint=3000 / EFF + 500,
			expected_rate=3000
		),

		# --- DC-PV covers part of the rate: only the remainder is AC->DC and carries the loss
		Case(
			name="dcpv partial",
			dcpv=1000,
			consumption=500,
			rate=3000,
			expected_setpoint=2000 / EFF + 500,
			expected_rate=3000
		),
		Case(
			name="dcpv == rate",
			dcpv=3000,
			consumption=500,
			rate=3000,
			expected_setpoint=500,
			expected_rate=3000
		),

		# --- DC-PV exceeds rate: surplus DC->AC carries the loss and is fed in
		Case(
			name="dcpv surplus feeds in",
			dcpv=4000,
			consumption=500,
			rate=3000,
			expected_setpoint=-1000 * EFF + 500,
			expected_rate=3000
		),
		Case(
			name="dcpv surplus, no feedin",
			dcpv=4000,
			consumption=500,
			rate=3000,
			allow_feedin=False,
			expected_setpoint=0,
			expected_rate=3000
		),
		Case(
			name="dcpv surplus, feedin None",
			dcpv=4000,
			consumption=500,
			rate=3000,
			allow_feedin=None,
			expected_setpoint=0,
			expected_rate=3000
		),
		Case(
			name="dcpv surplus > cons",
			dcpv=4000,
			consumption=2000,
			rate=3000,
			allow_feedin=False,
			expected_setpoint=-1000 * EFF + 2000,
			expected_rate=3000
		),

		# --- AC-PV is subtracted 1:1 at the AC side
		Case(
			name="acpv only",
			acpv=2000,
			consumption=500,
			rate=3000,
			expected_setpoint=3000 / EFF - 2000 + 500,
			expected_rate=3000
		),
		Case(
			name="acpv surplus feeds in",
			acpv=5000,
			consumption=500,
			rate=3000,
			expected_setpoint=3000 / EFF - 5000 + 500,
			expected_rate=3000
		),
		Case(
			name="acpv surplus, no feedin",
			acpv=5000,
			consumption=500,
			rate=3000,
			allow_feedin=False,
			expected_setpoint=0,
			expected_rate=3000
		),
		Case(
			name="mixed pv",
			dcpv=2000,
			acpv=1500,
			consumption=800,
			rate=5000,
			expected_setpoint=3000 / EFF - 1500 + 800,
			expected_rate=5000
		),
		Case(
			name="zero rate, solar export",
			dcpv=1000,
			acpv=1000,
			consumption=200,
			rate=0,
			expected_setpoint=-1000 * EFF - 1000 + 200,
			expected_rate=0
		),
		Case(
			name="phases summed",
			dcpv=1000,
			acpv=1800,
			consumption=900,
			rate=3000,
			acpv_split=True,
			consumption_split=True,
			expected_setpoint=2000 / EFF - 1800 + 900,
			expected_rate=3000
		),

		# --- GRID2BAT: rate capped at DC-PV + AC-PV * eff
		Case(
			name="grid2bat caps rate",
			dcpv=1000,
			acpv=2000,
			consumption=500,
			rate=5000,
			restrictions=G2B,
			expected_setpoint=(2850 - 1000) / EFF - 2000 + 500,
			expected_rate=1000 + 2000 * EFF
		),
		Case(
			name="grid2bat no solar",
			consumption=500,
			rate=5000,
			restrictions=G2B,
			expected_setpoint=500,
			expected_rate=0
		),
		Case(
			name="grid2bat rate below solar",
			dcpv=1000,
			acpv=2000,
			consumption=500,
			rate=1500,
			restrictions=G2B,
			expected_setpoint=500 / EFF - 2000 + 500,
			expected_rate=1500
		),
		Case(
			name="grid2bat dcpv only",
			dcpv=3000,
			consumption=100,
			rate=5000,
			restrictions=G2B,
			expected_setpoint=100,
			expected_rate=3000
		),
		Case(
			name="grid2bat + bat2grid",
			dcpv=1000,
			acpv=2000,
			consumption=500,
			rate=5000,
			restrictions=G2B | Restrictions.BAT2GRID,
			expected_setpoint=(2850 - 1000) / EFF - 2000 + 500,
			expected_rate=1000 + 2000 * EFF
		),
		Case(
			name="Excel Case 1",
			dcpv=1000,
			rate=500,
			expected_setpoint = -462.500,
			expected_rate=500
		),
		Case(
			name="Excel Case 2",
			dcpv=600,
			rate=500,
			expected_setpoint = -92.500,
			expected_rate=500
		),
		Case(
			name="Excel Case 3",
			dcpv=400,
			rate=500,
			expected_setpoint = 108.108,
			expected_rate=500
		),
		Case(
			name="Excel Case 4",
			dcpv=400,
			rate=500,
			acpv=200,
			expected_setpoint = 108.108 - 200,
			expected_rate=500
		)
		,
		Case(
			name="Excel Case 5",
			dcpv=400,
			rate=500,
			acpv=200,
			consumption=100,
			expected_setpoint = 108.108 - 200 + 100,
			expected_rate=500
		)
	]

	def test_charge_combinations(self):
		for case in self.CASES:
			with self.subTest(case.name):
				dev = self.make_for(case)
				ret = dev.charge(case.flags, case.restrictions, case.rate, case.allow_feedin)
				self.assertExpected(self.setpoint(), case.expected_setpoint, "AcPowerSetpoint")
				self.assertExpected(ret, case.expected_rate, "returned rate")
				self.assertDisableFeedin(case.allow_feedin)
				self.assertEqual(self.service.writes['/Ess/DisableCharge'], 0)
				self.assertEqual(self.service.writes['/Ess/DisableDischarge'], 0)
				self.assertEqual(self.service.writes['/Ess/UseInverterPowerSetpoint'], 0)

	def test_efficiency_only_applied_to_converted_part(self):
		# With 100% efficiency the setpoint is just the AC balance.
		for case in [
			Case(
				name="grid only",
				rate=3000,
				expected_setpoint=3000
			),
			Case(
				name="mixed",
				dcpv=1000,
				acpv=500,
				consumption=300,
				rate=3000,
				expected_setpoint=3000 - 1000 - 500 + 300
			),
			Case(
				name="dcpv surplus",
				dcpv=5000,
				consumption=200,
				rate=3000,
				expected_setpoint=3000 - 5000 + 200
			),
		]:
			with self.subTest(case.name):
				dev = self.make_for(case, eff=1.0)
				dev.charge(case.flags, case.restrictions, case.rate, case.allow_feedin)
				self.assertExpected(self.setpoint(), case.expected_setpoint, "AcPowerSetpoint")

	def test_setpoint_is_continuous_around_dcpv_equals_rate(self):
		dev = self.make(dcpv=3000, consumption=500)
		dev.charge(Flags.NONE, Restrictions.NONE, 2999, True)
		below = self.setpoint()
		dev.charge(Flags.NONE, Restrictions.NONE, 3001, True)
		above = self.setpoint()
		self.assertLess(below, 500)
		self.assertGreater(above, 500)
		self.assertLess(above - below, 3)

	def test_fastcharge_uses_battery_charge_limit(self):
		C_BATTERY_CHARGE_LIMIT.current_value = 5.0
		C_GRID_IMPORT_LIMIT.current_value = 10.0
		for case in [
			Case(
				name="grid-only, 400 consumption",
				dcpv=0,
				consumption=400,
				rate=1234,
				flags=Flags.FASTCHARGE,
				expected_setpoint = 5000 + 400,
				expected_rate = 5000 * EFF
			),
			Case(
				name="fast charge saturated by dcpv",
				dcpv=5000 * EFF, #exactly what the battery could take for now ac requirement
				consumption=400,
				rate=1234,
				flags=Flags.FASTCHARGE,
				expected_setpoint = 400,
				expected_rate = 5000 * EFF
			)
		]:
			with self.subTest(case.name):
				dev = self.make_for(case)
				ret = dev.charge(case.flags, case.restrictions, case.rate, case.allow_feedin)
				self.assertExpected(ret, case.expected_rate, "returned rate")
				self.assertExpected(self.setpoint(), case.expected_setpoint, "AcPowerSetpoint")

	def test_grid_import_limit(self):
		C_GRID_IMPORT_LIMIT.current_value = 4.0
		dev = self.make(consumption=1000)
		dev.charge(Flags.NONE, Restrictions.NONE, 5000, True)
		self.assertAlmostEqual(self.setpoint(), 4000.0)

	def test_grid_export_limit(self):
		C_GRID_EXPORT_LIMIT.current_value = 2.0
		dev = self.make(dcpv=8000, acpv=2000)
		dev.charge(Flags.NONE, Restrictions.NONE, 1000, True)
		self.assertAlmostEqual(self.setpoint(), -2000.0)


class MultiRsDischargeTest(MultiRsTestBase):
	# rate is passed in positive (discharge power), returned negative.
	B2G = Restrictions.BAT2GRID
	CASES = [
		Case(
			name="battery only",
			rate=2000,
			expected_setpoint=-2000 * EFF,
			expected_rate=-2000
		),
		Case(
			name="battery + consumption",
			consumption=500,
			rate=2000,
			expected_setpoint=500 - 2000 * EFF,
			expected_rate=-2000
		),
		Case(
			name="rate below consumption",
			consumption=3000,
			rate=1000,
			expected_setpoint=3000 - 1000 * EFF,
			expected_rate=-1000
		),
		Case(
			name="with dcpv",
			dcpv=1000,
			consumption=500,
			rate=2000,
			expected_setpoint=-1000 * EFF + 500 - 2000 * EFF,
			expected_rate=-2000
		),
		Case(
			name="with acpv",
			acpv=1000,
			consumption=500,
			rate=2000,
			expected_setpoint=-1000 + 500 - 2000 * EFF,
			expected_rate=-2000
		),
		Case(
			name="mixed pv",
			dcpv=1500,
			acpv=700,
			consumption=1200,
			rate=3000,
			expected_setpoint=-700 - 1500 * EFF + 1200 - 3000 * EFF,
			expected_rate=-3000
		),
		Case(
			name="zero rate",
			dcpv=1000,
			acpv=1000,
			consumption=500,
			rate=0,
			expected_setpoint=-1000 - 1000 * EFF + 500,
			expected_rate=0
		),
		Case(
			name="no feedin -> clamp 0",
			consumption=500,
			rate=2000,
			allow_feedin=False,
			expected_setpoint=0,
			expected_rate=-2000
		),
		Case(
			name="no feedin, cons > rate",
			consumption=3000,
			rate=1000,
			allow_feedin=False,
			expected_setpoint=3000 - 1000 * EFF,
			expected_rate=-1000
		),
		Case(
			name="feedin None -> clamp 0",
			dcpv=1000,
			consumption=500,
			rate=2000,
			allow_feedin=None,
			expected_setpoint=0,
			expected_rate=-2000
		),

		# --- BAT2GRID: battery discharge limited to consumption / eff, solar may still be exported
		Case(
			name="bat2grid limits to cons",
			consumption=500,
			rate=2000,
			restrictions=B2G,
			expected_setpoint=0,
			expected_rate=-500 / EFF
		),
		Case(
			name="bat2grid, solar exports",
			dcpv=1000,
			acpv=500,
			consumption=1000,
			rate=3000,
			restrictions=B2G,
			expected_setpoint=-500 - 1000 * EFF,
			expected_rate=-1000 / EFF
		),
		Case(
			name="bat2grid, rate < cons",
			consumption=3000,
			rate=1000,
			restrictions=B2G,
			expected_setpoint=3000 - 1000 * EFF,
			expected_rate=-1000
		),
		Case(
			name="bat2grid, no consumption",
			dcpv=1000,
			rate=2000,
			restrictions=B2G,
			expected_setpoint=-1000 * EFF,
			expected_rate=0
		),
		Case(
			name="bat2grid + grid2bat",
			consumption=500,
			rate=2000,
			restrictions=B2G | Restrictions.GRID2BAT,
			expected_setpoint=0,
			expected_rate=-500 / EFF
		),
	]

	def test_discharge_combinations(self):
		for case in self.CASES:
			with self.subTest(case.name):
				dev = self.make_for(case)
				ret = dev.discharge(case.flags, case.restrictions, case.rate, case.allow_feedin)
				self.assertExpected(self.setpoint(), case.expected_setpoint, "AcPowerSetpoint")
				self.assertExpected(ret, case.expected_rate, "returned rate")
				self.assertDisableFeedin(case.allow_feedin)
				self.assertEqual(self.service.writes['/Ess/DisableCharge'], 0)
				self.assertEqual(self.service.writes['/Ess/DisableDischarge'], 0)
				self.assertEqual(self.service.writes['/Ess/UseInverterPowerSetpoint'], 0)

	def test_grid_export_limit(self):
		C_GRID_EXPORT_LIMIT.current_value = 1.0
		dev = self.make(dcpv=1000, consumption=500)
		dev.discharge(Flags.NONE, Restrictions.NONE, 2000, True)
		self.assertAlmostEqual(self.setpoint(), -1000.0)

	def test_grid_import_limit(self):
		C_GRID_IMPORT_LIMIT.current_value = 2.0
		dev = self.make(consumption=3000)
		dev.discharge(Flags.NONE, Restrictions.NONE, 500, True)
		self.assertAlmostEqual(self.setpoint(), 2000.0)


class MultiRsIdleTest(MultiRsTestBase):
	CASES = [
		Case(
			name="nothing",
			expected_setpoint=0
		),
		Case(
			name="consumption only",
			consumption=1000,
			expected_setpoint=1000
		),
		Case(
			name="dcpv partial",
			dcpv=400,
			consumption=1000,
			expected_setpoint=1000 - 400 * EFF
		),
		Case(
			name="acpv partial",
			acpv=300,
			consumption=1000,
			expected_setpoint=700
		),
		Case(
			name="mixed partial",
			dcpv=400,
			acpv=300,
			consumption=1000,
			expected_setpoint=1000 - 300 - 400 * EFF
		),
		Case(
			name="dcpv surplus",
			dcpv=2000,
			consumption=500,
			expected_setpoint=500 - 2000 * EFF
		),
		Case(
			name="acpv surplus",
			acpv=2000,
			consumption=500,
			expected_setpoint=-1500
		),
		# idle does not clamp the setpoint for feedin; DisableFeedIn takes care of it.
		Case(
			name="surplus, no feedin",
			dcpv=2000,
			acpv=1000,
			consumption=500,
			allow_feedin=False,
			expected_setpoint=500 - 1000 - 2000 * EFF
		),
		Case(
			name="surplus, feedin None",
			dcpv=2000,
			acpv=1000,
			consumption=500,
			allow_feedin=None,
			expected_setpoint=500 - 1000 - 2000 * EFF
		),
	]

	def test_idle_combinations(self):
		for case in self.CASES:
			with self.subTest(case.name):
				dev = self.make_for(case)
				self.assertIsNone(dev.idle(case.allow_feedin))
				self.assertExpected(self.setpoint(), case.expected_setpoint, "AcPowerSetpoint")
				self.assertDisableFeedin(case.allow_feedin)
				self.assertEqual(self.service.writes['/Ess/UseInverterPowerSetpoint'], 0)

	def test_disable_charge_discharge_without_external_mppt(self):
		for case in [
			ExternalMpptCase(
				name="no external chargers",
				expected_disabled=1
			),
			ExternalMpptCase(
				name="external charger at 0",
				chargers={SOLARCHARGER: 0},
				expected_disabled=1
			),
			ExternalMpptCase(
				name="external charger None",
				chargers={SOLARCHARGER: None},
				expected_disabled=1
			),
			ExternalMpptCase(
				name="external charger 0.2W",
				chargers={SOLARCHARGER: 0.2},
				expected_disabled=0
			),
			ExternalMpptCase(
				name="external charger producing",
				chargers={SOLARCHARGER: 800},
				expected_disabled=0
			),
		]:
			with self.subTest(case.name):
				dev = self.make(dcpv=500, consumption=1000, external_solarchargers=case.chargers)
				dev.idle(True)
				self.assertEqual(self.service.writes['/Ess/DisableDischarge'], case.expected_disabled)
				self.assertEqual(self.service.writes['/Ess/DisableCharge'], case.expected_disabled)

	def test_grid_limits(self):
		C_GRID_EXPORT_LIMIT.current_value = 1.0
		C_GRID_IMPORT_LIMIT.current_value = 0.5
		dev = self.make(dcpv=3000, consumption=100)
		dev.idle(True)
		self.assertAlmostEqual(self.setpoint(), -1000.0)
		dev = self.make(consumption=3000)
		dev.idle(True)
		self.assertAlmostEqual(self.setpoint(), 500.0)


class MultiRsSelfConsumeTest(MultiRsTestBase):
	INPUTS = [
		Case(
			name="nothing"
		),
		Case(
			name="dcpv surplus",
			dcpv=3000,
			consumption=500
		),
		Case(
			name="acpv surplus",
			acpv=3000,
			consumption=500
		),
		Case(
			name="high load",
			dcpv=2000,
			acpv=1000,
			consumption=5000
		),
	]
	RESTRICTIONS = (Restrictions.NONE, Restrictions.GRID2BAT, Restrictions.BAT2GRID,
		Restrictions.GRID2BAT | Restrictions.BAT2GRID)

	def test_self_consume_is_plain_ess(self):
		for case in self.INPUTS:
			for restr in self.RESTRICTIONS:
				for feedin in (True, False, None):
					with self.subTest(case.name, restrictions=restr, allow_feedin=feedin):
						dev = self.make_for(case)
						self.assertIsNone(dev.self_consume(restr, feedin))
						self.assertEqual(self.service.writes, {
							'/Ess/DisableFeedIn': 1 if feedin is False else 0,
							'/Ess/AcPowerSetpoint': 0,
							'/Ess/UseInverterPowerSetpoint': 0,
							'/Ess/DisableDischarge': 0,
							'/Ess/DisableCharge': 0,
						})


# =========================================================================== VE.Bus

def hub4(path):
	return (HUB4_SERVICE, path)


class VebusTestBase(_DeviceTestBase):
	def setUp(self):
		super().setUp()
		# Default: no DESS export limit configured, no local limit -> unlimited feed-in.
		C_GRID_EXPORT_LIMIT.current_value = None
		C_BATTERY_CHARGE_LIMIT.current_value = None

	def make(self, dcpv=0, acpv=0, consumption=0, eff=EFF, local_feedin_limit=-1,
		     charge_power_capability=None, **kw):
		values = system_values(dcpv, acpv, consumption, **kw)
		values[(SETTINGS_SERVICE, '/Settings/CGwacs/MaxFeedInPower')] = local_feedin_limit
		self.monitor = FakeMonitor(values)
		dess = FakeDess(oneway_efficiency=eff, charge_power_capability=charge_power_capability)
		return VebusDevice(dess, self.monitor, FakeService({'/Hub4/AssistantId': 5}))

	def w(self, path):
		return self.monitor.writes[hub4(path)]

	def assertFeedin(self, allow_feedin):
		self.assertEqual(self.w('/Overrides/FeedInExcess'), {None: 0, True: 2, False: 1}[allow_feedin])


class VebusMaxFeedinTest(VebusTestBase):
	CASES = [
		FeedinLimitCase(
			name="no limits",
			local_limit=-1,
			dess_limit=None,
			expected=-MAX_FEEDIN_VALUE
		),
		FeedinLimitCase(
			name="local only",
			local_limit=3000,
			dess_limit=None,
			expected=-3000
		),
		FeedinLimitCase(
			name="dess only",
			local_limit=-1,
			dess_limit=2.0,
			expected=-2000
		),
		FeedinLimitCase(
			name="both, dess lower",
			local_limit=3000,
			dess_limit=2.0,
			expected=-2000
		),
		FeedinLimitCase(
			name="both, local lower",
			local_limit=1500,
			dess_limit=2.0,
			expected=-1500
		),
		FeedinLimitCase(
			name="local zero",
			local_limit=0,
			dess_limit=None,
			expected=0
		),
	]

	def test_combinations(self):
		for case in self.CASES:
			with self.subTest(case.name):
				C_GRID_EXPORT_LIMIT.current_value = case.dess_limit
				dev = self.make(local_feedin_limit=case.local_limit)
				self.assertEqual(dev.maxfeedinpower, case.expected)


class VebusChargeTest(VebusTestBase):
	# Normal (forced) charge path.
	CHARGE_CASES = [
		Case(
			name="grid only",
			rate=3000,
			expected_max_charge_power=3000 / EFF
		),
		Case(
			name="grid only, consumption",
			consumption=2000,
			rate=3000,
			expected_max_charge_power=3000 / EFF
		),
		Case(
			name="dcpv partial",
			dcpv=1000,
			consumption=500,
			rate=3000,
			expected_max_charge_power=3000 / EFF - 1000 * 0.98
		),
		Case(
			name="dcpv == rate",
			dcpv=3000,
			consumption=500,
			rate=3000,
			expected_max_charge_power=3000 / EFF - 3000 * 0.98
		),
		Case(
			name="dcpv just above rate",
			dcpv=3050,
			consumption=500,
			rate=3000,
			expected_max_charge_power=3000 / EFF - 3050 * 0.98
		),
		Case(
			name="acpv ignored",
			acpv=2000,
			consumption=500,
			rate=3000,
			expected_max_charge_power=3000 / EFF
		),
		Case(
			name="mixed, no feedin",
			dcpv=1000,
			acpv=2000,
			consumption=500,
			rate=3000,
			allow_feedin=False,
			expected_max_charge_power=3000 / EFF - 1000 * 0.98
		),
		Case(
			name="feedin None",
			dcpv=1000,
			consumption=500,
			rate=3000,
			allow_feedin=None,
			expected_max_charge_power=3000 / EFF - 1000 * 0.98
		),
		Case(
			name="floor at 50W",
			dcpv=510,
			rate=500,
			expected_max_charge_power=50
		),
		# restrictions don't influence the VE.Bus charge path
		Case(
			name="grid2bat",
			dcpv=1000,
			acpv=500,
			consumption=300,
			rate=3000,
			restrictions=Restrictions.GRID2BAT,
			expected_max_charge_power=3000 / EFF - 1000 * 0.98
		),
		Case(
			name="bat2grid",
			dcpv=1000,
			acpv=500,
			consumption=300,
			rate=3000,
			restrictions=Restrictions.BAT2GRID,
			expected_max_charge_power=3000 / EFF - 1000 * 0.98
		),
		Case(
			name="both restrictions",
			dcpv=1000,
			acpv=500,
			consumption=300,
			rate=3000,
			restrictions=Restrictions.GRID2BAT | Restrictions.BAT2GRID,
			expected_max_charge_power=3000 / EFF - 1000 * 0.98
		),
	]

	def test_charge_combinations(self):
		for case in self.CHARGE_CASES:
			with self.subTest(case.name):
				dev = self.make_for(case)
				dev.charge(case.flags, case.restrictions, case.rate, case.allow_feedin)
				self.assertFeedin(case.allow_feedin)
				self.assertIsNone(self.w('/Overrides/Setpoint'))
				self.assertEqual(self.w('/Overrides/ForceCharge'), 1)
				self.assertEqual(self.w('/Overrides/MaxDischargePower'), -1.0)
				self.assertExpected(self.w('/Overrides/MaxChargePower'), case.expected_max_charge_power, "MaxChargePower")

	# Requested rate < 98% of DC-PV: enter discharge so the multi exports the excess DC-PV.
	# discharge() gets -rate, so MaxDischargePower = (dcpv - rate) * eff.
	REDIRECT_CASES = [
		Case(
			name="feedin",
			dcpv=4000,
			consumption=500,
			rate=3000,
			expected_max_discharge_power=1000 * EFF,
			expected_setpoint=-MAX_FEEDIN_VALUE
		),
		Case(
			name="zero rate",
			dcpv=2000,
			consumption=500,
			rate=0,
			expected_max_discharge_power=2000 * EFF,
			expected_setpoint=-MAX_FEEDIN_VALUE
		),
		Case(
			name="small dcpv surplus",
			dcpv=3100,
			consumption=500,
			rate=3030,
			expected_max_discharge_power=70 * EFF,
			expected_setpoint=-MAX_FEEDIN_VALUE
		),
		Case(
			name="bat2grid, cons small",
			dcpv=4000,
			consumption=100,
			rate=1000,
			restrictions=Restrictions.BAT2GRID,
			expected_max_discharge_power=3000 * EFF,
			expected_setpoint=-MAX_FEEDIN_VALUE
		),
		Case(
			name="no feedin -> plain ESS",
			dcpv=4000,
			consumption=500,
			rate=3000,
			allow_feedin=False,
			expected_max_discharge_power=-1,
			expected_setpoint=None
		),
	]

	def test_rate_below_dcpv_redirects_to_discharge(self):
		for case in self.REDIRECT_CASES:
			with self.subTest(case.name):
				dev = self.make_for(case)
				dev.charge(case.flags, case.restrictions, case.rate, case.allow_feedin)
				self.assertFeedin(case.allow_feedin)
				self.assertEqual(self.w('/Overrides/ForceCharge'), 0)
				self.assertEqual(self.w('/Overrides/MaxChargePower'), -1.0)
				self.assertExpected(self.w('/Overrides/MaxDischargePower'), case.expected_max_discharge_power, "MaxDischargePower")
				self.assertExpected(self.w('/Overrides/Setpoint'), case.expected_setpoint, "Setpoint")

	def test_fastcharge_without_limits_is_unrestricted(self):
		# Even with rate < dcpv, fast charge must not be redirected to discharge.
		for case in [
			Case(
				name="grid only",
				rate=3000,
				flags=Flags.FASTCHARGE
			),
			Case(
				name="rate below dcpv",
				dcpv=5000,
				rate=1000,
				flags=Flags.FASTCHARGE
			),
			Case(
				name="zero rate",
				dcpv=1000,
				rate=0,
				flags=Flags.FASTCHARGE
			),
		]:
			with self.subTest(case.name):
				dev = self.make_for(case)
				dev.charge(case.flags, case.restrictions, case.rate, case.allow_feedin)
				self.assertEqual(self.w('/Overrides/ForceCharge'), 1)
				self.assertEqual(self.w('/Overrides/MaxChargePower'), -1.0)

	def test_fastcharge_cleared_when_capability_within_limit(self):
		C_BATTERY_CHARGE_LIMIT.current_value = 5.0
		dev = self.make(dcpv=1000, charge_power_capability=4000)
		dev.charge(Flags.FASTCHARGE, Restrictions.NONE, 3000, True)
		self.assertEqual(self.w('/Overrides/MaxChargePower'), -1.0)

	def test_fastcharge_limited_when_capability_exceeds_limit(self):
		C_BATTERY_CHARGE_LIMIT.current_value = 5.0
		dev = self.make(dcpv=1000, charge_power_capability=8000)
		dev.charge(Flags.FASTCHARGE, Restrictions.NONE, 3000, True)
		# Current behaviour: rate = limit / eff, then setrate = rate / eff - dcpv * 0.98.
		rate = 5000 / EFF
		self.assertAlmostEqual(self.w('/Overrides/MaxChargePower'), rate / EFF - 1000 * 0.98, places=3)

	def test_fastcharge_with_unknown_capability_is_unrestricted(self):
		C_BATTERY_CHARGE_LIMIT.current_value = 5.0
		dev = self.make(dcpv=1000, charge_power_capability=None)
		dev.charge(Flags.FASTCHARGE, Restrictions.NONE, 3000, True)
		self.assertEqual(self.w('/Overrides/MaxChargePower'), -1.0)


class VebusDischargeTest(VebusTestBase):
	B2G = Restrictions.BAT2GRID
	CASES = [
		Case(
			name="battery only",
			rate=2000,
			expected_max_discharge_power=2000 * EFF
		),
		Case(
			name="with dcpv",
			dcpv=1000,
			consumption=500,
			rate=2000,
			expected_max_discharge_power=3000 * EFF
		),
		Case(
			name="acpv does not matter",
			acpv=3000,
			consumption=500,
			rate=2000,
			expected_max_discharge_power=2000 * EFF
		),
		Case(
			name="zero rate, dcpv only",
			dcpv=1500,
			consumption=500,
			rate=0,
			expected_max_discharge_power=1500 * EFF
		),
		Case(
			name="rate None, dcpv only",
			dcpv=1500,
			consumption=500,
			rate=None,
			expected_max_discharge_power=1500 * EFF
		),
		Case(
			name="nothing -> 1W",
			consumption=500,
			rate=0,
			expected_max_discharge_power=1.0
		),
		Case(
			name="bat2grid limits to load",
			dcpv=1000,
			consumption=500,
			rate=2000,
			restrictions=B2G,
			expected_max_discharge_power=1500 * EFF + 1.0
		),
		Case(
			name="bat2grid, big load",
			dcpv=1000,
			consumption=5000,
			rate=2000,
			restrictions=B2G,
			expected_max_discharge_power=3000 * EFF
		),
		Case(
			name="bat2grid, acpv ignored",
			acpv=2000,
			consumption=300,
			rate=2000,
			restrictions=B2G,
			expected_max_discharge_power=300 * EFF + 1.0
		),
		Case(
			name="bat2grid, no load",
			rate=2000,
			restrictions=B2G,
			expected_max_discharge_power=1.0
		),
		Case(
			name="bat2grid + grid2bat",
			dcpv=1000,
			consumption=500,
			rate=2000,
			restrictions=B2G | Restrictions.GRID2BAT,
			expected_max_discharge_power=1500 * EFF + 1.0
		),
		Case(
			name="grid2bat only",
			dcpv=1000,
			consumption=500,
			rate=2000,
			restrictions=Restrictions.GRID2BAT,
			expected_max_discharge_power=3000 * EFF
		),
	]

	def test_discharge_combinations(self):
		for case in self.CASES:
			with self.subTest(case.name):
				dev = self.make_for(case)
				self.assertIsNone(dev.discharge(case.flags, case.restrictions, case.rate, case.allow_feedin))
				self.assertFeedin(case.allow_feedin)
				self.assertEqual(self.w('/Overrides/ForceCharge'), 0)
				self.assertEqual(self.w('/Overrides/MaxChargePower'), -1.0)
				self.assertEqual(self.w('/Overrides/Setpoint'), -MAX_FEEDIN_VALUE)
				self.assertExpected(self.w('/Overrides/MaxDischargePower'), case.expected_max_discharge_power, "MaxDischargePower")

	def test_setpoint_follows_feedin_limit(self):
		C_GRID_EXPORT_LIMIT.current_value = 4.0
		dev = self.make(dcpv=1000, consumption=500, local_feedin_limit=6000)
		dev.discharge(Flags.NONE, Restrictions.NONE, 2000, True)
		self.assertEqual(self.w('/Overrides/Setpoint'), -4000)

	def test_fastcharge_flag_unlimited_discharge(self):
		dev = self.make(dcpv=1000, consumption=500)
		dev.discharge(Flags.FASTCHARGE, Restrictions.BAT2GRID, 2000, True)
		self.assertEqual(self.w('/Overrides/MaxDischargePower'), -1)
		self.assertEqual(self.w('/Overrides/Setpoint'), -MAX_FEEDIN_VALUE)

	def test_no_feedin_falls_back_to_plain_ess(self):
		for case in [
			Case(
				name="feedin False",
				dcpv=1000,
				consumption=500,
				rate=2000,
				allow_feedin=False
			),
			Case(
				name="feedin None",
				dcpv=1000,
				consumption=500,
				rate=2000,
				allow_feedin=None
			),
		]:
			with self.subTest(case.name):
				dev = self.make_for(case)
				dev.discharge(case.flags, case.restrictions, case.rate, case.allow_feedin)
				self.assertFeedin(case.allow_feedin)
				self.assertIsNone(self.w('/Overrides/Setpoint'))
				self.assertEqual(self.w('/Overrides/MaxDischargePower'), -1)
				self.assertEqual(self.w('/Overrides/ForceCharge'), 0)


class VebusIdleTest(VebusTestBase):
	CASES = [
		Case(
			name="no pv, feedin",
			consumption=500,
			expected_max_discharge_power=1.0,
			expected_setpoint=-MAX_FEEDIN_VALUE
		),
		Case(
			name="dcpv, feedin",
			dcpv=2000,
			consumption=500,
			expected_max_discharge_power=2000,
			expected_setpoint=-MAX_FEEDIN_VALUE
		),
		Case(
			name="acpv only, feedin",
			acpv=3000,
			consumption=500,
			expected_max_discharge_power=1.0,
			expected_setpoint=-MAX_FEEDIN_VALUE
		),
		Case(
			name="mixed, feedin",
			dcpv=1200,
			acpv=3000,
			consumption=4000,
			expected_max_discharge_power=1200,
			expected_setpoint=-MAX_FEEDIN_VALUE
		),
		Case(
			name="fractional dcpv",
			dcpv=0.4,
			expected_max_discharge_power=1.0,
			expected_setpoint=-MAX_FEEDIN_VALUE
		),
		Case(
			name="no pv, no feedin",
			consumption=500,
			allow_feedin=False,
			expected_max_discharge_power=1.0,
			expected_setpoint=0
		),
		Case(
			name="dcpv, no feedin",
			dcpv=2000,
			consumption=500,
			allow_feedin=False,
			expected_max_discharge_power=2000,
			expected_setpoint=0
		),
		Case(
			name="mixed, no feedin",
			dcpv=1200,
			acpv=3000,
			consumption=4000,
			allow_feedin=False,
			expected_max_discharge_power=1200,
			expected_setpoint=0
		),
		Case(
			name="dcpv, feedin None",
			dcpv=2000,
			consumption=500,
			allow_feedin=None,
			expected_max_discharge_power=2000,
			expected_setpoint=0
		),
	]

	def test_idle_combinations(self):
		for case in self.CASES:
			with self.subTest(case.name):
				dev = self.make_for(case)
				self.assertIsNone(dev.idle(case.allow_feedin))
				self.assertFeedin(case.allow_feedin)
				self.assertEqual(self.w('/Overrides/ForceCharge'), 0)
				self.assertEqual(self.w('/Overrides/MaxChargePower'), -1.0)
				self.assertExpected(self.w('/Overrides/MaxDischargePower'), case.expected_max_discharge_power, "MaxDischargePower")
				self.assertExpected(self.w('/Overrides/Setpoint'), case.expected_setpoint, "Setpoint")

	def test_setpoint_follows_feedin_limit(self):
		dev = self.make(dcpv=2000, local_feedin_limit=2500)
		dev.idle(True)
		self.assertEqual(self.w('/Overrides/Setpoint'), -2500)


class VebusSelfConsumeTest(VebusTestBase):
	G2B = Restrictions.GRID2BAT
	CASES = [
		Case(
			name="unrestricted",
			dcpv=1000,
			acpv=2000,
			consumption=500,
			expected_max_charge_power=-1.0
		),
		Case(
			name="bat2grid only",
			dcpv=1000,
			acpv=2000,
			consumption=500,
			restrictions=Restrictions.BAT2GRID,
			expected_max_charge_power=-1.0
		),
		Case(
			name="grid2bat -> acpv",
			dcpv=1000,
			acpv=2000,
			consumption=500,
			restrictions=G2B,
			expected_max_charge_power=2000
		),
		Case(
			name="grid2bat, acpv split",
			acpv=1800,
			consumption=500,
			restrictions=G2B,
			acpv_split=True,
			expected_max_charge_power=1800
		),
		Case(
			name="grid2bat, no acpv -> 50W",
			dcpv=3000,
			consumption=500,
			restrictions=G2B,
			expected_max_charge_power=50
		),
		Case(
			name="grid2bat, tiny acpv -> 50W",
			acpv=20,
			restrictions=G2B,
			expected_max_charge_power=50
		),
		Case(
			name="both restrictions",
			dcpv=500,
			acpv=4000,
			consumption=1000,
			restrictions=G2B | Restrictions.BAT2GRID,
			expected_max_charge_power=4000
		),
	]

	def test_self_consume_combinations(self):
		for case in self.CASES:
			for feedin in (True, False, None):
				with self.subTest(case.name, allow_feedin=feedin):
					dev = self.make_for(case)
					self.assertIsNone(dev.self_consume(case.restrictions, feedin))
					self.assertFeedin(feedin)
					self.assertIsNone(self.w('/Overrides/Setpoint'))
					self.assertEqual(self.w('/Overrides/ForceCharge'), 0)
					self.assertEqual(self.w('/Overrides/MaxDischargePower'), -1.0)
					self.assertExpected(self.w('/Overrides/MaxChargePower'), case.expected_max_charge_power, "MaxChargePower")


if __name__ == "__main__":
	unittest.main()
