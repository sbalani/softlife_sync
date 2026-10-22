import hashlib
import json
import uuid
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from ..models.softlife_sync_client import SoftlifeAPIError
from ..models.manufacturing_documents import _softlife_manufacturing_internal_scope


class TestFiscalInvoices(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = cls.env['softlife.sync.client']

    @staticmethod
    def _hash(payload):
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        ).encode()).hexdigest()

    def _records_and_payload(self, invoice_count=1, price_include=True, zero_value=False, quantity=1):
        company = self.env.company
        country = company.account_fiscal_country_id
        if not country or country.code != 'ES' or company.currency_id.name != 'EUR':
            self.skipTest('Test company is not a Spanish EUR accounting company.')
        account = self.env['account.account'].with_company(company).search([
            ('account_type', 'in', ('income', 'income_other')),
        ], limit=1)
        if not account:
            self.skipTest('Test company has no income account.')
        customer = self.env['res.partner'].create({
            'name': 'SoftLife fiscal final consumer', 'country_id': country.id,
        })
        journal = self.env['account.journal'].with_company(company).create({
            'name': 'SoftLife fiscal invoice tests', 'code': 'SLFI',
            'type': 'sale', 'company_id': company.id, 'restrict_mode_hash_table': True,
        })
        tax = self.env['account.tax'].with_company(company).create({
            'name': 'SoftLife fiscal invoice test 10%', 'amount': 10,
            'amount_type': 'percent', 'type_tax_use': 'sale',
            'company_id': company.id, 'country_id': country.id,
            'price_include': price_include,
        })
        category = self.env['product.category'].with_company(company).create({
            'name': 'SoftLife fiscal invoice tests',
            'property_account_income_categ_id': account.id,
        })
        product = self.env['product.product'].with_company(company).create({
            'name': 'SoftLife fiscal invoice product', 'sale_ok': True, 'type': 'service',
            'categ_id': category.id, 'softlife_recipe_id': 'fiscal-invoice-test',
            'taxes_id': [(6, 0, tax.ids)],
        })
        self.env['ir.config_parameter'].sudo().set_param(
            'softlife.sync.fiscal_company_id', str(company.id),
        )
        contract = {
            'contract_version': 1,
            'required_capabilities': {'fiscal_zero_value_invoices': 2},
            'expected': {
                'journal_code': journal.code, 'customer_odoo_id': customer.id,
                'vat_rate': 10, 'currency': 'EUR', 'income_account_code': account.code,
                'tax_treatment_approved': True,
            },
        }
        invoices = []
        gross_cents = 0 if zero_value else 1100
        tax_base_cents = 0 if zero_value else 1000
        vat_cents = 0 if zero_value else 100
        for index in range(invoice_count):
            invoice = {
                'platform_invoice_id': str(uuid.uuid4()),
                'move_type': 'out_invoice', 'invoice_date': '2026-09-29',
                'currency': 'EUR', 'reference': f'SoftLife fiscal {index + 1}',
                'expected_total_cents': gross_cents,
                'zero_value_reason': 'free' if zero_value else None,
                'lines': [{
                    'odoo_product_id': product.id, 'description': f'Fiscal sale {index + 1}',
                    'quantity': quantity, 'gross_cents': gross_cents, 'tax_base_cents': tax_base_cents,
                    'vat_cents': vat_cents, 'odoo_tax_id': tax.id,
                }],
            }
            invoice['invoice_payload_sha256'] = self._hash(invoice)
            invoices.append(invoice)
        payload = {
            'contract_version': 1,
            'configuration_report_id': 'fiscal-configuration-report',
            'configuration_payload_sha256': 'a' * 64,
            'company': {'odoo_id': company.id, 'country_code': 'ES', 'currency': 'EUR'},
            'journal': {'code': journal.code}, 'customer': {'odoo_id': customer.id},
            'tax': {
                'odoo_tax_id': tax.id, 'rate': 10, 'country_code': 'ES',
                'type_tax_use': 'sale', 'amount_type': 'percent',
                'price_include': price_include,
            },
            'invoices': invoices,
        }
        return company, journal, customer, tax, product, contract, payload

    def _v2_source_sale(self, customer, product, source_id):
        warehouse = self.env['stock.warehouse'].search([
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        if not warehouse:
            self.skipTest('Test company has no warehouse.')
        line_key = f'{source_id}:1'
        with _softlife_manufacturing_internal_scope():
            sale = self.env['sale.order'].create({
                'partner_id': customer.id, 'warehouse_id': warehouse.id,
                'softlife_warehouse_id': warehouse.id,
                'softlife_sale_key': f'transaction:{source_id}',
                'softlife_source_order_id': source_id,
                'softlife_source_payload_sha256': 'b' * 64,
                'softlife_manufacturing_contract_version': 3,
                'softlife_document_date': '2026-09-29',
                'softlife_source_orders': [{'platform_order_id': source_id}],
                'order_line': [(0, 0, {
                    'product_id': product.id, 'name': 'Fiscal sale 1',
                    'product_uom_qty': 1,
                    'product_uom': product.uom_id.id, 'price_unit': 11,
                    'tax_id': [(6, 0, [])],
                    'softlife_platform_line_key': line_key,
                })],
            })
        with _softlife_manufacturing_internal_scope():
            sale.action_confirm()
        sale.order_line.qty_delivered = 1
        return sale

    def _v2_payload(self, company, customer, payload, sources, mode):
        invoice = payload['invoices'][0]
        invoice['invoice_mode'] = mode
        invoice['local_month'] = '2026-09'
        request_id = None
        if mode == 'monthly_consolidated':
            invoice['customer'] = {'odoo_id': customer.id}
        else:
            state = self.env['res.country.state'].search([
                ('country_id.code', '=', 'ES'),
            ], limit=1)
            if not state:
                self.skipTest('Spanish fiscal test requires one configured province.')
            request_id = str(uuid.uuid4())
            details = {
                'legal_name': 'Requested Customer', 'tax_id': 'ES12345678Z',
                'email': 'customer@example.com', 'address': 'Calle Test 1',
                'postal_code': '28001', 'city': 'Madrid', 'province': state.name,
                'country_code': 'ES',
            }
            invoice['customer'] = {
                **details, 'request_id': request_id,
                'fiscal_details_sha256': self._hash(details),
            }
        base_line = invoice['lines'][0]
        allocations = []
        lines = []
        for index, source_id in enumerate(sources, start=1):
            sale = self.env['sale.order'].search([
                ('softlife_source_order_id', '=', source_id),
            ], limit=1)
            allocation_id = str(uuid.uuid4())
            allocation = {
                'platform_allocation_id': allocation_id,
                'preflight_item_id': str(uuid.uuid4()), 'source_order_id': source_id,
                'source_sha256': 'c' * 64,
                'supporting_sale_order_source_sha256': 'b' * 64,
                'invoice_request_id': request_id,
                'supporting_sale_order_id': str(uuid.uuid4()),
                'odoo_sale_order_id': sale.id,
                'odoo_sale_order_line_id': sale.order_line.id,
                'line_sequence': index,
            }
            allocations.append(allocation)
            lines.append({
                **base_line, 'platform_allocation_id': allocation_id,
                'zero_value_reason': invoice.get('zero_value_reason'),
                'odoo_sale_order_id': sale.id,
                'odoo_sale_order_line_id': sale.order_line.id,
            })
        invoice['allocations'] = allocations
        invoice['lines'] = lines
        invoice['expected_total_cents'] = 1100 * len(sources)
        invoice['invoice_payload_sha256'] = self._hash({
            key: value for key, value in invoice.items() if key != 'invoice_payload_sha256'
        })
        payload['contract_version'] = 2
        payload['local_month'] = '2026-09'
        payload.pop('customer')
        return payload

    def test_independent_opt_ins_fail_before_platform_calls(self):
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=False), \
                patch.object(Client, '_fiscal_invoice_confirmation_enabled', return_value=False), \
                patch.object(Client, '_api_request') as api:
            with self.assertRaisesRegex(SoftlifeAPIError, 'draft creation is disabled'):
                self.client._create_fiscal_invoice_drafts({})
            with self.assertRaisesRegex(SoftlifeAPIError, 'confirmation is disabled'):
                self.client._confirm_fiscal_invoices({})
        api.assert_not_called()

    def test_draft_schema_and_per_invoice_hash_are_exact(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        payload['unexpected'] = True
        with self.assertRaisesRegex(SoftlifeAPIError, 'payload fields are invalid'):
            self.client._validate_fiscal_invoice_draft_payload(payload, contract)
        payload.pop('unexpected')
        payload['invoices'][0]['reference'] = 'Changed after hashing'
        with self.assertRaisesRegex(SoftlifeAPIError, 'identity or header is invalid'):
            self.client._validate_fiscal_invoice_draft_payload(payload, contract)

    def test_invalid_later_invoice_creates_nothing(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload(invoice_count=2)
        payload['invoices'][1]['lines'][0]['gross_cents'] += 1
        payload['invoices'][1]['invoice_payload_sha256'] = self._hash({
            key: value for key, value in payload['invoices'][1].items()
            if key != 'invoice_payload_sha256'
        })
        before = self.env['account.move'].search_count([
            ('softlife_fiscal_invoice_id', '!=', False),
        ])
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            with self.assertRaisesRegex(SoftlifeAPIError, 'line values are invalid'):
                self.client._create_fiscal_invoice_drafts(payload)
        self.assertEqual(self.env['account.move'].search_count([
            ('softlife_fiscal_invoice_id', '!=', False),
        ]), before)

    def test_zero_value_reason_is_required_and_exact(self):
        Client = type(self.client)
        cases = [
            (True, None),
            (True, 'unsupported'),
            (False, 'free'),
        ]
        for zero_value, reason in cases:
            company, journal, customer, tax, product, contract, payload = \
                self._records_and_payload(zero_value=zero_value)
            invoice = payload['invoices'][0]
            if reason is None:
                invoice.pop('zero_value_reason')
            else:
                invoice['zero_value_reason'] = reason
            invoice['invoice_payload_sha256'] = self._hash({
                key: value for key, value in invoice.items()
                if key != 'invoice_payload_sha256'
            })
            with self.assertRaisesRegex(SoftlifeAPIError, 'zero-value reason is invalid'):
                self.client._validate_fiscal_invoice_draft_payload(payload, contract)

    def test_coupon_zero_value_reason_is_accepted(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload(zero_value=True)
        invoice = payload['invoices'][0]
        invoice['zero_value_reason'] = 'coupon'
        invoice['invoice_payload_sha256'] = self._hash({
            key: value for key, value in invoice.items()
            if key != 'invoice_payload_sha256'
        })
        self.client._validate_fiscal_invoice_draft_payload(payload, contract)

    def test_creates_exact_draft_and_idempotently_reuses_it(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            first = self.client._create_fiscal_invoice_drafts(payload)
            second = self.client._create_fiscal_invoice_drafts(payload)
        move = self.env['account.move'].browse(first['invoices'][0]['odoo_move_id'])
        self.assertEqual(move.state, 'draft')
        self.assertEqual(move.company_id, company)
        self.assertEqual(move.journal_id, journal)
        self.assertEqual(move.partner_id, customer)
        self.assertEqual(move.invoice_line_ids.tax_ids, tax)
        self.assertEqual(self.client._amount_cents(move.amount_total), 1100)
        expected_snapshot = dict(payload['invoices'][0])
        expected_snapshot['lines'] = [dict(payload['invoices'][0]['lines'][0])]
        expected_snapshot['lines'][0]['account_id'] = move.invoice_line_ids.account_id.id
        self.assertEqual(move.softlife_fiscal_invoice_snapshot, expected_snapshot)
        self.assertEqual(
            self.client._fiscal_invoice_snapshot_sha256(move.softlife_fiscal_invoice_snapshot),
            move.softlife_fiscal_payload_sha256,
        )
        self.assertTrue(first['invoices'][0]['created'])
        self.assertFalse(second['invoices'][0]['created'])
        self.assertEqual(first['invoices'][0]['odoo_move_id'], second['invoices'][0]['odoo_move_id'])

    def test_positive_legacy_payload_without_zero_value_reason_is_accepted(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        invoice = payload['invoices'][0]
        invoice.pop('zero_value_reason')
        invoice['invoice_payload_sha256'] = self._hash({
            key: value for key, value in invoice.items()
            if key != 'invoice_payload_sha256'
        })
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            result = self.client._create_fiscal_invoice_drafts(payload)
        self.assertEqual(result['invoices'][0]['state'], 'draft')

    def test_creates_and_posts_zero_value_invoice(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload(zero_value=True, quantity=2)
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            draft = self.client._create_fiscal_invoice_drafts(payload)['invoices'][0]
        move = self.env['account.move'].browse(draft['odoo_move_id'])
        self.assertEqual(move.invoice_line_ids.quantity, 2)
        self.assertEqual(move.invoice_line_ids.price_unit, 0)
        self.assertEqual(self.client._amount_cents(move.amount_total), 0)
        confirmation = {
            'contract_version': 1, 'company': {'odoo_id': company.id},
            'invoices': [{
                'platform_invoice_id': draft['platform_invoice_id'],
                'odoo_move_id': draft['odoo_move_id'],
                'invoice_payload_sha256': draft['invoice_payload_sha256'],
            }],
        }
        with patch.object(Client, '_fiscal_invoice_confirmation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            result = self.client._confirm_fiscal_invoices(confirmation)
        self.assertTrue(result['accepted'])
        self.assertEqual(move.state, 'posted')

    def test_existing_identity_with_changed_hash_is_rejected(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            self.client._create_fiscal_invoice_drafts(payload)
            payload['invoices'][0]['reference'] = 'Different frozen invoice'
            frozen = dict(payload['invoices'][0])
            frozen.pop('invoice_payload_sha256')
            payload['invoices'][0]['invoice_payload_sha256'] = self._hash(frozen)
            with self.assertRaisesRegex(SoftlifeAPIError, 'snapshot does not match provenance'):
                self.client._create_fiscal_invoice_drafts(payload)

    def test_tax_excluded_draft_uses_tax_base_for_unit_price(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload(price_include=False)
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            result = self.client._create_fiscal_invoice_drafts(payload)
        move = self.env['account.move'].browse(result['invoices'][0]['odoo_move_id'])
        self.assertEqual(move.invoice_line_ids.price_unit, 10)
        self.assertEqual(self.client._amount_cents(move.amount_total), 1100)

    def test_confirmation_posts_only_listed_and_posted_retry_succeeds(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload(invoice_count=2)
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            drafts = self.client._create_fiscal_invoice_drafts(payload)
        confirmation = {
            'contract_version': 1, 'company': {'odoo_id': company.id},
            'invoices': [{
                'platform_invoice_id': drafts['invoices'][0]['platform_invoice_id'],
                'odoo_move_id': drafts['invoices'][0]['odoo_move_id'],
                'invoice_payload_sha256': drafts['invoices'][0]['invoice_payload_sha256'],
            }],
        }
        with patch.object(Client, '_fiscal_invoice_confirmation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            first = self.client._confirm_fiscal_invoices(confirmation)
            second = self.client._confirm_fiscal_invoices(confirmation)
        self.assertEqual(first['invoices'][0]['state'], 'posted')
        self.assertTrue(first['invoices'][0]['confirmed'])
        self.assertTrue(first['invoices'][0]['name'])
        self.assertFalse(second['invoices'][0]['confirmed'])
        self.assertEqual(self.env['account.move'].browse(
            drafts['invoices'][1]['odoo_move_id'],
        ).state, 'draft')

    def test_invalid_confirmation_batch_posts_nothing(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload(invoice_count=2)
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            drafts = self.client._create_fiscal_invoice_drafts(payload)
        confirmation = {
            'contract_version': 1, 'company': {'odoo_id': company.id},
            'invoices': [{
                'platform_invoice_id': row['platform_invoice_id'],
                'odoo_move_id': row['odoo_move_id'],
                'invoice_payload_sha256': row['invoice_payload_sha256'],
            } for row in drafts['invoices']],
        }
        confirmation['invoices'][1]['invoice_payload_sha256'] = 'f' * 64
        with patch.object(Client, '_fiscal_invoice_confirmation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            with self.assertRaisesRegex(SoftlifeAPIError, 'does not match Odoo'):
                self.client._confirm_fiscal_invoices(confirmation)
        self.assertEqual(set(self.env['account.move'].browse([
            row['odoo_move_id'] for row in drafts['invoices']
        ]).mapped('state')), {'draft'})

    def test_draft_lines_and_accounting_date_are_frozen(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            draft = self.client._create_fiscal_invoice_drafts(payload)['invoices'][0]
        move = self.env['account.move'].browse(draft['odoo_move_id'])
        with self.assertRaisesRegex(UserError, 'lines are frozen'):
            move.invoice_line_ids.write({'name': 'User-edited line'})
        with self.assertRaisesRegex(UserError, 'headers are frozen'):
            move.write({'date': '2026-09-30'})
        self.assertEqual(move.state, 'draft')

    def test_direct_post_and_provenance_changes_are_blocked(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            draft = self.client._create_fiscal_invoice_drafts(payload)['invoices'][0]
        move = self.env['account.move'].browse(draft['odoo_move_id'])
        with self.assertRaisesRegex(UserError, 'validated platform confirmation'):
            move.action_post()
        with self.assertRaisesRegex(UserError, 'provenance cannot be changed'):
            move.write({'softlife_fiscal_payload_sha256': 'f' * 64})
        with self.assertRaisesRegex(UserError, 'headers are frozen'):
            move.write({'ref': 'Manual edit'})
        self.assertEqual(move.state, 'draft')

    def test_confirmation_supports_standard_sales_journal(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            draft = self.client._create_fiscal_invoice_drafts(payload)['invoices'][0]
        journal.restrict_mode_hash_table = False
        confirmation = {
            'contract_version': 1, 'company': {'odoo_id': company.id},
            'invoices': [{
                'platform_invoice_id': draft['platform_invoice_id'],
                'odoo_move_id': draft['odoo_move_id'],
                'invoice_payload_sha256': draft['invoice_payload_sha256'],
            }],
        }
        with patch.object(Client, '_fiscal_invoice_confirmation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            result = self.client._confirm_fiscal_invoices(confirmation)
        self.assertTrue(result['accepted'])
        self.assertEqual(self.env['account.move'].browse(draft['odoo_move_id']).state, 'posted')

    def test_posted_invoice_links_to_verified_softlife_sales_order(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            draft = self.client._create_fiscal_invoice_drafts(payload)['invoices'][0]
        confirmation = {
            'contract_version': 1, 'company': {'odoo_id': company.id},
            'invoices': [{
                'platform_invoice_id': draft['platform_invoice_id'],
                'odoo_move_id': draft['odoo_move_id'],
                'invoice_payload_sha256': draft['invoice_payload_sha256'],
            }],
        }
        with patch.object(Client, '_fiscal_invoice_confirmation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            self.client._confirm_fiscal_invoices(confirmation)

        warehouse = self.env['stock.warehouse'].search([
            ('company_id', '=', company.id),
        ], limit=1)
        if not warehouse:
            self.skipTest('Test company has no warehouse.')
        source_order_id = str(uuid.uuid4())
        export_id = str(uuid.uuid4())
        with _softlife_manufacturing_internal_scope():
            sale = self.env['sale.order'].create({
                'partner_id': customer.id,
                'warehouse_id': warehouse.id,
                'date_order': '2026-09-29 00:00:00',
                'softlife_export_id': export_id,
                'softlife_warehouse_id': warehouse.id,
                'softlife_source_orders': [{
                    'platform_order_id': source_order_id,
                    'order_code': 'TEST-ORDER',
                    'machine_id': str(uuid.uuid4()),
                    'machine_imei': 'TEST-IMEI',
                    'machine_name': 'Test machine',
                }],
                'order_line': [(0, 0, {
                    'product_id': product.id,
                    'product_uom_qty': 1,
                    'product_uom': product.uom_id.id,
                    'price_unit': 11,
                    'tax_id': [(6, 0, [])],
                })],
            })
        with _softlife_manufacturing_internal_scope():
            sale.action_confirm()
        link_payload = {
            'contract_version': 1,
            'local_month': '2026-09',
            'links': [{
                'platform_invoice_id': draft['platform_invoice_id'],
                'odoo_move_id': draft['odoo_move_id'],
                'source_order_id': source_order_id,
                'export_id': export_id,
                'recipe_version_id': str(uuid.uuid4()),
                'odoo_warehouse_id': warehouse.id,
                'odoo_sale_order_id': sale.id,
                'odoo_product_id': product.id,
                'quantity': 1,
            }],
        }
        first = self.client._link_fiscal_invoices_to_sales(link_payload)
        second = self.client._link_fiscal_invoices_to_sales(link_payload)
        move = self.env['account.move'].browse(draft['odoo_move_id'])
        self.assertEqual(move.invoice_line_ids.sale_line_ids, sale.order_line)
        self.assertIn(move, sale.invoice_ids)
        self.assertEqual(sale.invoice_status, 'invoiced')
        self.assertTrue(first['links'][0]['linked'])
        self.assertFalse(first['links'][0]['already_linked'])
        self.assertTrue(second['links'][0]['already_linked'])

    def test_sale_link_rejects_wrong_source_without_mutation(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            draft = self.client._create_fiscal_invoice_drafts(payload)['invoices'][0]
        confirmation = {
            'contract_version': 1, 'company': {'odoo_id': company.id},
            'invoices': [{
                'platform_invoice_id': draft['platform_invoice_id'],
                'odoo_move_id': draft['odoo_move_id'],
                'invoice_payload_sha256': draft['invoice_payload_sha256'],
            }],
        }
        with patch.object(Client, '_fiscal_invoice_confirmation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            self.client._confirm_fiscal_invoices(confirmation)
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        if not warehouse:
            self.skipTest('Test company has no warehouse.')
        with _softlife_manufacturing_internal_scope():
            sale = self.env['sale.order'].create({
                'partner_id': customer.id, 'warehouse_id': warehouse.id,
                'softlife_export_id': str(uuid.uuid4()), 'softlife_warehouse_id': warehouse.id,
                'softlife_source_orders': [{'platform_order_id': str(uuid.uuid4())}],
                'order_line': [(0, 0, {'product_id': product.id, 'product_uom_qty': 1, 'price_unit': 11, 'tax_id': [(6, 0, [])]})],
            })
        with _softlife_manufacturing_internal_scope():
            sale.action_confirm()
        link_payload = {
            'contract_version': 1, 'local_month': '2026-09',
            'links': [{
                'platform_invoice_id': draft['platform_invoice_id'],
                'odoo_move_id': draft['odoo_move_id'],
                'source_order_id': str(uuid.uuid4()),
                'export_id': sale.softlife_export_id,
                'recipe_version_id': str(uuid.uuid4()),
                'odoo_warehouse_id': warehouse.id,
                'odoo_sale_order_id': sale.id,
                'odoo_product_id': product.id,
                'quantity': 1,
            }],
        }
        with self.assertRaisesRegex(SoftlifeAPIError, 'does not contain'):
            self.client._link_fiscal_invoices_to_sales(link_payload)
        self.assertFalse(self.env['account.move'].browse(
            draft['odoo_move_id']).invoice_line_ids.sale_line_ids)

    def test_v2_named_invoice_creates_partner_and_exact_source_link_idempotently(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        source_id = str(uuid.uuid4())
        sale = self._v2_source_sale(customer, product, source_id)
        payload = self._v2_payload(
            company, customer, payload, [source_id], 'individual_requested',
        )
        Client = type(self.client)
        FiscalPosition = type(self.env['account.fiscal.position'])
        no_fiscal_position = self.env['account.fiscal.position']

        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract), \
                patch.object(FiscalPosition, '_get_fiscal_position',
                             return_value=no_fiscal_position):
            first = self.client._create_fiscal_invoice_drafts(payload)
            second = self.client._create_fiscal_invoice_drafts(payload)

        move = self.env['account.move'].browse(first['invoices'][0]['odoo_move_id'])
        self.assertEqual(first['contract_version'], 2)
        self.assertTrue(first['invoices'][0]['created'])
        self.assertFalse(second['invoices'][0]['created'])
        request_id = payload['invoices'][0]['customer']['request_id']
        self.assertEqual(move.softlife_fiscal_accounting_key, f'individual:{request_id}')
        self.assertEqual(move.invoice_line_ids.softlife_fiscal_source_order_id, source_id)
        self.assertEqual(
            move.invoice_line_ids.softlife_fiscal_allocation_id,
            payload['invoices'][0]['allocations'][0]['platform_allocation_id'],
        )
        self.assertEqual(move.invoice_line_ids.sale_line_ids, sale.order_line)
        self.assertEqual(move.partner_id.softlife_fiscal_customer_id,
                         request_id)
        confirmation = {
            'contract_version': 2, 'company': {'odoo_id': company.id},
            'invoices': [{
                'platform_invoice_id': first['invoices'][0]['platform_invoice_id'],
                'odoo_move_id': move.id,
                'invoice_payload_sha256': first['invoices'][0]['invoice_payload_sha256'],
            }],
        }
        with patch.object(Client, '_fiscal_invoice_confirmation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract), \
                patch.object(FiscalPosition, '_get_fiscal_position',
                             return_value=no_fiscal_position):
            confirmed = self.client._confirm_fiscal_invoices(confirmation)
            retried = self.client._confirm_fiscal_invoices(confirmation)
        self.assertEqual(confirmed['contract_version'], 2)
        self.assertTrue(confirmed['invoices'][0]['confirmed'])
        self.assertFalse(retried['invoices'][0]['confirmed'])
        self.assertEqual(move.state, 'posted')

    def test_v2_consolidated_invoice_keeps_one_line_per_source_sale(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        source_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        sales = self.env['sale.order']
        for source_id in source_ids:
            sales |= self._v2_source_sale(customer, product, source_id)
        payload = self._v2_payload(
            company, customer, payload, source_ids, 'monthly_consolidated',
        )
        Client = type(self.client)

        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            result = self.client._create_fiscal_invoice_drafts(payload)

        move = self.env['account.move'].browse(result['invoices'][0]['odoo_move_id'])
        self.assertEqual(move.softlife_fiscal_accounting_key,
                         f'consolidated:{company.id}:2026-09')
        self.assertEqual(set(move.invoice_line_ids.mapped('softlife_fiscal_source_order_id')),
                         set(source_ids))
        self.assertEqual(len(move.invoice_line_ids), 2)
        self.assertEqual(set(move.invoice_line_ids.sale_line_ids.ids), set(sales.order_line.ids))

    def test_v2_rejects_duplicate_source_before_creating_accounting(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        source_id = str(uuid.uuid4())
        self._v2_source_sale(customer, product, source_id)
        payload = self._v2_payload(
            company, customer, payload, [source_id, source_id], 'monthly_consolidated',
        )

        with self.assertRaisesRegex(SoftlifeAPIError, 'allocation identity is invalid'):
            self.client._validate_fiscal_invoice_draft_payload_v2(payload, contract)
        self.assertFalse(self.env['account.move'].search([
            ('softlife_fiscal_accounting_key', '=', f'consolidated:{company.id}:2026-09'),
        ]))

    def test_v2_allows_platform_excluded_refund_sale_outside_payload(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        source_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        included_sale = self._v2_source_sale(customer, product, source_ids[0])
        # Refund exclusion is authoritative on the platform; its supporting Odoo sale remains.
        platform_excluded_refund_sale = self._v2_source_sale(
            customer, product, source_ids[1],
        )
        payload = self._v2_payload(
            company, customer, payload, source_ids[:1], 'monthly_consolidated',
        )

        _configuration, _tax, _existing, validated = \
            self.client._validate_fiscal_invoice_draft_payload_v2(payload, contract)

        self.assertEqual(
            validated['invoices'][0]['allocations'][0]['odoo_sale_order_id'],
            included_sale.id,
        )
        self.assertNotEqual(platform_excluded_refund_sale, included_sale)

    def test_v2_requires_zero_value_capability_version_two(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        source_id = str(uuid.uuid4())
        self._v2_source_sale(customer, product, source_id)
        payload = self._v2_payload(
            company, customer, payload, [source_id], 'monthly_consolidated',
        )
        contract['required_capabilities']['fiscal_zero_value_invoices'] = 1

        with self.assertRaisesRegex(SoftlifeAPIError, 'capability version 2'):
            self.client._validate_fiscal_invoice_draft_payload_v2(payload, contract)

    def test_v2_rejects_supporting_sale_hash_mismatch(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        source_id = str(uuid.uuid4())
        self._v2_source_sale(customer, product, source_id)
        payload = self._v2_payload(
            company, customer, payload, [source_id], 'monthly_consolidated',
        )
        invoice = payload['invoices'][0]
        invoice['allocations'][0]['supporting_sale_order_source_sha256'] = 'd' * 64
        invoice['invoice_payload_sha256'] = self._hash({
            key: value for key, value in invoice.items() if key != 'invoice_payload_sha256'
        })

        with self.assertRaisesRegex(SoftlifeAPIError, 'source sales order does not match'):
            self.client._validate_fiscal_invoice_draft_payload_v2(payload, contract)

    def test_v2_rejects_customer_snapshot_hash_mismatch(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        source_id = str(uuid.uuid4())
        self._v2_source_sale(customer, product, source_id)
        payload = self._v2_payload(
            company, customer, payload, [source_id], 'individual_requested',
        )
        invoice = payload['invoices'][0]
        invoice['customer']['fiscal_details_sha256'] = 'd' * 64
        invoice['invoice_payload_sha256'] = self._hash({
            key: value for key, value in invoice.items() if key != 'invoice_payload_sha256'
        })

        with self.assertRaisesRegex(SoftlifeAPIError, 'snapshot hash is invalid'):
            self.client._validate_fiscal_invoice_draft_payload_v2(payload, contract)

    def test_v2_rejects_inconsistent_line_zero_value_reason(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        source_id = str(uuid.uuid4())
        self._v2_source_sale(customer, product, source_id)
        payload = self._v2_payload(
            company, customer, payload, [source_id], 'monthly_consolidated',
        )
        invoice = payload['invoices'][0]
        invoice['lines'][0]['zero_value_reason'] = 'free'
        invoice['invoice_payload_sha256'] = self._hash({
            key: value for key, value in invoice.items() if key != 'invoice_payload_sha256'
        })

        with self.assertRaisesRegex(SoftlifeAPIError, 'line values are invalid'):
            self.client._validate_fiscal_invoice_draft_payload_v2(payload, contract)

    def test_v2_accepts_canonical_consolidated_zero_value_line(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        source_id = str(uuid.uuid4())
        sale = self._v2_source_sale(customer, product, source_id)
        with _softlife_manufacturing_internal_scope():
            sale.order_line.price_unit = 0
        payload = self._v2_payload(
            company, customer, payload, [source_id], 'monthly_consolidated',
        )
        invoice = payload['invoices'][0]
        invoice['expected_total_cents'] = 0
        invoice['zero_value_reason'] = None
        invoice['lines'][0].update({
            'gross_cents': 0, 'tax_base_cents': 0, 'vat_cents': 0,
            'zero_value_reason': 'free',
        })
        invoice['invoice_payload_sha256'] = self._hash({
            key: value for key, value in invoice.items() if key != 'invoice_payload_sha256'
        })

        self.client._validate_fiscal_invoice_draft_payload_v2(payload, contract)

    def test_v2_rejects_more_than_5000_total_lines(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        source_id = str(uuid.uuid4())
        self._v2_source_sale(customer, product, source_id)
        payload = self._v2_payload(
            company, customer, payload, [source_id], 'monthly_consolidated',
        )
        invoice = payload['invoices'][0]
        invoice['allocations'] = invoice['allocations'] * 5001
        invoice['lines'] = invoice['lines'] * 5001
        invoice['invoice_payload_sha256'] = self._hash({
            key: value for key, value in invoice.items() if key != 'invoice_payload_sha256'
        })

        with self.assertRaisesRegex(SoftlifeAPIError, 'more than 5000 lines'):
            self.client._validate_fiscal_invoice_draft_payload_v2(payload, contract)

    def test_v2_fiscal_partner_snapshot_fields_are_frozen(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        source_id = str(uuid.uuid4())
        self._v2_source_sale(customer, product, source_id)
        payload = self._v2_payload(
            company, customer, payload, [source_id], 'individual_requested',
        )
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            result = self.client._create_fiscal_invoice_drafts(payload)
        partner = self.env['account.move'].browse(
            result['invoices'][0]['odoo_move_id']
        ).partner_id

        with self.assertRaisesRegex(UserError, 'snapshot fields are frozen'):
            partner.write({'email': 'changed@example.com'})

    def test_v2_confirmation_revalidates_supporting_sale_line(self):
        company, journal, customer, tax, product, contract, payload = \
            self._records_and_payload()
        source_id = str(uuid.uuid4())
        sale = self._v2_source_sale(customer, product, source_id)
        payload = self._v2_payload(
            company, customer, payload, [source_id], 'monthly_consolidated',
        )
        Client = type(self.client)
        with patch.object(Client, '_fiscal_invoice_draft_creation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract):
            draft = self.client._create_fiscal_invoice_drafts(payload)['invoices'][0]
        with _softlife_manufacturing_internal_scope():
            sale.order_line.price_unit = 12
        confirmation = {
            'contract_version': 2, 'company': {'odoo_id': company.id},
            'invoices': [{
                'platform_invoice_id': draft['platform_invoice_id'],
                'odoo_move_id': draft['odoo_move_id'],
                'invoice_payload_sha256': draft['invoice_payload_sha256'],
            }],
        }

        with patch.object(Client, '_fiscal_invoice_confirmation_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value=contract), \
                self.assertRaisesRegex(SoftlifeAPIError, 'source sales order does not match'):
            self.client._confirm_fiscal_invoices(confirmation)

    def test_settings_require_company_for_each_new_opt_in(self):
        Params = self.env['ir.config_parameter'].sudo()
        Params.set_param('softlife.sync.fiscal_company_id', '')
        settings = self.env['res.config.settings'].create({
            'softlife_fiscal_invoice_draft_creation_enabled': True,
        })
        with self.assertRaisesRegex(UserError, 'Select the Fiscal issuing company'):
            settings.set_values()
        settings = self.env['res.config.settings'].create({
            'softlife_fiscal_invoice_confirmation_enabled': True,
        })
        with self.assertRaisesRegex(UserError, 'Select the Fiscal issuing company'):
            settings.set_values()
