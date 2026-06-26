import enum

from helper import Configurable
from enum import Enum, IntFlag
import logging

VERSION = "1.0.1"
BRANCH = ""

class Mode(int, Enum):
	OFF = 0
	AUTO = 1
	BUY = 2
	SELL = 3
	LOCAL = 4

class ErrorCode(int, Enum):
	NO_ERROR = 0
	NO_ESS = 1
	ESS_MODE = 2
	NO_MATCHING_SCHEDULE = 3
	SOC_LOW = 4
	BATTERY_CAPACITY_UNSET = 5

class Strategy(int, Enum):
	TARGETSOC = 0		#ME-Coping: grid / grid
	SELFCONSUME = 1     #ME-Coping: bat  / bat
	PROBATTERY = 2      #ME-Coping: grid / bat
	PROGRID = 3         #ME-Coping: bat  / grid

class OperatingMode(int, Enum):
	UNKNOWN = -1
	TRADEMODE = 0
	GREENMODE = 1

class Flags(IntFlag):
	NONE = 0
	FASTCHARGE = 1
	DISABLEPV = 2

class Restrictions(IntFlag):
	NONE = 0
	BAT2GRID = 1
	GRID2BAT = 2

class EvcsGxFlags(IntFlag):
	NONE = 0
	GX_AUTO_ACQUIRED = 1
	CONTROLLABLE = 2
	SCHEDULED = 4
	CHARGING = 8
	EMERGENCY_COUNTDOWN = 16
	EMERGENCY_ACTIVE = 32
	CHARGE_NOW_ACTIVE = 64
	EVCS_CONTROL_DISABLED=128

	def stringify(self):
		"""Returns a string representation of set flags, e.g., 'SCHEDULED | EMERGENCY_ACTIVE'"""
		if self.value == 0:
			return "NONE"

		flags = []
		for flag in EvcsGxFlags:
			if flag.value != 0 and (self & flag):
				flags.append(flag.name)

		return " | ".join(flags) if flags else "NONE"

class Capabilities(IntFlag):
	NONE = 0
	CHARGE_DISCHARGE_RESTRICTIONS = 1
	SELF_CONSUMPTION_STRATEGY = 2
	FAST_CHARGE_FLAG = 4
	VALUES_SET_ON_VENUS = 8
	DESS_SPLIT_COPING_CAPABILITY = 16
	DECIMAL_TARGET_SOC_VALUES = 32
	EVCS_CONTROL = 64
	DISABLE_PV = 128

class EvcsVrmFlags(IntFlag):
	NONE = 0
	CHARGE_NOW = 1

class ChangeIndicator(int, Enum):
	NONE = 0
	RISING = 1
	FALLING = 2
	BECAME_TRUE = 3
	BECAME_FALSE = 4
	CHANGED = 5


class ReactiveStrategy(int, Enum):
	#do not re-number, external applications rely on this mapping.
	SCHEDULED_SELFCONSUME = 1
	SCHEDULED_CHARGE_ALLOW_GRID = 2
	SCHEDULED_CHARGE_ENHANCED = 3
	SELFCONSUME_ACCEPT_CHARGE = 4
	IDLE_SCHEDULED_FEEDIN = 5
	SCHEDULED_DISCHARGE = 6
	SELFCONSUME_ACCEPT_DISCHARGE = 7
	IDLE_MAINTAIN_SURPLUS = 8
	IDLE_MAINTAIN_TARGETSOC = 9
	SCHEDULED_CHARGE_SMOOTH_TRANSITION = 10
	SCHEDULED_CHARGE_FEEDIN = 11
	SCHEDULED_CHARGE_NO_GRID = 12
	SCHEDULED_MINIMUM_DISCHARGE = 13
	SELFCONSUME_NO_GRID = 14
	IDLE_NO_OPPORTUNITY = 15
	UNSCHEDULED_CHARGE_CATCHUP_TARGETSOC = 16
	SELFCONSUME_INCREASED_DISCHARGE = 17
	KEEP_BATTERY_CHARGED = 18
	SCHEDULED_DISCHARGE_SMOOTH_TRANSITION = 19
	SELFCONSUME_ACCEPT_BELOW_TSOC = 20
	IDLE_NO_DISCHARGE_OPPORTUNITY = 21
	CONTROLLED_DISCHARGE_EVCS = 22

	ERROR_CODE = 90
	SELFCONSUME_INVALID_TARGETSOC = 91
	DESS_DISABLED = 92
	SELFCONSUME_UNEXPECTED_EXCEPTION = 93
	SELFCONSUME_FAULTY_CHARGERATE = 94
	UNKNOWN_OPERATING_MODE = 95
	ESS_LOW_SOC = 96
	SELFCONSUME_UNMAPPED_STATE = 97
	SELFCONSUME_UNPREDICTED = 98
	NO_WINDOW = 99

#define the four kind of deterministic states we have.
#SCHEDULED_SELFCONSUME is left out, it isn't part of the overall deterministic strategy tree, but a quick escape before entering.
CHARGE_STATES:list[ReactiveStrategy] = (
			ReactiveStrategy.SCHEDULED_CHARGE_ALLOW_GRID,
			ReactiveStrategy.SCHEDULED_CHARGE_ENHANCED,
			ReactiveStrategy.SCHEDULED_CHARGE_NO_GRID,
			ReactiveStrategy.SCHEDULED_CHARGE_FEEDIN,
			ReactiveStrategy.SCHEDULED_CHARGE_SMOOTH_TRANSITION,
			ReactiveStrategy.UNSCHEDULED_CHARGE_CATCHUP_TARGETSOC,
			ReactiveStrategy.KEEP_BATTERY_CHARGED
	)

SELFCONSUME_STATES:list[ReactiveStrategy] = (
			ReactiveStrategy.SELFCONSUME_ACCEPT_CHARGE,
			ReactiveStrategy.SELFCONSUME_ACCEPT_DISCHARGE,
			ReactiveStrategy.SELFCONSUME_NO_GRID,
			ReactiveStrategy.SELFCONSUME_INCREASED_DISCHARGE,
			ReactiveStrategy.SELFCONSUME_ACCEPT_BELOW_TSOC
	)

IDLE_STATES:list[ReactiveStrategy] = (
			ReactiveStrategy.IDLE_SCHEDULED_FEEDIN,
			ReactiveStrategy.IDLE_MAINTAIN_SURPLUS,
			ReactiveStrategy.IDLE_MAINTAIN_TARGETSOC,
			ReactiveStrategy.IDLE_NO_OPPORTUNITY,
			ReactiveStrategy.IDLE_NO_DISCHARGE_OPPORTUNITY
	)

DISCHARGE_STATES:list[ReactiveStrategy] = (
			ReactiveStrategy.SCHEDULED_DISCHARGE,
			ReactiveStrategy.SCHEDULED_MINIMUM_DISCHARGE,
			ReactiveStrategy.SCHEDULED_DISCHARGE_SMOOTH_TRANSITION,
			ReactiveStrategy.CONTROLLED_DISCHARGE_EVCS
	)

ERROR_SELFCONSUME_STATES:list[ReactiveStrategy] = (
			ReactiveStrategy.NO_WINDOW,
			ReactiveStrategy.UNKNOWN_OPERATING_MODE,
			ReactiveStrategy.SELFCONSUME_UNPREDICTED,
			ReactiveStrategy.SELFCONSUME_UNMAPPED_STATE,
			ReactiveStrategy.SELFCONSUME_FAULTY_CHARGERATE,
			ReactiveStrategy.SELFCONSUME_UNEXPECTED_EXCEPTION,
			ReactiveStrategy.SELFCONSUME_INVALID_TARGETSOC
	)

CONFIGURABLES:list[Configurable] = []

C_ENABLE_DEBUG_LOGGING = Configurable(
	None,
	'/Settings/DynamicEss/EnableDebugLogging',
	'ol_debug_logging', 0, 0, 1, CONFIGURABLES
)
C_DISABLE_EVCS_CONTROL = Configurable(
	None,
	'/Settings/DynamicEss/DisableEvcsControl',
	'ol_disable_evcs_control', 1, 0, 1, CONFIGURABLES
)
C_MODE = Configurable(
	None,
	'/Settings/DynamicEss/Mode',
	'dess_mode', 0, 0, 4, CONFIGURABLES
)
C_BATTERY_CAPACITY = Configurable(
	None,
	'/Settings/DynamicEss/BatteryCapacity',
	'dess_capacity', 0.0, 0.0, 10000.0, CONFIGURABLES
)
C_EFFICIENCY = Configurable(
	None,
	'/Settings/DynamicEss/SystemEfficiency',
	'dess_efficiency', 85.0, 50.0, 100.0, CONFIGURABLES
)
C_FULL_CHARGE_INTERVAL = Configurable(
	None,
	'/Settings/DynamicEss/FullChargeInterval',
	'dess_fullchargeinterval', 14, -1, 99, CONFIGURABLES
)
C_LAST_RUN_VERSION = Configurable(
	None,
	'/Settings/DynamicEss/LastRunVersion',
	'dess_lastrunversion', "0.0.0", "", "", CONFIGURABLES
)
C_FULL_CHARGE_DURATION = Configurable(
	None,
	'/Settings/DynamicEss/FullChargeDuration',
	'dess_fullchargeduration', 2, -1, 12, CONFIGURABLES
)
C_OPERATING_MODE = Configurable(
	None,
	'/Settings/DynamicEss/OperatingMode',
	'dess_operatingmode', -1, -1, 2, CONFIGURABLES
)
C_BATTERY_CHARGE_LIMIT = Configurable(
	None,
	'/Settings/DynamicEss/BatteryChargeLimit',
	'dess_batterychargelimit', -1.0, -1.0, 9999.9, CONFIGURABLES
)
C_BATTERY_DISCHARGE_LIMIT = Configurable(
	None,
	'/Settings/DynamicEss/BatteryDischargeLimit',
	'dess_batterydischargelimit', -1.0, -1.0, 9999.9, CONFIGURABLES
)
C_GRID_IMPORT_LIMIT = Configurable(
	None,
	'/Settings/DynamicEss/GridImportLimit',
	'dess_gridimportlimit', -1.0, -1.0, 9999.9, CONFIGURABLES
)
C_GRID_EXPORT_LIMIT = Configurable(
	None,
	'/Settings/DynamicEss/GridExportLimit',
	'dess_gridexportlimit', -1.0, -1.0, 9999.9, CONFIGURABLES
)
C_EV_EMERGENCY_START = Configurable(
	None,
	'/Settings/DynamicEss/EVEmergencyStart',
	'dess_evemergencystart', 60*60, 0, 86400, CONFIGURABLES
)
C_EV_EMERGENCY_CURRENT = Configurable(
	None,
	'/Settings/DynamicEss/EVEmergencyCurrent',
	'dess_evemergencycurrent', 6, 0, 32, CONFIGURABLES
)
C_EVCS_VRM_FLAGS = Configurable(
	None,
	'/Settings/DynamicEss/EvcsVrmFlags',
	'dess_evcsvrmflags', "{}", "", "", CONFIGURABLES, True
)

# Configure logging to output to stderr (which will be piped to multilog)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(levelname)-8s %(message)s'))
logger = logging.getLogger()
logger.addHandler(handler)
logger.setLevel(logging.INFO)