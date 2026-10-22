from unittest.mock import Mock, patch
import uuid

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

from ..models.locks import softlife_company_month_advisory_lock
from ..models.manufacturing_documents import _softlife_manufacturing_internal_scope


class TestManufacturingRun(TransactionCase):

    def test_remote_values_normalize_iso_datetimes_to_utc(self):
        values = self.env['softlife.manufacturing.run']._remote_values({
            'export_id': 'export-1',
            'period_from': '2026-07-08T22:00:00+00:00',
            'period_to': '2026-07-10T00:00:00+02:00',
            'manufacturing_contract_version': 2,
        })

        self.assertEqual(values['period_from'], '2026-07-08 22:00:00')
        self.assertEqual(values['period_to'], '2026-07-09 22:00:00')
        self.assertEqual(values['payload']['manufacturing_contract_version'], 2)

    def test_remote_values_preserve_replenishment_payload(self):
        replenishment = {
            'required': True,
            'plan_complete': True,
            'source_warehouse_id': 3,
            'observed_at': '2026-09-17T10:00:00Z',
            'requirements': [{'odoo_product_id': 10, 'quantity': 4}],
            'uncovered': [],
            'transfers': [{'transfer_key': 'transfer-1'}],
        }

        values = self.env['softlife.manufacturing.run']._remote_values({
            'export_id': 'export-replenishment',
            'status': 'replenishment_ready',
            'replenishment': replenishment,
        })

        self.assertEqual(values['platform_status'], 'replenishment_ready')
        self.assertEqual(values['payload']['replenishment'], replenishment)

    def test_upsert_recognizes_replenishment_statuses(self):
        model = self.env['softlife.manufacturing.run']

        run = model.upsert_remote({
            'export_id': 'export-replenishment-status',
            'status': 'replenishment_ready',
            'payload_sha256': 'sha',
        })
        self.assertEqual(run.replenishment_state, 'pending')

        model.upsert_remote({
            'export_id': run.export_id,
            'status': 'replenishment_failed',
            'payload_sha256': 'sha',
        })
        self.assertEqual(run.replenishment_state, 'failed')

    def test_remote_values_reject_invalid_datetime(self):
        with self.assertRaisesRegex(ValidationError, 'invalid datetime'):
            self.env['softlife.manufacturing.run']._remote_values({
                'export_id': 'export-1',
                'period_from': 'not-a-datetime',
            })

    def test_processing_requires_warehouse_company_to_be_active(self):
        other_company = self.env['res.company'].create({'name': 'Other Manufacturing Company'})
        warehouse = self.env['stock.warehouse'].search([
            ('company_id', '=', other_company.id),
        ], limit=1)
        if not warehouse:
            warehouse = self.env['stock.warehouse'].create({
                'name': 'Other Manufacturing Warehouse',
                'code': 'OMW',
                'company_id': other_company.id,
            })
        run = self.env['softlife.manufacturing.run'].new({
            'payload': {'warehouses': [{'odoo_warehouse_id': warehouse.id}]},
        })

        with self.assertRaisesRegex(UserError, 'Switch the active Odoo company'):
            run._check_active_company()

    def test_processing_rejects_legacy_manufacturing_contract(self):
        run = self.env['softlife.manufacturing.run'].new({
            'payload': {'manufacturing_contract_version': 1, 'warehouses': [{}]},
        })

        with self.assertRaisesRegex(ValidationError, 'contract version 2'):
            run._process_documents()

    def test_september_rejects_aggregate_contract(self):
        run = self.env['softlife.manufacturing.run'].new({
            'document_date': '2026-09-01',
            'payload': {'manufacturing_contract_version': 2, 'warehouses': [{}]},
        })

        with self.assertRaisesRegex(ValidationError, 'contract 3 is required'):
            run._process_documents()

    def test_august_rejects_transaction_contract(self):
        run = self.env['softlife.manufacturing.run'].new({
            'document_date': '2026-08-31',
            'payload': {'manufacturing_contract_version': 3, 'warehouses': [{}]},
        })

        with self.assertRaisesRegex(ValidationError, 'reserved for pre-September'):
            run._process_documents()

    def test_contract_three_requires_recipe_payload_contract_two(self):
        run = self.env['softlife.manufacturing.run'].new({
            'document_date': '2026-09-30',
            'payload': {
                'payload_contract_version': 3,
                'manufacturing_contract_version': 3,
                'warehouses': [{}],
            },
        })

        with self.assertRaisesRegex(ValidationError, 'payload_contract_version 2'):
            run._process_documents()

    def test_contract_three_locks_document_month_before_sales_validation(self):
        run = self.env['softlife.manufacturing.run'].new({
            'document_date': '2026-09-30',
            'payload': {
                'payload_contract_version': 2,
                'manufacturing_contract_version': 3,
                'warehouses': [{}],
            },
        })
        events = []

        def stop_validation(_warehouses):
            events.append('validate')
            raise ValidationError('stop after lock')

        with patch.object(type(run), '_lock_company_month',
                          side_effect=lambda date: events.append(('lock', date))), \
                patch.object(type(run), '_validate_contract_three_sales',
                             side_effect=stop_validation), \
                self.assertRaisesRegex(ValidationError, 'stop after lock'):
            run._process_documents()

        self.assertEqual(events, [
            ('lock', fields.Date.to_date('2026-09-30')), 'validate',
        ])

    def test_company_month_lock_uses_fiscal_key_shape(self):
        cursor = Mock()

        key = softlife_company_month_advisory_lock(cursor, 17, '2026-09-30')

        self.assertEqual(key, 202609)
        cursor.execute.assert_called_once_with(
            'SELECT pg_advisory_xact_lock(%s, %s)', [17, 202609],
        )

    def test_contract_three_sales_values_require_exact_unique_identity(self):
        run = self.env['softlife.manufacturing.run'].new({
            'document_date': '2026-09-30',
        })
        product = self.env['product.product'].create({'name': 'Transaction product'})
        source_id = str(uuid.uuid4())
        recipe_version_id = str(uuid.uuid4())
        currency = self.env.company.currency_id
        line_key = f'{source_id}:1'
        sale = {
            'platform_order_id': source_id, 'platform_line_key': line_key,
            'source_sha256': 'a' * 64, 'recipe_version_id': recipe_version_id,
            'order_code': 'TX-1',
            'order_date': '2026-09-29', 'customer_odoo_id': 7,
            'currency': currency.name, 'reference': 'TX-1', 'expected_total_cents': 800,
            'lines': [{
                'platform_line_key': line_key, 'odoo_product_id': product.id,
                'description': 'Transaction product', 'quantity': 2, 'gross_cents': 800,
            }],
        }
        warehouse = {
            'odoo_customer_id': 7,
            'source_orders': [{
                'platform_order_id': source_id, 'order_code': 'TX-1',
                'machine_id': None, 'machine_imei': None, 'machine_name': 'Test machine',
            }],
            'sales_orders': [sale],
        }

        values, totals = run._sales_order_values(
            warehouse, {recipe_version_id: product}, currency,
        )

        self.assertEqual(values[0]['source_id'], source_id)
        self.assertEqual(totals, {recipe_version_id: [2, 800]})
        with self.assertRaisesRegex(ValidationError, 'appears more than once'):
            run._sales_order_values(
                {**warehouse, 'sales_orders': [sale, sale]},
                {recipe_version_id: product}, currency,
            )

    def test_contract_three_recipe_cents_match_sum_of_per_order_cents(self):
        product = self.env['product.product'].create({'name': 'Aggregate cents product'})
        currency = self.env.company.currency_id
        source_id = str(uuid.uuid4())
        recipe_id = str(uuid.uuid4())
        recipe_version_id = str(uuid.uuid4())
        line_key = f'{source_id}:1'
        component = {
            'odoo_product_id': product.id, 'quantity_per_unit': 1,
            'total_quantity': 2, 'uom': product.uom_id.name,
            'stock_quantity_per_unit': 1, 'stock_total_quantity': 2,
            'stock_uom': product.uom_id.name,
            'package_content_quantity': None, 'package_content_uom': None,
        }
        group = {
            'odoo_warehouse_id': 7, 'odoo_customer_id': 8,
            'warehouse_name': 'Test warehouse', 'recipe_id': recipe_id,
            'recipe_version_id': recipe_version_id, 'version': 1,
            'name': product.name, 'odoo_finished_product_id': product.id,
            'units_sold': 2, 'gross_sales': 8.0, 'gross_sales_cents': 800,
            'currency': currency.name, 'components': [component],
        }
        warehouse = {
            'odoo_warehouse_id': 7, 'odoo_customer_id': 8,
            'source_orders': [{
                'platform_order_id': source_id, 'order_code': 'TX-1',
                'machine_id': None, 'machine_imei': None, 'machine_name': 'Test machine',
            }],
            'sales_orders': [{
                'platform_order_id': source_id, 'platform_line_key': line_key,
                'source_sha256': 'a' * 64,
                'recipe_version_id': recipe_version_id, 'order_code': 'TX-1',
                'order_date': '2026-09-29', 'customer_odoo_id': 8,
                'currency': currency.name, 'reference': 'TX-1',
                'expected_total_cents': 800,
                'lines': [{
                    'platform_line_key': line_key, 'odoo_product_id': product.id,
                    'description': product.name, 'quantity': 2, 'gross_cents': 800,
                }],
            }],
            'recipes': [group],
        }
        source_id_2 = str(uuid.uuid4())
        recipe_version_id_2 = str(uuid.uuid4())
        line_key_2 = f'{source_id_2}:1'
        warehouse['source_orders'].append({
            'platform_order_id': source_id_2, 'order_code': 'TX-2',
            'machine_id': None, 'machine_imei': None, 'machine_name': 'Test machine',
        })
        warehouse['sales_orders'].append({
            'platform_order_id': source_id_2, 'platform_line_key': line_key_2,
            'source_sha256': 'b' * 64, 'recipe_version_id': recipe_version_id_2,
            'order_code': 'TX-2', 'order_date': '2026-09-29',
            'customer_odoo_id': 8, 'currency': currency.name, 'reference': 'TX-2',
            'expected_total_cents': 300,
            'lines': [{
                'platform_line_key': line_key_2, 'odoo_product_id': product.id,
                'description': product.name, 'quantity': 1, 'gross_cents': 300,
            }],
        })
        warehouse['recipes'].append({
            **group, 'recipe_version_id': recipe_version_id_2, 'version': 2,
            'units_sold': 1, 'gross_sales': 3.0, 'gross_sales_cents': 300,
            'components': [{
                **component, 'total_quantity': 1, 'stock_total_quantity': 1,
            }],
        })
        run = self.env['softlife.manufacturing.run'].new({
            'document_date': '2026-09-30',
            'payload': {'payload_contract_version': 2},
        })
        with patch.object(
                type(run), '_recipe_from_group',
                return_value={'odoo_finished_product_id': product.id}):
            run._validate_contract_three_sales([warehouse])
            warehouse['recipes'][0]['gross_sales'] = 8.01
            with self.assertRaisesRegex(ValidationError, 'contradicts gross_sales_cents'):
                run._validate_contract_three_sales([warehouse])
            warehouse['recipes'][0]['gross_sales'] = 7.99
            warehouse['recipes'][0]['gross_sales_cents'] = 799
            with self.assertRaisesRegex(ValidationError, 'do not match'):
                run._validate_contract_three_sales([warehouse])

    def test_contract_three_rejects_sale_outside_document_month(self):
        run = self.env['softlife.manufacturing.run'].new({
            'document_date': '2026-09-30',
        })
        product = self.env['product.product'].create({'name': 'Month-bound product'})
        source_id = str(uuid.uuid4())
        recipe_version_id = str(uuid.uuid4())
        line_key = f'{source_id}:1'
        currency = self.env.company.currency_id
        warehouse = {
            'odoo_customer_id': 7,
            'source_orders': [{
                'platform_order_id': source_id, 'order_code': 'TX-1',
                'machine_id': None, 'machine_imei': None, 'machine_name': 'Test machine',
            }],
            'sales_orders': [{
                'platform_order_id': source_id, 'platform_line_key': line_key,
                'source_sha256': 'a' * 64, 'recipe_version_id': recipe_version_id,
                'order_code': 'TX-1', 'order_date': '2026-08-31',
                'customer_odoo_id': 7, 'currency': currency.name,
                'reference': 'TX-1', 'expected_total_cents': 800,
                'lines': [{
                    'platform_line_key': line_key, 'odoo_product_id': product.id,
                    'description': product.name, 'quantity': 2, 'gross_cents': 800,
                }],
            }],
        }

        with self.assertRaisesRegex(ValidationError, 'invalid header or total'):
            run._sales_order_values(
                warehouse, {recipe_version_id: product}, currency,
            )

    def test_connector_sales_core_fields_are_frozen_for_ordinary_writes(self):
        warehouse = self.env['stock.warehouse'].search([
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        if not warehouse:
            self.skipTest('Test company has no warehouse.')
        partner = self.env['res.partner'].create({'name': 'Frozen sale customer'})
        other_partner = self.env['res.partner'].create({'name': 'Other customer'})
        product = self.env['product.product'].create({
            'name': 'Frozen sale product', 'type': 'service',
        })
        source_id = str(uuid.uuid4())
        with _softlife_manufacturing_internal_scope():
            sale = self.env['sale.order'].create({
                'partner_id': partner.id, 'warehouse_id': warehouse.id,
                'softlife_export_id': str(uuid.uuid4()),
                'softlife_warehouse_id': warehouse.id,
                'softlife_sale_key': f'transaction:{source_id}',
                'softlife_source_order_id': source_id,
                'softlife_source_payload_sha256': 'a' * 64,
                'softlife_manufacturing_contract_version': 3,
                'softlife_document_date': '2026-09-30',
                'softlife_source_orders': [{'platform_order_id': source_id}],
                'order_line': [(0, 0, {
                    'product_id': product.id, 'name': product.name,
                    'product_uom_qty': 1, 'product_uom': product.uom_id.id,
                    'price_unit': 1, 'tax_id': [(6, 0, [])],
                    'softlife_platform_line_key': f'{source_id}:1',
                })],
            })
            sale.action_confirm()
        line = sale.order_line

        for values in (
                {'partner_id': other_partner.id}, {'partner_invoice_id': other_partner.id},
                {'partner_shipping_id': other_partner.id}, {'company_id': self.env.company.id},
                {'warehouse_id': warehouse.id}, {'currency_id': sale.currency_id.id},
                {'pricelist_id': sale.pricelist_id.id}, {'date_order': '2026-10-01'},
                {'state': 'cancel'}, {'client_order_ref': 'changed'}):
            with self.subTest(values=values), \
                    self.assertRaisesRegex(UserError, 'fields are frozen'):
                sale.write(values)
        for values in (
                {'softlife_export_id': str(uuid.uuid4())},
                {'softlife_warehouse_id': warehouse.id}):
            with self.subTest(values=values), \
                    self.assertRaisesRegex(ValidationError, 'provenance cannot be changed'):
                sale.write(values)
        for values in (
                {'product_id': product.id}, {'product_uom_qty': 2},
                {'product_uom': product.uom_id.id}, {'price_unit': 2},
                {'tax_id': [(6, 0, [])]}, {'name': 'Changed'},
                {'softlife_rounding_adjustment': True}):
            with self.subTest(values=values), \
                    self.assertRaisesRegex(UserError, 'lines are frozen'):
                line.write(values)
        with self.assertRaisesRegex(UserError, 'lines are frozen'):
            self.env['sale.order.line'].create({
                'order_id': sale.id, 'product_id': product.id,
                'product_uom_qty': 1, 'product_uom': product.uom_id.id,
            })

        sale.write({'note': 'Unprotected computed/supporting fields remain writable.'})
        with _softlife_manufacturing_internal_scope():
            sale.write({'client_order_ref': 'Connector-authorized update'})
            line.write({'name': 'Connector-authorized line'})

    def test_repeated_rejection_does_not_replace_platform_result(self):
        run = self.env['softlife.manufacturing.run'].create({
            'export_id': 'export-retry',
            'platform_status': 'failed',
            'processing_state': 'result_pending',
            'platform_result': {'accepted': False, 'error': 'First failure'},
            'result_payload': {'accepted': False, 'error': 'Retry failure'},
        })
        client = self.env['softlife.sync.client']

        with patch.object(type(client), '_api_request') as request:
            run.action_retry_callback()

        request.assert_not_called()
        self.assertEqual(run.processing_state, 'failed')
        self.assertFalse(run.callback_error)

    def test_manufacturing_customer_uses_configured_vending_partner(self):
        partner = self.env['res.partner'].create({'name': 'Consumidor Final'})
        self.env['ir.config_parameter'].sudo().set_param('softlife.sync.default_partner_id', partner.id)

        customer = self.env['softlife.manufacturing.run']._manufacturing_customer()

        self.assertEqual(customer, partner)

    def test_manufacturing_customer_requires_configuration(self):
        self.env['ir.config_parameter'].sudo().set_param('softlife.sync.default_partner_id', '')

        with self.assertRaisesRegex(ValidationError, 'Vending Customer / Consumidor Final'):
            self.env['softlife.manufacturing.run']._manufacturing_customer()

    def test_source_orders_preserve_order_to_machine_mapping(self):
        run = self.env['softlife.manufacturing.run']
        sources = run._source_orders({'source_orders': [{
            'platform_order_id': 'order-id', 'order_code': 'S00124',
            'machine_id': 'machine-id', 'machine_imei': '860000000000001',
            'machine_name': 'Flowers',
        }]})

        self.assertEqual(sources[0]['order_code'], 'S00124')
        self.assertEqual(sources[0]['machine_id'], 'machine-id')
        sale = self.env['sale.order'].new({'softlife_source_orders': sources})
        sale._compute_softlife_source_summary()
        self.assertIn('S00124 - Flowers', sale.softlife_source_summary)

    def test_sales_rounding_adjustment_reconciles_exact_currency_total(self):
        currency = self.env.company.currency_id
        partner = self.env['res.partner'].create({'name': 'Rounding Customer'})
        product = self.env['product.product'].create({
            'name': 'Rounding Test Product', 'type': 'service',
        })
        sale = self.env['sale.order'].create({
            'partner_id': partner.id,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': 3,
                'price_unit': 2.67,
                'tax_id': [(6, 0, [])],
            })],
        })

        difference = self.env['softlife.manufacturing.run']._add_sales_rounding_adjustment(
            sale, 8.00, currency,
        )

        adjustment = sale.order_line.filtered('softlife_rounding_adjustment')
        self.assertEqual(difference, -0.01)
        self.assertEqual(len(adjustment), 1)
        self.assertEqual(adjustment.product_id.default_code, 'SOFTLIFE-ROUNDING')
        self.assertEqual(adjustment.product_id.type, 'service')
        self.assertEqual(adjustment.tax_id, self.env['account.tax'])
        self.assertEqual(currency.round(sale.amount_total), 8.00)

    def _replenishment_records(self):
        source = self.env['stock.warehouse'].search([
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        if not source:
            source = self.env['stock.warehouse'].create({
                'name': 'Replenishment Source', 'code': 'RPS', 'company_id': self.env.company.id,
            })
        destination = self.env['stock.warehouse'].create({
            'name': 'Replenishment Destination', 'code': 'RPD',
            'company_id': self.env.company.id,
        })
        product_values = {'name': 'Lot-tracked replenishment product', 'type': 'consu', 'tracking': 'lot'}
        if 'is_storable' in self.env['product.template']._fields:
            product_values['is_storable'] = True
        product = self.env['product.template'].create(product_values).product_variant_id
        lot = self.env['stock.lot'].create({
            'name': 'REPLENISHMENT-LOT', 'product_id': product.id, 'company_id': self.env.company.id,
        })
        return source, destination, product, lot

    def _replenishment_run(self, source, destination, product, lot, **transfer_overrides):
        transfer = {
            'transfer_key': 'transfer-1',
            'source_warehouse_id': source.id,
            'destination_warehouse_id': destination.id,
            'odoo_product_id': product.id,
            'stock_uom': product.uom_id.name,
            'quantity': 2.0,
            'lots': [{'odoo_lot_id': lot.id, 'quantity': 2.0}],
        }
        transfer.update(transfer_overrides)
        return self.env['softlife.manufacturing.run'].create({
            'export_id': 'export-replenishment-test',
            'idempotency_key': 'period-key',
            'platform_status': 'replenishment_ready',
            'document_date': '2026-09-16',
            'payload_sha256': 'payload-sha',
            'replenishment_state': 'pending',
            'payload': {'replenishment': {
                'required': True,
                'plan_complete': True,
                'source_warehouse_id': source.id,
                'observed_at': '2026-09-17T10:00:00Z',
                'effective_date': '2026-09-15',
                'requirements': [],
                'uncovered': [],
                'transfers': [transfer],
            }},
        })

    def test_replenishment_values_validate_selected_lot_total_and_external_key(self):
        source, destination, product, lot = self._replenishment_records()
        run = self._replenishment_run(source, destination, product, lot)

        values = run._replenishment_values()

        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]['lots'], [(lot, 2.0)])
        self.assertEqual(values[0]['external_key'], 'period-key:replenishment:transfer-1')

    def test_replenishment_values_reject_partial_selected_lots(self):
        source, destination, product, lot = self._replenishment_records()
        run = self._replenishment_run(
            source, destination, product, lot,
            lots=[{'odoo_lot_id': lot.id, 'quantity': 1.5}],
        )

        with self.assertRaisesRegex(ValidationError, 'whole requested quantity'):
            run._replenishment_values()

    def test_replenishment_values_reject_cross_company_warehouses(self):
        source, _destination, product, lot = self._replenishment_records()
        other_company = self.env['res.company'].create({'name': 'Replenishment Other Company'})
        destination = self.env['stock.warehouse'].create({
            'name': 'Other Company Destination', 'code': 'OCD', 'company_id': other_company.id,
        })
        run = self._replenishment_run(source, destination, product, lot)

        with self.assertRaisesRegex(ValidationError, 'active company'):
            run._replenishment_values()

    def test_replenishment_creates_exact_lot_transfer_and_is_idempotent(self):
        source, destination, product, lot = self._replenishment_records()
        run = self._replenishment_run(source, destination, product, lot)
        self.env['stock.quant']._update_available_quantity(
            product, source.lot_stock_id, 2.0, lot_id=lot,
        )

        first = run._process_replenishment()
        second = run._process_replenishment()

        self.assertEqual(first, second)
        self.assertTrue(first['accepted'])
        self.assertEqual(len(first['picking_ids']), 1)
        self.assertEqual(first['transfers'], [{'transfer_key': 'transfer-1', 'picking_id': first['picking_ids'][0]}])
        picking = self.env['stock.picking'].browse(first['picking_ids'])
        self.assertEqual(picking.state, 'done')
        self.assertEqual(picking.picking_type_id, source.int_type_id)
        self.assertEqual(picking.location_id, source.lot_stock_id)
        self.assertEqual(picking.location_dest_id, destination.lot_stock_id)
        self.assertEqual(picking.softlife_replenishment_key, 'period-key:replenishment:transfer-1')
        self.assertEqual(picking.softlife_payload_sha256, run.payload_sha256)
        self.assertEqual(picking.date_done.date().isoformat(), '2026-09-15')
        self.assertEqual(picking.move_ids.move_line_ids.lot_id, lot)
        self.assertEqual(picking.move_ids.move_line_ids.quantity, 2.0)
        self.assertEqual(self.env['stock.picking'].search_count([
            ('softlife_replenishment_key', '=', picking.softlife_replenishment_key),
        ]), 1)
        self.assertEqual(self.env['stock.quant']._get_available_quantity(
            product, source.lot_stock_id, lot_id=lot, strict=True,
        ), 0.0)
        self.assertEqual(self.env['stock.quant']._get_available_quantity(
            product, destination.lot_stock_id, lot_id=lot, strict=True,
        ), 2.0)

    def test_replenishment_rejects_short_selected_lot_without_negative_stock(self):
        source, destination, product, lot = self._replenishment_records()
        run = self._replenishment_run(source, destination, product, lot)
        self.env['stock.quant']._update_available_quantity(
            product, source.lot_stock_id, 1.0, lot_id=lot,
        )

        with self.assertRaisesRegex(ValidationError, 'has 1.0 available; 2.0 is required'):
            run._process_replenishment()

        self.assertFalse(self.env['stock.picking'].search([
            ('softlife_replenishment_key', '=', 'period-key:replenishment:transfer-1'),
        ]))
        self.assertEqual(self.env['stock.quant']._get_available_quantity(
            product, source.lot_stock_id, lot_id=lot, strict=True,
        ), 1.0)

    def test_replenishment_failure_rolls_back_all_transfer_records(self):
        source, destination, product, lot = self._replenishment_records()
        run = self._replenishment_run(source, destination, product, lot)
        partner_name = 'Must roll back with replenishment transfers'

        def fail_after_write(record):
            record.env['res.partner'].create({'name': partner_name})
            raise ValidationError('second transfer failed')

        with patch.object(type(run), '_process_replenishment', fail_after_write):
            run.action_process_replenishment()

        self.assertFalse(self.env['res.partner'].search([('name', '=', partner_name)]))
        self.assertEqual(run.replenishment_state, 'result_pending')
        self.assertFalse(run.replenishment_result['accepted'])
        self.assertEqual(run.replenishment_result['picking_ids'], [])
        self.assertEqual(run.replenishment_result['error']['type'], 'ValidationError')

    def test_replenishment_callback_uses_dedicated_route_and_refreshes_snapshot(self):
        source, destination, product, lot = self._replenishment_records()
        run = self._replenishment_run(source, destination, product, lot)
        result = {
            'payload_sha256': run.payload_sha256,
            'accepted': True,
            'picking_ids': [91],
            'transfers': [{'transfer_key': 'transfer-1', 'picking_id': 91}],
            'error': None,
        }
        run.write({'replenishment_state': 'result_pending', 'replenishment_result': result})
        Client = type(self.env['softlife.sync.client'])

        with patch.object(Client, '_api_request', return_value={
            'export_id': run.export_id,
            'status': 'draft',
            'payload_sha256': run.payload_sha256,
        }) as request, patch.object(Client, 'sync_odoo_lot_stock', return_value=1) as snapshot:
            run.action_retry_replenishment_callback()

        request.assert_called_once_with(
            'POST',
            f'/api/internal/odoo/manufacturing-periods/{run.export_id}/replenishment-result',
            payload=result,
        )
        snapshot.assert_called_once_with()
        self.assertEqual(run.replenishment_state, 'completed')
        self.assertEqual(run.platform_status, 'draft')
        self.assertEqual(run.idempotency_key, 'period-key')
        self.assertTrue(run.payload['replenishment']['plan_complete'])

    def test_manufacturing_processing_rejects_replenishment_status_before_catalog_sync(self):
        run = self.env['softlife.manufacturing.run'].create({
            'export_id': 'export-not-ready-for-manufacturing',
            'platform_status': 'replenishment_ready',
            'payload': {},
        })

        with patch.object(type(run), 'action_sync_catalog') as catalog:
            with self.assertRaisesRegex(UserError, 'Only ready runs'):
                run.action_process()

        catalog.assert_not_called()
