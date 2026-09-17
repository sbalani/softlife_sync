from odoo import api, fields, models


class MrpBom(models.Model):
    _inherit = 'mrp.bom'

    softlife_recipe_version_id = fields.Char(index=True, copy=False)
    softlife_component_hash = fields.Char(copy=False)
    softlife_payload_contract_version = fields.Integer(copy=False)

    _sql_constraints = [
        ('softlife_recipe_version_unique', 'unique(softlife_recipe_version_id)',
         'A SoftLife recipe version can only have one bill of materials.'),
    ]


class MrpBomLine(models.Model):
    _inherit = 'mrp.bom.line'

    softlife_dosage_quantity = fields.Float(digits=(16, 6), copy=False)
    softlife_dosage_uom = fields.Char(copy=False)
    softlife_stock_quantity_per_unit = fields.Float(digits=(16, 9), copy=False)
    softlife_package_content_quantity = fields.Float(digits=(16, 6), copy=False)
    softlife_package_content_uom = fields.Char(copy=False)
    softlife_conversion_audit = fields.Char(copy=False)


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


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    softlife_replenishment_key = fields.Char(index=True, copy=False)
    softlife_export_id = fields.Char(index=True, copy=False)
    softlife_transfer_key = fields.Char(index=True, copy=False)
    softlife_payload_sha256 = fields.Char(copy=False)

    _sql_constraints = [
        ('softlife_replenishment_key_unique', 'unique(softlife_replenishment_key)',
         'This SoftLife replenishment transfer has already been created.'),
    ]


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    softlife_export_id = fields.Char(index=True, copy=False)
    softlife_warehouse_id = fields.Many2one('stock.warehouse', copy=False)
    softlife_source_orders = fields.Json(copy=False)
    softlife_source_summary = fields.Text(compute='_compute_softlife_source_summary')

    @api.depends('softlife_source_orders')
    def _compute_softlife_source_summary(self):
        for order in self:
            order.softlife_source_summary = '\n'.join(
                '%s - %s (machine %s, IMEI %s)' % (
                    source.get('order_code') or source.get('platform_order_id') or 'Unknown order',
                    source.get('machine_name') or 'Unknown machine',
                    source.get('machine_id') or 'unknown',
                    source.get('machine_imei') or 'unknown',
                )
                for source in (order.softlife_source_orders or [])
                if isinstance(source, dict)
            )

    _sql_constraints = [
        ('softlife_sale_order_unique', 'unique(softlife_export_id, softlife_warehouse_id)',
         'This SoftLife warehouse-period sale order already exists.'),
    ]
