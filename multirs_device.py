import math
from ess_device import EssDevice
from globals import C_BATTERY_CHARGE_LIMIT, C_BATTERY_DISCHARGE_LIMIT, C_GRID_EXPORT_LIMIT, C_GRID_IMPORT_LIMIT, Restrictions, Flags

class MultiRsDevice(EssDevice):
	@property
	def has_ess_assistant(self):
		return self.service.get_value('/Hub4/AssistantId') == 5

	@property
	def available(self):
		return self.service.get_value('/Capabilities/HasDynamicEssSupport') == 1

	@property
	def minsoc(self):
		# The minsoc is here on the Multi-RS
		return self.service.get_value('/Ess/ActiveSocLimit')

	@property
	def mode(self):
		return self.service.get_value('/Settings/Ess/Mode')

	def check_conditions(self):
		# Not in optimised mode, no point in doing anything
		if self.mode not in (0, 1):
			return 2 # ESS mode is wrong
		if self.minsoc is None:
			return 4 # SOC low, happens during firmware updates
		return 0

	def charge(self, flags, restrictions:Restrictions, rate, allow_feedin):
		# the incoming rate is the desired battery charge rate (dc)
		# it's maximum is accounting for a converted AC-Rate-Limit already.
		# i.e. if the system is configured to a maximum rate of 10000W, the incoming
		# rate will never be higher than 10000*0,925 = 9250W.
		self.service.set_value_async('/Ess/DisableFeedIn',  int(not allow_feedin) if allow_feedin is not None else 0)
		self.service.set_value_async('/Ess/DisableDischarge', 0)
		self.service.set_value_async('/Ess/DisableCharge', 0)
		self.service.set_value_async('/Ess/UseInverterPowerSetpoint', 0)
		fast_charge_requested = Flags.FASTCHARGE in flags
		batteryimport = Restrictions.GRID2BAT not in restrictions

		# if fastcharge is requested, use the maximum power allowed as per user definition.
		if fast_charge_requested:
			rate = C_BATTERY_CHARGE_LIMIT.current_value * 1000.0

		#if we have a grid2bat restriction, the maximum amount we can charge is solar.
		#consumption can be ignored, may be pulled from grid. (this just validates a grid2bat, not a grid2anywhere restriction)
		#only applicable for charge cases. In that case, acpv has a slight penalty.
		if not batteryimport:
			rate = min(rate, (self.pvpower or 0) + (self.acpv or 0) * self._dynamic_ess.oneway_efficiency)

		# we now have to translate the dc_rate into a ac_setpoint.

		# In an unrestricted case, we just feedin everything, keep consumption - plus, what we actually want to flow TO the battery.
		# DCPV has a slight penalty, when feeding in. When requesting a certain battery rate, we need to request less at the setpoint due to efficiency losses.
		setpoint = - (self.acpv or 0) - ((self.pvpower or 0) * self._dynamic_ess.oneway_efficiency) + ((self.consumption or 0) + rate * self._dynamic_ess.oneway_efficiency)

		#- If Feedin is restricted, setpoint is not allowed to be negative.
		#this needs to be checked for charge cases as well, because a low chargerate may cause feedin.
		if not allow_feedin:
			setpoint = max(0, setpoint)

		#finally, make sure we stay within user configured grid bounds with our request.
		if setpoint < 0:
			setpoint = max(setpoint, C_GRID_EXPORT_LIMIT.current_value * -1000.0)
		elif setpoint > 0:
			setpoint = min(setpoint, C_GRID_IMPORT_LIMIT.current_value * 1000.0)

		#done, request the desired setpoint.
		self.service.set_value_async('/Ess/AcPowerSetpoint', setpoint)
		return rate

	def discharge(self, flags, restrictions:Restrictions, rate, allow_feedin):
		# the incoming rate is the desired battery discharge rate (dc)
		# it's maximum is accounting for a converted AC-Rate-Limit already.
		# i.e. if the system is configured to a maximum discharge rate of 10000W, the incoming
		# rate will never be higher than 10000/0,925 = 10810W.
		rate = rate * -1 #commes in positive
		batteryexport = not Restrictions.BAT2GRID in restrictions

		self.service.set_value_async('/Ess/DisableFeedIn', int(not allow_feedin) if allow_feedin is not None else 0)
		self.service.set_value_async('/Ess/UseInverterPowerSetpoint', 0)
		self.service.set_value_async('/Ess/DisableDischarge', 0)
		self.service.set_value_async('/Ess/DisableCharge', 0)

		#If we have a bat2grid restriction, the maximum amount we can send to grid is solar.
		#In that case, we need to limit the fraction of battery discharge to consumption/0.95.
		if not batteryexport:
			rate = max(rate, -(self.consumption or 0) / self._dynamic_ess.oneway_efficiency)

		#In an unrestricted case, we just feedin everything, keep consumption
		# rate should be feedin, but since rate is negative has to go in positive.
		setpoint = - (self.acpv or 0) - ((self.pvpower or 0) * self._dynamic_ess.oneway_efficiency) + ((self.consumption or 0) + rate * self._dynamic_ess.oneway_efficiency)

		#- If Feedin is restricted, setpoint is not allowed to be negative.
		if not allow_feedin:
			setpoint = max(0, setpoint)

		#finally, make sure we stay within user configured bounds with our request.
		if setpoint < 0:
			setpoint = max(setpoint, C_GRID_EXPORT_LIMIT.current_value * -1000.0)
		elif setpoint > 0:
			setpoint = min(setpoint, C_GRID_IMPORT_LIMIT.current_value * 1000.0)

		#done, request the desired setpoint.
		self.service.set_value_async('/Ess/AcPowerSetpoint', setpoint)
		return rate

	def idle(self, allow_feedin):
		self.service.set_value_async('/Ess/DisableFeedIn', int(not allow_feedin) if allow_feedin is not None else 0)
		self.service.set_value_async('/Ess/UseInverterPowerSetpoint', 0)

		#idling means: Grid needs to deliver consumption - ACPV - DCPV * 0.95.
		#if there is more solar than consumption, we don't have to mind, the feedin-setting will either allow for it or not.
		acps = (self.consumption or 0) - (self.acpv or 0) - (self.pvpower or 0) * self._dynamic_ess.oneway_efficiency

		#finally, make sure we stay within user configured bounds with our request.
		if acps < 0:
			acps = max(acps, C_GRID_EXPORT_LIMIT.current_value * -1000.0)
		elif acps > 0:
			acps = min(acps, C_GRID_IMPORT_LIMIT.current_value * 1000.0)

		self.service.set_value_async('/Ess/AcPowerSetpoint', acps)

		#when idling during 0 external mppt power, we can additionally disable discharge to improve setpoint stability.
		if (math.ceil(self.external_pvpower or 0)) == 0:
			self.service.set_value_async('/Ess/DisableDischarge', 1)
			self.service.set_value_async('/Ess/DisableCharge', 1)
		else:
			self.service.set_value_async('/Ess/DisableDischarge', 0)
			self.service.set_value_async('/Ess/DisableCharge', 0)

	def self_consume(self, restrictions:Restrictions, allow_feedin):
		self.service.set_value_async('/Ess/DisableFeedIn', int(not allow_feedin) if allow_feedin is not None else 0)
		self.service.set_value_async('/Ess/AcPowerSetpoint', 0)
		self.service.set_value_async('/Ess/UseInverterPowerSetpoint', 0)
		self.service.set_value_async('/Ess/DisableDischarge', 0)
		self.service.set_value_async('/Ess/DisableCharge', 0)

	def deactivate(self):
		self.service.set_value_async('/Ess/DisableFeedIn', 0)
		self.service.set_value_async('/Ess/AcPowerSetpoint', 0)
		self.service.set_value_async('/Ess/UseInverterPowerSetpoint', 0)
		self.service.set_value_async('/Ess/InverterPowerSetpoint', 0)
		self.service.set_value_async('/Ess/DisableDischarge', 0)
		self.service.set_value_async('/Ess/DisableCharge', 0)
