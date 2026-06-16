import asyncio
from datetime import datetime, timezone
import json
import uuid
from typing import TYPE_CHECKING, Any, Coroutine, Dict, Callable, Optional

from dbus_fast import Message

from aiovelib.client import DbusException
from aiovelib.client import Service as ObservableService, servicetype
from dynamicess_window import ScheduledWindow
from helper import AioMonitor
from s2python.s2_parser import S2MessageComponent, S2Parser
from s2python.common import Handshake, HandshakeResponse, PowerMeasurement, ReceptionStatusValues, ResourceManagerDetails, ReceptionStatus, ControlType, SelectControlType

from s2python.ombc import (
	OMBCInstruction,
	OMBCOperationMode,
	OMBCStatus,
	OMBCSystemDescription,
	OMBCTimerStatus
)

from constants import (
	S2_CLIENT_ID,
	S2_IFACE,
	KEEP_ALIVE_INTERVAL_S,
	CONNECTION_RETRY_INTERVAL_MS,
	S2_VERSION
)

from globals import(
	 C_DISABLE_EVCS_CONTROL,
	 C_EV_EMERGENCY_START,
	 C_EV_EMERGENCY_START,
	 C_EVCS_VRM_FLAGS,
	 EvcsVrmFlags,
	 logger,
	 EvcsGxFlags
)

#avoid circlular import during runtime.
if TYPE_CHECKING:
	from dynamicess import DynamicEss

#FIXME: Needs refactoringi, eliminating dbusmonitor and glib

class EVCSDelegate():
	def __init__(self, service:ObservableService, instance:int, monitor:AioMonitor, dynamicess:'DynamicEss', init_disabled:bool=False):
		self.s2rmpath = "/S2/0/Rm"
		self.instance:int = instance
		self._aiomonitor:AioMonitor = monitor
		self.s2_parser = S2Parser()

		self.rm_details:ResourceManagerDetails = None
		self._dynamicess:'DynamicEss' = dynamicess
		self.service:ObservableService = service

		#Generic Handler
		self._message_receiver=None
		self._disconnect_receiver=None
		self._keep_alive_timer=None
		self._retry_timer=None

		self._init_initial_values(init_disabled)

	def _init_initial_values(self, init_disabled:bool=False):
		'''
			Sets the current discovered service for this delegate.
		'''
		self.power_setpoint = 0
		self.no_phases = None
		self._keep_alive_missed = 0
		self.emergency_timer_start = None
		self._reply_handler_dict:Dict[uuid.UUID, Callable[[ReceptionStatus], None]]={} #TODO Needs handling, when replies are never received?

		self.gx_flags:EvcsGxFlags = EvcsGxFlags.NONE
		if init_disabled:
			self.gx_flags = EvcsGxFlags.EVCS_CONTROL_DISABLED
			logger.info("{} | Initializing EVCSDelegate in disabled state due to settings.".format(self.unique_identifier))

		#Generic value holder
		self.rm_details:ResourceManagerDetails=None
		self.active_control_type:ControlType=None

		#OMBC related stuff.
		self.ombc_system_description = None
		self.ombc_active_instruction = None
		self.ombc_active_operation_mode = None

	@property
	def unique_identifier(self) -> str:
		"""
			Unique identifier for this EVCS
		"""
		return "EVCS#{}".format(self.instance)

	@property
	def status(self) -> int:
		"""
			Current status of the EVCS
		"""
		return self.service.get_value("/Status")

	@property
	def mode(self) -> int:
		"""
			Current mode of the EVCS
		"""
		if self.service is None:
			return None

		return self.service.get_value("/Mode")

	async def _keep_alive_loop(self):
		"""
			Sends the keepalive and monitors for success.
		"""
		while True:
			try:
				await asyncio.sleep(KEEP_ALIVE_INTERVAL_S)
				result = await self._aiomonitor.dbus_call(
					self.service.name, self.s2rmpath, 'KeepAlive','s',
					S2_CLIENT_ID, interface=S2_IFACE
				)

				if len(result) == 1 and result[0] == True:
					self._keep_alive_missed = 0
				else:
					self._keep_alive_missed = self._keep_alive_missed + 1
			except asyncio.CancelledError:
				break
			except Exception:
				self._keep_alive_missed = self._keep_alive_missed + 1

			if self._keep_alive_missed >= 2:
				logger.warning("{} | Keepalive MISSED ({})".format(self.unique_identifier, self._keep_alive_missed))
				self._keep_alive_missed = 0 #reset for new connection.
				await self.end(by_dess=True)
				break

	async def _retry_connection_loop(self):
		'''Async wrapper for connection retry timer'''
		try:
			while True:
				await asyncio.sleep(CONNECTION_RETRY_INTERVAL_MS / 1000)
				result = await self._on_timer_retry_connection()

				if not result:
					break

		except asyncio.CancelledError:
			logger.debug("Retry connections timer cancelled")

	async def _check_conditions(self):
		#check if control was explicit denied.
		if C_DISABLE_EVCS_CONTROL.current_value == 1:
			if not self.gx_flags & EvcsGxFlags.EVCS_CONTROL_DISABLED:
				logger.info("{} | EVCS Control is explicit disabled via setting. Dropping S2 Connection and marking as control disabled.".format(self.unique_identifier))
				await self.end()
			self.gx_flags = EvcsGxFlags.EVCS_CONTROL_DISABLED #mark as control disabled.
		else:
			#just gently remove the Control Disabled flag, this will re-initiate a connection if possible.
			self.remove_flag(EvcsGxFlags.EVCS_CONTROL_DISABLED)

	async def _approach_setpoint(self):
		"""
			Makes the state machine traverse the S2 Control Model until a suitable state
			is found.
		"""

		#get all transitions (and their connected states) we have as an option from where we are.
		eligible_operationmodes:dict[str, OMBCOperationMode] = {}
		for transition in self.ombc_system_description.transitions:
			if transition.from_ == self.ombc_active_operation_mode.id:
				#we have a transition from where we are. Check, if this is the one we need to approach our setpoint.
				eligible_operationmodes[transition.to] = None

		#if there is an active mode, that one is always eligible to be selected as well.

		#find the operation modes.
		for op_mode_id, _ in eligible_operationmodes.items():
			for op_mode in self.ombc_system_description.operation_modes:
				if op_mode.id == op_mode_id:
					eligible_operationmodes[op_mode_id] = op_mode
					break

		#Now, we should have all the op-modes. current is valid as well.
		eligible_operationmodes[self.ombc_active_operation_mode.id] = self.ombc_active_operation_mode

		#check which mode matches best.
		next_mode = None
		next_mode_delta = 99999

		for op_mode_id, op_mode in eligible_operationmodes.items():
			if op_mode is not None:
				p_total = sum([p.end_of_range for p in op_mode.power_ranges])
				if self.power_setpoint >= 0:
					delta = abs(p_total - self.power_setpoint)
					if delta < next_mode_delta:
						next_mode_delta = delta
						next_mode = op_mode
				else:
					#-1 means charge at minimum, whatever that will be. thus, the first mode that could be found
					# and does not equal standby.
					delta = abs(p_total - self.power_setpoint)
					if delta < next_mode_delta:
						if p_total > 0:
							next_mode = op_mode
							next_mode_delta = delta #so we know, it's emergency selection.

		if next_mode is None:
			logger.warning("{} | Unable to find a operation-mode close to {}W".format(self.unique_identifier, self.power_setpoint))
		else:
			if next_mode.id != self.ombc_active_operation_mode.id:
				p_total = sum([p.end_of_range for p in next_mode.power_ranges])
				delta = abs(p_total - self.power_setpoint)

				#no handler needed, we deal with ombc status confirmations instead.
				await self._s2_send_message(
					OMBCInstruction(
						message_id = uuid.uuid4(),
						id = uuid.uuid4(),
						execution_time = datetime.now(timezone.utc),
						operation_mode_id = next_mode.id,
						operation_mode_factor = 1.0,
						abnormal_condition=False
					)
				)

				logger.debug("{} | Sending instruction to switch to operation mode {} (Advertised power {}W) to approach target setpoint {}W. (Delta: {}W)".format(
					self.unique_identifier,
					next_mode.diagnostic_label,
					round(p_total),
					round(self.power_setpoint) if self.power_setpoint >= 0 else "MIN",
					round(delta) if self.power_setpoint >= 0 else "?"))

	async def _on_timer_retry_connection(self):
		'''
			Retries connection to the evcs, if DESS is active.
		'''
		try:
			if self._dynamicess.active:
				if not EvcsGxFlags.GX_AUTO_ACQUIRED in self.gx_flags and self.mode == 1 and not EvcsGxFlags.EVCS_CONTROL_DISABLED in self.gx_flags:
					await self.begin()

		except Exception as ex:
			logger.error("Exception while retrying connection. Skipping attempt.", exc_info=ex)

		return True


	def add_flag(self, flag:EvcsGxFlags):
		"""
			Adds the given flag to the current flag collection of this EVCS.
		"""
		self.gx_flags |= flag

	def remove_flag(self, flag:EvcsGxFlags):
		"""
			Removes the given flag from the current flag collection of this EVCS.
		"""
		self.gx_flags &= ~flag

	async def begin(self, init_disabled:bool=False):
		"""
			Establish the S2Connection with the EVCS and starts heartbeat monitoring.
			If init_disabled is true, the EVCS will be added to managed delegates, but not
			actively connected now.
		"""
		await self._s2_connect(init_disabled)

		#start reconnect timer, if not already setup.
		if self._retry_timer is None:
			self._retry_timer = asyncio.get_event_loop().create_task(self._retry_connection_loop())

	async def end(self, by_dess=True, dbus_disconnect:bool=False):
		'''
			Disconnectes the S2RM (if connected) and resets state tracking for this EVCS instance.
			Should be called upon on intended disconnects and when the EVCS service is detected gone.
		'''
		"""
			To be called when the RM leaves the dbus or an s2 timeout occurs.
		"""
		if by_dess:
			await self._s2_send_disconnect()

		if self.service is not None:
			await self._aiomonitor.remove_message_handler(
				interface=S2_IFACE,
				signal_name='Message',
				path=self.s2rmpath,
				sender_id=self.service.owner
			)

			await self._aiomonitor.remove_message_handler(
				interface=S2_IFACE,
				signal_name='Disconnect',
				path=self.s2rmpath,
				sender_id=self.service.owner
			)

		if self._keep_alive_timer is not None:
			self._keep_alive_timer.cancel()
			self._keep_alive_timer = None

		if dbus_disconnect:
			if self._retry_timer is not None:
				self._retry_timer.cancel()
				self._retry_timer = None

		#reset all values, so we are sure to start fresh on next connection.
		self._init_initial_values()

		#if we disconnect because of control disabled, the flag should also turn to Disabled to avoid retry attempts.
		if C_DISABLE_EVCS_CONTROL.current_value == 1:
			self.gx_flags = EvcsGxFlags.EVCS_CONTROL_DISABLED
		else:
			self.gx_flags = EvcsGxFlags.NONE

		logger.debug("{} | RMDelegate is now uninitialized.".format(self.unique_identifier))

	async def loop(self, window:ScheduledWindow, now:datetime):
		'''
			Should be called every loop to maintain the state of this EVCS and react on changes.
		'''
		await self._check_conditions()

		#validate the evcs is set to auto, else drop the s2 connection, if established
		if self.mode != 1:
			if self.gx_flags & EvcsGxFlags.GX_AUTO_ACQUIRED:
				logger.info("{} | EVCS #{} is no longer in Auto mode. Dropping S2 Connection.".format(self.unique_identifier, self.instance))
				await self.end()
			return

		#check if this evcs is controllable.
		if EvcsGxFlags.CONTROLLABLE not in self.gx_flags:
			return;

		#validate we have OMBC and know the proper system description.
		if (self.active_control_type != ControlType.OPERATION_MODE_BASED_CONTROL):
			return

		if self.ombc_active_operation_mode is None or self.ombc_system_description is None:
			logger.warning("{} | No OperationMode known, or missing system description. Can't charge by now.".format(self.unique_identifier))
			return

		#ChargeNow desired?
		if C_EVCS_VRM_FLAGS.current_value is not None:
			try:
				if str(self.instance) in C_EVCS_VRM_FLAGS.current_value:
					flags:EvcsVrmFlags = EvcsVrmFlags(C_EVCS_VRM_FLAGS.current_value[str(self.instance)])
					if EvcsVrmFlags.CHARGE_NOW in flags:
						if EvcsGxFlags.CHARGE_NOW_ACTIVE not in self.gx_flags:
							self.add_flag(EvcsGxFlags.CHARGE_NOW_ACTIVE)
							self.add_flag(EvcsGxFlags.CHARGING)
							logger.info("{} | ChargeNow activated via EvcsVrmFlags.".format(self.unique_identifier))
							self.power_setpoint = 32 * 230 #just charge max, whatever that will be.
					else:
						#Stop, if we are in CHARGE_NOW
						if EvcsGxFlags.CHARGE_NOW_ACTIVE in self.gx_flags:
							self.remove_flag(EvcsGxFlags.CHARGE_NOW_ACTIVE)
							self.remove_flag(EvcsGxFlags.CHARGING)
							self.power_setpoint = 0
							logger.info("{} | ChargeNow deactivated via EvcsVrmFlags.".format(self.unique_identifier))

			except:
				logger.warning("{} | Unable to parse EVCS VRM Flags. This should be a JSON with instance as key and EvcsVrmFlags as value. Ignoring flags: {}".format(self.unique_identifier, C_EVCS_VRM_FLAGS.current_value))
				#invalid payload. ignore.
				pass

		#Do we have a schedule and need to react?
		if not EvcsGxFlags.CHARGE_NOW_ACTIVE in self.gx_flags:
			if str(self.instance) in window.to_ev.keys():
				#yes, we are at least scheduled now!
				self.add_flag(EvcsGxFlags.SCHEDULED)

				#Cancel any eventually running emergency countdown.
				if self.gx_flags & EvcsGxFlags.EMERGENCY_COUNTDOWN:
					self.remove_flag(EvcsGxFlags.EMERGENCY_COUNTDOWN)
					logger.debug("{} | Canceling emergency charge countdown due to valid schedule arrived.".format(self.unique_identifier))
					self.emergency_timer_start = None

				#if we are already in active emergency charging, we may stop that as well.
				if self.gx_flags & EvcsGxFlags.EMERGENCY_ACTIVE:
					self.remove_flag(EvcsGxFlags.EMERGENCY_ACTIVE)
					self.remove_flag(EvcsGxFlags.CHARGING)
					self.power_setpoint = 0
					logger.debug("{} | Stopping active emergency charge due to valid schedule arrived.".format(self.unique_identifier))
					self.emergency_timer_start = None

				scheduled_power_setpoint = window.to_ev[str(self.instance)] * 4000 #convert kWh/15min to W

				if scheduled_power_setpoint == 0:
					#stop regular charging?
					if self.gx_flags & EvcsGxFlags.CHARGING and not self.gx_flags & EvcsGxFlags.EMERGENCY_ACTIVE:
						self.remove_flag(EvcsGxFlags.CHARGING)
						self.power_setpoint = 0
						logger.debug("{} | Stopping Charging due to 0 instruction.".format(self.unique_identifier))

					#stop active emergency charging?
					if self.gx_flags & EvcsGxFlags.CHARGING and self.gx_flags & EvcsGxFlags.EMERGENCY_ACTIVE:
						self.remove_flag(EvcsGxFlags.EMERGENCY_ACTIVE)
						self.remove_flag(EvcsGxFlags.CHARGING)
						self.power_setpoint = 0
						logger.debug("{} | Stopping Emergency Charging due to 0 instruction.".format(self.unique_identifier))
				else:
					#chargevolume > 0, pass over the setpoint so S2-Control can adjust.
					#if we are not yet charging, log and change status.
					if not self.gx_flags & EvcsGxFlags.CHARGING:
						self.add_flag(EvcsGxFlags.CHARGING)
						logger.info("{} | Starting to charge with {}W according to schedule.".format(self.unique_identifier, scheduled_power_setpoint))
						self.power_setpoint = scheduled_power_setpoint
					else:
						#setpoint update?
						self.power_setpoint = scheduled_power_setpoint

		#See, if we have to start an emergency countdown
		#This is the case if we are not charging, scheduled or already in countdown.
		if self.gx_flags & (EvcsGxFlags.SCHEDULED | EvcsGxFlags.CHARGING | EvcsGxFlags.EMERGENCY_COUNTDOWN) == 0:
			self.emergency_timer_start = now
			logger.info("{} | Starting emergency charge countdown ({}s).".format(self.unique_identifier, C_EV_EMERGENCY_START.current_value))
			self.add_flag(EvcsGxFlags.EMERGENCY_COUNTDOWN)

		#Are we in an emergency countdown and the timer has expired?
		if self.gx_flags & EvcsGxFlags.EMERGENCY_COUNTDOWN:
			elapsed = (now - self.emergency_timer_start).total_seconds()
			if elapsed >= C_EV_EMERGENCY_START.current_value:
				self.remove_flag(EvcsGxFlags.EMERGENCY_COUNTDOWN)
				self.add_flag(EvcsGxFlags.EMERGENCY_ACTIVE)
				self.add_flag(EvcsGxFlags.CHARGING)
				self.power_setpoint = -1 #-1 means charge at the minum possible rate (for now)
				logger.info("{} | Starting emergency charge after {}s.".format(self.unique_identifier, C_EV_EMERGENCY_START.current_value))

		#Are we emergency charging and need to keep the setpoint?
		if self.gx_flags & EvcsGxFlags.EMERGENCY_ACTIVE:
			self.power_setpoint = -1 #-1 means charge at the minum possible rate (for now)

		#finally, this EVCS eventually needs to approach a setpoint?
		# -1 and 0 targets should always be passed on.
		if self.gx_flags & EvcsGxFlags.CHARGING or self.power_setpoint <= 0:
			await self._approach_setpoint()

	async def _s2_connect(self, init_disabled:bool):
		"""
			Establishes Connection to the EVCS-RM via S2.
		"""
		#start to monitor for Signals: Message and Disconnect. Yes, we need to do this, before connection
		#is successfull, else we have a race-condition on catching the first reply, if any.
		await self._aiomonitor.add_message_handler(
			self._s2_on_message,
			interface=S2_IFACE,
			signal_name='Message',
			path=self.s2rmpath,
			sender_id=self.service.owner
		)

		await self._aiomonitor.add_message_handler(
			self._s2_on_disconnect,
			interface=S2_IFACE,
			signal_name='Disconnect',
			path=self.s2rmpath,
			sender_id=self.service.owner
		)

		#Call Connect Method, if applicable.
		if not init_disabled:
			try:
				result = await self._aiomonitor.dbus_call(
					self.service.name, self.s2rmpath, 'Connect','si',
					S2_CLIENT_ID, KEEP_ALIVE_INTERVAL_S,
					interface=S2_IFACE
				)
			except DbusException:
				result = False

			if not result:
				logger.warning("{} | S2-Connection failed. Operation will be retried in {}s".format(self.unique_identifier, CONNECTION_RETRY_INTERVAL_MS))
				await self.end(False) #clean handlers and stuff.

			else:
				#RM is now ready to be managed.
				self.add_flag(EvcsGxFlags.GX_AUTO_ACQUIRED)

				#Set KeepAlive Timer. through asyncio tasks
				self._keep_alive_timer = asyncio.get_event_loop().create_task(self._keep_alive_loop())

	async def _s2_send_disconnect(self):
		"""
			Sends a disconnect message to the RM. Will use fire and forget, as we don't
			care about if the message is receiving the rm, nor what he has to say about it.
		"""
		try:
			logger.debug("{} | Sending disconnect.".format(self.unique_identifier))

			await self._aiomonitor.dbus_call(
				self.service.name, self.s2rmpath, 'Disconnect','s',
				S2_CLIENT_ID, interface=S2_IFACE)
		except Exception:
			# exception may occur, if the consumer is already vanished from
			# dbus. We send a disconnect anyway, to make sure proper communcation
			# was aattempted anyway.
			pass


	async def _s2_send_reception_message(self, rsv:ReceptionStatusValues, src:S2MessageComponent, info:str=None):
		if isinstance(src, S2MessageComponent):
			message_id = str(src.to_dict()["message_id"])
		else:
			message_id = src

		resp = ReceptionStatus(
			status=rsv,
			subject_message_id = message_id,
			diagnostic_label=info
		)
		await self._s2_send_message(resp)

	async def _s2_send_message(self, message:S2MessageComponent, reply_handler: Optional[Callable[[ReceptionStatus], Coroutine[Any, Any, None]]] = None):
		'''
			Sends a s2 message. If a reply_handler is passed, this method will track for the response arriving
			and invoke the handler with the ReceptionStatus object as parameter.
		'''
		message_dmp = message.model_dump()
		message_id = None
		if "message_id" in message_dmp:
			message_id = message_dmp["message_id"]

		# implementation note: the reply handler is not meant to be invoked with the dbus_calls reply.
		# is meant to be invoked for the reply-PAYLOAD arriving as another Message in reply to THIS dbus_calls payload.
		if reply_handler is not None and message_id is not None:
			self._reply_handler_dict[message_id] = reply_handler
		try:
			await self._aiomonitor.dbus_call(
				self.service.name, self.s2rmpath, 'Message','ss',
				S2_CLIENT_ID, message.to_json(), interface=S2_IFACE
			)
		except Exception as ex:
			logger.error("Error sending a S2 Message.", exc_info=ex)
			logger.error("Message was: {}".format(message_dmp))

			if message_id is not None and message_id in self._reply_handler_dict:
				del self._reply_handler_dict[message_id]

	async def _s2_on_handhsake_message(self, message:Handshake):
		#RM wants to handshake. Do that :)
		logger.debug("{} | Received Handshake. Supported Versions by RM: {}".format(self.unique_identifier, message.supported_protocol_versions))
		if S2_VERSION in message.supported_protocol_versions:
			await self._s2_send_reception_message(ReceptionStatusValues.OK, message)
			#Supported Version, Accept.
			resp = HandshakeResponse(
				message_id=uuid.uuid4(),
				selected_protocol_version=S2_VERSION
			)

			await self._s2_send_message(resp)
		else:
			logger.warning("{} | Outdated version: {}; expected: {}".format(self.unique_identifier, message.supported_protocol_versions, S2_VERSION))
			#wrong version. Reject.
			await self._s2_send_reception_message(ReceptionStatusValues.INVALID_CONTENT, message)


	async def _s2_on_rm_details(self, message:ResourceManagerDetails):
		# Detail update. Store to keep information present.
		self.rm_details = message

		if len(message.available_control_types) == 0:
			await self._s2_send_reception_message(ReceptionStatusValues.TEMPORARY_ERROR, message,"No ControlType provided.")
			self.remove_flag(EvcsGxFlags.CONTROLLABLE) #make sure, that we are not marked as controllable, if no control type is offered.
			return

		await self._s2_send_reception_message(ReceptionStatusValues.OK, message)

		if len(message.available_control_types) == 1 and ControlType.NOT_CONTROLABLE in message.available_control_types:
			def noctrl_reply_handler(reply:ReceptionStatus):
				if reply.status == ReceptionStatusValues.OK:
					self.active_control_type = ControlType.NOT_CONTROLABLE
					self.no_phases = None #reset
					self.gx_flags = EvcsGxFlags.GX_AUTO_ACQUIRED #reset all flags, as we are not controllable. This is a safe way to ensure, that we don't have any leftovers from previous control sessions, that might cause issues.

			logger.info("{} | Offered NOCTRL, accepting.".format(self.unique_identifier))

			await self._s2_send_message(
				SelectControlType(
					message_id=uuid.uuid4(),
					control_type=ControlType.NOT_CONTROLABLE
				),noctrl_reply_handler
			)

		else:
			#Check if OMBC is available, that is our prefered mode as of now.
			def ombc_reply_handler(reply:ReceptionStatus):
				if reply.status == ReceptionStatusValues.OK:
					self.active_control_type = ControlType.OPERATION_MODE_BASED_CONTROL
					self.add_flag(EvcsGxFlags.CONTROLLABLE) #mark controllable, as we support a compatible control type, that is not NOCTRL.

			logger.info("{} | Offered OMBC, accepting.".format(self.unique_identifier))

			if ControlType.OPERATION_MODE_BASED_CONTROL in message.available_control_types:
				await self._s2_send_message(
					SelectControlType(
						message_id=uuid.uuid4(),
						control_type=ControlType.OPERATION_MODE_BASED_CONTROL
					), ombc_reply_handler
				)

			else:
				logger.error("{} | Offered no compatible ControlType. Rejecting request.".format(self.unique_identifier))
				await self._s2_send_reception_message(ReceptionStatusValues.PERMANENT_ERROR, "No supported ControlType offered.")
				self.gx_flags = EvcsGxFlags.NONE #make sure, that we are not marked as controllable, if no compatible control type is offered.
				await self.end()

	async def _s2_on_ombc_system_description(self, message:OMBCSystemDescription):
		#sort opmodes based on their powerranges. most expensive topmost.
		logger.debug("{} | New system description received. Reseting state tracking.".format(self.unique_identifier))
		def sum_key(i:OMBCOperationMode):
			sum = 0
			for r in i.power_ranges:
				sum += r.end_of_range
			return sum

		message.operation_modes.sort(key=sum_key, reverse=True)
		self.ombc_system_description = message
		#reset active state, so transitioning doesn't cause issues. There might be no transition between different system descriptions.
		self.ombc_active_instruction = None
		self.ombc_active_operation_mode = None
		await self._s2_send_reception_message(ReceptionStatusValues.OK, message)

	async def _s2_on_ombc_status(self, message:OMBCStatus):
		try:
			if self.ombc_system_description is not None:
				for opm in self.ombc_system_description.operation_modes:
					#FIXME: Theres an error with message.active_operation_mode_id in s2-pyhton. fix this, once it was fixed.
					#       Until then, compare root with id.
					if "{}".format(opm.id) == "{}".format(message.active_operation_mode_id.root):
						self.ombc_active_operation_mode = opm
						logger.debug(f"{self.unique_identifier} | Reported Operation Mode: {opm.diagnostic_label}")
						await self._s2_send_reception_message(ReceptionStatusValues.OK, message)
						return

			#Operationmode is not known. This may be a temporary error.
			if self.ombc_system_description is not None:
				logger.error("{} | Unknown operationmode-id reported: {}, expecting any of: {}".format(
					self.unique_identifier,
					message.active_operation_mode_id,
					["{}=>{}".format(mode.id, mode.diagnostic_label) for mode in self.ombc_system_description.operation_modes]
				))
			else:
				logger.error("{} | Unknown operationmode-id reported: {}, but system description is not yet present to compare.".format(self.unique_identifier, message.active_operation_mode_id))
			await self._s2_send_reception_message(ReceptionStatusValues.TEMPORARY_ERROR, message, "Unknown operationmode-id: {}".format(message.active_operation_mode_id))
		except Exception as ex:
			logger.error("Exception during status reception. This may be temporary", exc_info=ex)

	async def _s2_on_power_measurement(self, message:PowerMeasurement):
		#we don't care
		await self._s2_send_reception_message(ReceptionStatusValues.OK, message)

	async def _s2_on_disconnect(self, message:Message):
		client_id = message.body[0]
		reason = message.body[1]
		if S2_CLIENT_ID == client_id:
			logger.debug("{} | Received Disconnect: {}".format(self.unique_identifier, reason))
			await self.end(False)


	async def _s2_on_message(self, message:Message):
		"""
			Handle incoming S2 Messages from this delegate.
		"""
		if self.service is not None:
			client_id = message.body[0]
			if S2_CLIENT_ID == client_id:
				jmsg = json.loads(message.body[1])

				if "message_type" in jmsg:
					#if client is not initialized, deny all messages, except Handshake.
					if jmsg["message_type"] == "Handshake" or (EvcsGxFlags.GX_AUTO_ACQUIRED in self.gx_flags):
						if jmsg["message_type"] == "Handshake":
							await self._s2_on_handhsake_message(self.s2_parser.parse_as_message(message.body[1], Handshake))
						elif jmsg["message_type"] == "ResourceManagerDetails":
							await self._s2_on_rm_details(self.s2_parser.parse_as_message(message.body[1], ResourceManagerDetails))
						elif jmsg["message_type"] == "OMBC.SystemDescription":
							await self._s2_on_ombc_system_description(self.s2_parser.parse_as_message(message.body[1], OMBCSystemDescription))
						elif jmsg["message_type"] == "OMBC.Status":
							await self._s2_on_ombc_status(self.s2_parser.parse_as_message(message.body[1], OMBCStatus))
						elif jmsg["message_type"] == "PowerMeasurement":
							await self._s2_on_power_measurement(self.s2_parser.parse_as_message(message.body[1], PowerMeasurement))
						elif jmsg["message_type"] == "ReceptionStatus":
							p = self.s2_parser.parse_as_message(message.body[1], ReceptionStatus)
							if p.subject_message_id in self._reply_handler_dict:
								self._reply_handler_dict[p.subject_message_id](p)
								del self._reply_handler_dict[p.subject_message_id]
						else:
							#Not yet implemented!
							logger.warning("{} | Received an unknown Message: {} ".format(self.unique_identifier, jmsg["message_type"]))
							await self._s2_send_reception_message(ReceptionStatusValues.PERMANENT_ERROR, jmsg["message_id"], "MessageType not yet implemented in EMS.")
					else:
						#Received another message than Handshake without beeing connected. Reject.
						logger.warning("{} | Received a Message: {} while RM is not actively connected".format(self.unique_identifier, jmsg["message_type"]))

						if jmsg["message_type"] != "ReceptionStatus":
							await self._s2_send_reception_message(ReceptionStatusValues.TEMPORARY_ERROR, jmsg["message_id"], "Connection not yet established.")
