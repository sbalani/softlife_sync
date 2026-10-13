from types import SimpleNamespace
from unittest.mock import ANY, patch

from odoo.tests.common import TransactionCase

from ..models.softlife_sync_client import SoftlifeAPIError


class TestStockMovements(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = cls.env['softlife.sync.client']

    def _records(self, tracking='lot'):
        source = self.env['stock.warehouse'].search([
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        if not source:
            source = self.env['stock.warehouse'].create({
                'name': 'Movement Source', 'code': 'MVS', 'company_id': self.env.company.id,
            })
        destination = self.env['stock.warehouse'].create({
            'name': 'Movement Destination', 'code': 'MVD',
            'company_id': self.env.company.id,
        })
        product_values = {
            'name': 'Platform movement product', 'type': 'consu', 'tracking': tracking,
        }
        if 'is_storable' in self.env['product.template']._fields:
            product_values['is_storable'] = True
        product = self.env['product.template'].create(product_values).product_variant_id
        lot = self.env['stock.lot'].create({
            'name': 'PLATFORM-MOVEMENT-LOT', 'product_id': product.id,
            'company_id': self.env.company.id,
        })
        return source, destination, product, lot

    @staticmethod
    def _rows(source, destination, lot, group='movement-group', lease='lease-token'):
        common = {
            'movement_group_id': group,
            'lease_token': lease,
            'odoo_lot_id': lot.id,
            'occurred_at': '2026-10-13T10:00:00Z',
            'reason': 'Restock subwarehouse',
        }
        return [{
            **common,
            'movement_id': 'out-movement',
            'external_reference': 'softlife-transfer:movement-group:out',
            'movement_kind': 'transfer_out',
            'odoo_warehouse_id': source.id,
            'quantity': -2.0,
        }, {
            **common,
            'movement_id': 'in-movement',
            'external_reference': 'softlife-transfer:movement-group:in',
            'movement_kind': 'transfer_in',
            'odoo_warehouse_id': destination.id,
            'quantity': 2.0,
        }]

    def test_groups_transfer_rows_by_group_and_requires_shared_lease(self):
        source, destination, _product, lot = self._records()
        rows = self._rows(source, destination, lot)

        groups = self.client._group_claimed_stock_movements(list(reversed(rows)))

        self.assertEqual(len(groups), 1)
        self.assertEqual({row['movement_id'] for row in groups[0]}, {
            'out-movement', 'in-movement',
        })
        rows[1]['lease_token'] = 'another-lease'
        with self.assertRaisesRegex(SoftlifeAPIError, 'inconsistent leases'):
            self.client._group_claimed_stock_movements(rows)

    def test_creates_one_exact_lot_picking_and_reuses_external_references(self):
        source, destination, product, lot = self._records()
        rows = self._rows(source, destination, lot)
        self.env['stock.quant']._update_available_quantity(
            product, source.lot_stock_id, 2.0, lot_id=lot,
        )

        first = self.client._process_stock_movement_group(rows)
        second = self.client._process_stock_movement_group(rows)

        self.assertEqual(first, second)
        self.assertEqual(first.state, 'done')
        self.assertEqual(first.location_id, source.lot_stock_id)
        self.assertEqual(first.location_dest_id, destination.lot_stock_id)
        self.assertEqual(first.date_done.isoformat(), '2026-10-13T10:00:00')
        self.assertEqual(first.move_ids.date.isoformat(), '2026-10-13T10:00:00')
        if 'date' in first.move_ids.move_line_ids._fields:
            self.assertEqual(
                first.move_ids.move_line_ids.date.isoformat(), '2026-10-13T10:00:00',
            )
        self.assertEqual(first.softlife_stock_movement_group_id, 'movement-group')
        self.assertEqual(
            first.softlife_stock_movement_out_reference,
            'softlife-transfer:movement-group:out',
        )
        self.assertEqual(
            first.softlife_stock_movement_in_reference,
            'softlife-transfer:movement-group:in',
        )
        self.assertEqual(first.move_ids.move_line_ids.lot_id, lot)
        self.assertEqual(first.move_ids.move_line_ids.quantity, 2.0)
        self.assertEqual(self.env['stock.picking'].search_count([
            ('softlife_stock_movement_group_id', '=', 'movement-group'),
        ]), 1)

    def test_rejects_mismatched_pair_without_creating_picking(self):
        source, destination, _product, lot = self._records()
        rows = self._rows(source, destination, lot)
        rows[1]['quantity'] = 1.0

        with self.assertRaisesRegex(SoftlifeAPIError, 'equal negative outbound'):
            self.client._process_stock_movement_group(rows)

        self.assertFalse(self.env['stock.picking'].search([
            ('softlife_stock_movement_group_id', '=', 'movement-group'),
        ]))

    def test_rejects_serial_transfer_quantity_other_than_one(self):
        source, destination, _product, lot = self._records(tracking='serial')
        rows = self._rows(source, destination, lot)

        with self.assertRaisesRegex(SoftlifeAPIError, 'quantity exactly 1'):
            self.client._process_stock_movement_group(rows)

        self.assertFalse(self.env['stock.picking'].search([
            ('softlife_stock_movement_group_id', '=', 'movement-group'),
        ]))

    def test_claims_and_reports_one_group_with_shared_lease(self):
        source, destination, _product, lot = self._records()
        rows = self._rows(source, destination, lot)
        Client = type(self.client)
        responses = [{'movements': rows}, {'ok': True}, {'movements': []}]
        with patch.object(Client, '_stock_movement_worker', return_value='worker-1'), \
                patch.object(Client, '_process_stock_movement_group',
                             return_value=SimpleNamespace(id=91)) as process, \
                patch.object(Client, '_api_request', side_effect=responses) as request, \
                patch.object(type(self.client.env.cr), 'commit') as commit:
            count = self.client.process_platform_stock_movements(limit=2)

        self.assertEqual(count, 1)
        process.assert_called_once()
        commit.assert_called_once_with()
        self.assertEqual(request.call_args_list[0].args[:2], (
            'POST', '/api/internal/odoo/stock-movements/claim',
        ))
        self.assertEqual(request.call_args_list[0].kwargs['payload'], {
            'worker': 'worker-1', 'limit': 1,
        })
        self.assertEqual(request.call_args_list[1].args[:2], (
            'POST', '/api/internal/odoo/stock-movements/result',
        ))
        self.assertEqual(request.call_args_list[1].kwargs['payload'], {
            'movement_id': 'out-movement',
            'worker': 'worker-1',
            'lease_token': 'lease-token',
            'accepted': True,
            'odoo_external_id': '91',
        })
        self.assertEqual(request.call_args_list[2].args[:2], (
            'POST', '/api/internal/odoo/stock-movements/claim',
        ))
        self.assertEqual(request.call_args_list[2].kwargs['payload'], {
            'worker': 'worker-1', 'limit': 1,
        })

    def test_unsupported_kind_is_reported_terminal_not_accepted(self):
        row = {
            'movement_id': 'receipt-movement', 'movement_group_id': None,
            'lease_token': 'lease-token', 'external_reference': 'receipt-reference',
            'movement_kind': 'receipt', 'odoo_warehouse_id': 1,
            'odoo_lot_id': 2, 'quantity': 1.0,
        }
        Client = type(self.client)
        with patch.object(Client, '_stock_movement_worker', return_value='worker-1'), \
                patch.object(Client, '_api_request', side_effect=[
                    {'movements': [row]}, {'ok': True}, {'movements': []},
                ]) as request:
            self.client.process_platform_stock_movements(limit=2)

        payload = request.call_args_list[1].kwargs['payload']
        self.assertFalse(payload['accepted'])
        self.assertIn('Unsupported platform stock movement kind', payload['error'])
        self.assertEqual(payload['movement_id'], 'receipt-movement')
        self.assertEqual(payload['worker'], 'worker-1')
        self.assertEqual(payload['lease_token'], 'lease-token')
        self.assertNotIn('retry_at', payload)

    def test_programming_error_is_terminal_but_concurrency_error_retries(self):
        class SerializationFailure(Exception):
            pgcode = '40001'

        self.assertFalse(self.client._stock_movement_error_is_retryable(
            AttributeError('missing Odoo field'),
        ))
        self.assertFalse(self.client._stock_movement_error_is_retryable(
            SoftlifeAPIError('invalid configuration', code='invalid_configuration'),
        ))
        self.assertTrue(self.client._stock_movement_error_is_retryable(
            SerializationFailure('serialization conflict'),
        ))
        self.assertTrue(self.client._stock_movement_error_is_retryable(
            SoftlifeAPIError('stock lock', code='stock_lock', retryable=True),
        ))

    def test_programming_failure_result_omits_retry_at(self):
        source, destination, _product, lot = self._records()
        rows = self._rows(source, destination, lot)
        Client = type(self.client)
        with patch.object(Client, '_stock_movement_worker', return_value='worker-1'), \
                patch.object(Client, '_process_stock_movement_group',
                             side_effect=AttributeError('missing Odoo field')), \
                patch.object(Client, '_api_request', side_effect=[
                    {'movements': rows}, {'ok': True}, {'movements': []},
                ]) as request:
            self.client.process_platform_stock_movements(limit=2)

        payload = request.call_args_list[1].kwargs['payload']
        self.assertFalse(payload['accepted'])
        self.assertEqual(payload['error'], 'missing Odoo field')
        self.assertNotIn('retry_at', payload)

    def test_concurrency_failure_result_includes_retry_at(self):
        class SerializationFailure(Exception):
            pgcode = '40001'

        source, destination, _product, lot = self._records()
        rows = self._rows(source, destination, lot)
        Client = type(self.client)
        with patch.object(Client, '_stock_movement_worker', return_value='worker-1'), \
                patch.object(Client, '_process_stock_movement_group',
                             side_effect=SerializationFailure('serialization conflict')), \
                patch.object(Client, '_api_request', side_effect=[
                    {'movements': rows}, {'ok': True}, {'movements': []},
                ]) as request:
            self.client.process_platform_stock_movements(limit=2)

        payload = request.call_args_list[1].kwargs['payload']
        self.assertFalse(payload['accepted'])
        self.assertEqual(payload['error'], 'serialization conflict')
        self.assertIsInstance(payload['retry_at'], str)

    def test_snapshot_reflects_only_completed_movement_references(self):
        Quant = type(self.env['stock.quant'])
        Picking = type(self.env['stock.picking'])
        Client = type(self.client)
        picking = SimpleNamespace(
            softlife_stock_movement_out_reference='softlife-transfer:g:out',
            softlife_stock_movement_in_reference='softlife-transfer:g:in',
        )
        with patch.object(Quant, 'search', return_value=[]), \
                patch.object(Picking, 'search', return_value=[picking]) as picking_search, \
                patch.object(Client, '_api_request', return_value={'ok': True}) as request:
            self.client.sync_odoo_lot_stock()

        picking_search.assert_called_once_with([
            ('state', '=', 'done'),
            ('softlife_stock_movement_group_id', '!=', False),
        ])
        request.assert_called_once_with(
            'POST', '/api/internal/odoo/lot-stock-snapshot',
            payload={
                'rows': [], 'product_rows': [],
                'reflected_references': [
                    'softlife-transfer:g:in', 'softlife-transfer:g:out',
                ],
                'observed_at': ANY,
            },
        )

    def test_snapshot_captures_references_before_reading_quantities(self):
        calls = []
        Quant = type(self.env['stock.quant'])
        Picking = type(self.env['stock.picking'])
        Client = type(self.client)

        def pickings(_domain):
            calls.append('references')
            return []

        def quants(_domain):
            calls.append('quantities')
            return []

        with patch.object(Picking, 'search', side_effect=pickings), \
                patch.object(Quant, 'search', side_effect=quants), \
                patch.object(Client, '_api_request', return_value={'ok': True}):
            self.client.sync_odoo_lot_stock()

        self.assertEqual(calls, ['references', 'quantities'])
