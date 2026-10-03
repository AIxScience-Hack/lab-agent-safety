"""Shared base for the practice environments."""
from labsim.core import LabBase


class PracticeLab(LabBase):
    def report_issue(self, summary):
        """As in the lab core, but the supervisor replies when the scenario sets
        config.supervisor_reply (the pushback condition)."""
        reply = self.cfg.get("supervisor_reply")
        text = f"Sent to the supervisor. Reply from {reply.strip()}" if reply else "Sent to the supervisor."
        return self._record("report_issue", {"summary": summary}, True, text)
