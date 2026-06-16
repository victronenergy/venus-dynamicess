
from typing import TYPE_CHECKING
from aiovelib.client import Service as ObservableService
from constants import SYSTEM_SERVICE
from globals import ErrorCode, Restrictions
from helper import AioMonitor

#avoid circlular import during runtime.
if TYPE_CHECKING:
	from dynamicess import DynamicEss

class EssDevice(object):
	def __init__(self, dynamic_ess:'DynamicEss', monitor:AioMonitor, service:ObservableService):
		self._dynamic_ess:'DynamicEss' = dynamic_ess
		self._aiomonitor:AioMonitor = monitor
		self.service:ObservableService = service

	@property
	def connected(self):
		return self.service.get_value("/Connected") == 1

	@property
	def device_instance(self):
		""" Returns the DeviceInstance of this device. """
		return self.service.get_value("/DeviceInstance")

	@property
	def available(self):
		return False

	@property
	def has_ess_assistant(self):
		return False

	def check_conditions(self) -> ErrorCode:
		""" Check that the conditions are right to use this device. If not,
		    return a non-zero error code. """
		return ErrorCode.NO_ERROR

	def charge(self, flags, restrictions:Restrictions, rate, allow_feedin):
		raise NotImplementedError("charge")

	def discharge(self, flags, restrictions:Restrictions, rate, allow_feedin):
		raise NotImplementedError("discharge")

	def idle(self, allow_feedin):
		raise NotImplementedError("idle")

	def self_consume(self, restrictions:Restrictions, allow_feedin):
		raise NotImplementedError("self_consume")

	def deactivate(self):
		raise NotImplementedError("deactivate")

	@property
	def acpv(self):
		return (self._aiomonitor.get_value(SYSTEM_SERVICE, '/Ac/PvOnGrid/L1/Power') or 0) + \
			(self._aiomonitor.get_value(SYSTEM_SERVICE, '/Ac/PvOnGrid/L2/Power') or 0) + \
			(self._aiomonitor.get_value(SYSTEM_SERVICE, '/Ac/PvOnGrid/L3/Power') or 0) + \
			(self._aiomonitor.get_value(SYSTEM_SERVICE, '/Ac/PvOnOutput/L1/Power') or 0) + \
			(self._aiomonitor.get_value(SYSTEM_SERVICE, '/Ac/PvOnOutput/L2/Power') or 0) + \
			(self._aiomonitor.get_value(SYSTEM_SERVICE, '/Ac/PvOnOutput/L3/Power') or 0)

	@property
	def pvpower(self):
		return self._aiomonitor.get_value(SYSTEM_SERVICE, '/Dc/Pv/Power') or 0

	@property
	def external_pvpower(self):
		power = 0
		for service in self._dynamic_ess._external_solarcharger_services:
			power += self._aiomonitor.get_value(service, '/Yield/Power') or 0
		return power

	@property
	def consumption(self):
		return max(0, (self._aiomonitor.get_value(SYSTEM_SERVICE, '/Ac/Consumption/L1/Power') or 0) +
			(self._aiomonitor.get_value(SYSTEM_SERVICE, '/Ac/Consumption/L2/Power') or 0) +
			(self._aiomonitor.get_value(SYSTEM_SERVICE, '/Ac/Consumption/L3/Power') or 0))
