import datetime
import logging
import math
import re
import uuid
from decimal import Decimal, ROUND_HALF_UP

from odoo import _, api, Command, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.float_utils import float_compare

from .manufacturing_documents import _softlife_manufacturing_internal_scope
from .locks import softlife_company_month_advisory_lock

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
    def _uom_by_code(self, code):
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
        return uom

    @api.model
    def _uom(self, code, product):
        uom = self._uom_by_code(code)
        unit_uom = self.env.ref('uom.product_uom_unit', raise_if_not_found=False)
        if str(code or '').strip().lower() in {'unit', 'units', 'u', 'each'} \
                and unit_uom and product.uom_id.category_id == unit_uom.category_id:
            return product.uom_id
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
        try:
            contract_version = int(recipe.get('payload_contract_version') or 0)
        except (TypeError, ValueError):
            contract_version = 0
        if contract_version != 2:
            raise ValidationError(_(
                'Recipe %s must use payload_contract_version 2 with frozen stock quantities.'
            ) % recipe.get('recipe_version_id'))
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
            dosage_quantity = float(component.get('quantity') or component.get('quantity_per_unit') or 0)
            if dosage_quantity <= 0:
                raise ValidationError(_('Component quantity must be positive for %s.') % product.display_name)
            dosage_uom = self._uom_by_code(component.get('uom'))
            stock_quantity = float(
                component.get('stock_quantity_per_unit') or component.get('stock_quantity') or 0
            )
            if stock_quantity <= 0:
                raise ValidationError(_(
                    'Frozen stock_quantity_per_unit must be positive for %s.'
                ) % product.display_name)
            stock_uom = self._uom(component.get('stock_uom'), product)
            if stock_uom != product.uom_id:
                raise ValidationError(_(
                    'Frozen stock UoM %(stock)s must equal inventory UoM %(inventory)s for %(product)s.',
                    stock=stock_uom.display_name,
                    inventory=product.uom_id.display_name,
                    product=product.display_name,
                ))

            package_quantity = float(component.get('package_content_quantity') or 0)
            package_code = component.get('package_content_uom')
            if dosage_uom.category_id == stock_uom.category_id:
                if bool(package_quantity) != bool(package_code):
                    raise ValidationError(_(
                        'Frozen package content quantity and UoM must be supplied together for %s.'
                    ) % product.display_name)
                expected_stock = dosage_uom._compute_quantity(
                    dosage_quantity, stock_uom, round=False,
                )
                calculation = '%g %s = %.9g %s' % (
                    dosage_quantity, component.get('uom'), expected_stock, component.get('stock_uom'),
                )
            else:
                unit_uom = self.env.ref('uom.product_uom_unit', raise_if_not_found=False)
                if not unit_uom or stock_uom.category_id != unit_uom.category_id:
                    raise ValidationError(_(
                        'Dosage UoM is incompatible with inventory UoM for %s and inventory is not Units.'
                    ) % product.display_name)
                if package_quantity <= 0 or not package_code:
                    raise ValidationError(_(
                        'Frozen package content is required to convert %(dosage)s to inventory Units for %(product)s.',
                        dosage=component.get('uom'), product=product.display_name,
                    ))
                package_uom = self._uom_by_code(package_code)
                if package_uom.category_id != dosage_uom.category_id:
                    raise ValidationError(_(
                        'Package Content UoM %(package)s is incompatible with dosage UoM %(dosage)s for %(product)s.',
                        package=package_code, dosage=component.get('uom'), product=product.display_name,
                    ))
                package_in_dosage_uom = package_uom._compute_quantity(
                    package_quantity, dosage_uom, round=False,
                )
                expected_stock = dosage_quantity / package_in_dosage_uom
                calculation = '%g %s / %g %s = %.9g %s' % (
                    dosage_quantity, component.get('uom'), package_quantity, package_code,
                    expected_stock, component.get('stock_uom'),
                )
            tolerance = max(1e-9, abs(expected_stock) * 1e-8)
            if abs(stock_quantity - expected_stock) > tolerance:
                raise ValidationError(_(
                    'Frozen stock conversion is inconsistent for %(product)s: expected %(expected).9g, got %(actual).9g.',
                    product=product.display_name, expected=expected_stock, actual=stock_quantity,
                ))
            result.append({
                'product': product,
                'quantity': stock_quantity,
                'uom': stock_uom,
                'dosage_quantity': dosage_quantity,
                'dosage_uom': str(component.get('uom') or ''),
                'package_quantity': package_quantity,
                'package_uom': str(package_code or ''),
                'calculation': calculation,
            })
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
        for component in components:
            product = component['product']
            line = by_product.get(product.id)
            if not line or line.product_uom_id != component['uom'] \
                    or float_compare(
                        line.product_qty, component['quantity'],
                        precision_rounding=component['uom'].rounding,
                    ) != 0 \
                    or abs(line.softlife_dosage_quantity - component['dosage_quantity']) > 1e-9 \
                    or line.softlife_dosage_uom != component['dosage_uom'] \
                    or abs(line.softlife_stock_quantity_per_unit - component['quantity']) > 1e-9 \
                    or abs(line.softlife_package_content_quantity - component['package_quantity']) > 1e-9 \
                    or (line.softlife_package_content_uom or '') != component['package_uom'] \
                    or line.softlife_conversion_audit != component['calculation']:
                raise ValidationError(_('Mapped BOM does not exactly match component %s.') % product.display_name)

    @api.model
    def _bom_line_commands(self, components):
        return [Command.create({
            'product_id': component['product'].id,
            'product_qty': component['quantity'],
            'product_uom_id': component['uom'].id,
            'sequence': sequence * 10,
            'softlife_dosage_quantity': component['dosage_quantity'],
            'softlife_dosage_uom': component['dosage_uom'],
            'softlife_stock_quantity_per_unit': component['quantity'],
            'softlife_package_content_quantity': component['package_quantity'],
            'softlife_package_content_uom': component['package_uom'],
            'softlife_conversion_audit': component['calculation'],
        }) for sequence, component in enumerate(components, 1)]

    @api.model
    def _upgrade_or_validate_bom(self, bom, product, components, component_hash, version_id):
        if bom.softlife_payload_contract_version == 2:
            self._validate_bom(bom, product, components, component_hash)
            return
        if bom.product_tmpl_id != product.product_tmpl_id or (bom.product_id and bom.product_id != product):
            raise ValidationError(_('Mapped BOM belongs to a different finished product.'))
        if bom.softlife_component_hash and bom.softlife_component_hash != component_hash:
            raise ValidationError(_('Mapped BOM has a different component hash.'))
        bom.write({
            'softlife_recipe_version_id': version_id,
            'softlife_component_hash': component_hash,
            'softlife_payload_contract_version': 2,
            'bom_line_ids': [Command.delete(line.id) for line in bom.bom_line_ids]
                + self._bom_line_commands(components),
        })
        self._validate_bom(bom, product, components, component_hash)

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
            bom = mapped
        elif mapped_id:
            raise ValidationError(_('Mapped mrp.bom %s does not exist.') % mapped_id)
        if bom:
            self._upgrade_or_validate_bom(bom, product, components, component_hash, version_id)
        else:
            bom = self.env['mrp.bom'].create({
                'product_tmpl_id': product.product_tmpl_id.id,
                'product_id': product.id,
                'product_qty': 1.0,
                'product_uom_id': product.uom_id.id,
                'type': 'normal',
                'softlife_recipe_version_id': version_id,
                'softlife_component_hash': component_hash,
                'softlife_payload_contract_version': 2,
                'bom_line_ids': self._bom_line_commands(components),
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
    replenishment_state = fields.Selection([
        ('not_required', 'Not Required'), ('pending', 'Pending'),
        ('processing', 'Processing'), ('result_pending', 'Result Pending'),
        ('completed', 'Completed'), ('failed', 'Failed'),
    ], default='not_required', required=True, index=True, readonly=True)
    replenishment_result = fields.Json(readonly=True)
    replenishment_error = fields.Text(readonly=True)
    replenishment_callback_error = fields.Text(readonly=True)
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
    def _utc_datetime(self, value):
        if not value:
            return False
        try:
            parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00')) \
                if isinstance(value, str) else fields.Datetime.to_datetime(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(_('Platform returned an invalid datetime: %s') % value) from exc
        if parsed.tzinfo:
            parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return fields.Datetime.to_string(parsed)

    @api.model
    def _remote_values(self, remote, current_payload=None):
        values = {}
        direct = {
            'export_id': 'export_id', 'idempotency_key': 'idempotency_key',
            'initiated_by': 'initiated_by', 'status': 'platform_status',
            'time_zone': 'time_zone', 'document_date': 'document_date',
            'payload_sha256': 'payload_sha256', 'blocked_items': 'blocked_items',
            'odoo_result': 'platform_result',
        }
        for remote_key, field_name in direct.items():
            if remote_key in remote:
                if remote_key == 'blocked_items':
                    values[field_name] = remote.get(remote_key) or []
                elif remote_key == 'odoo_result':
                    values[field_name] = remote.get(remote_key) or False
                else:
                    values[field_name] = remote.get(remote_key)
        for remote_key, field_name in (('period_from', 'period_from'), ('period_to', 'period_to')):
            if remote_key in remote:
                values[field_name] = self._utc_datetime(remote.get(remote_key))
        payload_fields = (
            'payload_contract_version', 'manufacturing_contract_version',
            'warehouses', 'replenishment',
        )
        if any(key in remote for key in payload_fields):
            payload = dict(current_payload or {})
            for key in payload_fields:
                if key in remote:
                    payload[key] = remote.get(key) or ([] if key == 'warehouses' else False)
            values['payload'] = payload
        return values

    @api.model
    def upsert_remote(self, remote):
        export_id = remote.get('export_id')
        if not export_id:
            raise ValidationError(_('Platform run omitted export_id.'))
        run = self.search([('export_id', '=', export_id)], limit=1)
        values = self._remote_values(remote, run.payload if run else None)
        if run:
            if run.payload_sha256 and values['payload_sha256'] and run.payload_sha256 != values['payload_sha256']:
                raise ValidationError(_('Platform changed the immutable payload hash for %s.') % export_id)
            run.write(values)
        else:
            if remote.get('status') == 'replenishment_ready':
                values['replenishment_state'] = 'pending'
            elif remote.get('status') == 'replenishment_failed':
                values['replenishment_state'] = 'failed'
            run = self.create(values)
        if remote.get('status') == 'replenishment_ready' \
                and run.replenishment_state in ('not_required', 'failed'):
            run.replenishment_state = 'pending'
        elif remote.get('status') == 'replenishment_failed' \
                and run.replenishment_state not in ('completed', 'result_pending'):
            run.replenishment_state = 'failed'
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
                if not recipe.get('payload_contract_version') and page.get('payload_contract_version'):
                    recipe = dict(recipe, payload_contract_version=page['payload_contract_version'])
                if int(recipe.get('payload_contract_version') or 0) != 2:
                    continue
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

    def _recipe_from_group(self, group, payload_contract_version=None):
        sync = self.env['softlife.recipe.sync'].search([
            ('recipe_version_id', '=', str(group.get('recipe_version_id') or '')),
        ], limit=1)
        if not sync or not sync.component_hash:
            raise ValidationError(_('Recipe version %s was not received through the catalog.') % group.get('recipe_version_id'))
        return {
            'recipe_id': group.get('recipe_id'), 'recipe_version_id': group.get('recipe_version_id'),
            'component_hash': sync.component_hash, 'name': group.get('name'),
            'payload_contract_version': group.get('payload_contract_version') or payload_contract_version,
            'odoo_finished_product_id': group.get('odoo_finished_product_id'),
            'components': [{
                'odoo_product_id': component.get('odoo_product_id'),
                'quantity': component.get('quantity_per_unit'), 'uom': component.get('uom'),
                'stock_quantity_per_unit': component.get('stock_quantity_per_unit'),
                'stock_uom': component.get('stock_uom'),
                'package_content_quantity': component.get('package_content_quantity'),
                'package_content_uom': component.get('package_content_uom'),
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
        mo.with_context(force_date=True).write(values)

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

    def _check_active_company(self):
        self.ensure_one()
        warehouse_ids = {
            int(warehouse.get('odoo_warehouse_id') or 0)
            for warehouse in (self.payload or {}).get('warehouses') or []
        } - {0}
        replenishment = (self.payload or {}).get('replenishment') or {}
        warehouse_ids.update({
            int(warehouse_id or 0)
            for transfer in replenishment.get('transfers') or []
            for warehouse_id in (
                transfer.get('source_warehouse_id'), transfer.get('destination_warehouse_id'),
            )
        } - {0})
        if not warehouse_ids:
            return
        warehouses = self.env['stock.warehouse'].browse(sorted(warehouse_ids)).exists()
        missing_ids = warehouse_ids - set(warehouses.ids)
        if missing_ids:
            raise ValidationError(_(
                'Run references missing Odoo warehouses: %s.'
            ) % ', '.join(map(str, sorted(missing_ids))))
        companies = warehouses.company_id
        if len(companies) != 1:
            raise ValidationError(_(
                'Run warehouses span multiple Odoo companies: %s.'
            ) % ', '.join(companies.mapped('display_name')))
        if companies != self.env.company:
            raise UserError(_(
                'Switch the active Odoo company to %(required)s before processing this run. '
                'The current active company is %(current)s.',
                required=companies.display_name,
                current=self.env.company.display_name,
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

    @api.model
    def _manufacturing_customer(self):
        raw_id = self.env['ir.config_parameter'].sudo().get_param('softlife.sync.default_partner_id')
        try:
            customer_id = int(raw_id or 0)
        except (TypeError, ValueError):
            customer_id = 0
        customer = self.env['res.partner'].browse(customer_id).exists()
        if not customer:
            raise ValidationError(_(
                'Configure Vending Customer / Consumidor Final in SoftLife Sync settings before processing runs.'
            ))
        return customer

    @api.model
    def _source_orders(self, warehouse_payload):
        sources = []
        seen = set()
        for source in warehouse_payload.get('source_orders') or []:
            if not isinstance(source, dict):
                continue
            values = {
                'platform_order_id': str(source.get('platform_order_id') or ''),
                'order_code': str(source.get('order_code') or ''),
                'machine_id': str(source.get('machine_id') or ''),
                'machine_imei': str(source.get('machine_imei') or ''),
                'machine_name': str(source.get('machine_name') or ''),
            }
            key = values['platform_order_id'] or (
                values['order_code'], values['machine_id'], values['machine_imei'],
            )
            if key in seen:
                continue
            seen.add(key)
            sources.append(values)
        return sorted(sources, key=lambda source: (
            source['order_code'], source['platform_order_id'], source['machine_id'],
        ))

    @api.model
    def _add_sales_rounding_adjustment(self, sale, expected_gross, currency):
        difference = currency.round(expected_gross - sale.amount_total)
        if currency.is_zero(difference):
            return False
        Product = self.env['product.product'].with_context(active_test=False)
        product = Product.search([('default_code', '=', 'SOFTLIFE-ROUNDING')], limit=1)
        if product and product.type != 'service':
            raise ValidationError(_('SOFTLIFE-ROUNDING must be an Odoo service product.'))
        if not product:
            product = Product.create({
                'name': 'SoftLife Sales Rounding Adjustment',
                'default_code': 'SOFTLIFE-ROUNDING',
                'type': 'service',
                'sale_ok': True,
                'purchase_ok': False,
            })
        elif not product.active:
            product.active = True
        sale.write({'order_line': [(0, 0, {
            'name': product.display_name,
            'product_id': product.id,
            'product_uom_qty': 1,
            'product_uom': product.uom_id.id,
            'price_unit': difference,
            'tax_id': [(6, 0, [])],
            'softlife_rounding_adjustment': True,
        })]})
        return difference

    @api.model
    def _canonical_uuid(self, value, label):
        try:
            normalized = str(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError(_('%s must be a canonical UUID.') % label) from exc
        if normalized != value:
            raise ValidationError(_('%s must be a canonical UUID.') % label)
        return normalized

    def _sales_order_values(self, warehouse_payload, recipe_products, currency):
        self.ensure_one()
        document_date = fields.Date.to_date(self.document_date)
        if not document_date:
            raise ValidationError(_('Manufacturing payload requires document_date.'))
        document_month = document_date.strftime('%Y-%m')
        sales_orders = warehouse_payload.get('sales_orders')
        if not isinstance(sales_orders, list) or not sales_orders:
            raise ValidationError(_('Manufacturing contract 3 requires frozen sales_orders.'))
        seen_orders = set()
        seen_lines = set()
        totals = {}
        result = []
        source_rows = warehouse_payload.get('source_orders')
        source_fields = {
            'platform_order_id', 'order_code', 'machine_id', 'machine_imei', 'machine_name',
        }
        if not isinstance(source_rows, list) or any(
                not isinstance(source, dict) or set(source) != source_fields
                for source in source_rows):
            raise ValidationError(_('Manufacturing contract 3 source_orders are invalid.'))
        source_orders = {source['platform_order_id']: source for source in source_rows}
        if len(source_orders) != len(source_rows):
            raise ValidationError(_('Manufacturing contract 3 source_orders contain duplicates.'))
        expected_order_fields = {
            'platform_order_id', 'platform_line_key', 'source_sha256',
            'recipe_version_id', 'order_code', 'order_date', 'customer_odoo_id',
            'currency', 'reference',
            'expected_total_cents', 'lines',
        }
        expected_line_fields = {
            'platform_line_key', 'odoo_product_id', 'description', 'quantity', 'gross_cents',
        }
        for sales_order in sales_orders:
            if not isinstance(sales_order, dict) or set(sales_order) != expected_order_fields:
                raise ValidationError(_('Every frozen sales order must use the canonical fields.'))
            source_id = self._canonical_uuid(
                sales_order.get('platform_order_id'), 'platform_order_id',
            )
            if source_id in seen_orders:
                raise ValidationError(_('Platform order %s appears more than once.') % source_id)
            seen_orders.add(source_id)
            recipe_version_id = self._canonical_uuid(
                sales_order.get('recipe_version_id'), 'recipe_version_id',
            )
            payload_hash = sales_order.get('source_sha256')
            if not isinstance(payload_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', payload_hash):
                raise ValidationError(_('Platform order %s has an invalid source hash.') % source_id)
            platform_line_key = sales_order.get('platform_line_key')
            lines = sales_order.get('lines')
            if (
                not isinstance(platform_line_key, str) or not platform_line_key.strip()
                or platform_line_key != '%s:1' % source_id
                or platform_line_key in seen_lines
                or not isinstance(lines, list) or len(lines) != 1
                or not isinstance(lines[0], dict) or set(lines[0]) != expected_line_fields
                or lines[0].get('platform_line_key') != platform_line_key
            ):
                raise ValidationError(_('Platform order %s has invalid line identity.') % source_id)
            seen_lines.add(platform_line_key)
            line = lines[0]
            product_id = line.get('odoo_product_id')
            if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0:
                raise ValidationError(_('Platform order %s has an invalid product ID.') % source_id)
            product = recipe_products.get(recipe_version_id)
            if not product or product.id != product_id:
                raise ValidationError(_(
                    'Platform order %s does not match its recipe aggregate product.'
                ) % source_id)
            quantity = line.get('quantity')
            gross_cents = line.get('gross_cents')
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
                raise ValidationError(_('Platform order %s quantity must be a positive integer.') % source_id)
            if not isinstance(gross_cents, int) or isinstance(gross_cents, bool) or gross_cents < 0:
                raise ValidationError(_('Platform order %s gross_cents must be nonnegative.') % source_id)
            try:
                order_date = datetime.date.fromisoformat(sales_order.get('order_date', '')).isoformat()
            except (TypeError, ValueError):
                order_date = False
            if (
                sales_order.get('currency') != currency.name
                or sales_order.get('customer_odoo_id') != warehouse_payload.get('odoo_customer_id')
                or not isinstance(sales_order.get('customer_odoo_id'), int)
                or isinstance(sales_order.get('customer_odoo_id'), bool)
                or sales_order['customer_odoo_id'] <= 0
                or not isinstance(sales_order.get('order_code'), str) or not sales_order['order_code'].strip()
                or not isinstance(sales_order.get('reference'), str) or not sales_order['reference'].strip()
                or order_date != sales_order.get('order_date')
                or not order_date
                or order_date[:7] != document_month
                or sales_order.get('expected_total_cents') != gross_cents
                or not isinstance(line.get('description'), str) or not line['description'].strip()
            ):
                raise ValidationError(_('Platform order %s has an invalid header or total.') % source_id)
            source = source_orders.get(source_id)
            if not source:
                raise ValidationError(_('Platform order %s has no frozen source-order reference.') % source_id)
            result.append({
                'source_id': source_id, 'payload_hash': payload_hash,
                'platform_line_key': platform_line_key,
                'recipe_version_id': recipe_version_id, 'product': product,
                'quantity': quantity, 'gross_cents': gross_cents,
                'order_code': sales_order['order_code'], 'order_date': order_date,
                'reference': sales_order['reference'], 'description': line['description'],
                'source': source,
            })
            current = totals.setdefault(recipe_version_id, [0, 0])
            current[0] += quantity
            current[1] += gross_cents
        if seen_orders != set(source_orders):
            raise ValidationError(_(
                'Manufacturing contract 3 sales_orders must cover source_orders exactly.'
            ))
        return result, totals

    def _validate_contract_three_sales(self, warehouses):
        self.ensure_one()
        payload_contract_version = (self.payload or {}).get('payload_contract_version')
        if not isinstance(payload_contract_version, int) \
                or isinstance(payload_contract_version, bool) \
                or payload_contract_version != 2:
            raise ValidationError(_(
                'Manufacturing contract 3 requires payload_contract_version 2.'
            ))
        for warehouse_payload in warehouses:
            if not isinstance(warehouse_payload, dict) or set(warehouse_payload) != {
                    'odoo_warehouse_id', 'odoo_customer_id', 'source_orders',
                    'sales_orders', 'recipes'}:
                raise ValidationError(_('Manufacturing contract 3 warehouse fields are invalid.'))
            groups = warehouse_payload.get('recipes') if isinstance(warehouse_payload, dict) else None
            if not isinstance(groups, list) or not groups:
                raise ValidationError(_('Manufacturing contract 3 requires recipe aggregates.'))
            currencies = {group.get('currency') for group in groups if isinstance(group, dict)}
            if len(currencies) != 1 or not next(iter(currencies), None):
                raise ValidationError(_('Manufacturing contract 3 requires one warehouse currency.'))
            currency = self.env['res.currency'].search([('name', '=', next(iter(currencies)))], limit=1)
            if not currency:
                raise ValidationError(_('Manufacturing contract 3 references an unknown currency.'))
            recipe_products = {}
            recipe_totals = {}
            group_fields = {
                'odoo_warehouse_id', 'odoo_customer_id', 'warehouse_name', 'recipe_id',
                'recipe_version_id', 'version', 'name', 'odoo_finished_product_id',
                'units_sold', 'gross_sales', 'gross_sales_cents', 'currency', 'components',
            }
            component_fields = {
                'odoo_product_id', 'quantity_per_unit', 'total_quantity', 'uom',
                'stock_quantity_per_unit', 'stock_total_quantity', 'stock_uom',
                'package_content_quantity', 'package_content_uom',
            }
            for group in groups:
                if not isinstance(group, dict) or set(group) != group_fields:
                    raise ValidationError(_('Manufacturing contract 3 recipe aggregates must be objects.'))
                if (
                    group.get('odoo_warehouse_id') != warehouse_payload['odoo_warehouse_id']
                    or group.get('odoo_customer_id') != warehouse_payload['odoo_customer_id']
                ):
                    raise ValidationError(_('Recipe aggregate warehouse identity is invalid.'))
                product_id = group.get('odoo_finished_product_id')
                quantity = group.get('units_sold')
                gross = group.get('gross_sales')
                gross_cents = group.get('gross_sales_cents')
                if (
                    not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0
                    or not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0
                    or not isinstance(gross, (int, float)) or isinstance(gross, bool)
                    or not math.isfinite(gross) or gross < 0
                    or not isinstance(gross_cents, int) or isinstance(gross_cents, bool)
                    or gross_cents < 0
                ):
                    raise ValidationError(_('Manufacturing contract 3 has invalid recipe aggregates.'))
                rounded_gross_cents = int(
                    (Decimal(str(currency.round(gross))) * 100).quantize(
                        Decimal('1'), rounding=ROUND_HALF_UP,
                    )
                )
                if rounded_gross_cents != gross_cents:
                    raise ValidationError(_(
                        'Recipe gross_sales contradicts gross_sales_cents at currency rounding.'
                    ))
                product = self.env['product.product'].browse(product_id).exists()
                if not product:
                    raise ValidationError(_('Manufacturing contract 3 references an unknown product.'))
                recipe = self._recipe_from_group(
                    group, (self.payload or {}).get('payload_contract_version'),
                )
                if recipe.get('odoo_finished_product_id') != product_id:
                    raise ValidationError(_('Recipe finished product does not match the frozen payload.'))
                for component in group.get('components') or []:
                    if not isinstance(component, dict) or set(component) != component_fields:
                        raise ValidationError(_('Recipe components must be objects.'))
                    dosage_per_unit = component.get('quantity_per_unit')
                    dosage_total = component.get('total_quantity')
                    stock_per_unit = component.get('stock_quantity_per_unit')
                    stock_total = component.get('stock_total_quantity')
                    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                               and math.isfinite(value) and value > 0 for value in (
                                   dosage_per_unit, dosage_total, stock_per_unit, stock_total)):
                        raise ValidationError(_('Recipe component quantities are invalid.'))
                    if (
                        abs(dosage_total - dosage_per_unit * quantity)
                        > max(1e-9, abs(dosage_per_unit * quantity) * 1e-8)
                        or abs(stock_total - stock_per_unit * quantity)
                        > max(1e-9, abs(stock_per_unit * quantity) * 1e-8)
                    ):
                        raise ValidationError(_('Recipe component totals do not match units_sold.'))
                recipe_version_id = self._canonical_uuid(
                    group.get('recipe_version_id'), 'recipe_version_id',
                )
                if recipe_version_id in recipe_products:
                    raise ValidationError(_('Recipe aggregates contain duplicate versions.'))
                recipe_products[recipe_version_id] = product
                recipe_totals[recipe_version_id] = [quantity, gross_cents]
            _sales, sales_totals = self._sales_order_values(
                warehouse_payload, recipe_products, currency,
            )
            if sales_totals != recipe_totals:
                raise ValidationError(_(
                    'Frozen sales-order quantities or gross cents do not match the recipe aggregates.'
                ))

    def _lock_company_month(self, document_date):
        self.ensure_one()
        return softlife_company_month_advisory_lock(
            self.env.cr, self.env.company.id, document_date,
        )

    def _process_transaction_sales(self, warehouse_payload, warehouse, customer, pricelist,
                                   currency, recipe_products, recipe_totals, warehouse_mo_ids,
                                   document_datetime):
        self.ensure_one()
        sales_orders, sales_order_totals = self._sales_order_values(
            warehouse_payload, recipe_products, currency,
        )
        expected_totals = {
            recipe_version_id: [
                int(quantity), int(gross_cents),
            ]
            for recipe_version_id, (quantity, gross_cents) in recipe_totals.items()
        }
        if sales_order_totals != expected_totals:
            raise ValidationError(_(
                'Frozen transaction quantities or gross cents do not match the recipe aggregates.'
            ))
        transaction_results = []
        for transaction in sales_orders:
            product = transaction['product']
            quantity = transaction['quantity']
            expected_gross = currency.round(transaction['gross_cents'] / 100.0)
            sale_key = 'transaction:%s' % transaction['source_id']
            sale = self.env['sale.order'].search([
                '|', ('softlife_sale_key', '=', sale_key),
                ('softlife_source_order_id', '=', transaction['source_id']),
            ], limit=1)
            created = not sale
            if not sale:
                with _softlife_manufacturing_internal_scope():
                    sale = self.env['sale.order'].create({
                        'partner_id': customer.id, 'warehouse_id': warehouse.id,
                        'pricelist_id': pricelist.id,
                        'date_order': fields.Datetime.to_datetime(transaction['order_date']),
                        'client_order_ref': transaction['reference'],
                        'softlife_export_id': self.export_id,
                        'softlife_warehouse_id': warehouse.id,
                        'softlife_sale_key': sale_key,
                        'softlife_source_order_id': transaction['source_id'],
                        'softlife_source_payload_sha256': transaction['payload_hash'],
                        'softlife_manufacturing_contract_version': 3,
                        'softlife_document_date': transaction['order_date'],
                        'softlife_source_orders': [transaction['source']],
                        'order_line': [Command.create({
                            'product_id': product.id, 'name': transaction['description'],
                            'product_uom_qty': quantity, 'product_uom': product.uom_id.id,
                            'price_unit': expected_gross / quantity,
                            'tax_id': [Command.set([])],
                            'softlife_platform_line_key': transaction['platform_line_key'],
                        })],
                    })
                    self._add_sales_rounding_adjustment(sale, expected_gross, currency)
                    sale.action_confirm()
            main_lines = sale.order_line.filtered(
                lambda line: not line.display_type and not line.softlife_rounding_adjustment)
            if (
                sale.softlife_sale_key != sale_key
                or sale.softlife_source_order_id != transaction['source_id']
                or sale.softlife_source_payload_sha256 != transaction['payload_hash']
                or sale.softlife_export_id != self.export_id
                or sale.softlife_warehouse_id != warehouse
                or sale.softlife_manufacturing_contract_version != 3
                or fields.Date.to_string(sale.softlife_document_date) != transaction['order_date']
                or sale.partner_id != customer or sale.currency_id != currency
                or sale.warehouse_id != warehouse
                or sale.state not in ('sale', 'done')
                or len(main_lines) != 1 or main_lines.product_id != product
                or main_lines.softlife_platform_line_key != transaction['platform_line_key']
                or float_compare(
                    main_lines.product_uom_qty, quantity,
                    precision_rounding=product.uom_id.rounding,
                )
                or float_compare(
                    main_lines.price_unit, expected_gross / quantity,
                    precision_rounding=currency.rounding,
                )
            ):
                raise ValidationError(_(
                    'Existing sales order for source transaction %s does not match the immutable payload.'
                ) % transaction['source_id'])
            if sale.softlife_source_orders != [transaction['source']]:
                raise ValidationError(_('Existing sales order source provenance changed.'))
            if sale.order_line.filtered(lambda line: not line.display_type and line.tax_id):
                raise ValidationError(_('Existing transaction sales order has unexpected taxes.'))
            adjustments = sale.order_line.filtered(
                lambda line: not line.display_type and line.softlife_rounding_adjustment)
            if len(adjustments) > 1 or adjustments and (
                    adjustments.product_id.default_code != 'SOFTLIFE-ROUNDING'
                    or adjustments.product_id.type != 'service'
                    or float_compare(
                        adjustments.product_uom_qty, 1.0,
                        precision_rounding=adjustments.product_uom.rounding,
                    )):
                raise ValidationError(_('Existing transaction sales rounding adjustment is invalid.'))
            if float_compare(sale.amount_total, expected_gross, precision_rounding=currency.rounding):
                raise ValidationError(_('Existing transaction sales order total is not exact.'))
            self._complete_sale_deliveries(sale, document_datetime)
            if float_compare(
                main_lines.qty_delivered, quantity,
                precision_rounding=main_lines.product_uom.rounding,
            ):
                raise ValidationError(_('Transaction sales order was not fully delivered.'))
            deliveries = sale.picking_ids.filtered(
                lambda picking: picking.state == 'done'
                and picking.picking_type_code == 'outgoing'
                and picking.location_dest_id.usage == 'customer'
            )
            if not deliveries:
                raise ValidationError(_('Transaction sales order has no completed delivery.'))
            transaction_results.append({
                'platform_order_id': transaction['source_id'],
                'platform_line_key': transaction['platform_line_key'],
                'source_sha256': transaction['payload_hash'],
                'odoo_sale_order_id': sale.id,
                'odoo_sale_order_line_id': main_lines.id,
                'created': created,
            })
        return {
            'odoo_warehouse_id': warehouse.id,
            'manufacturing_order_ids': warehouse_mo_ids,
            'sales_orders': transaction_results,
        }

    def _process_documents(self):
        self.ensure_one()
        warehouses = (self.payload or {}).get('warehouses') or []
        if not warehouses:
            raise ValidationError(_('Ready run has no warehouse payload.'))
        contract_version = int((self.payload or {}).get('manufacturing_contract_version') or 0)
        if contract_version not in (2, 3):
            raise ValidationError(_(
                'Manufacturing payload must use contract version 2 or 3.'
            ))
        cutover = fields.Date.to_date('2026-09-01')
        document_date = fields.Date.to_date(self.document_date)
        if not document_date:
            raise ValidationError(_('Manufacturing payload requires document_date.'))
        if (contract_version == 2 and document_date >= cutover) \
                or (contract_version == 3 and document_date < cutover):
            raise ValidationError(_(
                'Manufacturing contract 2 is reserved for pre-September legacy runs; '
                'contract 3 is required from 2026-09-01.'
            ))
        if contract_version == 3:
            payload_contract_version = (self.payload or {}).get('payload_contract_version')
            if not isinstance(payload_contract_version, int) \
                    or isinstance(payload_contract_version, bool) \
                    or payload_contract_version != 2:
                raise ValidationError(_(
                    'Manufacturing contract 3 requires payload_contract_version 2.'
                ))
            self._lock_company_month(document_date)
            self._validate_contract_three_sales(warehouses)
        customer = self._manufacturing_customer()
        warehouse_results = []
        document_datetime = fields.Datetime.to_datetime(self.document_date)
        for warehouse_payload in warehouses:
            warehouse_id = int(warehouse_payload.get('odoo_warehouse_id') or 0)
            warehouse = self.env['stock.warehouse'].browse(warehouse_id).exists()
            if not warehouse:
                raise ValidationError(_('Warehouse %s is missing in Odoo.') % warehouse_id)
            if warehouse.company_id != self.env.company:
                raise ValidationError(_(
                    'Warehouse %(warehouse)s belongs to %(company)s; run this process in that Odoo company.',
                    warehouse=warehouse.display_name, company=warehouse.company_id.display_name,
                ))
            if contract_version == 3 and warehouse_payload.get('odoo_customer_id') != customer.id:
                raise ValidationError(_(
                    'Warehouse %(warehouse)s customer does not match the configured vending customer.',
                    warehouse=warehouse.display_name,
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
            recipe_products = {}
            recipe_totals = {}
            warehouse_mo_ids = []
            for group in groups:
                quantity = float(group.get('units_sold') or 0)
                gross = float(group.get('gross_sales') or 0)
                if quantity <= 0 or gross < 0:
                    raise ValidationError(_('Invalid platform quantities for recipe %s.') % group.get('recipe_version_id'))
                gross_cents = group.get('gross_sales_cents') if contract_version == 3 else int(
                    (Decimal(str(gross)) * 100).quantize(
                        Decimal('1'), rounding=ROUND_HALF_UP,
                    )
                )
                if contract_version == 3 and (
                    not quantity.is_integer()
                    or not isinstance(gross_cents, int) or isinstance(gross_cents, bool)
                    or gross_cents < 0
                ):
                    raise ValidationError(_(
                        'Contract 3 recipe %s requires integer units_sold and gross_sales_cents aggregates.'
                    ) % group.get('recipe_version_id'))
                for component in group.get('components') or []:
                    dosage_per_unit = float(component.get('quantity_per_unit') or 0)
                    dosage_total = float(component.get('total_quantity') or 0)
                    stock_per_unit = float(component.get('stock_quantity_per_unit') or 0)
                    stock_total = float(component.get('stock_total_quantity') or 0)
                    expected_dosage_total = dosage_per_unit * quantity
                    expected_stock_total = stock_per_unit * quantity
                    if dosage_total <= 0 or abs(dosage_total - expected_dosage_total) > max(
                            1e-9, abs(expected_dosage_total) * 1e-8):
                        raise ValidationError(_(
                            'Frozen total_quantity is missing or inconsistent for recipe component %s.'
                        ) % component.get('odoo_product_id'))
                    if stock_total <= 0 or abs(stock_total - expected_stock_total) > max(
                            1e-9, abs(expected_stock_total) * 1e-8):
                        raise ValidationError(_(
                            'Frozen stock_total_quantity is missing or inconsistent for recipe component %s.'
                        ) % component.get('odoo_product_id'))
                recipe_sync = self.env['softlife.recipe.sync'].ensure_recipe(self._recipe_from_group(
                    group, (self.payload or {}).get('payload_contract_version'),
                ))
                self._require_make_to_stock(recipe_sync.product_id)
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
                payload_product_id = group.get('odoo_finished_product_id')
                if contract_version == 3 and recipe_sync.product_id.id != payload_product_id:
                    raise ValidationError(_('Recipe finished product does not match the frozen payload.'))
                recipe_products[recipe_sync.recipe_version_id] = recipe_sync.product_id
                recipe_totals[recipe_sync.recipe_version_id] = [
                    quantity,
                    gross_cents if contract_version == 3 else int(currency.round(gross) * 100),
                ]
                totals = sale_totals.setdefault(recipe_sync.product_id, [0.0, 0.0])
                totals[0] += quantity
                totals[1] += gross
            if contract_version == 3:
                warehouse_results.append(self._process_transaction_sales(
                    warehouse_payload, warehouse, customer, pricelist, currency,
                    recipe_products, recipe_totals, warehouse_mo_ids, document_datetime,
                ))
                continue
            order_lines = [(0, 0, {
                'product_id': product.id, 'product_uom_qty': totals[0],
                'product_uom': product.uom_id.id, 'price_unit': totals[1] / totals[0],
                'tax_id': [(6, 0, [])],
            }) for product, totals in sale_totals.items()]
            expected_gross = currency.round(sum(totals[1] for totals in sale_totals.values()))
            source_orders = self._source_orders(warehouse_payload)
            if not source_orders:
                raise ValidationError(_('Warehouse %s has no frozen source-order references.') % warehouse.display_name)
            sale = self.env['sale.order'].search([
                ('softlife_export_id', '=', self.export_id),
                ('softlife_warehouse_id', '=', warehouse.id),
            ], limit=1)
            if not sale:
                with _softlife_manufacturing_internal_scope():
                    sale = self.env['sale.order'].create({
                        'partner_id': customer.id, 'warehouse_id': warehouse.id,
                        'pricelist_id': pricelist.id,
                        'date_order': document_datetime,
                        'client_order_ref': self.export_id,
                        'softlife_export_id': self.export_id,
                        'softlife_warehouse_id': warehouse.id,
                        'softlife_sale_key': 'legacy:%s:%s' % (self.export_id, warehouse.id),
                        'softlife_manufacturing_contract_version': 2,
                        'softlife_document_date': self.document_date,
                        'softlife_source_orders': source_orders,
                        'order_line': order_lines,
                    })
                    self._add_sales_rounding_adjustment(sale, expected_gross, currency)
                    sale.action_confirm()
                self._complete_sale_deliveries(sale, document_datetime)
            elif sale.state not in ('sale', 'done') or sale.currency_id != currency \
                    or sale.partner_id != customer or sale.warehouse_id != warehouse:
                raise ValidationError(_('Existing sale order does not match the immutable payload.'))
            else:
                if sale.softlife_source_orders != source_orders:
                    raise ValidationError(_('Existing sale order source references do not match the immutable payload.'))
                expected = {
                    (values[2]['product_id'], values[2]['product_uom']): (
                        values[2]['product_uom_qty'], values[2]['price_unit'],
                    ) for values in order_lines
                }
                main_lines = sale.order_line.filtered(
                    lambda line: not line.display_type and not line.softlife_rounding_adjustment)
                actual = {(line.product_id.id, line.product_uom.id): (
                    line.product_uom_qty, line.price_unit,
                ) for line in main_lines}
                if set(expected) != set(actual) or any(
                    float_compare(
                        actual[key][0], values[0],
                        precision_rounding=self.env['uom.uom'].browse(key[1]).rounding,
                    )
                    or float_compare(actual[key][1], values[1], precision_rounding=currency.rounding)
                    for key, values in expected.items()
                ):
                    raise ValidationError(_('Existing sale order lines do not match the immutable payload.'))
                adjustments = sale.order_line.filtered(
                    lambda line: not line.display_type and line.softlife_rounding_adjustment)
                if len(adjustments) > 1 or adjustments and (
                        adjustments.product_id.default_code != 'SOFTLIFE-ROUNDING'
                        or adjustments.product_id.type != 'service'
                        or float_compare(adjustments.product_uom_qty, 1.0,
                                         precision_rounding=adjustments.product_uom.rounding)):
                    raise ValidationError(_('Existing sales rounding adjustment is invalid.'))
                if sale.order_line.filtered(lambda line: not line.display_type and line.tax_id):
                    raise ValidationError(_('Existing sale order has taxes but the frozen export is gross sales.'))
                self._complete_sale_deliveries(sale, document_datetime)
            if float_compare(sale.amount_total, expected_gross, precision_rounding=currency.rounding):
                raise ValidationError(_(
                    'Sales order %(sale)s total %(actual)s does not match platform gross sales %(expected)s.',
                    sale=sale.display_name, actual=sale.amount_total, expected=expected_gross,
                ))
            incomplete_lines = sale.order_line.filtered(
                lambda line: not line.display_type and not line.softlife_rounding_adjustment and float_compare(
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

    def _replenishment_external_key(self, transfer_key):
        self.ensure_one()
        return '%s:replenishment:%s' % (self.idempotency_key or self.export_id, transfer_key)

    def _validate_existing_replenishment(self, picking, values):
        self.ensure_one()
        move = picking.move_ids
        if picking.state != 'done' or len(move) != 1 \
                or picking.picking_type_id != values['source'].int_type_id \
                or picking.location_id != values['source'].lot_stock_id \
                or picking.location_dest_id != values['destination'].lot_stock_id \
                or move.product_id != values['product'] \
                or move.product_uom != values['uom'] \
                or float_compare(
                    move.product_uom_qty, values['quantity'],
                    precision_rounding=values['uom'].rounding,
                ):
            raise ValidationError(_(
                'Existing replenishment transfer %s does not match the immutable payload.'
            ) % values['transfer_key'])
        actual_lots = {}
        for line in move.move_line_ids:
            if line.lot_id:
                actual_lots[line.lot_id.id] = actual_lots.get(line.lot_id.id, 0.0) + line.quantity
        expected_lots = {lot.id: quantity for lot, quantity in values['lots']}
        actual_quantity = sum(move.move_line_ids.mapped('quantity'))
        if set(actual_lots) != set(expected_lots) or any(
            float_compare(actual_lots[lot_id], quantity, precision_rounding=values['uom'].rounding)
            for lot_id, quantity in expected_lots.items()
        ) or float_compare(actual_quantity, values['quantity'], precision_rounding=values['uom'].rounding):
            raise ValidationError(_(
                'Existing replenishment transfer %s does not match the selected lots.'
            ) % values['transfer_key'])

    def _replenishment_values(self):
        self.ensure_one()
        replenishment = (self.payload or {}).get('replenishment') or {}
        if not replenishment.get('required') or not replenishment.get('plan_complete'):
            raise ValidationError(_('Replenishment-ready run does not contain a complete required plan.'))
        source_warehouse_id = int(replenishment.get('source_warehouse_id') or 0)
        if not source_warehouse_id:
            raise ValidationError(_('Replenishment source_warehouse_id is required.'))
        transfers = replenishment.get('transfers') or []
        if not transfers:
            raise ValidationError(_('Replenishment-ready run has no transfers.'))
        result = []
        seen_keys = set()
        Recipe = self.env['softlife.recipe.sync']
        for transfer in transfers:
            transfer_key = str(transfer.get('transfer_key') or '')
            if not transfer_key or transfer_key in seen_keys:
                raise ValidationError(_('Every replenishment transfer_key must be present and unique.'))
            seen_keys.add(transfer_key)
            transfer_source_id = int(transfer.get('source_warehouse_id') or 0)
            destination_id = int(transfer.get('destination_warehouse_id') or 0)
            if transfer_source_id != source_warehouse_id:
                raise ValidationError(_(
                    'Transfer %s source warehouse differs from the replenishment source warehouse.'
                ) % transfer_key)
            source = self.env['stock.warehouse'].browse(transfer_source_id).exists()
            destination = self.env['stock.warehouse'].browse(destination_id).exists()
            if not source or not destination or source == destination:
                raise ValidationError(_('Transfer %s references invalid warehouses.') % transfer_key)
            if source.company_id != destination.company_id or source.company_id != self.env.company:
                raise ValidationError(_('Transfer %s warehouses must belong to the active company.') % transfer_key)
            if not source.lot_stock_id or not destination.lot_stock_id or not source.int_type_id:
                raise ValidationError(_('Transfer %s warehouses are not configured for internal transfers.') % transfer_key)
            product_id = int(transfer.get('odoo_product_id') or 0)
            product = self.env['product.product'].browse(product_id).exists()
            if not product:
                raise ValidationError(_('Transfer %s references a missing product.') % transfer_key)
            uom = Recipe._uom(transfer.get('stock_uom'), product)
            if uom != product.uom_id:
                raise ValidationError(_(
                    'Transfer %(transfer)s stock UoM must equal %(uom)s.',
                    transfer=transfer_key, uom=product.uom_id.display_name,
                ))
            quantity = float(transfer.get('quantity') or 0)
            if float_compare(quantity, 0.0, precision_rounding=uom.rounding) <= 0:
                raise ValidationError(_('Transfer %s quantity must be positive.') % transfer_key)
            lots = []
            seen_lots = set()
            for selected in transfer.get('lots') or []:
                lot_id = int(selected.get('odoo_lot_id') or 0)
                lot_quantity = float(selected.get('quantity') or 0)
                lot = self.env['stock.lot'].browse(lot_id).exists()
                if not lot or lot.product_id != product or lot_id in seen_lots \
                        or float_compare(lot_quantity, 0.0, precision_rounding=uom.rounding) <= 0:
                    raise ValidationError(_('Transfer %s contains an invalid selected lot.') % transfer_key)
                seen_lots.add(lot_id)
                lots.append((lot, lot_quantity))
            if product.tracking != 'none' and not lots:
                raise ValidationError(_(
                    'Transfer %s requires manually selected source lots.'
                ) % transfer_key)
            if product.tracking == 'none' and lots:
                raise ValidationError(_('Untracked transfer %s cannot contain source lots.') % transfer_key)
            if lots and float_compare(
                sum(lot_quantity for _lot, lot_quantity in lots), quantity,
                precision_rounding=uom.rounding,
            ):
                raise ValidationError(_(
                    'Transfer %s selected lot quantities must equal the whole requested quantity.'
                ) % transfer_key)
            result.append({
                'transfer_key': transfer_key,
                'external_key': self._replenishment_external_key(transfer_key),
                'source': source, 'destination': destination,
                'product': product, 'uom': uom, 'quantity': quantity, 'lots': lots,
            })
        return result

    def _create_replenishment_picking(self, values, document_datetime):
        self.ensure_one()
        Picking = self.env['stock.picking']
        existing = Picking.search([
            ('softlife_replenishment_key', '=', values['external_key']),
        ], limit=1)
        if existing:
            self._validate_existing_replenishment(existing, values)
            return existing

        quant_ids = self.env['stock.quant'].search([
            ('product_id', '=', values['product'].id),
            ('location_id', 'child_of', values['source'].lot_stock_id.id),
            *([('lot_id', 'in', [lot.id for lot, _quantity in values['lots']])]
              if values['lots'] else []),
        ]).ids
        if quant_ids:
            self.env.cr.execute('SELECT id FROM stock_quant WHERE id IN %s FOR UPDATE', [tuple(quant_ids)])
        Quant = self.env['stock.quant']
        for lot, quantity in values['lots']:
            available = Quant._get_available_quantity(
                values['product'], values['source'].lot_stock_id, lot_id=lot, strict=False,
            )
            if float_compare(available, quantity, precision_rounding=values['uom'].rounding) < 0:
                raise ValidationError(_(
                    'Transfer %(transfer)s lot %(lot)s has %(available)s available; %(required)s is required.',
                    transfer=values['transfer_key'], lot=lot.display_name,
                    available=available, required=quantity,
                ))

        picking = Picking.create({
            'picking_type_id': values['source'].int_type_id.id,
            'location_id': values['source'].lot_stock_id.id,
            'location_dest_id': values['destination'].lot_stock_id.id,
            'origin': self.export_id,
            'softlife_replenishment_key': values['external_key'],
            'softlife_export_id': self.export_id,
            'softlife_transfer_key': values['transfer_key'],
            'softlife_payload_sha256': self.payload_sha256,
            'move_ids': [Command.create({
                'name': values['product'].display_name,
                'product_id': values['product'].id,
                'product_uom_qty': values['quantity'],
                'product_uom': values['uom'].id,
                'location_id': values['source'].lot_stock_id.id,
                'location_dest_id': values['destination'].lot_stock_id.id,
            })],
        })
        picking.action_confirm()
        move = picking.move_ids
        if move.move_line_ids:
            move._do_unreserve()
        allocations = values['lots'] or [(False, values['quantity'])]
        for lot, quantity in allocations:
            reserve_kwargs = {'lot_id': lot} if lot else {}
            reserved = move._update_reserved_quantity(
                quantity, values['source'].lot_stock_id, strict=False, **reserve_kwargs,
            )
            if float_compare(reserved, quantity, precision_rounding=values['uom'].rounding):
                raise ValidationError(_(
                    'Transfer %(transfer)s could not fully reserve %(lot)s.',
                    transfer=values['transfer_key'], lot=lot.display_name if lot else values['product'].display_name,
                ))
        reserved_lots = {}
        for line in move.move_line_ids:
            if line.lot_id:
                reserved_lots[line.lot_id.id] = reserved_lots.get(line.lot_id.id, 0.0) + line.quantity
        expected_lots = {lot.id: quantity for lot, quantity in values['lots']}
        reserved_quantity = sum(move.move_line_ids.mapped('quantity'))
        if set(reserved_lots) != set(expected_lots) or any(
            float_compare(reserved_lots[lot_id], quantity, precision_rounding=values['uom'].rounding)
            for lot_id, quantity in expected_lots.items()
        ) or float_compare(reserved_quantity, values['quantity'], precision_rounding=values['uom'].rounding):
            raise ValidationError(_(
                'Transfer %s did not reserve exactly the selected lots.'
            ) % values['transfer_key'])
        if 'picked' in move._fields:
            move.picked = True
        result = picking.with_context(
            skip_backorder=True, cancel_backorder=False, force_period_date=self.document_date,
        ).button_validate()
        if picking.state != 'done' and isinstance(result, dict):
            raise UserError(_(
                'Internal transfer %(transfer)s requires wizard %(wizard)s; no partial transfer was accepted.',
                transfer=values['transfer_key'],
                wizard=result.get('res_model') or result.get('name') or 'unknown',
            ))
        if picking.state != 'done':
            raise UserError(_('Internal transfer %s did not reach Done.') % values['transfer_key'])
        backorders = Picking.search([('backorder_id', '=', picking.id)])
        if backorders:
            raise UserError(_('Internal transfer %s created a backorder.') % values['transfer_key'])
        picking.write({'date_done': document_datetime})
        move.write({'date': document_datetime})
        if 'date' in move.move_line_ids._fields:
            move.move_line_ids.write({'date': document_datetime})
        self._validate_existing_replenishment(picking, values)
        return picking

    def _process_replenishment(self):
        self.ensure_one()
        effective_date = ((self.payload or {}).get('replenishment') or {}).get('effective_date')
        if not effective_date:
            raise ValidationError(_('Replenishment effective_date is required.'))
        document_datetime = fields.Datetime.to_datetime(effective_date)
        pickings = self.env['stock.picking']
        transfer_results = []
        for values in self._replenishment_values():
            picking = self._create_replenishment_picking(values, document_datetime)
            pickings |= picking
            transfer_results.append({
                'transfer_key': values['transfer_key'], 'picking_id': picking.id,
            })
        return {
            'payload_sha256': self.payload_sha256,
            'accepted': True,
            'picking_ids': pickings.ids,
            'transfers': transfer_results,
            'error': None,
        }

    def action_process_replenishment(self):
        self.ensure_one()
        self._check_active_company()
        self.env.cr.execute(
            'SELECT id FROM softlife_manufacturing_run WHERE id = %s FOR UPDATE NOWAIT', [self.id],
        )
        self.invalidate_recordset()
        if self.platform_status != 'replenishment_ready':
            raise UserError(_('Only replenishment-ready runs can create internal transfers.'))
        if self.replenishment_state in ('result_pending', 'completed'):
            return True
        self.replenishment_state = 'processing'
        try:
            with self.env.cr.savepoint():
                result = self._process_replenishment()
            self.write({
                'replenishment_state': 'result_pending', 'replenishment_result': result,
                'replenishment_error': False, 'replenishment_callback_error': False,
            })
        except Exception as exc:
            error = {
                'code': 'replenishment_transfer_failed',
                'type': type(exc).__name__,
                'message': str(exc),
            }
            self.write({
                'replenishment_state': 'result_pending',
                'replenishment_result': {
                    'payload_sha256': self.payload_sha256,
                    'accepted': False, 'picking_ids': [], 'error': error,
                },
                'replenishment_error': str(exc), 'replenishment_callback_error': False,
            })
            _logger.exception('SoftLife replenishment for run %s failed', self.export_id)
        return True

    def action_retry_replenishment_callback(self):
        client = self.env['softlife.sync.client']
        for run in self.filtered(
                lambda row: row.replenishment_state == 'result_pending' and row.replenishment_result):
            try:
                if run.replenishment_result.get('accepted') is True:
                    client.sync_odoo_lot_stock()
                remote = client._api_request(
                    'POST',
                    f'/api/internal/odoo/manufacturing-periods/{run.export_id}/replenishment-result',
                    payload=run.replenishment_result,
                )
                run.upsert_remote(remote)
                accepted = run.replenishment_result.get('accepted') is True
                run.write({
                    'replenishment_state': 'completed' if accepted else 'failed',
                    'replenishment_callback_error': False,
                })
            except Exception as exc:
                run.replenishment_callback_error = str(exc)
                _logger.warning('SoftLife replenishment callback failed for %s: %s', run.export_id, exc)
        return True

    def action_process(self):
        self.ensure_one()
        self._check_active_company()
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
        if not self.env.context.get('softlife_catalog_synced'):
            self.action_sync_catalog()
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
                'processing_state': 'failed' if retrying_rejection else 'result_pending',
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
            if isinstance(run.result_payload, dict) \
                    and run.result_payload.get('accepted') is False \
                    and run.platform_status == 'failed' \
                    and isinstance(run.platform_result, dict) \
                    and run.platform_result.get('accepted') is False:
                run.write({'processing_state': 'failed', 'callback_error': False})
                continue
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
        self.search([('replenishment_state', '=', 'result_pending')]).action_retry_replenishment_callback()
        self.env['softlife.recipe.sync'].search([('callback_state', '=', 'pending')]).action_retry_callback()
        try:
            self.action_sync_catalog()
            self.action_refresh()
            for run in self.search([
                ('platform_status', '=', 'replenishment_ready'),
                ('replenishment_state', '=', 'pending'),
            ]):
                try:
                    source_id = int(((run.payload or {}).get('replenishment') or {}).get('source_warehouse_id') or 0)
                    source = self.env['stock.warehouse'].browse(source_id).exists()
                    with self.env.cr.savepoint():
                        (run.with_company(source.company_id) if source else run).action_process_replenishment()
                except Exception:
                    _logger.exception('SoftLife replenishment cron failed for %s', run.export_id)
            for run in self.search([('platform_status', '=', 'ready'), ('processing_state', '=', 'new')]):
                try:
                    warehouse_ids = [int(row.get('odoo_warehouse_id') or 0)
                                     for row in (run.payload or {}).get('warehouses') or []]
                    warehouse = self.env['stock.warehouse'].browse(warehouse_ids[:1]).exists()
                    target = run.with_company(warehouse.company_id) if warehouse else run
                    with self.env.cr.savepoint():
                        target.with_context(softlife_catalog_synced=True).action_process()
                except Exception:
                    _logger.exception('SoftLife manufacturing cron failed for %s', run.export_id)
        except Exception:
            _logger.exception('SoftLife manufacturing cron pass failed')
