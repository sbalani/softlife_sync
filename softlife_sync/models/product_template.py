from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    supabase_id = fields.Char(string='SoftLife ID', index=True, copy=False)
