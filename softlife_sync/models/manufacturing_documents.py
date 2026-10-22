from contextlib import contextmanager
from contextvars import ContextVar

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


_MANUFACTURING_AUTHORITY = object()
_manufacturing_authority = ContextVar('softlife_manufacturing_authority', default=None)


@contextmanager
def _softlife_manufacturing_internal_scope():
    token = _manufacturing_authority.set(_MANUFACTURING_AUTHORITY)
    try:
        yield
    finally:
        _manufacturing_authority.reset(token)


def _softlife_manufacturing_is_authorized():
    return _manufacturing_authority.get() is _MANUFACTURING_AUTHORITY


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
    softlife_stock_movement_group_id = fields.Char(index=True, copy=False)
    softlife_stock_movement_out_reference = fields.Char(index=True, copy=False)
    softlife_stock_movement_in_reference = fields.Char(index=True, copy=False)

    _sql_constraints = [
        ('softlife_replenishment_key_unique', 'unique(softlife_replenishment_key)',
         'This SoftLife replenishment transfer has already been created.'),
        ('softlife_stock_movement_group_unique', 'unique(softlife_stock_movement_group_id)',
         'This SoftLife stock movement group has already been created.'),
        ('softlife_stock_movement_out_reference_unique',
         'unique(softlife_stock_movement_out_reference)',
         'This SoftLife outbound stock movement has already been created.'),
        ('softlife_stock_movement_in_reference_unique',
         'unique(softlife_stock_movement_in_reference)',
         'This SoftLife inbound stock movement has already been created.'),
    ]


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    softlife_export_id = fields.Char(index=True, copy=False)
    softlife_warehouse_id = fields.Many2one('stock.warehouse', copy=False)
    softlife_sale_key = fields.Char(index=True, copy=False)
    softlife_source_order_id = fields.Char(index=True, copy=False)
    softlife_source_payload_sha256 = fields.Char(copy=False)
    softlife_manufacturing_contract_version = fields.Integer(copy=False)
    softlife_document_date = fields.Date(copy=False)
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
        ('softlife_sale_key_unique', 'unique(softlife_sale_key)',
         'This SoftLife sales document has already been created.'),
        ('softlife_source_order_unique', 'unique(softlife_source_order_id)',
         'This SoftLife source transaction already has a sales order.'),
        ('softlife_source_identity_paired',
         "CHECK ((softlife_source_order_id IS NULL AND softlife_source_payload_sha256 IS NULL) OR "
         "(softlife_source_order_id IS NOT NULL AND softlife_source_payload_sha256 IS NOT NULL))",
         'SoftLife transaction identity and hash must be stored together.'),
        ('softlife_source_order_id_format',
         "CHECK (softlife_source_order_id IS NULL OR softlife_source_order_id ~ "
         "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')",
         'The SoftLife source transaction ID must be a canonical UUID.'),
        ('softlife_source_payload_sha256_format',
         "CHECK (softlife_source_payload_sha256 IS NULL OR softlife_source_payload_sha256 ~ "
         "'^[0-9a-f]{64}$')",
         'The SoftLife source transaction hash must be lowercase SHA-256.'),
    ]

    def _softlife_manufacturing_documents(self):
        return self.filtered(
            lambda sale: sale.softlife_sale_key or sale.softlife_export_id
            or sale.softlife_source_order_id or sale.softlife_warehouse_id
            or sale.softlife_manufacturing_contract_version
        )

    @api.model_create_multi
    def create(self, vals_list):
        provenance = {
            'softlife_export_id', 'softlife_warehouse_id', 'softlife_sale_key',
            'softlife_source_order_id', 'softlife_source_payload_sha256',
            'softlife_manufacturing_contract_version', 'softlife_document_date',
            'softlife_source_orders',
        }
        if not _softlife_manufacturing_is_authorized() and any(
                provenance.intersection(vals) for vals in vals_list):
            raise UserError(_('SoftLife manufacturing sales provenance is managed internally.'))
        return super().create(vals_list)

    def write(self, vals):
        immutable = {
            'softlife_export_id', 'softlife_warehouse_id', 'softlife_sale_key',
            'softlife_source_order_id', 'softlife_source_payload_sha256',
            'softlife_manufacturing_contract_version', 'softlife_document_date',
            'softlife_source_orders',
        }
        if not _softlife_manufacturing_is_authorized():
            if immutable.intersection(vals):
                raise ValidationError(_('SoftLife sales provenance cannot be changed.'))
            protected = {
                'partner_id', 'partner_invoice_id', 'partner_shipping_id', 'company_id',
                'warehouse_id', 'currency_id', 'pricelist_id', 'date_order', 'state',
                'fiscal_position_id', 'payment_term_id', 'client_order_ref', 'order_line',
            }
            if protected.intersection(vals) and self._softlife_manufacturing_documents():
                raise UserError(_('SoftLife manufacturing sales fields are frozen.'))
        return super().write(vals)

    def unlink(self):
        if not _softlife_manufacturing_is_authorized() \
                and self._softlife_manufacturing_documents():
            raise ValidationError(_('SoftLife sales documents cannot be deleted.'))
        return super().unlink()


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    softlife_rounding_adjustment = fields.Boolean(copy=False)
    softlife_platform_line_key = fields.Char(index=True, copy=False)

    _sql_constraints = [
        ('softlife_platform_line_key_unique', 'unique(softlife_platform_line_key)',
         'This SoftLife platform sales line already exists.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _softlife_manufacturing_is_authorized():
            managed = {'softlife_platform_line_key', 'softlife_rounding_adjustment'}
            if any(managed.intersection(vals) for vals in vals_list):
                raise UserError(_('SoftLife manufacturing line provenance is managed internally.'))
            order_ids = {vals.get('order_id') for vals in vals_list if vals.get('order_id')}
            if self.env['sale.order'].browse(order_ids)._softlife_manufacturing_documents():
                raise UserError(_('SoftLife manufacturing sales lines are frozen.'))
        return super().create(vals_list)

    def write(self, vals):
        if not _softlife_manufacturing_is_authorized():
            managed = {'softlife_platform_line_key', 'softlife_rounding_adjustment'}
            if managed.intersection(vals):
                raise UserError(_('SoftLife manufacturing sales lines are frozen.'))
            protected = {
                'order_id', 'product_id', 'product_uom_qty', 'product_uom', 'price_unit',
                'discount', 'tax_id', 'name', 'display_type', 'sequence',
                'softlife_rounding_adjustment',
            }
            target_order = self.env['sale.order'].browse(vals.get('order_id')).exists()
            if protected.intersection(vals) and (
                self.filtered(
                    lambda line: line.order_id._softlife_manufacturing_documents()
                )
                or target_order._softlife_manufacturing_documents()
            ):
                raise UserError(_('SoftLife manufacturing sales lines are frozen.'))
        return super().write(vals)

    def unlink(self):
        if not _softlife_manufacturing_is_authorized() and self.filtered(
                lambda line: line.softlife_platform_line_key
                or line.order_id._softlife_manufacturing_documents()):
            raise UserError(_('SoftLife manufacturing sales lines are frozen.'))
        return super().unlink()

    def _compute_invoice_status(self):
        super()._compute_invoice_status()
        for line in self.filtered('softlife_rounding_adjustment'):
            line.invoice_status = 'invoiced'
