"""Working-day calendar used by CPM, forecasting, Monte Carlo and the optimiser.

Offsets are counted in working days from the project's first working day.
With a 7-day week and no holidays, working days equal calendar days.
"""
from datetime import timedelta

from odoo import fields, models

WORK_WEEKS = {
    "7": frozenset(range(7)),
    "6": frozenset(range(6)),   # Monday to Saturday
    "5": frozenset(range(5)),   # Monday to Friday
}


class WorkCalendar:
    """Lightweight working-day arithmetic (no ORM access)."""

    def __init__(self, start, work_week="7", holidays=()):
        self.workdays = WORK_WEEKS.get(str(work_week or "7"), WORK_WEEKS["7"])
        self.holidays = frozenset(holidays or ())
        self.start = self.next_workday(start)

    def is_workday(self, day):
        return day.weekday() in self.workdays and day not in self.holidays

    def next_workday(self, day):
        guard = 0
        while not self.is_workday(day):
            day += timedelta(days=1)
            guard += 1
            if guard > 3660:
                raise ValueError("No working day found within 10 years; check the calendar.")
        return day

    def prev_workday(self, day):
        guard = 0
        while not self.is_workday(day):
            day -= timedelta(days=1)
            guard += 1
            if guard > 3660:
                raise ValueError("No working day found within 10 years; check the calendar.")
        return day

    def shift(self, day, count):
        """Move ``count`` working days from ``day`` (normalised to a working day first)."""
        if count >= 0:
            day = self.next_workday(day)
            step = timedelta(days=1)
        else:
            day = self.prev_workday(day)
            step = timedelta(days=-1)
        remaining = abs(int(count))
        while remaining:
            day += step
            if self.is_workday(day):
                remaining -= 1
        return day

    def date_at(self, offset):
        return self.shift(self.start, offset)

    def offset_of(self, day):
        """Signed number of working days from the calendar start to ``day``."""
        day = self.next_workday(day)
        if day >= self.start:
            count, cursor = 0, self.start
            while cursor < day:
                cursor += timedelta(days=1)
                if self.is_workday(cursor):
                    count += 1
            return count
        count, cursor = 0, self.start
        while cursor > day:
            cursor -= timedelta(days=1)
            if self.is_workday(cursor):
                count -= 1
        return count

    def workdays_between(self, first, last):
        """Working days from ``first`` to ``last``, both included (0 if last < first)."""
        if last < first:
            return 0
        count, cursor = 0, first
        while cursor <= last:
            if self.is_workday(cursor):
                count += 1
            cursor += timedelta(days=1)
        return count

    def finish_from_start(self, start, duration):
        """Last working day of an activity that starts on ``start`` and lasts ``duration`` days."""
        start = self.next_workday(start)
        return self.shift(start, max(int(duration) - 1, 0))


class EpcHoliday(models.Model):
    _name = "epc.holiday"
    _description = "EPC Non-working Day"
    _order = "date"

    name = fields.Char(required=True)
    date = fields.Date(required=True)
    project_id = fields.Many2one("project.project", ondelete="cascade", index=True,
                                 help="Leave empty for a company-wide holiday.")
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, index=True)

    _date_project_uniq = models.Constraint(
        "unique(date, project_id, company_id)",
        "This holiday is already defined.",
    )


class ProjectProjectCalendar(models.Model):
    _inherit = "project.project"

    epc_work_week = fields.Selection(
        [("7", "7 days a week"), ("6", "Monday to Saturday"), ("5", "Monday to Friday")],
        string="Working Week", default="7", required=True,
        help="Durations, lags and forecasts are counted in working days of this calendar.")
    epc_holiday_ids = fields.One2many("epc.holiday", "project_id", string="Project Holidays")

    def _epc_calendar(self, start=None):
        self.ensure_one()
        Holiday = self.env["epc.holiday"].sudo()
        holidays = Holiday.search([
            "|", ("project_id", "=", self.id),
            "&", ("project_id", "=", False), ("company_id", "in", (self.company_id or self.env.company).ids),
        ]).mapped("date")
        start = start or self.date_start or fields.Date.context_today(self)
        return WorkCalendar(start, self.epc_work_week or "7", holidays)
