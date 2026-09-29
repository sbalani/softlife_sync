from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['ir.config_parameter'].set_param(
        'softlife.sync.fiscal_product_remediation_enabled', 'False',
    )
