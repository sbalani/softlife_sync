from unittest.mock import patch

from odoo.tests.common import TransactionCase

from ..models.softlife_sync_client import SoftlifeAPIError


class TestFiscalConfiguration(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = cls.env['softlife.sync.client']

    def test_gets_contract_then_posts_configuration_report(self):
        contract = {
            'contract_version': 1,
            'expected': {
                'journal_code': 'VEND', 'customer_odoo_id': 722,
                'vat_rate': 10, 'currency': 'EUR', 'income_account_code': '701000',
            },
        }
        payload = {'contract_version': 1, 'checked_at': '2026-09-29T15:00:00Z'}
        Client = type(self.client)
        with patch.object(Client, '_fiscal_configuration_reporting_enabled', return_value=True), \
                patch.object(Client, '_api_request', side_effect=[contract, {'accepted': True}]) as api, \
                patch.object(Client, '_fiscal_configuration_payload', return_value=payload) as build:
            result = self.client.report_fiscal_configuration()

        self.assertEqual(result, {'accepted': True})
        self.assertEqual(api.call_args_list[0].args, ('GET', '/api/internal/odoo/fiscal-configuration'))
        self.assertEqual(api.call_args_list[1].args, ('POST', '/api/internal/odoo/fiscal-configuration'))
        self.assertEqual(api.call_args_list[1].kwargs, {'payload': payload})
        build.assert_called_once_with(contract)

    def test_builds_report_from_effective_odoo_configuration(self):
        company = self.env.company
        self.env['ir.config_parameter'].sudo().set_param(
            'softlife.sync.fiscal_company_id', str(company.id),
        )
        country = company.account_fiscal_country_id
        if not country:
            self.skipTest('Test company has no accounting fiscal country.')
        income_accounts = self.env['account.account'].with_company(company).search([
            ('account_type', 'in', ('income', 'income_other')),
        ], limit=2)
        if not income_accounts:
            self.skipTest('Test company has no income account.')
        income = income_accounts[0]
        tax = self.env['account.tax'].with_company(company).create({
            'name': 'Fiscal reporter 17.53%', 'amount': 17.53, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'company_id': company.id, 'country_id': country.id,
            'price_include': True,
        })
        category = self.env['product.category'].with_company(company).create({
            'name': 'Fiscal reporter category', 'property_account_income_categ_id': income.id,
        })
        product = self.env['product.product'].with_company(company).create({
            'name': 'Fiscal reporter product', 'sale_ok': True, 'categ_id': category.id,
            'softlife_recipe_id': 'fiscal-reporter-test-recipe', 'taxes_id': [(6, 0, tax.ids)],
        })
        journal = self.env['account.journal'].with_company(company).create({
            'name': 'Fiscal reporter sales', 'code': 'FSC17', 'type': 'sale',
            'company_id': company.id, 'refund_sequence': True,
        })
        customer_values = {'name': 'Fiscal reporter consumer', 'country_id': country.id}
        expected_income = income
        if len(income_accounts) > 1:
            expected_income = income_accounts[1]
            fiscal_position = self.env['account.fiscal.position'].with_company(company).create({
                'name': 'Fiscal reporter account mapping', 'company_id': company.id,
                'account_ids': [(0, 0, {
                    'account_src_id': income.id, 'account_dest_id': expected_income.id,
                })],
            })
            customer_values['property_account_position_id'] = fiscal_position.id
        customer = self.env['res.partner'].create(customer_values)
        payload = self.client._fiscal_configuration_payload({
            'contract_version': 1,
            'expected': {
                'journal_code': journal.code, 'customer_odoo_id': customer.id,
                'vat_rate': 17.53, 'currency': company.currency_id.name,
                'income_account_code': expected_income.code,
            },
        })

        self.assertEqual(payload['company']['country_code'], country.code)
        self.assertEqual(payload['company']['odoo_id'], company.id)
        self.assertEqual(payload['capabilities'], {
            'fiscal_product_remediation': 1,
            'fiscal_invoice_draft_creation': 1,
            'fiscal_invoice_bulk_confirmation': 1,
            'fiscal_zero_value_invoices': 2,
        })
        self.assertEqual(payload['income_account'], {
            'odoo_id': expected_income.id,
            'code': expected_income.code,
            'account_type': expected_income.account_type,
        })
        self.assertEqual(payload['journal']['code'], journal.code)
        self.assertEqual(payload['customer']['odoo_id'], customer.id)
        self.assertEqual(payload['tax']['odoo_id'], tax.id)
        self.assertEqual(payload['tax']['country_code'], country.code)
        self.assertTrue(payload['tax']['price_include'])
        reported = next(row for row in payload['products'] if row['odoo_product_id'] == product.id)
        self.assertEqual(reported['income_account_code'], expected_income.code)
        self.assertEqual(reported['sale_tax_rates'], [17.53])
        self.assertEqual(reported['sale_tax_country_codes'], [country.code])
        self.assertEqual(reported['sale_tax_ids'], [tax.id])
        self.assertEqual(reported['sale_taxes'], [{
            'odoo_tax_id': tax.id, 'rate': 17.53,
            'country_code': country.code, 'price_include': True,
            'amount_type': 'percent', 'type_tax_use': 'sale',
        }])

    def test_requires_an_explicit_fiscal_company(self):
        contract = {
            'contract_version': 1,
            'expected': {
                'journal_code': 'VEND', 'customer_odoo_id': 722,
                'vat_rate': 10, 'currency': 'EUR', 'income_account_code': '701000',
            },
        }
        Client = type(self.client)
        with patch.object(Client, '_param', return_value=False):
            with self.assertRaisesRegex(SoftlifeAPIError, 'Select the Fiscal issuing company'):
                self.client._fiscal_configuration_payload(contract)

    def test_rejects_unsupported_contract_before_posting(self):
        Client = type(self.client)
        with patch.object(Client, '_fiscal_configuration_reporting_enabled', return_value=True), \
                patch.object(Client, '_api_request', return_value={'contract_version': 2, 'expected': {}}) as api:
            with self.assertRaisesRegex(SoftlifeAPIError, 'unsupported fiscal contract'):
                self.client.report_fiscal_configuration()
        self.assertEqual(api.call_count, 1)

    def test_direct_report_does_not_call_platform_when_disabled(self):
        Client = type(self.client)
        with patch.object(Client, '_fiscal_configuration_reporting_enabled', return_value=False), \
                patch.object(Client, '_api_request') as api:
            result = self.client.report_fiscal_configuration()
        self.assertEqual(result, {'accepted': False, 'disabled': True})
        api.assert_not_called()

    def test_cron_skips_when_fiscal_reporting_is_disabled(self):
        Client = type(self.client)
        with patch.object(Client, '_fiscal_configuration_reporting_enabled', return_value=False), \
                patch.object(Client, '_api_is_configured', return_value=True), \
                patch.object(Client, 'report_fiscal_configuration') as report:
            self.client._cron_fiscal_configuration()
        report.assert_not_called()

    def test_cron_exposes_reporting_failures(self):
        Client = type(self.client)
        with patch.object(Client, '_fiscal_configuration_reporting_enabled', return_value=True), \
                patch.object(Client, '_api_is_configured', return_value=True), \
                patch.object(Client, 'report_fiscal_configuration', side_effect=RuntimeError('report failed')):
            with self.assertRaisesRegex(RuntimeError, 'report failed'):
                self.client._cron_fiscal_configuration()
