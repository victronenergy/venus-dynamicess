
from ess_device import EssDevice
from constants import SETTINGS_SERVICE, SYSTEM_SERVICE, HUB4_SERVICE, MAX_FEEDIN_VALUE
from globals import C_BATTERY_CHARGE_LIMIT, C_GRID_EXPORT_LIMIT, ErrorCode, Restrictions, Flags

class VebusDevice(EssDevice):
	@property
	def has_ess_assistant(self):
		return self.service.get_value('/Hub4/AssistantId') == 5

	@property
	def available(self):
		return self.has_ess_assistant

	@property
	def hub4mode(self):
		return self._aiomonitor.get_value(SETTINGS_SERVICE, '/Settings/CGwacs/Hub4Mode')

	@property
	def maxfeedinpower(self):
		local_feedin_limit = self._aiomonitor.get_value(SETTINGS_SERVICE,
                '/Settings/CGwacs/MaxFeedInPower')

		dess_feedin_limit = C_GRID_EXPORT_LIMIT.current_value * 1000.0 if C_GRID_EXPORT_LIMIT.current_value is not None else -1

		if local_feedin_limit > -1 and dess_feedin_limit == -1:
			return local_feedin_limit * -1

		if dess_feedin_limit > -1 and local_feedin_limit == -1:
			return dess_feedin_limit * -1

		#if both limits are present, the more restricive one takes precedence.
		if dess_feedin_limit > -1 and local_feedin_limit > -1:
			return min(dess_feedin_limit, local_feedin_limit) * -1

		#No limit present
		return -MAX_FEEDIN_VALUE

	@property
	def minsoc(self):
		# The BatteryLife delegate puts the active soc limit here.
		return self._aiomonitor.get_value(SYSTEM_SERVICE, '/Control/ActiveSocLimit')

	def _set_feedin(self, allow_feedin):
		""" None = follow system setup
			True = allow
			False = restrict """

		self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/FeedInExcess', 0 if allow_feedin is None else 2 if allow_feedin else 1)

	def _set_charge_power(self, v):
		self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/MaxChargePower', None if v is None else max(v, 50))

	def check_conditions(self) -> ErrorCode:
		# Can't do anything unless we have a minsoc, and the ESS assistant
		if not self.has_ess_assistant:
			return ErrorCode.NO_ESS

		# In Keep-Charged mode or external control, no point in doing anything
		# BatteryLifeState.KeepCharged is not supported (nothing to do)
		#FIXME: Raise a Info-Level notification, so users remember to switch back.
		if self._aiomonitor.get_value(SYSTEM_SERVICE, '/Control/EssState') == 9 or self.hub4mode == 3:
			return ErrorCode.ESS_KEEPCHARGED

		# KeepCharged will also set minsoc to none - so this check should come after, else the minsoc
		# error will always dominate.
		if self.minsoc is None:
			return ErrorCode.SOC_LOW

		return ErrorCode.NO_ERROR

	def charge(self, flags, restrictions:Restrictions, rate, allow_feedin):
		self._set_feedin(allow_feedin)

		#if the desired rate is lower than dcpv, this would come down to NOT charging from AC,
		#but 100% of dcpv. To really achieve an overall charge-rate of what's requested, we need
		#to enter discharge mode instead. Discharge needs to be called with the desired discharge rate (positive)
		#minus once more dcpv, as the discharge-method will internally add dcpv again.
		# that'll be self.pvpower - rate - self.pvpower, hence comes down to rate * -1
		# or in other words: we leave the portion of rate * -1 from dcpv available for the battery.
		fast_charge_requested = Flags.FASTCHARGE in flags

		#don't forward fastcharge. That means "max power", so no forced discharge.
		if rate < self.pvpower * 0.98 and not fast_charge_requested:
			self.discharge(flags, restrictions, rate * -1, allow_feedin)
			return

		self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/Setpoint', None)
		self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/ForceCharge', 1)
		self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/MaxDischargePower', -1.0)

		# Fast charge, or controlled charge?
		fast_charge_clearance = True #Defaults to true, if we have no limit or can't determine technical limits, we just go for it (legacy behaviour).

		if fast_charge_requested and self._dynamic_ess.battery_charge_limit is not None and self._dynamic_ess.get_charge_power_capability() is not None:
			# limits and technical capabilities are known. So, only apply fast charge, if limit would be implicit obeyed.
			fast_charge_clearance = self._dynamic_ess.get_charge_power_capability() <= C_BATTERY_CHARGE_LIMIT.current_value / self._dynamic_ess.oneway_efficiency * 1000

		if rate is None or (fast_charge_requested and fast_charge_clearance):
			self._set_charge_power(None)
		else:
			# if fast charge is requested, but not yet cleared, use the configured battery charge limit as charge rate.
			# this way the limit is obeyed, but the desired "maximum charge" is achieved.
			if (fast_charge_requested and not fast_charge_clearance and C_BATTERY_CHARGE_LIMIT.current_value is not None):
				rate = C_BATTERY_CHARGE_LIMIT.current_value / self._dynamic_ess.oneway_efficiency * 1000

			# Upon first call of charge(), the input charge-rate eventually has some DC-AC losses considered.
			# (Originating from ac consumers currently beeing driven with dcsolar, reducing anticipated solar overhead)
			# As soon, as we start charging, there can't be a flow from dc to ac, so these losses will vanish
			# and the updated chargerate will be a little bit higher, if nothing else changes. This is fine and neglectable.
			# this only happens in certain charge-situations, scheduled charging from grid only changes the chargerate on soc change.
			# rate will already be adjusted for obeying batteryimport limitation, so these check can be omited.
			setrate = rate / self._dynamic_ess.oneway_efficiency - self.pvpower * 0.98
			self._set_charge_power(max(0.0, setrate))

	def discharge(self, flags, restrictions:Restrictions, rate, allow_feedin):
		batteryexport = not (Restrictions.BAT2GRID in restrictions)

		self._set_feedin(allow_feedin)
		self._set_charge_power(None)
		self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/ForceCharge', 0)

		if allow_feedin:
			# Calculate how fast to sell. If exporting the battery to the grid
			# is allowed, then export rate plus whatever DC-coupled PV is
			# making. If exporting the battery is not allowed, then limit that
			# to DC-coupled PV plus local consumption.
			self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/Setpoint', self.maxfeedinpower)

			if Flags.FASTCHARGE in flags:
				self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/MaxDischargePower', -1)
				return None
			else:
				srate = max(1.0, ((rate or 0) + self.pvpower) * self._dynamic_ess.oneway_efficiency) # 1.0 to allow selling overvoltage

				if (batteryexport):
					#discharging the battery by rate requires to discharge all available dcpv as well.
					self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/MaxDischargePower', srate)
				else:
					# this may lead to feedin anyway, but it then is "feedin of solar", while battery is only backing loads.
					self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/MaxDischargePower',
						(min (srate, (self.pvpower + self.consumption) * self._dynamic_ess.oneway_efficiency + 1.0))) # +1.0 to allow selling overvoltage

		else:
			# this should never be reached, as discharge won't be entered with restrictions - leaving it here for double safety.
			self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/Setpoint', None) # Normal ESS, no feedin
			self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/MaxDischargePower', -1)


	def idle(self, allow_feedin):
		self._set_feedin(allow_feedin)
		self._set_charge_power(None)
		self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/ForceCharge', 0)

		if allow_feedin:
			# This keeps battery idle by not allowing more power to be taken
			# from the DC bus than what DC-coupled PV provides.
			mdp = max(1.0, self.pvpower) # 1.0 to allow selling overvoltage
			self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/MaxDischargePower', mdp)
			self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/Setpoint', self.maxfeedinpower)
		else:
			self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/Setpoint', 0) # Normal ESS
			self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/MaxDischargePower', max(1.0, self.pvpower))

		return None

	def self_consume(self, restrictions:Restrictions, allow_feedin):
		batteryimport = not (Restrictions.GRID2BAT in restrictions)

		self._set_feedin(allow_feedin)

		self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/Setpoint', None) # Normal ESS
		self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/ForceCharge', 0)

		# If importing into battery is allowed, then no restriction, let the
		# setpoint determine that. If disallowed, then only AC-coupled PV may
		# be imported into battery.
		self._set_charge_power(None if batteryimport else self.acpv)

		# Don't limit the MaxDischargePower. If a User opts to select a negative setpoint
		# Same behaviour as regular ESS should apply, despite a bat2grid limitation. (possible)
		self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/MaxDischargePower', -1.0)

	def deactivate(self):
		self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/Setpoint', None)
		self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/ForceCharge', 0)
		self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/MaxDischargePower', -1.0)
		self._aiomonitor.set_value_async(HUB4_SERVICE, '/Overrides/FeedInExcess', 0)
		self._set_charge_power(None)
