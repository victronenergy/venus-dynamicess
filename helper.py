import os
import sys
import logging
import json
import asyncio

from enum import Enum, IntFlag
from typing import Any, Awaitable, Callable, Dict

#Victron packages and dbus
from constants import ACSYSTEM_SERVICE, BATTERY_SERVICE, EVCHARGER_SERVICE, HUB4_SERVICE, SOLARCHARGER_SERVICE, SYSTEM_SERVICE, VEBUS_SERVICE
from aiovelib.service import Service, IntegerItem, TextItem
from aiovelib.localsettings import Setting
from aiovelib.client import Monitor, Service as ObservableService, servicetype
from aiovelib.localsettings import SettingsService, SETTINGS_SERVICE

try:
	from dbus_fast import Message, MessageType, Variant
except ImportError:
	from dbus_next import Message, MessageType, Variant

from s2python.common import PowerRange, CommodityQuantity

class SettingsMonitor(Monitor):
	""" Monitor for the settings service. """
	def __init__(self, bus, itemsChanged=None, **kwargs):
		super().__init__(bus, itemsChanged=itemsChanged, handlers = {
			'com.victronenergy.settings': SettingsService
		}, **kwargs)

delta_logs = {}
def log_on_delta(level:int, key:str, message:str, tenant:str="dess"):
	'''
		Logs a recurringly generated message only if the content changes to avoid spaming
		log files, but leave a note about an important warning/info.
	'''
	key = f"{tenant}_{key}"

	if message is None:
		if key in delta_logs:
			del delta_logs[key]
		return

	if key not in delta_logs:
		delta_logs[key] = message
		logger.log(level, message)
	else:
		if delta_logs[key] != message:
			delta_logs[key] = message
			logger.log(level, message)

def version_str_to_tuple(version_str):
	""" Converts a version string to a tuple of integers for easy comparison. """
	return tuple(map(int, (version_str.split("."))))

async def wait_for_settings(bus, itemsChanged=None) -> SettingsService:
	""" Attempt a connection to localsettings. """
	settingsmonitor = await SettingsMonitor.create(bus, itemsChanged=itemsChanged)
	""" Attempt a connection to localsettings. If it does not show
			up within 5 seconds, return None. """
	try:
		return await asyncio.wait_for(
			settingsmonitor.wait_for_service(SETTINGS_SERVICE), 5)
	except TimeoutError:
		pass

	return None

class Hub4Service(ObservableService):
    servicetype = HUB4_SERVICE
    paths = [
		'/Overrides/ForceCharge',
		'/Overrides/MaxDischargePower',
		'/Overrides/MaxChargePower',
		'/Overrides/Setpoint',
		'/Overrides/FeedInExcess'
	]

class AcSystemService(ObservableService):
    servicetype = ACSYSTEM_SERVICE
    paths = [
		'/Connected',
		'/Capabilities/HasDynamicEssSupport',
		'/Ess/AcPowerSetpoint',
		'/Ess/InverterPowerSetpoint',
		'/Ess/UseInverterPowerSetpoint',
		'/Ess/DisableCharge',
		'/Ess/DisableDischarge',
		'/Ess/DisableFeedIn',
		'/Settings/Ess/Mode',
		'/Mode',
		'/Settings/Ess/MinimumSocLimit'
	]

class VEBusService(ObservableService):
    servicetype = VEBUS_SERVICE
    paths = [
		'/Connected',
		'/Hub4/AssistantId',
	]

class SystemService(ObservableService):
	servicetype = SYSTEM_SERVICE
	paths = [
		#FIXME: Needs all paths no longer part of the own service.
		'/Control/ActiveSocLimit',
		'/DynamicEss/ChargeControlAcquired',
		'/Control/EssState',
		'/SystemState/LowSoc',
		'/Ac/PvOnGrid/L1/Power',
		'/Ac/PvOnGrid/L2/Power',
		'/Ac/PvOnGrid/L3/Power',
		'/Ac/PvOnOutput/L1/Power',
		'/Ac/PvOnOutput/L2/Power',
		'/Ac/PvOnOutput/L3/Power',
		'/Dc/Pv/Power',
		'/Dc/Battery/Soc',
		'/Ac/Consumption/L1/Power',
		'/Ac/Consumption/L2/Power',
		'/Ac/Consumption/L3/Power'
	]

class OtherSettingsService(ObservableService):
	servicetype = SETTINGS_SERVICE
	paths = [
		'/Settings/CGwacs/Hub4Mode',
		'/Settings/CGwacs/MaxFeedInPower',
		'/Settings/CGwacs/PreventFeedback'
	]

class SolarchargerService(ObservableService):
	servicetype = SOLARCHARGER_SERVICE
	paths = [
		'/Yield/Power'
	]

class EvChargerService(ObservableService):
	servicetype = EVCHARGER_SERVICE
	paths = [
		'/StartStop',
		'/SetCurrent',
		'/Status',
		'/Mode',
		'/Ac/L1/Power',
		'/Ac/L3/Power'
	]

class BatteryService(ObservableService):
	servicetype = BATTERY_SERVICE
	paths = [
		'/Info/MaxChargeCurrent',
		'/Info/MaxChargeVoltage'
	]

class AioMonitor(Monitor):
	""" Monitor for various services we have to monitor. """
	def __init__(self,
			bus,
			on_service_added: Callable[[str, int, ObservableService], Awaitable[None]] = None,
			on_service_removed: Callable[[str, int, ObservableService], Awaitable[None]] = None,
			on_value_changed: Callable[[str, any, ObservableService], Awaitable[None]] = None,
			**kwargs):

		handlers = {
			SystemService.servicetype: SystemService,
			AcSystemService.servicetype: AcSystemService,
			VEBusService.servicetype: VEBusService,
			Hub4Service.servicetype: Hub4Service,
			OtherSettingsService.servicetype: OtherSettingsService,
			SolarchargerService.servicetype: SolarchargerService,
			EvChargerService.servicetype: EvChargerService
		}

		for k, v in handlers.items():
			if v.paths is not None:
				if '/DeviceInstance' not in v.paths:
					v.paths.append('/DeviceInstance')

		super().__init__(bus, handlers=handlers, **kwargs)

		self._on_value_changed = on_value_changed
		self._on_service_added = on_service_added
		self._on_service_removed = on_service_removed
		self._instance_id_by_name_map = {}
		self.service_lookup: Dict[str, Dict[int, ObservableService]] = {}

		#handlers for additional messages we define.
		self.bus.add_message_handler(self._handle_custom_handler)
		self._message_handlers: dict[tuple[str, str, str, str], list[Callable[[Message], Awaitable[None]]]] = {}

	@classmethod
	async def create(cls, bus, on_service_added: Callable[[str, int, ObservableService], Awaitable[None]] = None,
		on_service_removed: Callable[[str, int, ObservableService], Awaitable[None]] = None,
		on_value_changed: Callable[[str, any, ObservableService], Awaitable[None]] = None, **kwargs):
		return await Monitor.create.__func__(cls, bus, on_service_added=on_service_added,
			on_service_removed=on_service_removed, on_value_changed=on_value_changed, **kwargs)

	def itemsChanged(self, service:ObservableService, values):
		#We need to call the async callback _on_value_changed if present.
		#So, we have to raise a async io task. there may be multiple values
		#passed, hence we wrap that to have only 1 task creation and can await the handler within that.
		#so that the number of tasks created is smaller than number of values changed.
		if self._on_value_changed:
			asyncio.create_task(self._itemsChangedAsync(service, values))

	async def _itemsChangedAsync(self, service:ObservableService, values):
		for path, value in values.items():
			await self._on_value_changed(path, value, service)

	async def serviceAdded(self, service:ObservableService):
		""" Default method, called when service is added. """
		serviceName = service.name
		serviceType = servicetype(serviceName)
		if serviceName == 'com.victronenergy.settings' or serviceName == 'com.victronenergy.platform'  or serviceName == 'com.victronenergy.system':
			di = 0
		elif serviceName.startswith('com.victronenergy.vecan.'):
			di = 0
		else:
			try:
				di = service.get_value('/DeviceInstance')
				if di is None:
					raise KeyError()
			except KeyError:
				logger.debug("%s was skipped because it has no device instance" % serviceName)
				return None
			else:
				di = int(di)

		self._instance_id_by_name_map[service.name] = di

		#keep track of each service instance per type and instance id.
		if serviceType not in self.service_lookup:
			self.service_lookup[serviceType] = {}

		self.service_lookup[serviceType][di] = service

		logger.debug("Service added: {}#{}: {}".format(servicetype(service.name), di, service))

		if self._on_service_added:
			await self._on_service_added(service.name, di, service)

	async def serviceRemoved(self, service:ObservableService):
		""" called when service is removed. """
		di = self._instance_id_by_name_map.get(service.name, 0)
		logger.debug("Service removed: {}#{}: {}".format(servicetype(service.name), di, service))
		if self._on_service_removed:
			await self._on_service_removed(service.name, di, service)

		#remove from our internal tracking as well.
		del self._instance_id_by_name_map[service.name]
		del self.service_lookup[servicetype(service.name)][di]

	async def add_message_handler(self, handler: Callable[[Message], Awaitable[None]],interface:str, signal_name:str, path:str, sender_id:str=None):
		""" Adds a message handler for the given interface, signal name, sender and path. """
		self._message_handlers[(interface, signal_name, path, sender_id)] = handler
		await self.add_match(interface=interface, member=signal_name, path=path, sender=sender_id)

	async def remove_message_handler(self, interface:str, signal_name:str, path:str, sender_id:str=None):
		""" Removes the message handler for the given interface, signal name, sender and path. """

		if (interface, signal_name, path, sender_id) in self._message_handlers:
			del self._message_handlers[(interface, signal_name, path, sender_id)]
			await self.remove_match(interface=interface, member=signal_name, path=path, sender=sender_id)

	def _handle_custom_handler(self, message:Message):
		""" Internal method to handle custom handlers. """
		key = (message.interface, message.member, message.path, message.sender)
		handler = self._message_handlers.get(key, None)
		if handler is not None:
			asyncio.create_task(handler(message))

class Configurable():
	def __init__(self, system_path:str, settings_path:str, settings_key:str, default_value, min_value, max_value, configurables, decode_payload=False):
		self._system_path = system_path
		self._settings_path = settings_path
		self._settings_key = settings_key
		self._default_value = default_value
		self._current_value = default_value #init to default
		self._min_value = min_value
		self._max_value = max_value
		self._decode_payload = decode_payload
		configurables.append(self)

	@property
	def system_path(self) -> str:
		return self._system_path

	@property
	def settings_path(self) -> str:
		return self._settings_path

	@property
	def settings_key(self) -> str:
		return self._settings_key

	@property
	def default_value(self):
		return self._default_value

	@property
	def min_value(self):
		return self._min_value

	@property
	def max_value(self):
		return self._max_value

	@property
	def current_value(self):
		return self._current_value

	@current_value.setter
	def current_value(self, v):
		if self._decode_payload:
			self._current_value = json.loads(v)
		else:
			self._current_value=v

	async def force_write_to_settings(self, settings_service:SettingsService):
		"""
			forces a rewrite of current value to settings.
		"""
		if self._decode_payload:
			return await settings_service.set_value(self.settings_path, json.dumps(self.current_value))
		else:
			return await settings_service.set_value(self.settings_path, self.current_value)

from globals import logger
