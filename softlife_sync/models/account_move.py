from odoo import fields, models


class AccountMove(models.Model):
    _inherit = 'account.move'

    supabase_order_code = fields.Char(string='SoftLife Order #', index=True, copy=False)
