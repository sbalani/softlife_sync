from odoo import fields, models


class MrpBom(models.Model):
    _inherit = 'mrp.bom'

    softlife_recipe_version_id = fields.Char(index=True, copy=False)
    softlife_component_hash = fields.Char(copy=False)

    _sql_constraints = [
        ('softlife_recipe_version_unique', 'unique(softlife_recipe_version_id)',
         'A SoftLife recipe version can only have one bill of materials.'),
    ]


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    softlife_export_id = fields.Char(index=True, copy=False)
    softlife_recipe_version_id = fields.Char(index=True, copy=False)
    softlife_warehouse_id = fields.Many2one('stock.warehouse', copy=False)
    softlife_currency_id = fields.Many2one('res.currency', copy=False)

    _sql_constraints = [
        ('softlife_production_unique',
         'unique(softlife_export_id, softlife_warehouse_id, softlife_recipe_version_id, softlife_currency_id)',
         'This SoftLife manufacturing group has already been created.'),
    ]


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    softlife_export_id = fields.Char(index=True, copy=False)
    softlife_warehouse_id = fields.Many2one('stock.warehouse', copy=False)

    _sql_constraints = [
        ('softlife_sale_order_unique', 'unique(softlife_export_id, softlife_warehouse_id)',
         'This SoftLife warehouse-period sale order already exists.'),
    ]
