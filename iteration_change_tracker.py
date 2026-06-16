
from datetime import datetime
import logging

from globals import ChangeIndicator, logger


class IterationChangeTracker(object):
	'''
		The iteration change tracker analyzes changes occuring between iterations, if the actual strategy may depend on the triggering factor.
	'''
	def __init__(self, delegate):
		self._current_soc = None
		self._current_target_soc = None
		self._current_nw_tsoc_higher = None
		self._current_nw_tsoc_lower = None
		self._delegate = delegate

		self._previous_reactive_strategy = None
		self._previous_soc = None
		self._previous_target_soc = None
		self._previous_nw_tsoc_higher = None
		self._previous_nw_tsoc_lower = None

	def _check_soc_precision(self, soc):
		"""
			Determines the soc precision of the current soc value.
		"""
		p = 0
		x = round(soc, 2)
		for _ in range(2):
			p += 1
			x *= 10
			if x % 10 < 1e-2:
				return p - 1
		return 2

	def input(self, soc, soc_raw, target_soc, nw_tsoc_higher, nw_tsoc_lower):
		self._current_soc = soc
		self._current_target_soc = target_soc
		self._current_nw_tsoc_higher = nw_tsoc_higher
		self._current_nw_tsoc_lower = nw_tsoc_lower

		#determine if soc precision is higher than currently used. Round to 8 to avoid
		#issues like 1.1 would become 1.1000000000000001 and therefore an unreal precision.
		if self._delegate.soc_precision < 2:
			prec = self._check_soc_precision(soc_raw)
			if (prec > self._delegate.soc_precision):
				self._delegate.soc_precision = min(prec,2)

		#log changes as well.
		tme = datetime.today().strftime('%H:%M:%S')
		#if self.soc_change() != ChangeIndicator.NONE:
		#	logger.log(logging.DEBUG, "detected soc change from {} to {}, identified as: {}".format(
		#		self._previous_soc if self._previous_soc is not None else "None",
		#		self._current_soc,
		#		self.soc_change().name
		#	))

		#if self.target_soc_change() != ChangeIndicator.NONE:
		#	logger.log(logging.DEBUG, "detected target soc change from {} to {}, identified as: {}".format(
		#		self._previous_target_soc if self._previous_target_soc is not None else "None",
		#		self._current_target_soc if self._current_target_soc is not None else "None",
		#		self.target_soc_change().name
		#	))

		#if self.nw_tsoc_higher_change() != ChangeIndicator.NONE:
		#	logger.log(logging.DEBUG, "detected nw higher tsoc change from {} to {}, identified as: {}".format(
		#		self._previous_nw_tsoc_higher if self._previous_nw_tsoc_higher is not None else "None",
		#		self._current_nw_tsoc_higher,
		#		self.nw_tsoc_higher_change().name
		#	))

		#if self.nw_tsoc_lower_change() != ChangeIndicator.NONE:
		#	logger.log(logging.DEBUG, "detected nw lower tsoc change from {} to {}, identified as: {}".format(
		#		self._previous_nw_tsoc_lower if self._previous_nw_tsoc_lower is not None else "None",
		#		self._current_nw_tsoc_lower,
		#		self.nw_tsoc_lower_change().name
		#	))

	def soc_change(self) -> ChangeIndicator:
		if self._current_soc is None or self._current_soc == self._previous_soc:
			return ChangeIndicator.NONE

		if self._previous_soc is None or self._current_soc > self._previous_soc:
			return ChangeIndicator.RISING

		elif self._current_soc < self._previous_soc:
			return ChangeIndicator.FALLING

	def target_soc_change(self) -> ChangeIndicator:
		#handle None as 0 for indication
		ps = self._previous_target_soc or 0
		cs = self._current_target_soc or 0

		if ps < cs:
			return ChangeIndicator.RISING
		elif ps > cs:
			return ChangeIndicator.FALLING

		return ChangeIndicator.NONE

	def nw_tsoc_higher_change(self) -> ChangeIndicator:
		if self._current_nw_tsoc_higher is None or self._current_nw_tsoc_higher == self._previous_nw_tsoc_higher:
			return ChangeIndicator.NONE

		if self._current_nw_tsoc_higher and (self._previous_nw_tsoc_higher is None or not self._previous_nw_tsoc_higher):
			return ChangeIndicator.BECAME_TRUE

		elif not self._current_nw_tsoc_higher and (self._previous_nw_tsoc_higher is None or self._previous_nw_tsoc_higher):
			return ChangeIndicator.BECAME_FALSE

	def nw_tsoc_lower_change(self) -> ChangeIndicator:
		if self._current_nw_tsoc_lower is None or self._current_nw_tsoc_lower == self._previous_nw_tsoc_lower:
			return ChangeIndicator.NONE

		if self._current_nw_tsoc_lower and (self._previous_nw_tsoc_lower is None or not self._previous_nw_tsoc_lower):
			return ChangeIndicator.BECAME_TRUE

		elif not self._current_nw_tsoc_lower and (self._previous_nw_tsoc_lower is None or self._previous_nw_tsoc_lower):
			return ChangeIndicator.BECAME_FALSE

	def done(self, reactive_strategy):
		self._previous_soc = self._current_soc
		self._previous_target_soc = self._current_target_soc
		self._previous_nw_tsoc_higher = self._current_nw_tsoc_higher
		self._previous_nw_tsoc_lower = self._current_nw_tsoc_lower
		self._current_soc = None
		self._current_target_soc = None
		self._current_nw_tsoc_higher = None
		self._current_nw_tsoc_lower = None

		if (self._previous_reactive_strategy != reactive_strategy):
			tme = datetime.today().strftime('%H:%M:%S')
			logger.log(logging.INFO, "Strategy switch from {} to {}".format(
				self._previous_reactive_strategy.name if self._previous_reactive_strategy is not None else "None",
				reactive_strategy.name))

		self._previous_reactive_strategy = reactive_strategy
