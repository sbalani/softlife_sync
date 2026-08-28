import datetime
import pytz

from odoo import _, fields, models
from odoo.exceptions import ValidationError


class SoftlifeManufacturingPeriodWizard(models.TransientModel):
    _name = 'softlife.manufacturing.period.wizard'
    _description = 'Create SoftLife Manufacturing Period'

    date_from = fields.Date(required=True, default=lambda self: fields.Date.today().replace(day=1))
    date_to = fields.Date(required=True, default=fields.Date.today)
    time_zone = fields.Selection(
        selection=lambda self: [(zone, zone) for zone in pytz.common_timezones],
        required=True, default=lambda self: self.env.user.tz or 'UTC',
    )

    def action_prepare(self):
        self.ensure_one()
        if self.date_to < self.date_from:
            raise ValidationError(_('End date must be on or after start date.'))
        local_from = datetime.datetime.combine(self.date_from, datetime.time.min).isoformat()
        local_to = datetime.datetime.combine(self.date_to + datetime.timedelta(days=1), datetime.time.min).isoformat()
        key = '%s:%s:%s:%s' % (self.env.cr.dbname, self.date_from, self.date_to, self.time_zone)
        remote = self.env['softlife.sync.client']._api_request(
            'POST', '/api/internal/odoo/manufacturing-periods', payload={
                'idempotency_key': key, 'local_from': local_from, 'local_to': local_to,
                'time_zone': self.time_zone, 'initiated_by': 'odoo',
            },
        )
        run = self.env['softlife.manufacturing.run'].upsert_remote(remote)
        return {
            'type': 'ir.actions.act_window', 'res_model': run._name, 'res_id': run.id,
            'view_mode': 'form', 'target': 'current',
        }
