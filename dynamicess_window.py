import json
from datetime import timedelta
from globals import Restrictions, Flags

class ScheduledWindow(object):
	def __init__(self, starttime, duration):
		self.start = starttime
		self.stop = self.start + timedelta(seconds=duration)

	def __contains__(self, t):
		return self.start <= t < self.stop

	def __eq__(self, other):
		return self.start == other.start and self.stop == other.stop

	def __repr__(self):
		return "Start: {}, Stop: {}".format(self.start, self.stop)

class DynamicEssWindow(ScheduledWindow):

	def __init__(self, start, duration, soc, targetsoc, allow_feedin, restrictions, strategy, flags, slot, to_ev):
		super(DynamicEssWindow, self).__init__(start, duration)
		self.soc:float = targetsoc if (targetsoc is not None and targetsoc > 0) else soc #legacy support: fall back to /Soc, when /Targetsoc is 0 (default value)
		self.allow_feedin:bool = allow_feedin
		self.restrictions:Restrictions = Restrictions(restrictions)
		self.strategy:int = strategy
		self.flags:Flags = Flags(flags)
		self.slot:int = slot
		self.duration:int = duration
		self.to_ev:dict = json.loads(to_ev) if to_ev is not None and to_ev != "" else {}

	def get_window_progress(self, now) -> float:
		""" returns the progress of the window, 0.00 - 100.00. If the window is not or no longer active, this returns none.
			current time shall be passed as now, to ensure same result throughout multiple calls.
		"""

		if (now < self.start or now > self.stop):
			return None
		elif (now == self.start):
			return 0.00
		elif (now == self.stop):
			return 100.0

		passed_seconds = now - self.start
		progress = passed_seconds.total_seconds() / self.duration * 100.0
		return progress

	def __repr__(self):
		return "Start: {}, Stop: {}, Soc: {}".format(
			self.start, self.stop, self.soc)
