#!/usr/bin/python3 -u
# -*- coding: utf-8 -*-

#core imports
from ast import If
import enum
from functools import partial
import sys
import json
import os
import argparse
import logging
import signal
import asyncio
import dbus #type:ignore

from datetime import datetime, timedelta, timezone
from time import time
from typing import Dict, Optional, Tuple, cast, Callable

#aiovelib
from aiovelib.client import Service as ObservableService, DbusException, servicetype
from aiovelib.service import Item, Service, IntegerItem, TextItem, DoubleItem
from aiovelib.localsettings import SETTINGS_SERVICE, Setting, SettingsService

try:
	from dbus_fast.constants import BusType
except ImportError:
	from dbus_next.constants import BusType
try:
	from dbus_fast.aio import MessageBus
except ImportError:
	from dbus_next.aio import MessageBus

#s2
from s2python.common import (
	ControlType
)

#internals
from constants import (
	CONNECTION_RETRY_INTERVAL_MS,
	S2_IFACE,
	SYSTEM_SERVICE,
	ACSYSTEM_SERVICE,
	CONTROL_LOOP_INTERVAL,
	ERROR_TIMEOUT,
	S2_CLIENT_ID,
	NUM_SCHEDULES,
	TRANSITION_STATE_THRESHOLD,
)

from helper import (
	AioMonitor,
	SettingsMonitor,
	Configurable,
	version_str_to_tuple,
	wait_for_settings,
	log_on_delta
)

from ess_device import EssDevice
from dynamicess_window import DynamicEssWindow
from iteration_change_tracker import IterationChangeTracker
from evcs_delegate import EVCSDelegate
from multirs_device import MultiRsDevice
from vebus_device import VebusDevice

from globals import (
	BRANCH,
	C_BATTERY_CAPACITY,
	C_BATTERY_CHARGE_LIMIT,
	C_BATTERY_DISCHARGE_LIMIT,
	C_DISABLE_EVCS_CONTROL,
	C_EFFICIENCY,
	C_LAST_RUN_VERSION,
	C_OPERATING_MODE,
	C_ENABLE_DEBUG_LOGGING,
	Mode,
	Capabilities,
	ChangeIndicator,
	ErrorCode,
	EvcsGxFlags,
	Flags,
	OperatingMode,
	ReactiveStrategy,
	CHARGE_STATES,
	SELFCONSUME_STATES,
	IDLE_STATES,
	DISCHARGE_STATES,
	ERROR_SELFCONSUME_STATES,
	Restrictions,
	Restrictions,
	Strategy,
	logger,
	VERSION,
	CONFIGURABLES,
)

class DynamicEss():
	_get_time: Callable[..., datetime] = datetime.now

	def __init__(self, bus_type:BusType):
		self._bus_type = bus_type
		self.prevsoc_cr_calc = None
		self._external_solarcharger_services = []
		self.chargerate = None # Chargerate based on tsoc. Always to be set to DynamicEss/ChargeRate, even if an override is used.
		self.override_chargerate = None # chargerate if calculation based on tsco is overwritten.
		self._controlloop_task: asyncio.Task = None
		self._idle_setpoint_update_task: asyncio.Task = None
		self._devices = {}
		self._device:EssDevice = None
		self._errorcode = 0
		self.iteration_change_tracker = IterationChangeTracker(self)
		self._is_idle = False #Flag indicating if we are currently idling, resulting in a quick-update of the idle-setpoint upon value change.
		self._idle_feedin = None #Cache the feedin-allowance of the window during idle, to quickly update the idle setpoint upon value changes.
		self._is_pv_disabled = False #Flag indicating if PV is currently disabled.
		self._last_unprocessed_schedule_change = None #flag to see if we have to find new "last_schedule"

		self._evcs_delegates:dict[str, EVCSDelegate] = {}

	async def init(self):
		'''
			Async initializer. To be called after object creation.
		'''
		sigterm = getattr(signal, "SIGTERM", None)
		if sigterm is not None:
			asyncio.get_running_loop().add_signal_handler(sigterm, lambda: asyncio.create_task(self._on_sigterm()))

		self._bus = await MessageBus(bus_type=self._bus_type).connect()

		#during init of aiomonitor we need to defer scanning added devices.
		#TODO: Check, if we need deferred scans ? acsystem probably.
		self._deferred_service_scans = []

		#Create Settings device and dbus service
		self.settings_service:SettingsService = await self._init_settings()

		#initialize configurables with eventually stored settings.
		for c in CONFIGURABLES:
			try:
				v = self.settings_service.get_value(c.settings_path)
				if v is not None:
					logger.debug("Initializing configurable {} with value from settings: {}".format(c.settings_key, v))
					c.current_value = v

			except Exception as ex:
				logger.error("Ex", exc_info=ex)
				logger.warning("Couldn't load setting for Configurable {}:{}; Fine if not yet persisted something.".format(c.settings_key, c.settings_path))

		#debug logging through setting?
		if C_ENABLE_DEBUG_LOGGING.current_value:
			logger.setLevel(logging.DEBUG)
			logger.debug("Debug logging enabled via settings.")

		self._dbusservice = await self._init_dbus_service()

		self._aiomonitor = None #required, so we can detect the need for defered scans during discovery.
		self._aiomonitor:AioMonitor = await self._init_aiomonitor()

		#run defered scans now.
		for name, instance, service in self._deferred_service_scans:
			await self._on_service_added(name, instance, service)

		#No need to check for enabled or disabled, service will only be started,
		#if it is (and can be) enabled
		await self._enable()

		#determine current window end. Have to iterate all windows 1 time,
		#as we would to allow undetermined order and undetermined window length.
		await self._publish_last_window_start_end()

		#init done, perform post-startup-operations, if any.
		await self._post_startup()



	async def _init_settings(self) -> SettingsService:
		"""
			Creates the settings monitor and settings service, and registers all configurables.
		"""
		settings_service:SettingsService = await wait_for_settings(self._bus, self._on_settings_changed)

		for c in CONFIGURABLES:
			await settings_service.add_settings(Setting(c.settings_path, c.default_value, c.min_value, c.max_value, False, c.settings_key))

		#We also need settings for the 48 schedule windows. They are not configurables,
		#because they are only used/accessed in a iterative way.
		for i in range(NUM_SCHEDULES):
			for suffix, path, default, minv, maxv in [
				("start", "Start", 0, 0, 2**31-1),
				("duration", "Duration", 0, 0, 2**31-1),
				("targetsoc", "TargetSoc", 0.0, 0.0, 100.0),
				("soc", "Soc", 0, 0, 100),
				("allowgridfeedin", "AllowGridFeedIn", 0, 0, 1),
				("restrictions", "Restrictions", 0, 0, sum(res.value for res in Restrictions)),
				("strategy", "Strategy", 0, 0, max(strategy.value for strategy in Strategy)),
				("flags", "Flags", 0, 0, sum(flag.value for flag in Flags)),
				("toev", "ToEVBattery", "{}", None, None),
			]:
				await settings_service.add_settings(Setting(
					path="/Settings/DynamicEss/Schedule/{}/{}".format(i, path),
					default=default,
					_min=minv,
					_max=maxv,
					silent=False,
					alias="dess_{}_{}".format(suffix, i)
				))

		return settings_service

	async def _init_aiomonitor(self):
		logger.debug("Initializing AioMonitor...")

		monitor = await AioMonitor.create(
			self._bus,
			on_service_added=self._on_service_added,
			on_service_removed=self._on_service_removed,
		)

		logger.debug("AioMonitor initialized.")

		return monitor

	async def _init_dbus_service(self):
		dbusservice = Service(self._bus, 'com.victronenergy.dynamicess')

		dbusservice.add_item(TextItem("/Mgmt/ProcessName", "dynamicess.py"))
		dbusservice.add_item(TextItem("/Mgmt/ProcessVersion", f"{VERSION}{('-' + BRANCH) if BRANCH else ''}"))
		dbusservice.add_item(IntegerItem("/DeviceInstance", 0))
		dbusservice.add_item(TextItem("/ProductName", "DynamicEss"))

		#Output Paths we use.
		dbusservice.add_item(IntegerItem('/Capabilities', value=sum(c.value for c in Capabilities)))
		dbusservice.add_item(IntegerItem('/NumberOfSchedules', value=NUM_SCHEDULES))
		dbusservice.add_item(IntegerItem('/Active', value=0, text=lambda v: Mode(v).name if v in Mode._value2member_map_ else 'Unknown'))
		dbusservice.add_item(DoubleItem('/TargetSoc', value=0.0, text=lambda v: '{}%'.format(v)))
		dbusservice.add_item(DoubleItem('/WindowSoc', value=0.0, text=lambda v: '{}%'.format(v)))
		dbusservice.add_item(DoubleItem('/MinimumSoc', value=None, text=lambda v: '{}%'.format(v)))
		dbusservice.add_item(IntegerItem('/ErrorCode', value=0, text=lambda v: ErrorCode(v).name if v in ErrorCode._value2member_map_ else 'Unknown'))
		dbusservice.add_item(IntegerItem('/LastScheduledStart', value=None, text=lambda v: '{}'.format(datetime.fromtimestamp(v).strftime('%Y-%m-%d %H:%M:%S'))))
		dbusservice.add_item(IntegerItem('/LastScheduledEnd', value=None, text=lambda v: '{}'.format(datetime.fromtimestamp(v).strftime('%Y-%m-%d %H:%M:%S'))))
		dbusservice.add_item(DoubleItem('/ChargeRate', value=0, text=lambda v: '{}W'.format(v)))
		dbusservice.add_item(IntegerItem('/WindowSlot', value=0))
		dbusservice.add_item(IntegerItem('/Strategy', value=None, text=lambda v: Strategy(v).name))
		dbusservice.add_item(IntegerItem('/Ready', value=0, text=lambda v: 'Ready' if v else 'Not Ready'))
		dbusservice.add_item(IntegerItem('/WorkingSocPrecision', value=0))
		dbusservice.add_item(IntegerItem('/ReactiveStrategy', value=None, text=lambda v: ReactiveStrategy(v).name if v in ReactiveStrategy._value2member_map_ else 'Unknown'))
		dbusservice.add_item(IntegerItem('/Restrictions', value=None, text=lambda v: '{}'.format(Restrictions(v).name)))
		dbusservice.add_item(IntegerItem('/AllowGridFeedIn', value=None))
		dbusservice.add_item(IntegerItem('/Flags', value=None, text=lambda v: '{}'.format(Flags(v).name)))
		dbusservice.add_item(DoubleItem('/AvailableOverhead', value=None, text=lambda v: '{}W'.format(v)))
		dbusservice.add_item(DoubleItem('/ChargeHysteresis', value=0, text=lambda v: '{}%'.format(v)))
		dbusservice.add_item(DoubleItem('/DischargeHysteresis', value=0, text=lambda v: '{}%'.format(v)))
		dbusservice.add_item(TextItem('/WindowToEVBattery', value="{}"))
		dbusservice.add_item(TextItem('/EvcsGxFlags', value="{}")) #channel to communicate flags TO vrm. Inbound is a setting.

		#FIXME: Alarms DESS may generate. (long overdue)
		#dbusservice.add_item(IntegerItem('/Alarms/IncompatibleSystem', value=0))

		#Configurables may produce a Output/Input Path as well. Configurables are writable as per definition.
		#(For legacy backwards compatibility reasons, dynamic_ess does not yet use settings path in it's own settings
		#but relys on the paths in settings device. So, this is not needed, but left here cor completness)
		for c in CONFIGURABLES:
			if c.system_path is not None:
				#use partial to also provide the configurable to the generic callback, so we can decide which value changed.
				if isinstance(c.default_value, int) or isinstance(c.default_value, bool):
					dbusservice.add_item(IntegerItem(c.system_path, value=c.current_value or c.default_value, writeable=True, onchange=partial(self._on_dbus_own_value_changed, configurable=c)))
				elif isinstance(c.default_value, float):
					dbusservice.add_item(DoubleItem(c.system_path, value=c.current_value or c.default_value, writeable=True, onchange=partial(self._on_dbus_own_value_changed, configurable=c)))
				else:
					dbusservice.add_item(TextItem(c.system_path, value=c.current_value or c.default_value, writeable=True, onchange=partial(self._on_dbus_own_value_changed, configurable=c)))

		#done, register service.
		await dbusservice.register()
		return dbusservice

	async def _run_controlloop_timer(self):
		'''Async wrapper for control loop timer'''
		try:
			while True:
				await asyncio.sleep(CONTROL_LOOP_INTERVAL)
				result = await self._on_controlloop_timer()

				if not result:
					break
		except asyncio.CancelledError:
			logger.debug("Control loop timer cancelled")

	async def _run_idle_setpoint_update_timer(self):
		'''Async wrapper for idle setpoint update timer'''
		try:
			while True:
				await asyncio.sleep(1)
				result = await self._on_idle_setpoint_update_timer()

				if not result:
					break
		except asyncio.CancelledError:
			logger.debug("Idle setpoint update timer cancelled")

	async def _run_last_window_publish_timer(self):
		'''Async wrapper for last window publish timer'''
		try:
			while True:
				await asyncio.sleep(5) #every 5 seconds is enough.
				result = await self._on_last_window_publish_timer()

				if not result:
					break
		except asyncio.CancelledError:
			logger.debug("Last window publish timer cancelled")

	async def _on_dbus_own_value_changed(self, item:Item, value, configurable:Configurable):
		"""
			Callback, if one of our writeable service paths was changed.
		"""
		try:
			#generic configurable handling
			logger.debug("dbus-change on {} detected: {}".format(item.path, value))
			configurable.current_value = value
			#required cause we modified the internal "current_value" of the configurable.
			await configurable.force_write_to_settings(self.settings_service)

			#accept the change
			item.set_local_value(value)
		except Exception as e:
			logger.error("Error during setting change. Rejecting.", exc_info=e)

	async def _on_service_added(self, name:str, instance:int, service:ObservableService):
		#check, if we need to defer scanning.
		if self._aiomonitor is None:
			logger.debug("Deferring scan of {}#{} as aiomonitor is not yet initialized.".format(name, instance))
			self._deferred_service_scans.append((name, instance, service))
			return

		#Device detection.
		if name.startswith('com.victronenergy.vebus.'):
			logger.info("Registering Vebus #{} on {} for charge control.".format(instance, service))
			self._device = VebusDevice(self, self._aiomonitor, service)
		elif name.startswith('com.victronenergy.acsystem.'):
			logger.info("Registering AC System #{} on {} for charge control.".format(instance, service))
			self._device = MultiRsDevice(self, self._aiomonitor, service)
		elif name.startswith('com.victronenergy.solarcharger.'):
			logger.info("Registering Solar Charger #{} on {} for feed-in control.".format(instance, service))
			self._external_solarcharger_services.append(service)
		elif name.startswith('com.victronenergy.evcharger.'):
			logger.info("Registering EV Charger #{} on {} for ev control.".format(instance, service))
			evcs_disabled = C_DISABLE_EVCS_CONTROL.current_value == 1
			if str(instance) not in self._evcs_delegates.keys():
				delegate = EVCSDelegate(service, instance, self._aiomonitor, self, evcs_disabled)
				self._evcs_delegates[str(instance)] = delegate

				#connect in 10 seconds, to give dbus-mqtt enough time to sort s2 with the evcs after a restart.
				await asyncio.sleep(10)
				await self._evcs_delegates[str(instance)].begin(evcs_disabled)

	async def _on_service_removed(self, name:str, instance:int, service:ObservableService):
		if service in self._external_solarcharger_services:
			self._external_solarcharger_services.remove(service)
		elif name.startswith('com.victronenergy.evcharger.'):
			if str(instance) in self._evcs_delegates.keys():
				await self._evcs_delegates[str(instance)].end(False, True)
				#we can drop this RMS, as the service is gone now. If it comes back, it will be a new instance with new RM.
				del self._evcs_delegates[str(instance)]
				logger.info("EV Charger #{} on {} removed from ev control.".format(instance, service))
				await self.publish_evcs_flags()

		try:
			del self._devices[service]
		except KeyError:
			pass
		else:
			self._set_device()

	def _on_settings_changed(self, service, values):

		for setting, newvalue in values.items():
			#for the schedule settings, we are only interested in tracking the last window start/stop.
			if "/Schedule/" in setting:
				self._last_unprocessed_schedule_change = self._get_time() #flag for checking
			else:
				#configurable?
				for c in CONFIGURABLES:
					if c.settings_path == setting:

						if c == C_ENABLE_DEBUG_LOGGING:
							if newvalue:
								logger.setLevel(logging.DEBUG)
								logger.debug("Debug logging enabled via settings.")
							else:
								logger.setLevel(logging.INFO)
								logger.info("Debug logging disabled via settings.")

						if  c.current_value != newvalue:
							#omit logging for paths containing /Schedule
							if "/Schedule/" not in setting:
								logger.debug("Internal change on {} detected: {}".format(setting, newvalue))

							c.current_value = newvalue

							if c.system_path is not None:
								if self._dbusservice.get_item(c.system_path).value != newvalue:
									self._dbusservice.get_item(c.system_path).set_local_value(newvalue)

							break

		#accept change
		return True

	async def _on_sigterm(self):
		"""
			Controlled shutdown.
		"""
		logger.info("SIGTERM received, shutting down gracefully...")

		#enable pv, if it was disabled.
		await self._disable_pv(False) #make sure PV is enabled again, so it can be used by other services or after restart of DESS.

		#restore default ESS mode
		await self.pause(ErrorCode.NO_ERROR)

		#disconnect all active EVCS.
		for evcs in self._evcs_delegates.values():
			if EvcsGxFlags.GX_AUTO_ACQUIRED in evcs.gx_flags:
				await evcs.end(True)

		await self.publish_evcs_flags() #publish once after all are disconnected.

		#done, byebye.
		asyncio.get_running_loop().stop()

	async def _on_idle_setpoint_update_timer(self):
		# during idling, update the setpoint once per second.
		if self.active and self._device is not None and self._is_idle:
			self._device.idle(self._idle_feedin)

		return True #keep timer running.

	async def _on_last_window_publish_timer(self):
		#every 5 seconds, recalculate the last window start and end, to keep it up to date in case of any changes.
		#we only need to do this, if there is a schedule change incoming.
		if (self._last_unprocessed_schedule_change is not None) and (self._last_unprocessed_schedule_change + timedelta(seconds=5) < self._get_time()):
			self._last_unprocessed_schedule_change = None
			logger.debug("Schedule change detected, updating last window start and end on dbus.")
			await self._publish_last_window_start_end()

		return True #keep timer running.

	async def _on_controlloop_timer(self):
		try:
			error_code = await self.check_conditions()
			await self.handle_error_code(error_code)

			if error_code != ErrorCode.NO_ERROR:
				return True #skip rest of loop, but keep timer running, so we can recover once error condition is gone.

			if self._aiomonitor.get_value(SYSTEM_SERVICE, '/DynamicEss/ChargeControlAcquired') != 1:
				#We don't have the charge control token, so we can't do anything. Wait for next loop to check again.
				log_on_delta(logging.WARNING, 'ChargeControl', "Charge control not acquired. Waiting for acquisition ...")
				return True

			log_on_delta(logging.INFO, 'ChargeControl', "Charge control acquired. Proceeding with control.")

			now = self._get_time()
			start = None
			stop = None
			self._is_idle = False
			self._idle_feedin = None

			#Whenever an error occurs that is totally unexpected, the delegate
			#should enter self consume and not die.(try/catch around the control loop logic)

			final_strategy = ReactiveStrategy.NO_WINDOW
			current_window = None
			next_window = None

			# This is the ESS minsoc of the selected device
			self._dbusservice.get_item('/MinimumSoc').set_local_value(None if self._device is None else self._device.minsoc)

			#iterate through windows, find the current one. Usually it should be first,
			#but in case of update issues may not. Also grab the next window, to perform
			#some "look aheads" for optimizations.
			for w in self.windows():
				if now in w:
					self.active = 1 # Auto
					current_window = w

					self._dbusservice.get_item('/Strategy').set_local_value(w.strategy)
					self._dbusservice.get_item('/Restrictions').set_local_value(w.restrictions)
					self._dbusservice.get_item('/AllowGridFeedIn').set_local_value(int(w.allow_feedin))
					break # out of for loop

			if current_window is not None:
				#found current window, now we need nextWindow to do some look aheads as well.
				#next window is the one containing current.start + current.duration + 1.
				#finding next window is not required to enter the control loop, can be None.
				next_window_save_start = current_window.stop + timedelta(seconds = 1)
				for w in self.windows():
					if (next_window_save_start in w):
						next_window = w
						break # out of for loop

				# validate solar-system state
				await self._disable_pv(Flags.DISABLEPV in current_window.flags)

				#determine final strategy to use.
				final_strategy = await self._determine_reactive_strategy(current_window, next_window, current_window.restrictions, now)

				self._dbusservice.get_item('/ChargeRate').set_local_value(self.chargerate or 0) #Always set the anticipated chargerate on dbus.

				#check EV instructions, if any.
				for evcs in self._evcs_delegates.values():
					await evcs.loop(current_window, now)

				#Update EVCS Flags on dbus.
				await self.publish_evcs_flags()
			else:
				# No matching windows
				await self.pause(ErrorCode.NO_WINDOW)

			#write out current override strategy to determine if the local system behaves "out of schedule" on purpose.
			if self._aiomonitor.get_value(SYSTEM_SERVICE, "/SystemState/LowSoc") == 1:
				final_strategy= ReactiveStrategy.ESS_LOW_SOC

			#done, reset iteration_change_tracker
			self._dbusservice.get_item('/ReactiveStrategy').set_local_value(final_strategy.value)
			self.iteration_change_tracker.done(final_strategy)

		except Exception as ex:
			logger.log(logging.FATAL, "Unexpected exception inside Control Loop.", exc_info = ex)
			final_strategy = ReactiveStrategy.SELFCONSUME_UNEXPECTED_EXCEPTION
			self._dbusservice.get_item('/ReactiveStrategy').set_local_value(final_strategy.value)

		if final_strategy in ERROR_SELFCONSUME_STATES:
			#Do at least regular ESS.
			self.chargerate = None #self consume has no chargerate.
			self.charge_hysteresis = self.discharge_hysteresis = 0
			self._dbusservice.get_item('/ChargeRate').set_local_value(0)
			self._device.self_consume(Restrictions.NONE, None) #no schedule, no restrictions.

		return True

	async def _post_startup(self):
		prior_version = version_str_to_tuple(C_LAST_RUN_VERSION.current_value)
		current_version = version_str_to_tuple(VERSION)

		if prior_version < version_str_to_tuple("1.0.1"):
			#migrate efficiency figure stored. Only change defaults.
			if C_EFFICIENCY.current_value == 90:
				logger.debug("Migrating efficiency setting from 90% to 85 for better real-life matching.")
				C_EFFICIENCY.current_value = 85
				await C_EFFICIENCY.force_write_to_settings(self.settings_service)

		#update last_run_version setting
		C_LAST_RUN_VERSION.current_value = VERSION
		await C_LAST_RUN_VERSION.force_write_to_settings(self.settings_service)

	async def _publish_last_window_start_end(self):
		last_window_start = None
		last_window_end = None
		for w in range(NUM_SCHEDULES):
			start_time = self.settings_service.get_value("/Settings/DynamicEss/Schedule/{}/Start".format(w))
			duration = self.settings_service.get_value("/Settings/DynamicEss/Schedule/{}/Duration".format(w))
			if start_time is not None and duration is not None:
				if last_window_start is None or start_time > last_window_start:
					last_window_start = start_time
					last_window_end = start_time + duration

		if last_window_start is not None:
			self._dbusservice.get_item('/LastScheduledStart').set_local_value(last_window_start)
			self._dbusservice.get_item('/LastScheduledEnd').set_local_value(last_window_end)

	async def _enable(self):
		'''
			Enables DynamicEss.
		'''
		# Create asyncio tasks for all timers
		self._controlloop_task = asyncio.create_task(self._run_controlloop_timer())
		self._idle_setpoint_update_task = asyncio.create_task(self._run_idle_setpoint_update_timer())
		self._last_window_publish_task = asyncio.create_task(self._run_last_window_publish_timer())
		logger.info("DynamicEss activated with a control loop interval of {}s".format(CONTROL_LOOP_INTERVAL))

	async def _disable_pv(self, disabled:bool):
		'''
			Checks, if pv should be enabled or disabled and ensures that state.
		'''
		# If pv shall be disabled, we need to recuringly set the disabled path on system.
		if disabled:
			self._aiomonitor.set_value_async(SYSTEM_SERVICE, '/Pv/Disable', 1)
			self._is_pv_disabled = True
		else:
			# Only need to disable it once. This allows other services to keep pv disabled,
			# even if DESS itself does not need it anymore.
			if self._is_pv_disabled:
				self._aiomonitor.set_value_async(SYSTEM_SERVICE, '/Pv/Disable', 0)
				self._is_pv_disabled = False

	async def _update_chargerate(self, now, end, start_soc, end_soc):
		""" now is current time, end is end of slot, start_soc and end_soc determine the amount of intended soc change. Rate is the rate desired DC-Side. """

		# Only update the charge rate if a new soc value has to be considered or chargerate is none
		# round the soc, otherwise comparission fails for decimal socs and rate is calculated every 5 sec.
		# adapting a chargerate with a forced precision of 1 is enough.
		if self.chargerate is None or self.prevsoc_cr_calc is None or round(self.soc, 1) != round(self.prevsoc_cr_calc, 1):
			try:
				# a Watt is a Joule-second, a Wh is 3600 joules.
				# Capacity is kWh, so multiply by 100, percentage needs division by 100, therefore 36000.
				percentage = abs(start_soc - end_soc)
				duration = abs((end - now).total_seconds())
				chargerate = round((percentage * C_BATTERY_CAPACITY.current_value * 36000) / duration)

				logger.debug("Charging from {}% to {}% in {:.1f}s requires a {:.0f}W DC rate.".format(
					start_soc, end_soc, duration, chargerate
				))

				#Discharge and charge has two different limits for calculation. Scheduler sees the limitation on the AC-rate.
				#thus, we will limit the dc rate to something above or bellow the desired limit, depending on the systems efficiency factor.
				#this will ensure the same maximum chargerate, no matter how the composition of AC and DC charging will be later.
				if start_soc <= end_soc:
					chargerate = chargerate if C_BATTERY_CHARGE_LIMIT.current_value is None else min(chargerate, C_BATTERY_CHARGE_LIMIT.current_value * 1000 * self.oneway_efficiency)
				elif start_soc > end_soc:
					chargerate = chargerate if C_BATTERY_DISCHARGE_LIMIT.current_value is None else min(chargerate, C_BATTERY_DISCHARGE_LIMIT.current_value * 1000 / self.oneway_efficiency)

				# keeping up prior chargerate is no longer required at this point.
				self.chargerate = chargerate
				self.prevsoc_cr_calc = self.soc

			except ZeroDivisionError:
				logger.log(logging.WARNING, "Caught ZeroDivisionError in update_chargerate() for end='{}', now='{}'".format(end, now))
				self.chargerate = None

		#chargerate should be negative, if discharge-case to fit into maths elsewhere.
		#discharge_method then has to handle accordingly.
		if (end_soc < start_soc and self.chargerate is not None):
			self.chargerate = abs(self.chargerate) * -1

	async def _determine_reactive_strategy(self, w: DynamicEssWindow, nw: DynamicEssWindow, restrictions:Restrictions, now) -> ReactiveStrategy:
		'''
			Logic to be applied in Greenmode. Micro changes in strategy are applied to optimize solar gain / minimize grid pull. Returns the choosen strategy.
			Strategy has to be determined in a 100% deterministic way. After it has been determined the proper system reaction with different variable sets
			is called to minimize repetition of functional code.
		'''
		# required variables to make some improvement decissions
		# Generally, solar_plus is PV - Consumption
		# It needs to take efficency into account, legacy equation did this by multiplying acpv with 0.9
		# However it will be more precice to only consider the "available ac pv" with 0.9. Direct Consumption will basically
		# lower the available acpv without conversion losses.

		if w.soc is None:
			return ReactiveStrategy.SELFCONSUME_INVALID_TARGETSOC

		available_solar_plus = 0

		direct_acpv_consume = min(self._device.acpv or 0, self._device.consumption)
		remaining_ac_pv = max(0, (self._device.acpv or 0) - direct_acpv_consume)
		if remaining_ac_pv > 0:
			#dc can be used for charging 100%, ac is penalized with 10% conversion losses.
			available_solar_plus = (self._device.pvpower or 0) + remaining_ac_pv * self.oneway_efficiency
		else:
			#not enough ac pv. so, the part flowing from DC to remaining AC loads will lower the budget.
			#ac doesn't have to be considered, it's 100% consumed. Hower, dc consume is penalized by 10% conversion
			direct_dcpv_consume = self._device.consumption - direct_acpv_consume
			available_solar_plus = (self._device.pvpower or 0) - direct_dcpv_consume / self.oneway_efficiency

		available_solar_plus = round(available_solar_plus)

		self._dbusservice.get_item("/AvailableOverhead").set_local_value(available_solar_plus)
		self._dbusservice.get_item("/WindowSoc").set_local_value(round(w.soc, self.soc_precision))
		self._dbusservice.get_item("/WindowSlot").set_local_value(w.slot)
		self._dbusservice.get_item("/WindowToEVBattery").set_local_value(json.dumps(w.to_ev))

		#logger.log(logging.DEBUG, "ACPV / DCPV / Cons / Overhead is: {} / {} / {} / {}".format(self._device.acpv, self._device.pvpower, self._device.consumption, available_solar_plus))

		next_window_higher_target_soc = nw is not None and (nw.soc > w.soc) and nw.strategy != Strategy.SELFCONSUME
		next_window_lower_target_soc = nw is not None and (nw.soc < w.soc) and nw.strategy != Strategy.SELFCONSUME

		#pass new values to iteration change tracker.
		self.iteration_change_tracker.input(self.soc, self.soc_raw, self.targetsoc, next_window_higher_target_soc, next_window_lower_target_soc)
		soc_change = self.iteration_change_tracker.soc_change()
		target_soc_change = self.iteration_change_tracker.target_soc_change()
		window_progress = w.get_window_progress(now) or 0

		# When we have a Scheduled-Selfconsume, we can ommit to walk through the decission tree.
		if w.strategy == Strategy.SELFCONSUME:
			self.chargerate = None #No scheduled chargerate in this case.
			self.targetsoc = None
			self.charge_hysteresis = self.hysteresis
			self.discharge_hysteresis = 0
			self._device.self_consume(restrictions, w.allow_feedin)
			return ReactiveStrategy.SCHEDULED_SELFCONSUME

		# Below here, strategy is any of the target soc dependent strategies
		# some preparations
		self.override_chargerate = None
		new_targetsoc = round(w.soc, self.soc_precision)

		if new_targetsoc <= 0.1:
			#this should never happen. extra safety check to avoid undesired discharges.
			return ReactiveStrategy.SELFCONSUME_INVALID_TARGETSOC

		#detect soc drop during idle.
		if self.targetsoc is not None and round(self.targetsoc, self.soc_precision) != new_targetsoc:
			self.chargerate = None # For recalculation, if target soc changes.

		self.targetsoc = new_targetsoc
		self._dbusservice.get_item("/Flags").set_local_value(w.flags.value)

		#extract some flags for easy access.
		excess_to_grid = (w.strategy == Strategy.PROGRID) or (w.strategy == Strategy.TARGETSOC)
		missing_to_grid = (w.strategy == Strategy.TARGETSOC) or (w.strategy == Strategy.PROBATTERY)
		excess_to_bat = not excess_to_grid
		missing_to_bat = not missing_to_grid

		#Needs to be determined
		reactive_strategy = None

		if round(self.soc + self.charge_hysteresis, self.soc_precision) < self.targetsoc or self.targetsoc >= 100:
			# if 100% is reached, keep batteries charged.
			# Mind we need to leave this, if missing2bat copping is selected and the ME-indicator is negative.
			# (To be more precice, as soon as the 250 Watt requested couldnt't be served by solar, fall back to default behaviour)
			if self.targetsoc >= 100 and self.soc >= 100 and (missing_to_grid or (missing_to_bat and available_solar_plus > 250)):
				self.chargerate = 250
				reactive_strategy = ReactiveStrategy.KEEP_BATTERY_CHARGED

			# we are behind plan. Charging is required.
			else:
				await self._update_chargerate(now, w.stop, self.soc, self.targetsoc)

				# Based on the coping flags, charging has 4 options
				# Also restrictions may be applied (grid2bat).
				if available_solar_plus > self.chargerate:
					# 1) There is more solar than expected and we are EXCESSTOBAT -> charge enhanced.
					#    This state also needs to be enforced, when feedin is restricted
					if excess_to_bat or not w.allow_feedin:
						self.override_chargerate = available_solar_plus
						reactive_strategy = ReactiveStrategy.SCHEDULED_CHARGE_ENHANCED

					# 2) There is more solar than expected and we are EXCESSTOGRID -> charge at calculated charge rate, accept feedin happening.
					#    This state is dissallowed, when feedin is restricted, but then we already entered situation 1.
					elif excess_to_grid:
						reactive_strategy = ReactiveStrategy.SCHEDULED_CHARGE_FEEDIN
				else:
					#available_solar_plus <= self.chargerate

					# 3) There isn't enough solar and we are flagged MISSINGTOGRID -> use calculated charge rate.
					#    (Wording note: Missing2Grid describes the punishment of missing energy to the grid - so TAKING energy from the grid ;-))
					#    But, this state is dissallowed, if a Grid2Bat Restriction is active.
					if missing_to_grid and not (Restrictions.GRID2BAT in w.restrictions):
						reactive_strategy = ReactiveStrategy.SCHEDULED_CHARGE_ALLOW_GRID

					# 4) There isn't enough solar and we are flagged MISSINGTOBAT -> only use solar power that is availble.
					#    This is self consume, until condition changes.
					#    In case there is Grid2Bat restriction, this is our only option, even if the flag would indicate MISSINGTOGRID
					elif available_solar_plus > 0 and (missing_to_bat or (Restrictions.GRID2BAT in w.restrictions)):
						reactive_strategy = ReactiveStrategy.SELFCONSUME_NO_GRID

					# 5.) No Grid charge possible, no solar. We can't charge.
					#     However, when we have missing_to_bat, we allow to go bellow target soc.
					elif available_solar_plus <= 0 and missing_to_bat:
						reactive_strategy = ReactiveStrategy.SELFCONSUME_ACCEPT_BELOW_TSOC

					# 5.) No Grid charge possible, no solar. We can't charge.
					#     with missing2grid, but grid2bat restriction we can only idle now.
					#     missing2grid with no restriction is already handled in case 3.
					elif available_solar_plus <= 0 and missing_to_grid and (Restrictions.GRID2BAT in w.restrictions):
						reactive_strategy = ReactiveStrategy.IDLE_NO_OPPORTUNITY

		else:
			# if we are currently in any SCHEDULED_CHARGE_* State and our next window outlines an even higher target soc,
			# don't switch to idle, but keep a certain chargerate. As soon as target_soc changes, this state has to be left.
			# but only enter it, when window progress is >= TRANSITION_STATE_THRESHOLD
			if (self.iteration_change_tracker._previous_reactive_strategy in CHARGE_STATES and
	   			next_window_higher_target_soc and window_progress >= TRANSITION_STATE_THRESHOLD) or \
				(self.iteration_change_tracker._previous_reactive_strategy == ReactiveStrategy.SCHEDULED_CHARGE_SMOOTH_TRANSITION and target_soc_change == ChangeIndicator.NONE):
				# keep current charge rate untouched.
				# already targeting the new soc target of "next" window will cause a not smooth transition, if next window in slot 1 is outdated
				# and the next window beeing pushed to slot 0 indicates another target soc.
				reactive_strategy = ReactiveStrategy.SCHEDULED_CHARGE_SMOOTH_TRANSITION
			else:
				# we are above or equal to target soc, or the charge histeresis has not yet kicked in from a prior state.

				if (available_solar_plus > 0 and not excess_to_grid):
					# If surplus is available, always attempt to charge, unless we are flagged EXCESSTOGRID
					reactive_strategy = ReactiveStrategy.SELFCONSUME_ACCEPT_CHARGE

				else:
					# so, now we have: (availableSolarPlus <= 0 or solaroverhaed, but excess_to_grid) and (equal or above targetSoc).
					# so, most likely any of the discharge-variants is required (or ultimately idle)
					# if we are flagged EXESSTOGRID and MISSINGTOGRID, perform a strict discharge, based on soc difference.
					# Any imprecission shall be handled by the grid
					# not allowed with bat2grid restriction
					#       When we have a bat2grid restriction, we should discharge at full consumption, feeding in 100% of solar production.
					if self.soc - self.discharge_hysteresis > max(self.targetsoc, self._device.minsoc) and excess_to_grid and missing_to_grid \
						and not (Restrictions.BAT2GRID in restrictions):
						await self._update_chargerate(now, w.stop, self.soc, self.targetsoc)
						reactive_strategy = ReactiveStrategy.SCHEDULED_DISCHARGE

					# if flags are EXCESSTOGRID and MISSINGTOBAT, that means: keep a MINIMUM dischargerate, but allow to discharge more, if consumption-solar is higher.
					# not allowed with bat2grid restriction
					# so, we do some quick maths, if loads would require a higher discharge - then we let self consume handle that, over calculating a "better" discharge rate.
					elif self.soc - self.discharge_hysteresis > max(self.targetsoc, self._device.minsoc) and excess_to_grid and missing_to_bat \
						and not (Restrictions.BAT2GRID in restrictions):
						await self._update_chargerate(now, w.stop, self.soc, self.targetsoc)
						me_indicator = available_solar_plus - self.chargerate

						if me_indicator < 0:
							# missing, let self consume handle this over calculating a improved rate.
							reactive_strategy =  ReactiveStrategy.SELFCONSUME_INCREASED_DISCHARGE
						else:
							# excess, ensure the minimum discharge rate required to reach targetsoc as of "now".
							self.override_chargerate = abs(self.chargerate) * -1
							reactive_strategy =  ReactiveStrategy.SCHEDULED_MINIMUM_DISCHARGE

					# left over discharge cases:
					#	FIXME: When we have pro Grid and a battery restriction but Solar > consumption, self-consume states are not suitable - it will charge. Idle Instead.
					#   - bat2grid restricted -> Selfconsume to drive loads, or Idle
					#   - EXCESSTOBAT and MISSINGTOBAT -> self consume
					#   - EXCESSTOBAT and MISSINGTOGRID:
					#     Technically that means, we should have a MAXIMUM dischargerate and punish the energy above that to the grid
					#     However, that may cause some grid2consumption happening in the beginning of the window, but still ending up above target soc.
					#     So that would be gridpull for no reason.
					#     So, the more logical way is to accept ANY discharge, but simple stop when reaching target soc - and punish the remaining
					#     load during that window to the grid. -> also self consume
					# BUT: we are only doing this, If our next window has a smaller, equal or no target soc
					elif self.soc - self.discharge_hysteresis > max(self.targetsoc, self._device.minsoc):
						# we are supposed to drive loads only to achieve the indendet discharge. However, if solar > consumption and a bat2grid restriction,
						# we have no discharge opportunity, Then, we ultimately only can idle to stay close to target soc.
						if available_solar_plus > 0 and (Restrictions.BAT2GRID in restrictions):
							reactive_strategy = ReactiveStrategy.IDLE_NO_DISCHARGE_OPPORTUNITY
						else:
							if (self.is_ev_charging()):
								await self._update_chargerate(now, w.stop, self.soc, self.targetsoc)
								reactive_strategy = ReactiveStrategy.CONTROLLED_DISCHARGE_EVCS
							else:
								reactive_strategy = ReactiveStrategy.SELFCONSUME_ACCEPT_DISCHARGE

					else:
						# Here we are:
						# - Ahead of plan, but the next window indicates a higher soc target.
						# - Spot on target soc, so idling is imminent / above targetSoc by discharge_hysteresis %.
						# - available solar plus, but intended feedin.
						if available_solar_plus > 0 and excess_to_grid:
							# We have solar surplus, but VRM wants an explicit feedin.
							# since we are above or equal to target soc, we are going idle to achieve that.
							reactive_strategy = ReactiveStrategy.IDLE_SCHEDULED_FEEDIN
						else:
							if (self.iteration_change_tracker._previous_reactive_strategy in DISCHARGE_STATES and
								next_window_lower_target_soc and window_progress >= TRANSITION_STATE_THRESHOLD) or \
								(self.iteration_change_tracker._previous_reactive_strategy == ReactiveStrategy.SCHEDULED_DISCHARGE_SMOOTH_TRANSITION and target_soc_change == ChangeIndicator.NONE):
								# keep current charge rate untouched.
								# but only enter it, when window progress is >= TRANSITION_STATE_THRESHOLD
								reactive_strategy = ReactiveStrategy.SCHEDULED_DISCHARGE_SMOOTH_TRANSITION
							else:
								# else, we have soc==targetsoc, or soc - discharge_hystersis > targetsoc.
								# In Case of MISSING_TO_BAT, we allow to discharge bellow target soc.
								# Forced discharges are already handled, so we simply let self-consume handle the required amount
								# of discharge here.
								if missing_to_bat:
									reactive_strategy = ReactiveStrategy.SELFCONSUME_ACCEPT_BELOW_TSOC
								else:
									# else we ultimately idle.
									reactive_strategy = ReactiveStrategy.IDLE_MAINTAIN_TARGETSOC

		#bellow here, ReactiveStrategy should be determined. As well as chargerate, if required. If it isn't
		#Enter self consume, as conditions may change and situation will resolve.
		#(This would need to be resolved, there shouldn't be any unpredicted combination of parameters)
		if reactive_strategy is None:
			return ReactiveStrategy.SELFCONSUME_UNPREDICTED
		else:
			#depending on the reactive strategy choosen, system behaviour may be the same - just different value set
			#and/or different reasoning.
			final_chargerate = self.override_chargerate if self.override_chargerate is not None else self.chargerate

			if final_chargerate is None and (reactive_strategy in CHARGE_STATES or reactive_strategy in DISCHARGE_STATES):
				# failed to calculate a chargerate. This however is required for charge/discharge.
				# Temporary enter self-consume to keep the system moving, changed conditions may allow for successfull recalculation and
				# getting back on track.
				reactive_strategy = ReactiveStrategy.SELFCONSUME_FAULTY_CHARGERATE

			if reactive_strategy in CHARGE_STATES:
				self.charge_hysteresis = 0 #allow to reach tsoc spot on
				self.discharge_hysteresis = self.hysteresis #avoid discharging on overshoot
				self._device.charge(w.flags, restrictions, abs(final_chargerate), w.allow_feedin)

			elif reactive_strategy in SELFCONSUME_STATES:
				self.charge_hysteresis = self.hysteresis #avoid charge of minor tsoc raise
				self.discharge_hysteresis = 0
				self.chargerate = None #self consume has no chargerate.
				self._device.self_consume(restrictions, w.allow_feedin)

			elif reactive_strategy in IDLE_STATES:
				self.charge_hysteresis = self.hysteresis #avoid charge on idle soc drop
				self.discharge_hysteresis = 0 #allow follow a controlled discharge
				self.chargerate = None #idle has no chargerate.
				self._idle_feedin = w.allow_feedin #keep track of feedin permission during idle, to be able to react on changes during idle.
				self._is_idle = True
				#idle method is called from within a quicker control loop. (in update_values)

			elif reactive_strategy in DISCHARGE_STATES:
				self.charge_hysteresis = self.hysteresis #avoid charging on undershoot.
				self.discharge_hysteresis = 0 #allow to reach tsoc spot on
				#chargerate to be send to discharge method has to be always positive.
				self._device.discharge(w.flags, restrictions, abs(final_chargerate), w.allow_feedin)

			elif reactive_strategy in ERROR_SELFCONSUME_STATES:
				#errorstates are handled outside this method.
				return reactive_strategy

			else:
				#This should never happen, it means that there is a state that is not mapped to a reaction.
				#We enter self consume and use a own state for that :P
				#Doing at least self consume will make the system leave this unmapped state sooner or later for sure and not get stuck.
				return ReactiveStrategy.SELFCONSUME_UNMAPPED_STATE

			return reactive_strategy

	@property
	def active(self) -> bool:
		return self._dbusservice.get_item("/Active").value == 1

	@active.setter
	def active(self, v:bool):
		self._dbusservice.get_item("/Active").set_local_value(v)

	@property
	def charge_hysteresis(self):
		return self._dbusservice.get_item("/ChargeHysteresis").value

	@charge_hysteresis.setter
	def charge_hysteresis(self, v):
		self._dbusservice.get_item("/ChargeHysteresis").set_local_value(v)

	@property
	def discharge_hysteresis(self):
		return self._dbusservice.get_item("/DischargeHysteresis").value

	@discharge_hysteresis.setter
	def discharge_hysteresis(self, v):
		self._dbusservice.get_item("/DischargeHysteresis").set_local_value(v)

	@property
	def hysteresis(self) -> float:
		"""
			Determines the hysteresis value to use. We anticipate that the scheduler may never be off more than
			250 Wh. So, we use the equivalant of 250Wh of the battery size, but limit it to be 1%, as this may
			be the biggest soc-drop that could be encountered on a integer-based system during idle.
		"""
		#capacity (kWh) * 10 is 1% in Wh equivalent.
		return round(min(250.0 / (C_BATTERY_CAPACITY.current_value * 10), 1.0), self.soc_precision)

	@property
	def errorcode(self):
		return self._errorcode

	@property
	def soc(self) -> float:
		"""
			current soc 0 - 100
		"""
		return round(self._aiomonitor.get_value(SYSTEM_SERVICE, "/Dc/Battery/Soc", 0.0), self.soc_precision)

	@property
	def soc_raw(self) -> float:
		"""
			returns the unmodified soc. Required to detect actual precission.
		"""
		return self._aiomonitor.get_value(SYSTEM_SERVICE, "/Dc/Battery/Soc", 0.0)

	@property
	def soc_precision(self) -> int:
		"""
			Detected SoC Precision of the battery.
		"""
		return self._dbusservice.get_item('/WorkingSocPrecision').value

	@soc_precision.setter
	def soc_precision(self, v):
		self._dbusservice.get_item('/WorkingSocPrecision').set_local_value(v)

	@property
	def operating_mode(self) -> OperatingMode:
		return OperatingMode(C_OPERATING_MODE.current_value)

	@property
	def targetsoc(self):
		return self._dbusservice.get_item('/TargetSoc').value if self._dbusservice.get_item('/TargetSoc').value is not None and  self._dbusservice.get_item('/TargetSoc').value > 0 else None

	@targetsoc.setter
	def targetsoc(self, v):
		self._dbusservice.get_item('/TargetSoc').set_local_value(v or 0)

	@property
	def ready(self) -> bool:
		return self._dbusservice.get_item("/Ready").value == 1

	@ready.setter
	def ready(self, v:bool):
		self._dbusservice.get_item("/Ready").set_local_value(int(v))

	@property
	def oneway_efficiency(self) -> float:
		''' When charging from AC, only half of the efficiency-losses have to be considered
			So, with an overall system efficency of 0.8, the charging efficency would be 0.9 and so on.
		'''
		return min(1.0, ((1 - C_EFFICIENCY.current_value / 100.0) / -2.0) + 1.0)

	async def check_conditions(self) -> ErrorCode:
		'''
			Checks if all operational constraints are met. Then returns NO_ERROR.
		'''
		if C_BATTERY_CAPACITY.current_value is None or C_BATTERY_CAPACITY.current_value <= 0.0:
			return ErrorCode.BATTERY_CAPACITY_UNSET

		if self._device is None:
			return ErrorCode.NO_ESS

		if self.soc is None:
			return ErrorCode.SOC_LOW

		#finally, validate device has no error as well.
		return self._device.check_conditions()

	async def handle_error_code(self, error_code:ErrorCode):
		'''
			Handles the error code returned by check_conditions, by setting the appropriate errorcode and active state,
			so the system can react accordingly.
		'''
		if error_code == ErrorCode.NO_ERROR:
			self._dbusservice.get_item('/ErrorCode').set_local_value(ErrorCode.NO_ERROR.value)
			self.ready = True
			log_on_delta(logging.INFO, 'ConditionCheck', "All operational constraints met. Setting Ready-Flag.")
		else:
			if self.active or self.ready or self._dbusservice.get_item('/ErrorCode').value != error_code.value:
				await self.pause(error_code)
			self._dbusservice.get_item('/ReactiveStrategy').set_local_value(ReactiveStrategy.ERROR_CODE.value)
			log_on_delta(logging.ERROR, 'ConditionCheck', f"check_condition failed with {error_code}")
			log_on_delta(logging.INFO, 'ChargeControl', None) #reset delta logging for ChargeControl.

	async def pause(self, error_code:ErrorCode):
		'''
			Pauses DynamicEss, by setting the appropriate errorcode and active state,
			so the system can react accordingly.
		'''
		#pausing means enter regular ESS and flag dess inactive. Reset all dbus paths as well.
		self.chargerate = None #self consume has no chargerate.
		self._is_idle = False #pause idling, if we do.
		self.charge_hysteresis = self.discharge_hysteresis = 0
		self._dbusservice.get_item('/ChargeRate').set_local_value(None)
		self._dbusservice.get_item('/ErrorCode').set_local_value(error_code.value)
		self._dbusservice.get_item('/Active').set_local_value(0) #Inactive
		self._dbusservice.get_item('/Ready').set_local_value(0) #Not ready.
		self._dbusservice.get_item('/Strategy').set_local_value(None)
		self._dbusservice.get_item('/Restrictions').set_local_value(None)
		self._dbusservice.get_item('/AllowGridFeedIn').set_local_value(None)
		self._dbusservice.get_item('/Flags').set_local_value(None)
		self._dbusservice.get_item('/ReactiveStrategy').set_local_value(None)
		self._dbusservice.get_item('/WindowSoc').set_local_value(None)
		self._dbusservice.get_item('/WindowSlot').set_local_value(None)
		self._dbusservice.get_item('/ChargeHysteresis').set_local_value(0)
		self._dbusservice.get_item('/DischargeHysteresis').set_local_value(0)
		self._dbusservice.get_item('/WindowToEVBattery').set_local_value(None)
		self._dbusservice.get_item('/EvcsGxFlags').set_local_value(None)
		self._device.self_consume(Restrictions.NONE, None) #no schedule, no restrictions.

	def windows(self):
		#generator to avoid recreation of all schedules over and over, when generally working on window 0 and 1.
		for i in range(NUM_SCHEDULES):
			start = self.settings_service.get_value('/Settings/DynamicEss/Schedule/{}/Start'.format(i))
			duration = self.settings_service.get_value('/Settings/DynamicEss/Schedule/{}/Duration'.format(i))
			soc = self.settings_service.get_value('/Settings/DynamicEss/Schedule/{}/Soc'.format(i)) #keep legacy support for a while
			targetsoc = self.settings_service.get_value('/Settings/DynamicEss/Schedule/{}/TargetSoc'.format(i))
			allow_feedin = self.settings_service.get_value('/Settings/DynamicEss/Schedule/{}/AllowGridFeedIn'.format(i))
			restriction = self.settings_service.get_value('/Settings/DynamicEss/Schedule/{}/Restrictions'.format(i))
			strategy = self.settings_service.get_value('/Settings/DynamicEss/Schedule/{}/Strategy'.format(i))
			flags = self.settings_service.get_value('/Settings/DynamicEss/Schedule/{}/Flags'.format(i))
			toevbattery = self.settings_service.get_value('/Settings/DynamicEss/Schedule/{}/ToEVBattery'.format(i))

			yield DynamicEssWindow(
				datetime.fromtimestamp(start),
				duration,
				soc,
				targetsoc,
				allow_feedin,
				restriction,
				strategy,
				flags,
				i,
				toevbattery
			)

	def raise_alarm_at(self, datetime: datetime, alarm_path: str, severity:int=1):
		'''
			Raises an alarm at a given datetime unless it is revoked. Revoking an alert is done by calling raise_alarm_at
			for the same path with datetime none. If an alert is already scheduled, changing the datetime won't happen.
		'''
		if datetime is not None:
			if alarm_path not in self._alarm_queue:
				self._alarm_queue[alarm_path] = (datetime, severity)

			#check if the timer is expired and we have to raise the alarm on the bus.
			if self._alarm_queue[alarm_path][0] <= datetime.now(timezone.utc):
				self._dbusservice.get_item(alarm_path).set_local_value(self._alarm_queue[alarm_path][1]) #raise on bus
		else:
			if alarm_path in self._alarm_queue:
				del self._alarm_queue[alarm_path]
				self._dbusservice.get_item(alarm_path).set_local_value(0) #clear on bus as well.

	def get_charge_power_capability(self) -> float:
		'''
		  Determines the systems maximum battery charge capability in Watts.
		  If the ccl and cvl fails to be determined, then None is returned.
		  None is to be distinguished from 0 (which means no charging allowed by the bms)
		'''

		battery = self._dbusservice.get_item("/ActiveBmsService").value

		# first, try to obtain values from the bms service.
		if battery is not None and battery != "":
			ccl = self._aiomonitor.get_value(battery, '/Info/MaxChargeCurrent')
			cvl = self._aiomonitor.get_value(battery, '/Info/MaxChargeVoltage')

			if (ccl is not None and cvl is not None):
				return ccl * cvl

		return None

	def is_ev_charging(self) -> bool:
		"""
			Checks if any EV is currently charging, used to determine a different behaviour for the
			main battery discharge.
		"""
		for evcsid, evcs_state in self._evcs_delegates.items():
			#we only consider the EV charging, if the state is charging AND we have been the invoker
			#of the start. If it is full or not charging, battery usage behaviour shouldn't be affected.
			if EvcsGxFlags.CHARGING in evcs_state.gx_flags and evcs_state.status == 2:
				return True

		return False

	async def publish_evcs_flags(self) -> None:
		jo = {}
		jor = {}
		for evcs_delegate in self._evcs_delegates.values():
			jo[evcs_delegate.instance] = evcs_delegate.gx_flags
			jor[evcs_delegate.instance] = evcs_delegate.gx_flags.stringify()

		jos = json.dumps(jo)
		jors = json.dumps(jor)
		if jos != self._dbusservice.get_item('/EvcsGxFlags').value:
			logger.debug("EvcsGxFlags on dbus updated to: {} (4Human: {})".format(jos, jors))
			self._dbusservice.get_item('/EvcsGxFlags').set_local_value(jos)

if __name__ == "__main__":
	parser = argparse.ArgumentParser(
		description='Controls dynamic ESS.',
	)

	parser.add_argument('--dbus', help='dbus bus to use, defaults to system',
			default='system')
	parser.add_argument("-d", "--debug", help="set logging level to debug", action="store_true")

	args = parser.parse_args()
	logger.setLevel(logging.DEBUG if args.debug else logging.INFO)
	logger.debug("Startup args: dbus='{}', debug={}".format(args.dbus, args.debug))

	logger.debug("Using dbus lib {}".format(
		BusType.__module__.split('.')[0]))

	logger.info(f"Starting Dynamic ESS Version {VERSION}{('-' + BRANCH) if BRANCH else ''}")

	bus_type = {
		"system": BusType.SYSTEM,
		"session": BusType.SESSION
	}.get(args.dbus, BusType.SESSION)

	mainloop = asyncio.new_event_loop()
	asyncio.set_event_loop(mainloop)

	dynamic_ess = DynamicEss(bus_type)
	mainloop.run_until_complete(dynamic_ess.init())

	try:
		mainloop.run_forever()
	except KeyboardInterrupt:
		mainloop.stop()