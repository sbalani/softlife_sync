from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    supabase_id = fields.Char(string='SoftLife ID', index=True, copy=False)


class ProductProduct(models.Model):
    _inherit = 'product.product'

    softlife_recipe_id = fields.Char(string='SoftLife Recipe ID', index=True, copy=False)

    _sql_constraints = [
        ('softlife_recipe_id_unique', 'unique(softlife_recipe_id)',
         'A SoftLife recipe can only have one finished product.'),
    ]
