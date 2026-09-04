from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from ..models.softlife_sync_client import SoftlifeAPIError


class TestLotStockSnapshot(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = cls.env['softlife.sync.client']

    @staticmethod
    def _quant(lot_id, warehouse_id, quantity):
        warehouse = SimpleNamespace(id=warehouse_id) if warehouse_id else False
        return SimpleNamespace(
            lot_id=SimpleNamespace(id=lot_id),
            location_id=SimpleNamespace(warehouse_id=warehouse),
            quantity=quantity,
        )

    def test_groups_positive_internal_quantities_and_posts_complete_snapshot(self):
        quants = [
            self._quant(12, 7, 2.5),
            self._quant(12, 7, 1.5),
            self._quant(11, 4, 3.0),
            self._quant(13, 7, 0.0),
            self._quant(14, 7, -2.0),
            self._quant(15, 7, float('inf')),
            self._quant(16, 7, float('nan')),
            self._quant(17, None, 9.0),
        ]
        Quant = type(self.env['stock.quant'])
        Client = type(self.client)
        with patch.object(Quant, 'search', return_value=quants) as search, \
                patch.object(Client, '_api_request', return_value={'ok': True}) as request:
            count = self.client.sync_odoo_lot_stock()

        self.assertEqual(count, 2)
        search.assert_called_once_with([
            ('lot_id', '!=', False),
            ('location_id.usage', '=', 'internal'),
        ])
        request.assert_called_once_with(
            'POST',
            '/api/internal/odoo/lot-stock-snapshot',
            payload={
                'rows': [
                    {'odoo_lot_id': 11, 'odoo_warehouse_id': 4, 'qty': 3.0},
                    {'odoo_lot_id': 12, 'odoo_warehouse_id': 7, 'qty': 4.0},
                ],
                'reflected_references': [],
            },
        )

    def test_posts_intentionally_empty_complete_snapshot(self):
        Quant = type(self.env['stock.quant'])
        Client = type(self.client)
        with patch.object(Quant, 'search', return_value=[]), \
                patch.object(Client, '_api_request', return_value={'ok': True}) as request:
            count = self.client.sync_odoo_lot_stock()

        self.assertEqual(count, 0)
        request.assert_called_once_with(
            'POST',
            '/api/internal/odoo/lot-stock-snapshot',
            payload={'rows': [], 'reflected_references': []},
        )

    def _summary_with_lot_stock_error(self, error):
        Client = type(self.client)
        methods = (
            'sync_partners', 'sync_products', 'sync_machines',
            'sync_odoo_warehouses', 'sync_odoo_products', 'sync_odoo_lots',
        )
        patches = [patch.object(Client, name, return_value=0) for name in methods]
        with patch.object(Client, '_is_configured', return_value=True), \
                patch.object(Client, 'sync_odoo_lot_stock', side_effect=error):
            for mocked in patches:
                mocked.start()
            try:
                return self.client.sync_all()
            finally:
                for mocked in reversed(patches):
                    mocked.stop()

    def test_missing_platform_configuration_is_reported_in_sync_summary(self):
        summary = self._summary_with_lot_stock_error(SoftlifeAPIError(
            'Platform App URL / Odoo sync secret not configured.',
            code='not_configured',
        ))

        self.assertIn(
            'odoo_lot_stock: Platform App URL / Odoo sync secret not configured.',
            summary,
        )

    def test_http_failure_is_reported_in_sync_summary(self):
        summary = self._summary_with_lot_stock_error(SoftlifeAPIError(
            'Platform POST lot-stock-snapshot failed (500): unavailable',
            status=500,
            retryable=True,
        ))

        self.assertIn(
            'odoo_lot_stock: Platform POST lot-stock-snapshot failed (500): unavailable',
            summary,
        )
