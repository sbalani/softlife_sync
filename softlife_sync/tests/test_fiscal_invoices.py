import hashlib
import json
import uuid
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from ..models.softlife_sync_client import SoftlifeAPIError


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
            'name': 'SoftLife fiscal invoice product', 'sale_ok': True,
            'categ_id': category.id, 'softlife_recipe_id': 'fiscal-invoice-test',
            'taxes_id': [(6, 0, tax.ids)],
        })
        self.env['ir.config_parameter'].sudo().set_param(
            'softlife.sync.fiscal_company_id', str(company.id),
        )
        contract = {
            'contract_version': 1,
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
