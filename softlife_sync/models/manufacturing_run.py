import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.float_utils import float_compare

_logger = logging.getLogger(__name__)


class SoftlifeRecipeSync(models.Model):
    _name = 'softlife.recipe.sync'
    _description = 'SoftLife Recipe Version Sync'
    _order = 'write_date desc'

    recipe_id = fields.Char(required=True, index=True)
    recipe_version_id = fields.Char(required=True, index=True)
    component_hash = fields.Char(required=True)
    product_id = fields.Many2one('product.product', ondelete='restrict')
    bom_id = fields.Many2one('mrp.bom', ondelete='restrict')
    callback_state = fields.Selection(
        [('pending', 'Pending'), ('sent', 'Sent')], default='pending', required=True, index=True,
    )
    error = fields.Text()
    callback_error = fields.Text()

    _sql_constraints = [
        ('recipe_version_unique', 'unique(recipe_version_id)',
         'A SoftLife recipe version can only have one local sync record.'),
    ]

    @api.model
    def _uom(self, code, product):
        normalized = str(code or '').strip().lower()
        aliases = {
            'g': (('uom.product_uom_gram',), {'g', 'gram', 'grams'}),
            'kg': (('uom.product_uom_kgm',), {'kg', 'kilogram', 'kilograms'}),
            'ml': (('uom.product_uom_ml', 'uom.product_uom_milliliter'), {'ml', 'milliliter', 'milliliters'}),
            'l': (('uom.product_uom_litre',), {'l', 'liter', 'liters', 'litre', 'litres'}),
            'unit': (('uom.product_uom_unit',), {'unit', 'units', 'u', 'each'}),
        }
        canonical = next((key for key, (_, names) in aliases.items() if normalized in names), None)
        if not canonical:
            raise ValidationError(_('Unsupported component UoM: %s') % code)
        xmlids, names = aliases[canonical]
        uom = self.env['uom.uom']
        for xmlid in xmlids:
            uom = self.env.ref(xmlid, raise_if_not_found=False)
            if uom:
                break
        if not uom:
            uom = next((row for row in self.env['uom.uom'].search([])
                        if row.name.strip().lower() in names), self.env['uom.uom'])
        if not uom:
            raise ValidationError(_('Odoo UoM for %s is not installed.') % code)
        if uom.category_id != product.uom_id.category_id:
            raise ValidationError(_(
                'UoM %(uom)s is incompatible with product %(product)s (%(product_uom)s).',
                uom=uom.display_name, product=product.display_name,
                product_uom=product.uom_id.display_name,
            ))
        return uom

    @api.model
    def _finished_product(self, recipe):
        recipe_id = str(recipe.get('recipe_id') or '')
        mapped_id = recipe.get('odoo_finished_product_id')
        mapped = self.env['product.product'].browse(int(mapped_id)).exists() if mapped_id else self.env['product.product']
        existing = self.env['product.product'].search([('softlife_recipe_id', '=', recipe_id)], limit=1)
        if mapped:
            if existing and existing != mapped:
                raise ValidationError(_('Recipe %s has conflicting finished-product mappings.') % recipe_id)
            conflict = mapped.softlife_recipe_id
            if conflict and conflict != recipe_id:
                raise ValidationError(_('Mapped product is already assigned to recipe %s.') % conflict)
            if not existing:
                mapped.softlife_recipe_id = recipe_id
            return mapped
        if mapped_id:
            raise ValidationError(_('Mapped Odoo product.product %s does not exist.') % mapped_id)
        if existing:
            return existing
        vals = {
            'name': recipe.get('name') or _('SoftLife recipe %s') % recipe_id,
            'type': 'consu',
        }
        if 'is_storable' in self.env['product.template']._fields:
            vals['is_storable'] = True
        product = self.env['product.template'].create(vals).product_variant_id
        product.softlife_recipe_id = recipe_id
        return product

    @api.model
    def _component_values(self, recipe):
        result = []
        seen = set()
        for component in recipe.get('components') or []:
            product_id = component.get('odoo_product_id')
            if not product_id:
                raise ValidationError(_(
                    'Recipe %(recipe)s component %(component)s has no Odoo product mapping.',
                    recipe=recipe.get('recipe_version_id'), component=component.get('platform_product_id'),
                ))
            product = self.env['product.product'].browse(int(product_id)).exists()
            if not product:
                raise ValidationError(_('Component product.product %s does not exist.') % product_id)
            if product.id in seen:
                raise ValidationError(_('Recipe contains duplicate Odoo component %s.') % product.display_name)
            seen.add(product.id)
            quantity = float(component.get('quantity') or component.get('quantity_per_unit') or 0)
            if quantity <= 0:
                raise ValidationError(_('Component quantity must be positive for %s.') % product.display_name)
            result.append((product, quantity, self._uom(component.get('uom'), product)))
        if not result:
            raise ValidationError(_('Recipe %s has no components.') % recipe.get('recipe_version_id'))
        return result

    @api.model
    def _validate_bom(self, bom, product, components, component_hash):
        if bom.product_tmpl_id != product.product_tmpl_id or (bom.product_id and bom.product_id != product):
            raise ValidationError(_('Mapped BOM belongs to a different finished product.'))
        if bom.softlife_component_hash and bom.softlife_component_hash != component_hash:
            raise ValidationError(_('Mapped BOM has a different component hash.'))
        lines = list(bom.bom_line_ids)
        if len(lines) != len(components):
            raise ValidationError(_('Mapped BOM component count does not match the platform recipe.'))
        by_product = {line.product_id.id: line for line in lines}
        for component, quantity, uom in components:
            line = by_product.get(component.id)
            if not line or line.product_uom_id != uom or float_compare(
                    line.product_qty, quantity, precision_rounding=uom.rounding):
                raise ValidationError(_('Mapped BOM does not exactly match component %s.') % component.display_name)

    @api.model
    def ensure_recipe(self, recipe):
        version_id = str(recipe.get('recipe_version_id') or '')
        recipe_id = str(recipe.get('recipe_id') or '')
        component_hash = str(recipe.get('component_hash') or '')
        if not version_id or not recipe_id or not component_hash:
            raise ValidationError(_('Recipe identity and component_hash are required.'))
        sync = self.search([('recipe_version_id', '=', version_id)], limit=1)
        if sync and sync.component_hash != component_hash:
            raise ValidationError(_('Recipe version %s changed its immutable component hash.') % version_id)
        product = self._finished_product(recipe)
        components = self._component_values(recipe)
        mapped_id = recipe.get('odoo_bom_id')
        mapped = self.env['mrp.bom'].browse(int(mapped_id)).exists() if mapped_id else self.env['mrp.bom']
        bom = self.env['mrp.bom'].search([('softlife_recipe_version_id', '=', version_id)], limit=1)
        if mapped:
            if bom and bom != mapped:
                raise ValidationError(_('Recipe version has conflicting BOM mappings.'))
            conflict = mapped.softlife_recipe_version_id
            if conflict and conflict != version_id:
                raise ValidationError(_('Mapped BOM belongs to recipe version %s.') % conflict)
            self._validate_bom(mapped, product, components, component_hash)
            bom = mapped
            bom.write({
                'softlife_recipe_version_id': version_id,
                'softlife_component_hash': component_hash,
            })
        elif mapped_id:
            raise ValidationError(_('Mapped mrp.bom %s does not exist.') % mapped_id)
        elif bom:
            self._validate_bom(bom, product, components, component_hash)
        else:
            bom = self.env['mrp.bom'].create({
                'product_tmpl_id': product.product_tmpl_id.id,
                'product_id': product.id,
                'product_qty': 1.0,
                'product_uom_id': product.uom_id.id,
                'type': 'normal',
                'softlife_recipe_version_id': version_id,
                'softlife_component_hash': component_hash,
                'bom_line_ids': [(0, 0, {
                    'product_id': component.id,
                    'product_qty': quantity,
                    'product_uom_id': uom.id,
                    'sequence': sequence * 10,
                }) for sequence, (component, quantity, uom) in enumerate(components, 1)],
            })
        needs_callback = int(recipe.get('odoo_finished_product_id') or 0) != product.id \
            or int(recipe.get('odoo_bom_id') or 0) != bom.id
        vals = {
            'recipe_id': recipe_id, 'recipe_version_id': version_id,
            'component_hash': component_hash, 'product_id': product.id, 'bom_id': bom.id,
            'callback_state': 'pending' if needs_callback else 'sent',
            'error': False, 'callback_error': False,
        }
        if sync:
            if sync.product_id and sync.product_id != product or sync.bom_id and sync.bom_id != bom:
                raise ValidationError(_('Local recipe mapping conflicts with the platform callback mapping.'))
            sync.write(vals)
        else:
            sync = self.create(vals)
        return sync

    def action_retry_callback(self):
        client = self.env['softlife.sync.client']
        for sync in self.filtered(lambda row: row.callback_state == 'pending'):
            try:
                client._api_request('POST', '/api/internal/odoo/recipes/result', payload={
                    'recipe_version_id': sync.recipe_version_id,
                    'component_hash': sync.component_hash,
                    'accepted': not bool(sync.error),
                    'odoo_finished_product_id': sync.product_id.id or None,
                    'odoo_bom_id': sync.bom_id.id or None,
                    'error': sync.error or None,
                })
                sync.write({'callback_state': 'sent', 'callback_error': False})
            except Exception as exc:
                sync.callback_error = str(exc)
                _logger.warning('SoftLife recipe callback failed for %s: %s', sync.recipe_version_id, exc)
        return True


class SoftlifeManufacturingRun(models.Model):
    _name = 'softlife.manufacturing.run'
    _description = 'SoftLife Manufacturing Run'
    _order = 'period_from desc, id desc'

    export_id = fields.Char(required=True, index=True, readonly=True)
    idempotency_key = fields.Char(readonly=True)
    initiated_by = fields.Selection([('odoo', 'Odoo'), ('platform', 'Platform')], readonly=True)
    platform_status = fields.Char(readonly=True, index=True)
    period_from = fields.Datetime(readonly=True)
    period_to = fields.Datetime(readonly=True)
    time_zone = fields.Char(readonly=True)
    document_date = fields.Date(readonly=True)
    payload_sha256 = fields.Char(readonly=True)
    payload = fields.Json(readonly=True)
    blocked_items = fields.Json(readonly=True)
    platform_result = fields.Json(readonly=True)
    processing_state = fields.Selection([
        ('new', 'Not Processed'), ('processing', 'Processing'),
        ('result_pending', 'Result Pending'), ('completed', 'Completed'), ('failed', 'Failed'),
    ], default='new', required=True, index=True, readonly=True)
    result_payload = fields.Json(readonly=True)
    error = fields.Text(readonly=True)
    callback_error = fields.Text(readonly=True)
    _sql_constraints = [
        ('export_id_unique', 'unique(export_id)', 'A platform manufacturing export can only exist once.'),
    ]

    @api.model
    def _remote_values(self, remote):
        return {
            'export_id': remote.get('export_id'), 'idempotency_key': remote.get('idempotency_key'),
            'initiated_by': remote.get('initiated_by'), 'platform_status': remote.get('status'),
            'period_from': remote.get('period_from'), 'period_to': remote.get('period_to'),
            'time_zone': remote.get('time_zone'), 'document_date': remote.get('document_date'),
            'payload_sha256': remote.get('payload_sha256'),
            'payload': {'warehouses': remote.get('warehouses') or []},
            'blocked_items': remote.get('blocked_items') or [],
            'platform_result': remote.get('odoo_result') or False,
        }

    @api.model
    def upsert_remote(self, remote):
        export_id = remote.get('export_id')
        if not export_id:
            raise ValidationError(_('Platform run omitted export_id.'))
        run = self.search([('export_id', '=', export_id)], limit=1)
        values = self._remote_values(remote)
        if run:
            if run.payload_sha256 and values['payload_sha256'] and run.payload_sha256 != values['payload_sha256']:
                raise ValidationError(_('Platform changed the immutable payload hash for %s.') % export_id)
            run.write(values)
        else:
            run = self.create(values)
        if remote.get('status') == 'completed' and run.processing_state == 'result_pending':
            run.processing_state = 'completed'
        elif remote.get('status') == 'completed' and remote.get('odoo_result') and run.processing_state == 'new':
            run.processing_state = 'completed'
        elif remote.get('status') == 'failed' and remote.get('odoo_result') and run.processing_state == 'new':
            run.processing_state = 'failed'
        return run

    @api.model
    def action_refresh(self):
        client = self.env['softlife.sync.client']
        cursor = None
        while True:
            params = {'limit': 100}
            if cursor:
                params['cursor'] = cursor
            page = client._api_request('GET', '/api/internal/odoo/manufacturing-periods', params=params)
            for remote in page.get('runs') or []:
                self.upsert_remote(remote)
            if not page.get('has_more'):
                break
            cursor = page.get('next_cursor')
            if not cursor:
                raise ValidationError(_('Manufacturing pagination omitted next_cursor.'))
        return True

    @api.model
    def action_sync_catalog(self):
        Recipe = self.env['softlife.recipe.sync']
        for page in self.env['softlife.sync.client']._api_catalog_pages():
            for recipe in page.get('recipe_versions') or []:
                try:
                    with self.env.cr.savepoint():
                        Recipe.ensure_recipe(recipe)
                except Exception as exc:
                    version_id = str(recipe.get('recipe_version_id') or '')
                    if version_id:
                        sync = Recipe.search([('recipe_version_id', '=', version_id)], limit=1)
                        values = {
                            'recipe_id': str(recipe.get('recipe_id') or ''),
                            'recipe_version_id': version_id,
                            'component_hash': str(recipe.get('component_hash') or ''),
                            'callback_state': 'pending', 'error': str(exc),
                        }
                        sync.write(values) if sync else Recipe.create(values)
                    _logger.warning('SoftLife recipe %s rejected: %s', version_id, exc)
        return True

    def action_confirm_platform(self):
        self.ensure_one()
        if self.initiated_by != 'odoo' or self.platform_status != 'draft':
            raise UserError(_('Only Odoo-initiated draft runs can be confirmed.'))
        remote = self.env['softlife.sync.client']._api_request(
            'POST', f'/api/internal/odoo/manufacturing-periods/{self.export_id}/confirm',
            payload={'payload_sha256': self.payload_sha256},
        )
        self.upsert_remote(remote)
        return True

    def _recipe_from_group(self, group):
        sync = self.env['softlife.recipe.sync'].search([
            ('recipe_version_id', '=', str(group.get('recipe_version_id') or '')),
        ], limit=1)
        if not sync or not sync.component_hash:
            raise ValidationError(_('Recipe version %s was not received through the catalog.') % group.get('recipe_version_id'))
        return {
            'recipe_id': group.get('recipe_id'), 'recipe_version_id': group.get('recipe_version_id'),
            'component_hash': sync.component_hash, 'name': group.get('name'),
            'odoo_finished_product_id': group.get('odoo_finished_product_id'),
            'components': [{
                'odoo_product_id': component.get('odoo_product_id'),
                'quantity': component.get('quantity_per_unit'), 'uom': component.get('uom'),
            } for component in group.get('components') or []],
        }

    def _complete_mo(self, mo, document_datetime):
        mo.action_confirm()
        if hasattr(mo, 'action_assign'):
            mo.action_assign()
        unreserved = mo.move_raw_ids.filtered(lambda move: move.state not in ('assigned', 'done'))
        if unreserved:
            raise UserError(_(
                'Manufacturing order %(mo)s cannot fully reserve: %(products)s.',
                mo=mo.display_name,
                products=', '.join(unreserved.mapped('product_id.display_name')),
            ))
        mo.qty_producing = mo.product_qty
        (mo.move_raw_ids | mo.move_finished_ids).write({'date': document_datetime})
        for move in mo.move_raw_ids:
            if 'picked' in move._fields:
                move.picked = True
        result = mo.with_context(force_period_date=self.document_date).button_mark_done()
        if mo.state != 'done' and isinstance(result, dict):
            raise UserError(_(
                'Manufacturing order %(mo)s requires wizard %(wizard)s; no success was reported.',
                mo=mo.display_name, wizard=result.get('res_model') or result.get('name') or 'unknown',
            ))
        if mo.state != 'done':
            raise UserError(_('Manufacturing order %s did not reach Done.') % mo.display_name)

        values = {'date_start': document_datetime}
        if 'date_finished' in mo._fields:
            values['date_finished'] = document_datetime
        mo.write(values)
        (mo.move_raw_ids | mo.move_finished_ids).write({'date': document_datetime})

    def _validate_delivery(self, picking, document_datetime):
        picking.action_assign()
        unreserved = picking.move_ids.filtered(lambda move: move.state not in ('assigned', 'done'))
        if unreserved:
            return False
        picking.move_ids.write({'date': document_datetime})
        for move in picking.move_ids:
            if 'picked' in move._fields:
                move.picked = True
        result = picking.with_context(force_period_date=self.document_date).button_validate()
        if picking.state != 'done' and isinstance(result, dict):
            raise UserError(_(
                'Delivery %(picking)s requires wizard %(wizard)s; no success was reported.',
                picking=picking.display_name,
                wizard=result.get('res_model') or result.get('name') or 'unknown',
            ))
        if picking.state != 'done':
            raise UserError(_('Delivery %s did not reach Done.') % picking.display_name)
        picking.write({'date_done': document_datetime})
        picking.move_ids.write({'date': document_datetime})
        return True

    def _complete_sale_deliveries(self, sale, document_datetime):
        while True:
            pending = sale.picking_ids.filtered(lambda picking: picking.state not in ('done', 'cancel'))
            if not pending:
                return
            progressed = False
            for picking in pending.sorted('id'):
                if self._validate_delivery(picking, document_datetime):
                    progressed = True
            if not progressed:
                raise UserError(_(
                    'Sales order %(sale)s cannot fully reserve its pending transfers: %(pickings)s. '
                    'Available stock was not forced negative.',
                    sale=sale.display_name,
                    pickings=', '.join(pending.mapped('display_name')),
                ))

    def _require_make_to_stock(self, product):
        mto_route = self.env.ref('stock.route_warehouse0_mto', raise_if_not_found=False)
        routes = product.route_ids
        if 'total_route_ids' in product.categ_id._fields:
            routes |= product.categ_id.total_route_ids
        if mto_route and mto_route in routes:
            raise ValidationError(_(
                'Finished product %s uses Make To Order. Remove that route because this run creates '
                'the manufacturing order explicitly before confirming the sale.',
            ) % product.display_name)

    def _process_documents(self):
        self.ensure_one()
        warehouses = (self.payload or {}).get('warehouses') or []
        if not warehouses:
            raise ValidationError(_('Ready run has no warehouse payload.'))
        warehouse_results = []
        document_datetime = fields.Datetime.to_datetime(self.document_date)
        for warehouse_payload in warehouses:
            warehouse_id = int(warehouse_payload.get('odoo_warehouse_id') or 0)
            customer_id = int(warehouse_payload.get('odoo_customer_id') or 0)
            warehouse = self.env['stock.warehouse'].browse(warehouse_id).exists()
            customer = self.env['res.partner'].browse(customer_id).exists()
            if not warehouse or not customer:
                raise ValidationError(_('Warehouse %s or its configured customer is missing in Odoo.') % warehouse_id)
            if warehouse.company_id != self.env.company:
                raise ValidationError(_(
                    'Warehouse %(warehouse)s belongs to %(company)s; run this process in that Odoo company.',
                    warehouse=warehouse.display_name, company=warehouse.company_id.display_name,
                ))
            if not warehouse.manu_type_id or not warehouse.lot_stock_id:
                raise ValidationError(_('Warehouse %s is not configured for manufacturing.') % warehouse.display_name)
            groups = warehouse_payload.get('recipes') or []
            currencies = {str(group.get('currency') or '') for group in groups}
            if len(currencies) != 1 or not next(iter(currencies), ''):
                raise ValidationError(_('Warehouse %s contains missing or mixed currencies.') % warehouse.display_name)
            currency_code = next(iter(currencies))
            currency = self.env['res.currency'].search([('name', '=', currency_code)], limit=1)
            if not currency:
                raise ValidationError(_('Currency %s does not exist in Odoo.') % currency_code)
            pricelist = customer.property_product_pricelist
            if not pricelist or pricelist.currency_id != currency:
                pricelist = self.env['product.pricelist'].search([('currency_id', '=', currency.id)], limit=1)
            if not pricelist:
                raise ValidationError(_(
                    'No %(currency)s pricelist is available for customer %(customer)s.',
                    currency=currency.name, customer=customer.display_name,
                ))
            sale_totals = {}
            warehouse_mo_ids = []
            for group in groups:
                recipe_sync = self.env['softlife.recipe.sync'].ensure_recipe(self._recipe_from_group(group))
                self._require_make_to_stock(recipe_sync.product_id)
                quantity = float(group.get('units_sold') or 0)
                gross = float(group.get('gross_sales') or 0)
                if quantity <= 0 or gross < 0:
                    raise ValidationError(_('Invalid platform quantities for recipe %s.') % group.get('recipe_version_id'))
                mo = self.env['mrp.production'].search([
                    ('softlife_export_id', '=', self.export_id),
                    ('softlife_warehouse_id', '=', warehouse.id),
                    ('softlife_recipe_version_id', '=', recipe_sync.recipe_version_id),
                    ('softlife_currency_id', '=', currency.id),
                ], limit=1)
                if not mo:
                    mo = self.env['mrp.production'].create({
                        'product_id': recipe_sync.product_id.id,
                        'product_qty': quantity, 'product_uom_id': recipe_sync.product_id.uom_id.id,
                        'bom_id': recipe_sync.bom_id.id, 'picking_type_id': warehouse.manu_type_id.id,
                        'location_src_id': warehouse.lot_stock_id.id,
                        'location_dest_id': warehouse.lot_stock_id.id,
                        'date_start': document_datetime,
                        'softlife_export_id': self.export_id,
                        'softlife_warehouse_id': warehouse.id,
                        'softlife_recipe_version_id': recipe_sync.recipe_version_id,
                        'softlife_currency_id': currency.id,
                    })
                    self._complete_mo(mo, document_datetime)
                elif mo.state != 'done' or mo.product_id != recipe_sync.product_id \
                        or mo.bom_id != recipe_sync.bom_id or mo.picking_type_id != warehouse.manu_type_id \
                        or float_compare(mo.product_qty, quantity, precision_rounding=mo.product_uom_id.rounding):
                    raise ValidationError(_('Existing manufacturing order does not match the immutable payload.'))
                warehouse_mo_ids.append(mo.id)
                totals = sale_totals.setdefault(recipe_sync.product_id, [0.0, 0.0])
                totals[0] += quantity
                totals[1] += gross
            order_lines = [(0, 0, {
                'product_id': product.id, 'product_uom_qty': totals[0],
                'product_uom': product.uom_id.id, 'price_unit': totals[1] / totals[0],
                'tax_id': [(6, 0, [])],
            }) for product, totals in sale_totals.items()]
            sale = self.env['sale.order'].search([
                ('softlife_export_id', '=', self.export_id),
                ('softlife_warehouse_id', '=', warehouse.id),
            ], limit=1)
            if not sale:
                sale = self.env['sale.order'].create({
                    'partner_id': customer.id, 'warehouse_id': warehouse.id,
                    'pricelist_id': pricelist.id,
                    'date_order': document_datetime,
                    'client_order_ref': self.export_id,
                    'softlife_export_id': self.export_id,
                    'softlife_warehouse_id': warehouse.id,
                    'order_line': order_lines,
                })
                sale.action_confirm()
                self._complete_sale_deliveries(sale, document_datetime)
            elif sale.state not in ('sale', 'done') or sale.currency_id != currency \
                    or sale.partner_id != customer or sale.warehouse_id != warehouse:
                raise ValidationError(_('Existing sale order does not match the immutable payload.'))
            else:
                expected = {
                    (values[2]['product_id'], values[2]['product_uom']): (
                        values[2]['product_uom_qty'], values[2]['price_unit'],
                    ) for values in order_lines
                }
                actual = {(line.product_id.id, line.product_uom.id): (
                    line.product_uom_qty, line.price_unit,
                ) for line in sale.order_line.filtered(lambda line: not line.display_type)}
                if set(expected) != set(actual) or any(
                    float_compare(
                        actual[key][0], values[0],
                        precision_rounding=self.env['uom.uom'].browse(key[1]).rounding,
                    )
                    or float_compare(actual[key][1], values[1], precision_rounding=currency.rounding)
                    for key, values in expected.items()
                ):
                    raise ValidationError(_('Existing sale order lines do not match the immutable payload.'))
                if sale.order_line.filtered(lambda line: not line.display_type and line.tax_id):
                    raise ValidationError(_('Existing sale order has taxes but the frozen export is gross sales.'))
                self._complete_sale_deliveries(sale, document_datetime)
            expected_gross = sum(totals[1] for totals in sale_totals.values())
            if float_compare(sale.amount_total, expected_gross, precision_rounding=currency.rounding):
                raise ValidationError(_(
                    'Sales order %(sale)s total %(actual)s does not match platform gross sales %(expected)s.',
                    sale=sale.display_name, actual=sale.amount_total, expected=expected_gross,
                ))
            incomplete_lines = sale.order_line.filtered(
                lambda line: not line.display_type and float_compare(
                    line.qty_delivered, line.product_uom_qty,
                    precision_rounding=line.product_uom.rounding,
                ) != 0
            )
            if incomplete_lines:
                raise ValidationError(_(
                    'Sales order %(sale)s is not fully delivered: %(products)s.',
                    sale=sale.display_name,
                    products=', '.join(incomplete_lines.mapped('product_id.display_name')),
                ))
            deliveries = sale.picking_ids.filtered(
                lambda picking: picking.state == 'done'
                and picking.picking_type_code == 'outgoing'
                and picking.location_dest_id.usage == 'customer'
            )
            if not deliveries:
                raise ValidationError(_('Sales order %s has no completed delivery.') % sale.display_name)
            warehouse_results.append({
                'odoo_warehouse_id': warehouse.id,
                'manufacturing_order_ids': warehouse_mo_ids,
                'sales_order_id': sale.id,
                'delivery_id': deliveries[0].id,
                'delivery_ids': deliveries.ids,
            })
        return {
            'accepted': True, 'payload_sha256': self.payload_sha256,
            'warehouses': warehouse_results,
            'error': None,
        }

    def action_process(self):
        self.ensure_one()
        if not self.env.context.get('softlife_catalog_synced'):
            self.action_sync_catalog()
        self.env.cr.execute(
            'SELECT id FROM softlife_manufacturing_run WHERE id = %s FOR UPDATE NOWAIT', [self.id],
        )
        self.invalidate_recordset()
        retrying_rejection = self.platform_status == 'failed' \
            and isinstance(self.platform_result, dict) \
            and self.platform_result.get('accepted') is False
        if self.platform_status != 'ready' and not retrying_rejection:
            raise UserError(_('Only ready runs or previously rejected Odoo results can be processed.'))
        if self.processing_state in ('result_pending', 'completed'):
            return True
        self.processing_state = 'processing'
        try:
            with self.env.cr.savepoint():
                result = self._process_documents()
            self.write({
                'processing_state': 'result_pending', 'result_payload': result,
                'error': False, 'callback_error': False,
            })
        except Exception as exc:
            self.write({
                'processing_state': 'result_pending',
                'result_payload': {
                    'accepted': False, 'payload_sha256': self.payload_sha256, 'error': str(exc),
                },
                'error': str(exc), 'callback_error': False,
            })
            _logger.exception('SoftLife manufacturing run %s failed', self.export_id)
        return True

    def action_retry_callback(self):
        client = self.env['softlife.sync.client']
        for run in self.filtered(lambda row: row.processing_state == 'result_pending' and row.result_payload):
            try:
                remote = client._api_request(
                    'POST', f'/api/internal/odoo/manufacturing-periods/{run.export_id}/result',
                    payload=run.result_payload,
                )
                run.upsert_remote(remote)
                run.write({
                    'processing_state': 'completed' if run.result_payload.get('accepted') else 'failed',
                    'callback_error': False,
                })
            except Exception as exc:
                run.callback_error = str(exc)
                _logger.warning('SoftLife manufacturing callback failed for %s: %s', run.export_id, exc)
        return True

    @api.model
    def _cron_manufacturing(self):
        if not self.env['softlife.sync.client']._api_is_configured():
            return
        self.search([('processing_state', '=', 'result_pending')]).action_retry_callback()
        self.env['softlife.recipe.sync'].search([('callback_state', '=', 'pending')]).action_retry_callback()
        try:
            self.action_sync_catalog()
            self.action_refresh()
            for run in self.search([('platform_status', '=', 'ready'), ('processing_state', '=', 'new')]):
                run.with_context(softlife_catalog_synced=True).action_process()
        except Exception:
            _logger.exception('SoftLife manufacturing cron pass failed')
