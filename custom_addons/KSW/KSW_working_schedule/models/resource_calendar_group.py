from odoo import fields, models


class ResourceCalendarGroup(models.Model):
    _name = 'resource.calendar.group'
    _description = 'Resource Calendar Group'

    name = fields.Char(string='Name', required=True)

    line_ids = fields.One2many(
        'resource.calendar.group.line',
        'calendar_group_id',
        string='Attendance Lines',
        copy=True,
    )

    active = fields.Boolean(default=True)


class ResourceCalendarGroupLine(models.Model):
    _name = 'resource.calendar.group.line'
    _description = 'Resource Calendar Group Line'

    name = fields.Char(string='Description', required=True)
    calendar_group_id = fields.Many2one(
        'resource.calendar.group',
        string='Calendar Group',
        required=True,
        ondelete='cascade',
    )
    dayofweek = fields.Selection(
        [
            ('0', 'Monday'),
            ('1', 'Tuesday'),
            ('2', 'Wednesday'),
            ('3', 'Thursday'),
            ('4', 'Friday'),
            ('5', 'Saturday'),
            ('6', 'Sunday'),
        ],
        string='Day of Week',
        required=True, default='0',
    )
    day_period = fields.Selection(
        [
            ('morning', 'Morning'),
            ('afternoon', 'Afternoon'),
            ('full_day', 'Full Day'),
            ('break', 'Break'),
        ],
        string='Day Period',
        required=True,
        default='full_day',
    )
    hour_from = fields.Float(string='Work From', required=True, default=8.0)
    hour_to = fields.Float(string='Work To', required=True, default=16.5)
    start_date = fields.Date(string='Start Date')
    end_date = fields.Date(string='End Date')
    sequence = fields.Integer(default=10)

    def _duration_hours(self):
        """Total length of these lines in hours, wrapping past midnight.

        A night shift is stored wrapped — the 21:00-05:00 security schedule
        holds `hour_from = 21, hour_to = 5` — so a plain `hour_to - hour_from`
        yields **minus 16** hours a day.  That negative fed
        `hr.leave.number_of_days` through `_get_daily_work_hours` and tripped
        the `number_of_days >= 0` constraint ("If you want to change the number
        of days you should use the 'period' mode").  Every consumer measuring a
        line must come through here.
        """
        total = 0.0
        for line in self:
            delta = line.hour_to - line.hour_from
            total += delta + 24.0 if delta < 0 else delta
        return total