from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['ir.config_parameter'].set_param('softlife.sync.fiscal_reporting_enabled', 'False')
    cron = env.ref('softlife_sync.cron_softlife_fiscal_configuration', raise_if_not_found=False)
    if cron:
        cron.active = False
