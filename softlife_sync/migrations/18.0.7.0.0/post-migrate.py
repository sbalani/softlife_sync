from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    params = env['ir.config_parameter']
    params.set_param('softlife.sync.fiscal_invoice_draft_creation_enabled', 'False')
    params.set_param('softlife.sync.fiscal_invoice_confirmation_enabled', 'False')
