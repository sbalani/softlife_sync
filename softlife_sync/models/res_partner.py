from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    supabase_id = fields.Char(string='SoftLife ID', index=True, copy=False)
