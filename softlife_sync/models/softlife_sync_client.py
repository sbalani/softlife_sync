"""Connector: pulls operational data from the SoftLife platform (Supabase REST)
into Odoo. Odoo is the downstream ERP; the middleware is the system of record.
"""
import copy
import datetime
import hashlib
import json
import logging
import math
import re
import uuid
from collections import OrderedDict
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare

from .account_move import _softlife_fiscal_internal_scope
from .locks import softlife_company_month_advisory_lock
from .res_partner import _softlife_fiscal_partner_internal_scope

_logger = logging.getLogger(__name__)


class SoftlifeAPIError(UserError):
    """A platform HTTP failure with enough detail for a retry decision."""

    def __init__(self, message, status=None, code=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


class SoftlifeSyncClient(models.TransientModel):
    _name = 'softlife.sync.client'
    _description = 'SoftLife Platform Sync (Supabase connector)'
    _sync_lock_id = 731904628
    _pending_sync_result_key = 'softlife.sync.pending_platform_request_result'
    _stock_movement_retry_minutes = 10

    # ------------------------------------------------------------------
    # Config / HTTP
    # ------------------------------------------------------------------
    @api.model
    def _param(self, key, default=False):
        return self.env['ir.config_parameter'].sudo().get_param(key, default)

    @api.model
    def _is_configured(self):
        return bool(
            self._param('softlife.sync.supabase_url')
            and self._param('softlife.sync.supabase_key')
        )

    @api.model
    def _api_is_configured(self):
        return bool(
            self._param('softlife.sync.platform_url')
            and self._param('softlife.sync.odoo_sync_secret')
        )

    @api.model
    def _api_request(self, method, path, params=None, payload=None):
        import requests
        base = (self._param('softlife.sync.platform_url') or '').strip().rstrip('/')
        secret = self._param('softlife.sync.odoo_sync_secret')
        if not base or not secret:
            raise SoftlifeAPIError(_('Platform App URL / Odoo sync secret not configured.'), code='not_configured')
        url = urljoin(f'{base}/', path.lstrip('/'))
        try:
            response = requests.request(
                method, url, params=params, json=payload,
                headers={'x-odoo-sync-secret': secret, 'Accept': 'application/json'},
                timeout=(10, 60),
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise SoftlifeAPIError(
                _('Platform request failed: %s') % exc, code='network_error', retryable=True,
            ) from exc
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if not 200 <= response.status_code < 300:
            message = body.get('error') if isinstance(body, dict) else None
            code = body.get('code') if isinstance(body, dict) else None
            raise SoftlifeAPIError(
                _('Platform %s %s failed (%s): %s') % (
                    method, path, response.status_code, message or response.text[:500],
                ),
                status=response.status_code, code=code,
                retryable=response.status_code in (408, 425, 429) or response.status_code >= 500,
            )
        if not isinstance(body, dict):
            raise SoftlifeAPIError(_('Platform returned an invalid JSON object.'), code='invalid_response')
        return body

    @api.model
    def _api_catalog_pages(self):
        cursor = None
        while True:
            params = {'limit': 500}
            if cursor:
                params['cursor'] = cursor
            page = self._api_request('GET', '/api/internal/odoo/catalog', params=params)
            yield page
            if not page.get('has_more'):
                break
            cursor = page.get('next_cursor')
            if not cursor:
                raise SoftlifeAPIError(_('Catalog pagination omitted next_cursor.'), code='invalid_response')

    @api.model
    def _fiscal_configuration_payload(self, contract):
        if contract.get('contract_version') != 1 or not isinstance(contract.get('expected'), dict):
            raise SoftlifeAPIError(_('Platform returned an unsupported fiscal contract.'), code='invalid_response')
        expected = contract['expected']
        required = ('journal_code', 'customer_odoo_id', 'vat_rate', 'currency', 'income_account_code')
        if any(expected.get(field) in (None, '') for field in required):
            raise SoftlifeAPIError(_('Platform fiscal contract omitted required settings.'), code='invalid_response')

        company = self._fiscal_company()
        self = self.sudo().with_company(company)
        fiscal_country = company.account_fiscal_country_id
        income_accounts = self.env['account.account'].with_company(company).search([
            ('code', '=', str(expected['income_account_code'])),
            ('account_type', 'in', ('income', 'income_other')),
        ])
        income_accounts = income_accounts.filtered(
            lambda account: self._account_applies_to_company(account, company)
        )
        income_account = income_accounts if len(income_accounts) == 1 else self.env['account.account']
        journal = self.env['account.journal'].with_company(company).with_context(active_test=False).search([
            ('company_id', '=', company.id),
            ('code', '=', str(expected['journal_code'])),
        ], limit=1)
        customer_id = expected['customer_odoo_id']
        if not isinstance(customer_id, int) or isinstance(customer_id, bool) or customer_id <= 0:
            raise SoftlifeAPIError(_('Platform fiscal customer ID is invalid.'), code='invalid_response')
        customer = self.env['res.partner'].browse(customer_id).exists().with_company(company)
        try:
            expected_rate = float(expected['vat_rate'])
        except (TypeError, ValueError) as exc:
            raise SoftlifeAPIError(_('Platform fiscal VAT rate is invalid.'), code='invalid_response') from exc
        if not math.isfinite(expected_rate) or expected_rate <= 0:
            raise SoftlifeAPIError(_('Platform fiscal VAT rate is invalid.'), code='invalid_response')
        tax = self.env['account.tax'].with_company(company).search([
            ('company_id', '=', company.id),
            ('type_tax_use', '=', 'sale'),
            ('amount_type', '=', 'percent'),
            ('amount', '=', expected_rate),
            ('country_id', '=', fiscal_country.id),
        ], order='id', limit=1)

        products = self.env['product.product'].with_company(company).search([
            ('active', '=', True),
            ('softlife_recipe_id', '!=', False),
        ], order='id')
        fiscal_position = customer and self.env['account.fiscal.position'].with_company(company)._get_fiscal_position(customer)
        product_rows = []
        for product in products:
            income, taxes = self._effective_product_fiscal_configuration(
                product, company, fiscal_position,
            )
            rates = sorted(set(
                tax_row.amount for tax_row in taxes
                if tax_row.type_tax_use == 'sale' and tax_row.amount_type == 'percent'
            ))
            product_rows.append({
                'odoo_product_id': product.id,
                'sale_ok': bool(product.sale_ok),
                'income_account_code': income.code if income else None,
                'sale_tax_rates': rates,
                'sale_tax_country_codes': sorted(set(
                    tax_row.country_id.code for tax_row in taxes if tax_row.country_id.code
                )),
                'sale_tax_ids': sorted(taxes.ids),
                'sale_taxes': [{
                    'odoo_tax_id': tax_row.id,
                    'rate': tax_row.amount,
                    'country_code': tax_row.country_id.code if tax_row.country_id else None,
                    'price_include': bool(tax_row.price_include),
                    'amount_type': tax_row.amount_type,
                    'type_tax_use': tax_row.type_tax_use,
                } for tax_row in taxes.sorted('id')],
            })

        return {
            'contract_version': 1,
            'capabilities': self._fiscal_capabilities(),
            'checked_at': fields.Datetime.now().isoformat() + 'Z',
            'company': {
                'odoo_id': company.id,
                'country_code': fiscal_country.code if fiscal_country else None,
                'vat': company.vat or None,
                'currency': company.currency_id.name or None,
            },
            'income_account': {
                'odoo_id': income_account.id if income_account else None,
                'code': income_account.code if income_account else None,
                'account_type': income_account.account_type if income_account else None,
            },
            'journal': {
                'code': journal.code if journal else None,
                'type': journal.type if journal else None,
                'refund_sequence': bool(journal and journal.refund_sequence),
                'secure_posted_entries': bool(journal and journal.restrict_mode_hash_table),
            },
            'customer': {
                'odoo_id': customer.id if customer else None,
                'country_code': customer.country_id.code if customer and customer.country_id else None,
                'vat': (customer.vat or None) if customer else None,
            },
            'tax': {
                'odoo_id': tax.id if tax else None,
                'type_tax_use': tax.type_tax_use if tax else None,
                'rate': tax.amount if tax else None,
                'country_code': tax.country_id.code if tax and tax.country_id else None,
                'price_include': bool(tax and tax.price_include),
                'amount_type': tax.amount_type if tax else None,
            },
            'products': product_rows,
        }

    @api.model
    def _fiscal_capabilities(self):
        return {
            'fiscal_product_remediation': 1,
            'fiscal_invoice_draft_creation': 1,
            'fiscal_invoice_bulk_confirmation': 1,
            'fiscal_zero_value_invoices': 2,
            'manufacturing_transaction_sales': 1,
            'fiscal_invoice_allocation_v2': 1,
        }

    @api.model
    def _require_fiscal_v2_zero_value_capability(self, contract):
        required = contract.get('required_capabilities') if isinstance(contract, dict) else None
        required_version = required.get('fiscal_zero_value_invoices') \
            if isinstance(required, dict) else None
        advertised_version = self._fiscal_capabilities()['fiscal_zero_value_invoices']
        if (
            not isinstance(required, dict)
            or not isinstance(required_version, int) or isinstance(required_version, bool)
            or required_version != 2
            or not isinstance(advertised_version, int) or isinstance(advertised_version, bool)
            or advertised_version != 2
        ):
            self._fiscal_invoice_error(_(
                'Fiscal contract v2 requires zero-value capability version 2.'
            ))

    @api.model
    def _fiscal_company(self):
        raw_company_id = self._param('softlife.sync.fiscal_company_id')
        try:
            company_id = int(raw_company_id)
        except (TypeError, ValueError) as exc:
            raise SoftlifeAPIError(_(
                'Select the Fiscal issuing company in SoftLife Sync settings.'
            ), code='fiscal_company_not_configured') from exc
        company = self.env['res.company'].sudo().browse(company_id).exists()
        if not company or not company.active:
            raise SoftlifeAPIError(_(
                'The configured Fiscal issuing company is missing or inactive.'
            ), code='fiscal_company_not_configured')
        return company

    @api.model
    def _account_applies_to_company(self, account, company):
        if 'company_ids' in account._fields:
            return company in account.company_ids
        if 'company_id' in account._fields:
            return account.company_id == company
        return True

    @api.model
    def _effective_product_fiscal_configuration(self, product, company, fiscal_position):
        product = product.with_company(company)
        income = product.product_tmpl_id.with_company(company).get_product_accounts(
            fiscal_pos=fiscal_position,
        ).get('income')
        taxes = product.taxes_id._filter_taxes_by_company(company)
        if fiscal_position:
            taxes = fiscal_position.map_tax(taxes)
        return income, taxes

    @api.model
    def report_fiscal_configuration(self):
        if not self._fiscal_configuration_reporting_enabled():
            return {'accepted': False, 'disabled': True}
        contract = self._api_request('GET', '/api/internal/odoo/fiscal-configuration')
        payload = self._fiscal_configuration_payload(contract)
        return self._api_request(
            'POST', '/api/internal/odoo/fiscal-configuration', payload=payload,
        )

    @api.model
    def _fiscal_configuration_reporting_enabled(self):
        return str(self._param('softlife.sync.fiscal_reporting_enabled', 'False')).lower() in ('1', 'true')

    @api.model
    def _fiscal_product_remediation_enabled(self):
        return str(self._param(
            'softlife.sync.fiscal_product_remediation_enabled', 'False',
        )).lower() in ('1', 'true')

    @api.model
    def _fiscal_invoice_draft_creation_enabled(self):
        return str(self._param(
            'softlife.sync.fiscal_invoice_draft_creation_enabled', 'False',
        )).lower() in ('1', 'true')

    @api.model
    def _fiscal_invoice_confirmation_enabled(self):
        return str(self._param(
            'softlife.sync.fiscal_invoice_confirmation_enabled', 'False',
        )).lower() in ('1', 'true')

    @api.model
    def _remediation_error(self, message):
        raise SoftlifeAPIError(message, code='invalid_fiscal_product_remediation')

    @api.model
    def _validate_fiscal_product_remediation(self, payload, contract):
        expected_keys = {
            'contract_version', 'configuration_report_id', 'configuration_payload_sha256',
            'company', 'customer', 'target', 'products',
        }
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            self._remediation_error(_('Fiscal product remediation payload fields are invalid.'))
        if payload['contract_version'] != 1:
            self._remediation_error(_('Fiscal product remediation contract version is unsupported.'))
        report_id = payload['configuration_report_id']
        digest = payload['configuration_payload_sha256']
        if not isinstance(report_id, str) or not report_id.strip():
            self._remediation_error(_('Fiscal product remediation report ID is invalid.'))
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            self._remediation_error(_('Fiscal product remediation report hash is invalid.'))

        company_payload = payload['company']
        customer_payload = payload['customer']
        target = payload['target']
        products_payload = payload['products']
        if not isinstance(company_payload, dict) or set(company_payload) != {
                'odoo_id', 'country_code', 'currency'}:
            self._remediation_error(_('Fiscal product remediation company fields are invalid.'))
        if not isinstance(customer_payload, dict) or set(customer_payload) != {'odoo_id'}:
            self._remediation_error(_('Fiscal product remediation customer fields are invalid.'))
        if not isinstance(target, dict) or set(target) != {'income_account', 'sale_tax'}:
            self._remediation_error(_('Fiscal product remediation target fields are invalid.'))
        account_payload = target['income_account']
        tax_payload = target['sale_tax']
        if not isinstance(account_payload, dict) or set(account_payload) != {
                'odoo_id', 'code', 'account_type'}:
            self._remediation_error(_('Fiscal product remediation income account fields are invalid.'))
        if not isinstance(tax_payload, dict) or set(tax_payload) != {
                'odoo_tax_id', 'rate', 'country_code', 'type_tax_use', 'amount_type',
                'price_include'}:
            self._remediation_error(_('Fiscal product remediation sales tax fields are invalid.'))
        if not isinstance(products_payload, list) or not 1 <= len(products_payload) <= 500:
            self._remediation_error(_('Fiscal product remediation requires 1 to 500 products.'))

        product_rows = {}
        for row in products_payload:
            if not isinstance(row, dict) or set(row) != {
                    'odoo_product_id', 'remediate_income_account', 'remediate_customer_taxes'}:
                self._remediation_error(_('Fiscal product remediation product fields are invalid.'))
            product_id = row['odoo_product_id']
            account_flag = row['remediate_income_account']
            tax_flag = row['remediate_customer_taxes']
            if (
                not self._positive_int(product_id)
                or not isinstance(account_flag, bool) or not isinstance(tax_flag, bool)
                or not (account_flag or tax_flag) or product_id in product_rows
            ):
                self._remediation_error(_('Fiscal product remediation product entry is invalid.'))
            product_rows[product_id] = row

        company = self._fiscal_company()
        fiscal_country = company.account_fiscal_country_id
        if (
            not self._positive_int(company_payload['odoo_id'])
            or company_payload['odoo_id'] != company.id
            or company_payload['country_code'] != (fiscal_country.code if fiscal_country else None)
            or company_payload['country_code'] != 'ES'
            or company_payload['currency'] != company.currency_id.name
        ):
            self._remediation_error(_('Fiscal product remediation company does not match Odoo.'))
        expected = contract.get('expected') if isinstance(contract, dict) else None
        if not isinstance(contract, dict) or contract.get('contract_version') != 1 or not isinstance(expected, dict):
            self._remediation_error(_('Current platform fiscal contract is invalid.'))
        if expected.get('currency') != company.currency_id.name:
            self._remediation_error(_('Current platform fiscal currency does not match Odoo.'))
        customer_id = customer_payload['odoo_id']
        if (
            not self._positive_int(customer_id)
            or customer_id != expected.get('customer_odoo_id')
            or not self.env['res.partner'].sudo().browse(customer_id).exists()
        ):
            self._remediation_error(_('Fiscal product remediation customer is not the fiscal customer.'))

        account_id = account_payload['odoo_id']
        if not self._positive_int(account_id) or account_payload['account_type'] not in ('income', 'income_other'):
            self._remediation_error(_('Fiscal product remediation income account is invalid.'))
        account = self.env['account.account'].sudo().with_company(company).browse(account_id).exists()
        if (
            not account
            or account.code != account_payload['code']
            or account.account_type != account_payload['account_type']
            or account.code != str(expected.get('income_account_code'))
            or not self._account_applies_to_company(account, company)
        ):
            self._remediation_error(_('Fiscal product remediation income account does not match Odoo.'))

        tax_id = tax_payload['odoo_tax_id']
        rate = tax_payload['rate']
        if not self._positive_int(tax_id) or not self._number(rate) or rate <= 0:
            self._remediation_error(_('Fiscal product remediation sales tax is invalid.'))
        tax = self.env['account.tax'].sudo().with_context(active_test=False).browse(tax_id).exists()
        try:
            expected_rate = float(expected.get('vat_rate'))
        except (TypeError, ValueError):
            self._remediation_error(_('Current platform fiscal VAT rate is invalid.'))
        if (
            not tax or not tax.active or tax.company_id != company
            or tax.type_tax_use != 'sale' or tax_payload['type_tax_use'] != 'sale'
            or tax.amount_type != 'percent' or tax_payload['amount_type'] != 'percent'
            or tax.amount != rate or expected_rate != rate
            or not tax.country_id or tax.country_id.code != 'ES'
            or tax.country_id != fiscal_country
            or tax_payload['country_code'] != 'ES'
            or not isinstance(tax_payload['price_include'], bool)
            or bool(tax.price_include) != tax_payload['price_include']
        ):
            self._remediation_error(_('Fiscal product remediation sales tax does not match Odoo.'))

        products = self.env['product.product'].sudo().with_context(active_test=False).browse(
            list(product_rows)
        ).exists()
        if len(products) != len(product_rows) or any(
                not product.active or not product.softlife_recipe_id for product in products):
            self._remediation_error(_('Fiscal product remediation contains an inactive or non-SoftLife product.'))
        supplied_ids = set(product_rows)
        for template in products.product_tmpl_id:
            variant_ids = set(template.with_context(active_test=False).product_variant_ids.ids)
            if len(variant_ids) > 1 and not variant_ids.issubset(supplied_ids):
                self._remediation_error(_(
                    'Fiscal product remediation must include every variant of a product template.'
                ))
            variant_rows = [product_rows[variant_id] for variant_id in variant_ids]
            if len(variant_ids) > 1 and (
                len({row['remediate_income_account'] for row in variant_rows}) != 1
                or len({row['remediate_customer_taxes'] for row in variant_rows}) != 1
            ):
                self._remediation_error(_(
                    'Fiscal product remediation flags must match for every variant of a template.'
                ))
        return company, self.env['res.partner'].sudo().browse(customer_id), account, tax, products, product_rows

    @staticmethod
    def _positive_int(value):
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    @staticmethod
    def _nonnegative_int(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    @staticmethod
    def _number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    @api.model
    def remediate_fiscal_products(self, payload):
        if not self._fiscal_product_remediation_enabled():
            raise SoftlifeAPIError(
                _('Fiscal product remediation is disabled in Odoo Settings.'),
                code='fiscal_product_remediation_disabled',
            )
        contract = self._api_request('GET', '/api/internal/odoo/fiscal-configuration')
        company, customer, account, tax, products, rows = \
            self._validate_fiscal_product_remediation(payload, contract)
        fiscal_position = self.env['account.fiscal.position'].sudo().with_company(
            company,
        )._get_fiscal_position(customer.with_company(company))
        all_company_ids = self.env['res.company'].sudo().search([]).ids
        with self.env.cr.savepoint():
            for template in products.product_tmpl_id:
                template_products = products.filtered(lambda product: product.product_tmpl_id == template)
                if any(rows[product.id]['remediate_income_account'] for product in template_products):
                    template.sudo().with_company(company).property_account_income_id = account
                if any(rows[product.id]['remediate_customer_taxes'] for product in template_products):
                    template_all_companies = template.sudo().with_context(
                        active_test=False, allowed_company_ids=all_company_ids,
                    )
                    other_tax_ids = template_all_companies.taxes_id.filtered(
                        lambda current_tax: current_tax.company_id != company
                    ).ids
                    template_all_companies.with_company(company).taxes_id = [(6, 0, other_tax_ids + tax.ids)]
            for product in products:
                effective_account, effective_taxes = self._effective_product_fiscal_configuration(
                    product.sudo(), company, fiscal_position,
                )
                row = rows[product.id]
                if row['remediate_income_account'] and effective_account != account:
                    self._remediation_error(_(
                        'Fiscal product remediation income account verification failed.'
                    ))
                if row['remediate_customer_taxes'] and effective_taxes != tax:
                    self._remediation_error(_(
                        'Fiscal product remediation sales tax verification failed.'
                    ))
            verification_payload = self._fiscal_configuration_payload(contract)
            verification = self._api_request(
                'POST', '/api/internal/odoo/fiscal-configuration', payload=verification_payload,
            )
        result = {
            'accepted': True,
            'summary': _('Remediated and verified %s fiscal product configuration(s).') % len(products),
        }
        report_id = (
            verification.get('configuration_report_id')
            or verification.get('report_id')
            or verification.get('id')
        )
        report_hash = verification.get('configuration_payload_sha256') or verification.get('payload_sha256')
        if report_id:
            result['verification_report_id'] = report_id
        if report_hash:
            result['verification_payload_sha256'] = report_hash
        return result

    @api.model
    def _fiscal_invoice_error(self, message):
        raise SoftlifeAPIError(message, code='invalid_fiscal_invoice_request')

    @staticmethod
    def _canonical_sha256(payload):
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        ).encode()).hexdigest()

    @api.model
    def _current_fiscal_invoice_configuration(self, contract):
        expected = contract.get('expected') if isinstance(contract, dict) else None
        required = ('journal_code', 'customer_odoo_id', 'vat_rate', 'currency', 'income_account_code')
        if (
            not isinstance(contract, dict) or contract.get('contract_version') != 1
            or isinstance(contract.get('contract_version'), bool)
            or not isinstance(expected, dict)
            or any(expected.get(field) in (None, '') for field in required)
            or expected.get('tax_treatment_approved') is not True
        ):
            self._fiscal_invoice_error(_('Current platform fiscal contract is invalid.'))
        company = self._fiscal_company()
        fiscal_country = company.account_fiscal_country_id
        if (
            not fiscal_country or fiscal_country.code != 'ES'
            or company.currency_id.name != 'EUR' or expected['currency'] != 'EUR'
        ):
            self._fiscal_invoice_error(_('Current fiscal company must be Spanish and use EUR.'))
        customer_id = expected['customer_odoo_id']
        if not self._positive_int(customer_id):
            self._fiscal_invoice_error(_('Current platform fiscal customer is invalid.'))
        customer = self.env['res.partner'].sudo().with_context(active_test=False).browse(
            customer_id,
        ).exists()
        if not customer or not customer.active or (customer.company_id and customer.company_id != company):
            self._fiscal_invoice_error(_('Current platform fiscal customer is unavailable.'))
        journal = self.env['account.journal'].sudo().with_company(company).with_context(
            active_test=False,
        ).search([
            ('company_id', '=', company.id), ('code', '=', str(expected['journal_code'])),
        ], limit=1)
        if not journal or not journal.active or journal.type != 'sale':
            self._fiscal_invoice_error(_('Current platform fiscal sales journal is invalid.'))
        try:
            rate = float(expected['vat_rate'])
        except (TypeError, ValueError):
            self._fiscal_invoice_error(_('Current platform fiscal VAT rate is invalid.'))
        if not math.isfinite(rate) or rate <= 0:
            self._fiscal_invoice_error(_('Current platform fiscal VAT rate is invalid.'))
        return company, journal, customer, rate

    @api.model
    def _validate_fiscal_invoice_tax(self, tax_payload, company, expected_rate):
        if not isinstance(tax_payload, dict) or set(tax_payload) != {
                'odoo_tax_id', 'rate', 'country_code', 'type_tax_use', 'amount_type',
                'price_include'}:
            self._fiscal_invoice_error(_('Fiscal invoice tax fields are invalid.'))
        tax_id = tax_payload['odoo_tax_id']
        rate = tax_payload['rate']
        if (
            not self._positive_int(tax_id) or not self._number(rate) or rate <= 0
            or tax_payload['country_code'] != 'ES' or tax_payload['type_tax_use'] != 'sale'
            or tax_payload['amount_type'] != 'percent'
            or not isinstance(tax_payload['price_include'], bool)
        ):
            self._fiscal_invoice_error(_('Fiscal invoice tax is invalid.'))
        tax = self.env['account.tax'].sudo().with_context(active_test=False).browse(tax_id).exists()
        if (
            not tax or not tax.active or tax.company_id != company
            or tax.country_id != company.account_fiscal_country_id
            or tax.type_tax_use != 'sale' or tax.amount_type != 'percent'
            or tax.amount != rate or rate != expected_rate
            or bool(tax.price_include) != tax_payload['price_include']
        ):
            self._fiscal_invoice_error(_('Fiscal invoice tax does not match Odoo.'))
        return tax

    @api.model
    def _validate_fiscal_invoice_move(self, move, invoice, configuration, tax):
        company, journal, customer, _rate = configuration
        self._validate_fiscal_invoice_snapshot(move, invoice)
        if (
            move.softlife_fiscal_payload_sha256 != invoice['invoice_payload_sha256']
            or move.company_id != company or move.journal_id != journal
            or move.partner_id != customer or move.move_type != 'out_invoice'
            or move.currency_id != company.currency_id
            or fields.Date.to_string(move.invoice_date) != invoice['invoice_date']
            or fields.Date.to_string(move.date) != invoice['invoice_date']
            or move.ref != invoice['reference'] or move.state not in ('draft', 'posted')
        ):
            self._fiscal_invoice_error(_('Existing fiscal invoice does not match the frozen payload.'))
        lines = move.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
        if len(lines) != len(invoice['lines']):
            self._fiscal_invoice_error(_('Existing fiscal invoice lines do not match the frozen payload.'))
        for line, expected in zip(lines, invoice['lines']):
            price_unit = self._fiscal_invoice_price_unit(expected, tax.price_include)
            if (
                line.product_id.id != expected['odoo_product_id']
                or line.name != expected['description'] or line.quantity != expected['quantity']
                or abs(line.price_unit - price_unit) > 1e-9
                or line.account_id.id != expected['account_id'] or line.tax_ids != tax
                or self._amount_cents(line.price_total) != expected['gross_cents']
                or self._amount_cents(line.price_subtotal) != expected['tax_base_cents']
            ):
                self._fiscal_invoice_error(_('Existing fiscal invoice lines do not match the frozen payload.'))
        if self._amount_cents(move.amount_total) != invoice['expected_total_cents']:
            self._fiscal_invoice_error(_('Existing fiscal invoice total does not match the frozen payload.'))

    @api.model
    def _fiscal_invoice_snapshot_sha256(self, invoice):
        try:
            frozen = copy.deepcopy(invoice)
            invoice_hash = frozen.pop('invoice_payload_sha256')
            for line in frozen['lines']:
                account_id = line.pop('account_id')
                if not self._positive_int(account_id):
                    self._fiscal_invoice_error(_('Stored fiscal invoice snapshot account is invalid.'))
        except (AttributeError, KeyError, TypeError):
            self._fiscal_invoice_error(_('Stored fiscal invoice snapshot is invalid.'))
        computed = self._canonical_sha256(frozen)
        if not isinstance(invoice_hash, str) or computed != invoice_hash:
            self._fiscal_invoice_error(_('Stored fiscal invoice snapshot hash is invalid.'))
        return computed

    @api.model
    def _validate_fiscal_invoice_snapshot(self, move, invoice=None):
        snapshot = move.softlife_fiscal_invoice_snapshot
        if not isinstance(snapshot, dict):
            self._fiscal_invoice_error(_('Stored fiscal invoice snapshot is missing.'))
        self._fiscal_invoice_snapshot_sha256(snapshot)
        if (
            snapshot.get('platform_invoice_id') != move.softlife_fiscal_invoice_id
            or snapshot.get('invoice_payload_sha256') != move.softlife_fiscal_payload_sha256
            or (invoice is not None and snapshot != invoice)
        ):
            self._fiscal_invoice_error(_('Stored fiscal invoice snapshot does not match provenance.'))
        return copy.deepcopy(snapshot)

    @staticmethod
    def _amount_cents(amount):
        return int((Decimal(str(amount)) * 100).quantize(Decimal('1')))

    @staticmethod
    def _fiscal_invoice_price_unit(line, price_include):
        cents = line['gross_cents'] if price_include else line['tax_base_cents']
        return float(Decimal(cents) / Decimal(100) / Decimal(line['quantity']))

    @api.model
    def _canonical_uuid(self, value):
        try:
            normalized = str(uuid.UUID(value)) if isinstance(value, str) else ''
        except (ValueError, AttributeError):
            normalized = ''
        return normalized if normalized == value else False

    @api.model
    def _prepare_fiscal_v2_customer(self, customer, generic_customer, company):
        if not isinstance(customer, dict):
            self._fiscal_invoice_error(_('Fiscal invoice customer snapshot is invalid.'))
        if set(customer) == {'odoo_id'}:
            if customer['odoo_id'] != generic_customer.id:
                self._fiscal_invoice_error(_('Generic fiscal customer does not match the current contract.'))
            return {'partner': generic_customer, 'values': None, 'request_id': None}
        detail_fields = {
            'legal_name', 'tax_id', 'email', 'address', 'postal_code', 'city',
            'province', 'country_code',
        }
        expected = detail_fields | {'request_id', 'fiscal_details_sha256'}
        if set(customer) != expected or not self._canonical_uuid(customer.get('request_id')):
            self._fiscal_invoice_error(_('Named fiscal customer snapshot fields are invalid.'))
        if any(not isinstance(customer[field], str) or not customer[field].strip()
               for field in detail_fields):
            self._fiscal_invoice_error(_('Named fiscal customer snapshot values are invalid.'))
        if (
            not re.fullmatch(r'[0-9a-f]{64}', customer['fiscal_details_sha256'])
            or self._canonical_sha256({field: customer[field] for field in detail_fields})
            != customer['fiscal_details_sha256']
            or not re.fullmatch(r'[A-Z]{2}', customer['country_code'])
            or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', customer['email'])
        ):
            self._fiscal_invoice_error(_('Named fiscal customer snapshot hash is invalid.'))
        limits = {
            'legal_name': 200, 'tax_id': 50, 'email': 254, 'address': 300,
            'postal_code': 20, 'city': 100, 'province': 100, 'country_code': 2,
        }
        for field_name, maximum in limits.items():
            if len(customer[field_name]) > maximum:
                self._fiscal_invoice_error(_('Named fiscal customer snapshot values are invalid.'))
        country = self.env['res.country'].sudo().search([
            ('code', '=', customer['country_code']),
        ], limit=1)
        if not country:
            self._fiscal_invoice_error(_('Named fiscal customer country does not exist in Odoo.'))
        state = self.env['res.country.state'].sudo().search([
            ('country_id', '=', country.id), '|',
            ('name', '=ilike', customer['province']), ('code', '=ilike', customer['province']),
        ], limit=1)
        if not state:
            self._fiscal_invoice_error(_('Named fiscal customer province does not exist in Odoo.'))
        partner = self.env['res.partner'].sudo().with_context(active_test=False).search([
            ('softlife_fiscal_customer_id', '=', customer['request_id']),
        ], limit=1)
        values = {
            'name': customer['legal_name'], 'vat': customer['tax_id'],
            'email': customer['email'], 'street': customer['address'],
            'city': customer['city'], 'zip': customer['postal_code'],
            'state_id': state.id, 'country_id': country.id, 'company_id': company.id,
            'customer_rank': 1,
            'softlife_fiscal_customer_id': customer['request_id'],
            'softlife_fiscal_customer_sha256': customer['fiscal_details_sha256'],
        }
        if partner and (
            not partner.active or partner.company_id not in (company, self.env['res.company'])
            or partner.softlife_fiscal_customer_sha256 != customer['fiscal_details_sha256']
            or partner.name != values['name'] or (partner.vat or False) != values['vat']
            or (partner.email or False) != values['email']
            or (partner.street or False) != values['street']
            or (partner.city or False) != values['city'] or (partner.zip or False) != values['zip']
            or partner.country_id.id != values['country_id']
            or partner.state_id.id != values['state_id']
            or partner.company_id.id != values['company_id']
            or partner.softlife_fiscal_customer_id != values['softlife_fiscal_customer_id']
        ):
            self._fiscal_invoice_error(_('Existing named fiscal customer does not match its frozen snapshot.'))
        return {'partner': partner, 'values': values, 'request_id': customer['request_id']}

    @api.model
    def _validate_fiscal_v2_supporting_sale(self, invoice, line, allocation, company,
                                            sale=None, sale_line=None, allowed_move=None):
        sale = sale or self.env['sale.order'].sudo().browse(
            allocation['odoo_sale_order_id']
        ).exists()
        sale_line = sale_line or self.env['sale.order.line'].sudo().browse(
            allocation['odoo_sale_order_line_id']
        ).exists()
        main_lines = sale and sale.order_line.filtered(
            lambda row: not row.display_type and not row.softlife_rounding_adjustment)
        other_invoice_lines = sale_line and sale_line.invoice_lines.filtered(
            lambda row: row.move_id.state != 'cancel' and row.move_id != allowed_move)
        expected_price = float(
            Decimal(line['gross_cents']) / Decimal(100) / Decimal(line['quantity'])
        )
        if (
            not sale or not sale_line or sale_line.order_id != sale
            or len(main_lines) != 1 or main_lines != sale_line
            or sale_line.product_id.id != line['odoo_product_id']
            or sale_line.name != line['description']
            or sale_line.tax_id
            or sale.company_id != company or sale.currency_id != company.currency_id
            or not sale.warehouse_id or sale.warehouse_id != sale.softlife_warehouse_id
            or sale.state not in ('sale', 'done')
            or sale.softlife_manufacturing_contract_version != 3
            or sale.softlife_source_order_id != allocation['source_order_id']
            or sale.softlife_source_payload_sha256
            != allocation['supporting_sale_order_source_sha256']
            or not sale_line.softlife_platform_line_key
            or not sale.softlife_document_date
            or fields.Date.to_string(sale.softlife_document_date)[:7] != invoice['local_month']
            or float_compare(
                sale_line.product_uom_qty, line['quantity'],
                precision_rounding=sale_line.product_uom.rounding,
            )
            or float_compare(
                sale_line.qty_delivered, line['quantity'],
                precision_rounding=sale_line.product_uom.rounding,
            )
            or abs(sale_line.price_unit - expected_price) > 1e-9
            or self._amount_cents(sale_line.price_total) != line['gross_cents']
            or self._amount_cents(sale.amount_total) != line['gross_cents']
            or other_invoice_lines
        ):
            self._fiscal_invoice_error(_('Fiscal invoice source sales order does not match.'))
        return sale_line

    @api.model
    def _validate_fiscal_v2_included_sales(self, company, local_month, source_ids, sale_ids):
        included_sales = self.env['sale.order'].sudo().browse(sorted(sale_ids)).exists()
        if included_sales:
            self.env.cr.execute(
                'SELECT id FROM sale_order WHERE id IN %s ORDER BY id FOR UPDATE',
                [tuple(included_sales.ids)],
            )
            included_sales.invalidate_recordset()
        included_sources = included_sales.mapped('softlife_source_order_id')
        if (
            len(included_sources) != len(set(included_sources))
            or set(included_sources) != set(source_ids)
            or set(included_sales.ids) != set(sale_ids)
            or any(
                sale.company_id != company
                or sale.softlife_manufacturing_contract_version != 3
                or sale.state not in ('sale', 'done')
                or not sale.softlife_document_date
                or fields.Date.to_string(sale.softlife_document_date)[:7] != local_month
                for sale in included_sales
            )
        ):
            self._fiscal_invoice_error(_(
                'Fiscal v2 included allocations do not match their supporting source sales.'
            ))
        return included_sales

    @api.model
    def _validate_fiscal_invoice_draft_payload_v2(self, payload, contract):
        if not isinstance(payload, dict) or set(payload) != {
                'contract_version', 'configuration_report_id', 'configuration_payload_sha256',
                'company', 'journal', 'tax', 'local_month', 'invoices'}:
            self._fiscal_invoice_error(_('Fiscal invoice draft payload fields are invalid.'))
        if payload.get('contract_version') != 2 or isinstance(payload.get('contract_version'), bool):
            self._fiscal_invoice_error(_('Fiscal invoice draft contract version is unsupported.'))
        self._require_fiscal_v2_zero_value_capability(contract)
        if (
            not isinstance(payload.get('configuration_report_id'), str)
            or not payload['configuration_report_id'].strip()
            or not isinstance(payload.get('configuration_payload_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', payload['configuration_payload_sha256'])
        ):
            self._fiscal_invoice_error(_('Fiscal invoice configuration report identity is invalid.'))
        configuration = self._current_fiscal_invoice_configuration(contract)
        company, journal, generic_customer, expected_rate = configuration
        if payload.get('company') != {'odoo_id': company.id, 'country_code': 'ES', 'currency': 'EUR'} \
                or payload.get('journal') != {'code': journal.code}:
            self._fiscal_invoice_error(_('Fiscal invoice configuration does not match Odoo.'))
        if not isinstance(payload.get('local_month'), str) or not re.fullmatch(
                r'\d{4}-(0[1-9]|1[0-2])', payload['local_month']) \
                or payload['local_month'] < '2026-09':
            self._fiscal_invoice_error(_('Fiscal invoice month is invalid.'))
        month_start = datetime.date.fromisoformat(payload['local_month'] + '-01')
        month_end = (month_start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        madrid_today = datetime.datetime.now(
            datetime.timezone.utc,
        ).astimezone(ZoneInfo('Europe/Madrid')).date()
        if month_end > madrid_today:
            self._fiscal_invoice_error(_('Fiscal invoice month must be closed.'))
        softlife_company_month_advisory_lock(self.env.cr, company.id, month_start)
        tax = self._validate_fiscal_invoice_tax(payload.get('tax'), company, expected_rate)
        invoices = payload.get('invoices')
        if not isinstance(invoices, list) or not 1 <= len(invoices) <= 500:
            self._fiscal_invoice_error(_('Fiscal invoice draft requires 1 to 500 invoices.'))
        payload = copy.deepcopy(payload)
        seen_invoice_ids = set()
        seen_sources = set()
        seen_allocations = set()
        seen_supporting_sales = set()
        accounting_scopes = set()
        named_customer_hashes = {}
        product_ids = set()
        sale_ids = set()
        sale_line_ids = set()
        total_line_count = 0
        for invoice in payload['invoices']:
            required = {
                'platform_invoice_id', 'invoice_payload_sha256', 'move_type', 'invoice_date',
                'currency', 'reference', 'expected_total_cents', 'invoice_mode',
                'local_month', 'customer', 'zero_value_reason', 'allocations', 'lines',
            }
            if not isinstance(invoice, dict) or set(invoice) != required:
                self._fiscal_invoice_error(_('Fiscal invoice fields are invalid.'))
            invoice_id = invoice.get('platform_invoice_id')
            frozen = copy.deepcopy(invoice)
            invoice_hash = frozen.pop('invoice_payload_sha256', None)
            try:
                valid_date = datetime.date.fromisoformat(invoice.get('invoice_date', '')).isoformat()
            except (TypeError, ValueError):
                valid_date = False
            if (
                not self._canonical_uuid(invoice_id) or invoice_id in seen_invoice_ids
                or not isinstance(invoice_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', invoice_hash)
                or self._canonical_sha256(frozen) != invoice_hash
                or invoice.get('move_type') != 'out_invoice' or invoice.get('currency') != 'EUR'
                or not isinstance(invoice.get('reference'), str) or not invoice['reference'].strip()
                or not self._nonnegative_int(invoice.get('expected_total_cents'))
                or valid_date != invoice.get('invoice_date')
                or not isinstance(invoice.get('local_month'), str)
                or invoice['local_month'] != payload['local_month']
                or not invoice['invoice_date'].startswith(invoice['local_month'] + '-')
                or invoice.get('invoice_mode') not in (
                    'individual_requested', 'monthly_consolidated')
            ):
                self._fiscal_invoice_error(_('Fiscal invoice identity or header is invalid.'))
            customer_info = self._prepare_fiscal_v2_customer(
                invoice.get('customer'), generic_customer, company,
            )
            if invoice['invoice_mode'] == 'individual_requested' and not customer_info['request_id']:
                self._fiscal_invoice_error(_('Individual fiscal invoices require a named customer snapshot.'))
            if invoice['invoice_mode'] == 'monthly_consolidated' and customer_info['request_id']:
                self._fiscal_invoice_error(_('The consolidated fiscal invoice must use the generic customer.'))
            named_customer_id = customer_info['request_id']
            if named_customer_id:
                prior_hash = named_customer_hashes.setdefault(
                    named_customer_id, invoice['customer']['fiscal_details_sha256'],
                )
                if prior_hash != invoice['customer']['fiscal_details_sha256']:
                    self._fiscal_invoice_error(_(
                        'One named fiscal customer ID has conflicting snapshots.'
                    ))
            allocations = invoice.get('allocations')
            lines = invoice.get('lines')
            if (
                not isinstance(allocations, list) or not isinstance(lines, list)
                or not allocations or len(allocations) != len(lines)
                or invoice['invoice_mode'] == 'individual_requested' and len(lines) != 1
            ):
                self._fiscal_invoice_error(_('Fiscal invoice has an invalid source-line count.'))
            total_line_count += len(lines)
            if total_line_count > 5000:
                self._fiscal_invoice_error(_('Fiscal invoice request cannot contain more than 5000 lines.'))
            allocations_by_id = {}
            for index, allocation in enumerate(allocations, start=1):
                if not isinstance(allocation, dict) or set(allocation) != {
                        'platform_allocation_id', 'preflight_item_id', 'source_order_id',
                        'source_sha256', 'supporting_sale_order_source_sha256',
                        'invoice_request_id', 'supporting_sale_order_id',
                        'odoo_sale_order_id', 'odoo_sale_order_line_id', 'line_sequence'}:
                    self._fiscal_invoice_error(_('Fiscal invoice allocation fields are invalid.'))
                allocation_id = allocation.get('platform_allocation_id')
                source_id = allocation.get('source_order_id')
                request_id = allocation.get('invoice_request_id')
                supporting_sale_id = allocation.get('supporting_sale_order_id')
                if (
                    not self._canonical_uuid(allocation_id) or allocation_id in seen_allocations
                    or not self._canonical_uuid(allocation.get('preflight_item_id'))
                    or not self._canonical_uuid(source_id) or source_id in seen_sources
                    or not re.fullmatch(r'[0-9a-f]{64}', str(allocation.get('source_sha256') or ''))
                    or not re.fullmatch(
                        r'[0-9a-f]{64}',
                        str(allocation.get('supporting_sale_order_source_sha256') or ''),
                    )
                    or not self._canonical_uuid(supporting_sale_id)
                    or supporting_sale_id in seen_supporting_sales
                    or not self._positive_int(allocation.get('odoo_sale_order_id'))
                    or not self._positive_int(allocation.get('odoo_sale_order_line_id'))
                    or not self._positive_int(allocation.get('line_sequence'))
                    or allocation['line_sequence'] != index
                    or request_id != customer_info['request_id']
                ):
                    self._fiscal_invoice_error(_('Fiscal invoice allocation identity is invalid.'))
                allocations_by_id[allocation_id] = allocation
                seen_allocations.add(allocation_id)
                seen_sources.add(source_id)
                seen_supporting_sales.add(supporting_sale_id)
                sale_ids.add(allocation['odoo_sale_order_id'])
                sale_line_ids.add(allocation['odoo_sale_order_line_id'])
            gross_total = 0
            for index, line in enumerate(lines, start=1):
                if not isinstance(line, dict) or set(line) != {
                        'platform_allocation_id', 'odoo_product_id', 'description', 'quantity',
                        'gross_cents', 'tax_base_cents', 'vat_cents', 'zero_value_reason',
                        'odoo_tax_id',
                        'odoo_sale_order_id', 'odoo_sale_order_line_id'}:
                    self._fiscal_invoice_error(_('Fiscal invoice line fields are invalid.'))
                allocation = allocations_by_id.get(line.get('platform_allocation_id'))
                if (
                    not allocation or allocation['line_sequence'] != index
                    or not self._positive_int(line.get('odoo_product_id'))
                    or not isinstance(line.get('description'), str) or not line['description'].strip()
                    or not self._positive_int(line.get('quantity'))
                    or not self._nonnegative_int(line.get('gross_cents'))
                    or not self._nonnegative_int(line.get('tax_base_cents'))
                    or not self._nonnegative_int(line.get('vat_cents'))
                    or line.get('odoo_tax_id') != tax.id
                    or line['gross_cents'] != line['tax_base_cents'] + line['vat_cents']
                    or line.get('zero_value_reason') not in (
                        None, 'free', 'admin_override', 'coupon')
                    or (line['gross_cents'] == 0) != (
                        line['zero_value_reason'] is not None)
                    or line.get('odoo_sale_order_id') != allocation['odoo_sale_order_id']
                    or line.get('odoo_sale_order_line_id') != allocation['odoo_sale_order_line_id']
                ):
                    self._fiscal_invoice_error(_('Fiscal invoice line values are invalid.'))
                rate = Decimal(str(tax.amount)) / Decimal(100)
                computed_base = (Decimal(line['gross_cents']) / (Decimal(1) + rate)).quantize(
                    Decimal('1'), rounding=ROUND_HALF_UP,
                ) if tax.price_include else Decimal(line['tax_base_cents'])
                computed_vat = Decimal(line['gross_cents']) - computed_base if tax.price_include else \
                    (computed_base * rate).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
                if computed_vat != line['vat_cents']:
                    self._fiscal_invoice_error(_('Fiscal invoice line VAT cents are invalid.'))
                product_ids.add(line['odoo_product_id'])
                gross_total += line['gross_cents']
            if gross_total != invoice['expected_total_cents']:
                self._fiscal_invoice_error(_('Fiscal invoice line cents do not match its expected total.'))
            invoice_zero_reason = invoice.get('zero_value_reason')
            if invoice_zero_reason not in (None, 'free', 'admin_override', 'coupon'):
                self._fiscal_invoice_error(_('Fiscal invoice zero-value reason is invalid.'))
            if invoice['invoice_mode'] == 'individual_requested':
                if (
                    invoice_zero_reason != lines[0]['zero_value_reason']
                    or (invoice['expected_total_cents'] == 0)
                    != (invoice_zero_reason is not None)
                ):
                    self._fiscal_invoice_error(_('Individual invoice zero-value reason is invalid.'))
            elif invoice_zero_reason is not None:
                self._fiscal_invoice_error(_(
                    'The monthly consolidated invoice cannot have a header zero-value reason.'
                ))
            accounting_key = 'individual:%s' % customer_info['request_id'] \
                if invoice['invoice_mode'] == 'individual_requested' else \
                'consolidated:%s:%s' % (company.id, invoice['local_month'])
            if accounting_key in accounting_scopes:
                self._fiscal_invoice_error(_('Fiscal invoice accounting scope appears more than once.'))
            accounting_scopes.add(accounting_key)
            invoice['_customer_info'] = customer_info
            invoice['_accounting_key'] = accounting_key
            seen_invoice_ids.add(invoice_id)

        included_sales = self._validate_fiscal_v2_included_sales(
            company, payload['local_month'], seen_sources, sale_ids,
        )

        products = self.env['product.product'].sudo().with_company(company).with_context(
            active_test=False,
        ).browse(list(product_ids)).exists()
        products_by_id = {product.id: product for product in products}
        sales = included_sales
        sales_by_id = {sale.id: sale for sale in sales}
        sale_lines = self.env['sale.order.line'].sudo().browse(list(sale_line_ids)).exists()
        sale_lines_by_id = {line.id: line for line in sale_lines}
        if (
            len(products_by_id) != len(product_ids) or len(sales_by_id) != len(sale_ids)
            or len(sale_lines_by_id) != len(sale_line_ids)
        ):
            self._fiscal_invoice_error(_('Fiscal invoice references an unknown product or source sale.'))
        existing_moves = self.env['account.move'].sudo().with_context(active_test=False).search([
            '|', ('softlife_fiscal_invoice_id', 'in', list(seen_invoice_ids)),
            ('softlife_fiscal_accounting_key', 'in', [
                invoice['_accounting_key'] for invoice in payload['invoices']
            ]),
        ])
        if existing_moves:
            self.env.cr.execute(
                'SELECT id FROM account_move WHERE id IN %s FOR UPDATE', [tuple(existing_moves.ids)],
            )
            existing_moves.invalidate_recordset()
        existing_by_invoice = {move.softlife_fiscal_invoice_id: move for move in existing_moves}
        existing_source_lines = self.env['account.move.line'].sudo().search([
            '|', ('softlife_fiscal_source_order_id', 'in', list(seen_sources)),
            ('softlife_fiscal_allocation_id', 'in', list(seen_allocations)),
        ])
        for invoice in payload['invoices']:
            partner = invoice['_customer_info']['partner']
            conflicting = existing_moves.filtered(
                lambda move: move.softlife_fiscal_accounting_key == invoice['_accounting_key']
                and move.softlife_fiscal_invoice_id != invoice['platform_invoice_id'])
            if conflicting:
                self._fiscal_invoice_error(_('Fiscal accounting scope is already owned by another invoice.'))
            for line, allocation in zip(invoice['lines'], invoice['allocations']):
                product = products_by_id[line['odoo_product_id']]
                sale = sales_by_id[allocation['odoo_sale_order_id']]
                sale_line = sale_lines_by_id[allocation['odoo_sale_order_line_id']]
                allowed_existing = existing_by_invoice.get(invoice['platform_invoice_id'])
                conflicting_source_lines = existing_source_lines.filtered(
                    lambda row: (
                        row.softlife_fiscal_source_order_id == allocation['source_order_id']
                        or row.softlife_fiscal_allocation_id == allocation['platform_allocation_id'])
                    and row.move_id != allowed_existing)
                if (
                    not product.active or not product.sale_ok or not product.softlife_recipe_id
                    or conflicting_source_lines
                ):
                    self._fiscal_invoice_error(_('Fiscal invoice source sales order does not match.'))
                sale_line = self._validate_fiscal_v2_supporting_sale(
                    invoice, line, allocation, company, sale, sale_line, allowed_existing,
                )
                line['_sale_line_id'] = sale_line.id
                line['_source_order_id'] = allocation['source_order_id']
                line['_supporting_sale_order_id'] = allocation['supporting_sale_order_id']
                line['_supporting_source_sha256'] = allocation[
                    'supporting_sale_order_source_sha256'
                ]
            existing_move = existing_by_invoice.get(invoice['platform_invoice_id'])
            if existing_move:
                if not partner:
                    self._fiscal_invoice_error(_(
                        'Existing fiscal invoice has no matching named customer identity.'
                    ))
                self._prepare_fiscal_v2_accounts(invoice, company, tax, contract, partner)
                self._validate_fiscal_invoice_move_v2(
                    existing_move, invoice, company, journal, tax, partner,
                )
                if existing_move.state != 'draft':
                    self._fiscal_invoice_error(_(
                        'Fiscal invoice draft creation cannot reuse a posted invoice.'
                    ))
        return configuration, tax, existing_by_invoice, payload

    @api.model
    def _prepare_fiscal_v2_accounts(self, invoice, company, tax, contract, partner):
        fiscal_position = self.env['account.fiscal.position'].sudo().with_company(
            company,
        )._get_fiscal_position(partner.with_company(company))
        for line in invoice['lines']:
            product = self.env['product.product'].sudo().with_company(company).browse(
                line['odoo_product_id']
            )
            account, taxes = self._effective_product_fiscal_configuration(
                product, company, fiscal_position,
            )
            if (
                not product.active or not product.sale_ok or not product.softlife_recipe_id
                or not account or account.account_type not in ('income', 'income_other')
                or account.code != str(contract['expected']['income_account_code'])
                or not self._account_applies_to_company(account, company) or taxes != tax
            ):
                self._fiscal_invoice_error(_(
                    'Fiscal invoice product mapping does not match the expected tax and income account.'
                ))
            line['account_id'] = account.id

    @api.model
    def _validate_fiscal_invoice_move_v2(self, move, invoice, company, journal, tax, partner):
        snapshot = copy.deepcopy(invoice)
        snapshot.pop('_customer_info')
        accounting_key = snapshot.pop('_accounting_key')
        for line in snapshot['lines']:
            line.pop('_sale_line_id', None)
            line.pop('_source_order_id', None)
            line.pop('_supporting_sale_order_id', None)
            line.pop('_supporting_source_sha256', None)
        if (
            move.softlife_fiscal_invoice_id != snapshot['platform_invoice_id']
            or move.softlife_fiscal_payload_sha256 != snapshot['invoice_payload_sha256']
            or move.softlife_fiscal_accounting_key != accounting_key
            or move.softlife_fiscal_invoice_snapshot != snapshot
            or move.company_id != company or move.journal_id != journal or move.partner_id != partner
            or move.move_type != 'out_invoice' or move.currency_id != company.currency_id
            or fields.Date.to_string(move.invoice_date) != snapshot['invoice_date']
            or fields.Date.to_string(move.date) != snapshot['invoice_date']
            or move.ref != snapshot['reference'] or move.state not in ('draft', 'posted')
        ):
            self._fiscal_invoice_error(_('Existing fiscal invoice does not match the frozen payload.'))
        lines_by_allocation = {
            line.softlife_fiscal_allocation_id: line
            for line in move.invoice_line_ids.filtered(lambda row: row.display_type == 'product')
        }
        if set(lines_by_allocation) != {
                line['platform_allocation_id'] for line in invoice['lines']}:
            self._fiscal_invoice_error(_('Existing fiscal invoice source lines do not match.'))
        for expected in invoice['lines']:
            line = lines_by_allocation[expected['platform_allocation_id']]
            if (
                line.softlife_fiscal_source_order_id != expected['_source_order_id']
                or line.softlife_supporting_sale_order_id != expected['_supporting_sale_order_id']
                or line.softlife_supporting_source_sha256
                != expected['_supporting_source_sha256']
                or line.product_id.id != expected['odoo_product_id']
                or line.name != expected['description'] or line.quantity != expected['quantity']
                or abs(line.price_unit - self._fiscal_invoice_price_unit(expected, tax.price_include)) > 1e-9
                or line.account_id.id != expected['account_id'] or line.tax_ids != tax
                or line.sale_line_ids.ids != [expected['_sale_line_id']]
                or self._amount_cents(line.price_total) != expected['gross_cents']
                or self._amount_cents(line.price_subtotal) != expected['tax_base_cents']
            ):
                self._fiscal_invoice_error(_('Existing fiscal invoice source line does not match.'))
        if self._amount_cents(move.amount_total) != invoice['expected_total_cents']:
            self._fiscal_invoice_error(_('Existing fiscal invoice total does not match.'))

    @api.model
    def _validate_fiscal_invoice_draft_payload(self, payload, contract):
        if isinstance(payload, dict) and payload.get('contract_version') == 2:
            return self._validate_fiscal_invoice_draft_payload_v2(payload, contract)
        if not isinstance(payload, dict) or set(payload) != {
                'contract_version', 'configuration_report_id', 'configuration_payload_sha256',
                'company', 'journal', 'customer', 'tax', 'invoices'}:
            self._fiscal_invoice_error(_('Fiscal invoice draft payload fields are invalid.'))
        if payload['contract_version'] != 1 or isinstance(payload['contract_version'], bool):
            self._fiscal_invoice_error(_('Fiscal invoice draft contract version is unsupported.'))
        if (
            not isinstance(payload['configuration_report_id'], str)
            or not payload['configuration_report_id'].strip()
            or not isinstance(payload['configuration_payload_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', payload['configuration_payload_sha256'])
        ):
            self._fiscal_invoice_error(_('Fiscal invoice configuration report identity is invalid.'))
        if not isinstance(payload['company'], dict) or set(payload['company']) != {
                'odoo_id', 'country_code', 'currency'}:
            self._fiscal_invoice_error(_('Fiscal invoice company fields are invalid.'))
        if not isinstance(payload['journal'], dict) or set(payload['journal']) != {'code'}:
            self._fiscal_invoice_error(_('Fiscal invoice journal fields are invalid.'))
        if not isinstance(payload['customer'], dict) or set(payload['customer']) != {'odoo_id'}:
            self._fiscal_invoice_error(_('Fiscal invoice customer fields are invalid.'))
        invoices = payload['invoices']
        if not isinstance(invoices, list) or not 1 <= len(invoices) <= 500:
            self._fiscal_invoice_error(_('Fiscal invoice draft requires 1 to 500 invoices.'))

        # Keep validation annotations out of the immutable queue payload supplied by the caller.
        payload = copy.deepcopy(payload)
        invoices = payload['invoices']

        configuration = self._current_fiscal_invoice_configuration(contract)
        company, journal, customer, expected_rate = configuration
        if not self._positive_int(payload['company']['odoo_id']) or payload['company'] != {
                'odoo_id': company.id, 'country_code': 'ES', 'currency': 'EUR'}:
            self._fiscal_invoice_error(_('Fiscal invoice company does not match Odoo.'))
        if (
            not isinstance(payload['journal']['code'], str)
            or not payload['journal']['code'].strip()
            or payload['journal']['code'] != journal.code
        ):
            self._fiscal_invoice_error(_('Fiscal invoice journal does not match the current contract.'))
        if (
            not self._positive_int(payload['customer']['odoo_id'])
            or payload['customer']['odoo_id'] != customer.id
        ):
            self._fiscal_invoice_error(_('Fiscal invoice customer is not the current final consumer.'))
        tax = self._validate_fiscal_invoice_tax(payload['tax'], company, expected_rate)

        invoice_ids = set()
        product_ids = set()
        for invoice in invoices:
            invoice_fields = {
                'platform_invoice_id', 'invoice_payload_sha256', 'move_type', 'invoice_date',
                'currency', 'reference', 'expected_total_cents', 'lines',
            }
            if not isinstance(invoice, dict) or set(invoice) not in (
                    invoice_fields, invoice_fields | {'zero_value_reason'}):
                self._fiscal_invoice_error(_('Fiscal invoice fields are invalid.'))
            platform_id = invoice['platform_invoice_id']
            try:
                valid_uuid = isinstance(platform_id, str) and str(uuid.UUID(platform_id)) == platform_id
            except (ValueError, AttributeError):
                valid_uuid = False
            invoice_hash = invoice['invoice_payload_sha256']
            frozen_invoice = dict(invoice)
            frozen_invoice.pop('invoice_payload_sha256', None)
            if (
                not valid_uuid or platform_id in invoice_ids
                or not isinstance(invoice_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', invoice_hash)
                or self._canonical_sha256(frozen_invoice) != invoice_hash
                or invoice['move_type'] != 'out_invoice' or invoice['currency'] != 'EUR'
                or not isinstance(invoice['reference'], str) or not invoice['reference'].strip()
                or not self._nonnegative_int(invoice['expected_total_cents'])
                or not isinstance(invoice['invoice_date'], str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', invoice['invoice_date'])
            ):
                self._fiscal_invoice_error(_('Fiscal invoice identity or header is invalid.'))
            try:
                if datetime.date.fromisoformat(invoice['invoice_date']).isoformat() != invoice['invoice_date']:
                    raise ValueError
            except ValueError:
                self._fiscal_invoice_error(_('Fiscal invoice date is invalid.'))
            lines = invoice['lines']
            zero_value_reason = invoice.get('zero_value_reason')
            if (
                zero_value_reason not in (None, 'free', 'admin_override', 'coupon')
                or (invoice['expected_total_cents'] == 0) != (zero_value_reason is not None)
            ):
                self._fiscal_invoice_error(_('Fiscal invoice zero-value reason is invalid.'))
            if not isinstance(lines, list) or not 1 <= len(lines) <= 100:
                self._fiscal_invoice_error(_('Fiscal invoice requires 1 to 100 lines.'))
            gross_total = 0
            for line in lines:
                if not isinstance(line, dict) or set(line) != {
                        'odoo_product_id', 'description', 'quantity', 'gross_cents',
                        'tax_base_cents', 'vat_cents', 'odoo_tax_id'}:
                    self._fiscal_invoice_error(_('Fiscal invoice line fields are invalid.'))
                if (
                    not self._positive_int(line['odoo_product_id'])
                    or not isinstance(line['description'], str) or not line['description'].strip()
                    or not self._positive_int(line['quantity'])
                    or not self._nonnegative_int(line['gross_cents'])
                    or not isinstance(line['tax_base_cents'], int)
                    or isinstance(line['tax_base_cents'], bool) or line['tax_base_cents'] < 0
                    or not isinstance(line['vat_cents'], int) or isinstance(line['vat_cents'], bool)
                    or line['vat_cents'] < 0 or not self._positive_int(line['odoo_tax_id'])
                    or line['odoo_tax_id'] != tax.id
                    or line['gross_cents'] != line['tax_base_cents'] + line['vat_cents']
                ):
                    self._fiscal_invoice_error(_('Fiscal invoice line values are invalid.'))
                rate = Decimal(str(tax.amount)) / Decimal(100)
                if tax.price_include:
                    computed_base = (Decimal(line['gross_cents']) / (Decimal(1) + rate)).quantize(
                        Decimal('1'), rounding=ROUND_HALF_UP,
                    )
                    computed_vat = Decimal(line['gross_cents']) - computed_base
                else:
                    computed_vat = (Decimal(line['tax_base_cents']) * rate).quantize(
                        Decimal('1'), rounding=ROUND_HALF_UP,
                    )
                if computed_vat != line['vat_cents']:
                    self._fiscal_invoice_error(_('Fiscal invoice line VAT cents are invalid.'))
                gross_total += line['gross_cents']
                product_ids.add(line['odoo_product_id'])
            if gross_total != invoice['expected_total_cents']:
                self._fiscal_invoice_error(_('Fiscal invoice line cents do not match its expected total.'))
            invoice_ids.add(platform_id)

        products = self.env['product.product'].sudo().with_company(company).with_context(
            active_test=False,
        ).browse(list(product_ids)).exists()
        products_by_id = {product.id: product for product in products}
        if len(products_by_id) != len(product_ids):
            self._fiscal_invoice_error(_('Fiscal invoice contains an unknown product.'))
        fiscal_position = self.env['account.fiscal.position'].sudo().with_company(
            company,
        )._get_fiscal_position(customer.with_company(company))
        for invoice in invoices:
            for line in invoice['lines']:
                product = products_by_id[line['odoo_product_id']]
                account, taxes = self._effective_product_fiscal_configuration(
                    product, company, fiscal_position,
                )
                if (
                    not product.active or not product.sale_ok or not product.softlife_recipe_id
                    or not account or account.account_type not in ('income', 'income_other')
                    or account.code != str(contract['expected']['income_account_code'])
                    or not self._account_applies_to_company(account, company) or taxes != tax
                ):
                    self._fiscal_invoice_error(_(
                        'Fiscal invoice product is inactive, unsaleable, unlinked, or fiscally misconfigured.'
                    ))
                line['account_id'] = account.id

        existing = self.env['account.move'].sudo().with_context(active_test=False).search([
            ('softlife_fiscal_invoice_id', 'in', list(invoice_ids)),
        ])
        existing_by_id = {move.softlife_fiscal_invoice_id: move for move in existing}
        for invoice in invoices:
            move = existing_by_id.get(invoice['platform_invoice_id'])
            if move:
                self._validate_fiscal_invoice_move(move, invoice, configuration, tax)
                if move.state != 'draft':
                    self._fiscal_invoice_error(_(
                        'Fiscal invoice draft creation cannot reuse a posted invoice.'
                    ))
        return configuration, tax, existing_by_id, payload

    @api.model
    def _create_fiscal_invoice_drafts_v2(self, payload, contract):
        configuration, tax, existing_by_id, payload = \
            self._validate_fiscal_invoice_draft_payload_v2(payload, contract)
        company, journal, _generic_customer, _rate = configuration
        results = []
        partners_by_customer = {}
        with self.env.cr.savepoint():
            for invoice in payload['invoices']:
                info = invoice['_customer_info']
                customer_id = info['request_id']
                partner = info['partner'] or partners_by_customer.get(customer_id)
                if not partner:
                    with _softlife_fiscal_partner_internal_scope():
                        partner = self.env['res.partner'].sudo().with_company(company).create(
                            info['values']
                        )
                    partners_by_customer[customer_id] = partner
                info['partner'] = partner
            for invoice in payload['invoices']:
                self._prepare_fiscal_v2_accounts(
                    invoice, company, tax, contract, invoice['_customer_info']['partner'],
                )
            for invoice in payload['invoices']:
                move = existing_by_id.get(invoice['platform_invoice_id'])
                created = not move
                partner = invoice['_customer_info']['partner']
                if not move:
                    line_commands = [Command.create({
                        'product_id': line['odoo_product_id'],
                        'name': line['description'], 'quantity': line['quantity'],
                        'price_unit': self._fiscal_invoice_price_unit(line, tax.price_include),
                        'account_id': line['account_id'], 'tax_ids': [Command.set(tax.ids)],
                        'sale_line_ids': [Command.link(line['_sale_line_id'])],
                        'softlife_fiscal_source_order_id': line['_source_order_id'],
                        'softlife_fiscal_allocation_id': line['platform_allocation_id'],
                        'softlife_supporting_sale_order_id': line['_supporting_sale_order_id'],
                        'softlife_supporting_source_sha256': line[
                            '_supporting_source_sha256'
                        ],
                    }) for line in invoice['lines']]
                    snapshot = copy.deepcopy(invoice)
                    snapshot.pop('_customer_info')
                    accounting_key = snapshot.pop('_accounting_key')
                    for line in snapshot['lines']:
                        line.pop('_sale_line_id')
                        line.pop('_source_order_id')
                        line.pop('_supporting_sale_order_id')
                        line.pop('_supporting_source_sha256')
                    with _softlife_fiscal_internal_scope():
                        move = self.env['account.move'].sudo().with_company(company).create({
                            'company_id': company.id, 'journal_id': journal.id,
                            'partner_id': partner.id, 'move_type': 'out_invoice',
                            'currency_id': company.currency_id.id,
                            'date': invoice['invoice_date'], 'invoice_date': invoice['invoice_date'],
                            'ref': invoice['reference'], 'invoice_line_ids': line_commands,
                            'softlife_fiscal_invoice_id': invoice['platform_invoice_id'],
                            'softlife_fiscal_payload_sha256': invoice['invoice_payload_sha256'],
                            'softlife_fiscal_invoice_snapshot': snapshot,
                            'softlife_fiscal_accounting_key': accounting_key,
                        })
                self._validate_fiscal_invoice_move_v2(
                    move, invoice, company, journal, tax, partner,
                )
                if move.state != 'draft':
                    self._fiscal_invoice_error(_(
                        'Fiscal invoice draft creation cannot reuse a posted invoice.'
                    ))
                results.append({
                    'platform_invoice_id': invoice['platform_invoice_id'],
                    'odoo_move_id': move.id,
                    'invoice_payload_sha256': invoice['invoice_payload_sha256'],
                    'state': move.state, 'created': created,
                })
        return {
            'accepted': True,
            'summary': _('Created or verified %s source-bound fiscal invoice draft(s).') % len(results),
            'contract_version': 2,
            'invoices': results,
        }

    @api.model
    def _create_fiscal_invoice_drafts(self, payload):
        if not self._fiscal_invoice_draft_creation_enabled():
            raise SoftlifeAPIError(
                _('Fiscal invoice draft creation is disabled in Odoo Settings.'),
                code='fiscal_invoice_draft_creation_disabled',
            )
        contract = self._api_request('GET', '/api/internal/odoo/fiscal-configuration')
        if isinstance(payload, dict) and payload.get('contract_version') == 2:
            return self._create_fiscal_invoice_drafts_v2(payload, contract)
        configuration, tax, existing_by_id, payload = self._validate_fiscal_invoice_draft_payload(
            payload, contract,
        )
        company, journal, customer, _rate = configuration
        results = []
        with self.env.cr.savepoint():
            for invoice in payload['invoices']:
                move = existing_by_id.get(invoice['platform_invoice_id'])
                created = not move
                if not move:
                    line_commands = []
                    for line in invoice['lines']:
                        line_commands.append((0, 0, {
                            'product_id': line['odoo_product_id'],
                            'name': line['description'],
                            'quantity': line['quantity'],
                            'price_unit': self._fiscal_invoice_price_unit(line, tax.price_include),
                            'account_id': line['account_id'],
                            'tax_ids': [(6, 0, tax.ids)],
                        }))
                    with _softlife_fiscal_internal_scope():
                        move = self.env['account.move'].sudo().with_company(company).create({
                            'company_id': company.id,
                            'journal_id': journal.id,
                            'partner_id': customer.id,
                            'move_type': 'out_invoice',
                            'currency_id': company.currency_id.id,
                            'date': invoice['invoice_date'],
                            'invoice_date': invoice['invoice_date'],
                            'ref': invoice['reference'],
                            'invoice_line_ids': line_commands,
                            'softlife_fiscal_invoice_id': invoice['platform_invoice_id'],
                            'softlife_fiscal_payload_sha256': invoice['invoice_payload_sha256'],
                            'softlife_fiscal_invoice_snapshot': invoice,
                        })
                    self._validate_fiscal_invoice_move(move, invoice, configuration, tax)
                results.append({
                    'platform_invoice_id': invoice['platform_invoice_id'],
                    'odoo_move_id': move.id,
                    'invoice_payload_sha256': invoice['invoice_payload_sha256'],
                    'state': move.state,
                    'created': created,
                })
        return {
            'accepted': True,
            'summary': _('Created or verified %s fiscal invoice draft(s).') % len(results),
            'contract_version': 1,
            'invoices': results,
        }

    @api.model
    def _confirm_fiscal_invoices(self, payload):
        if not self._fiscal_invoice_confirmation_enabled():
            raise SoftlifeAPIError(
                _('Fiscal invoice confirmation is disabled in Odoo Settings.'),
                code='fiscal_invoice_confirmation_disabled',
            )
        if not isinstance(payload, dict) or set(payload) != {'contract_version', 'company', 'invoices'}:
            self._fiscal_invoice_error(_('Fiscal invoice confirmation payload fields are invalid.'))
        if payload['contract_version'] not in (1, 2) or isinstance(payload['contract_version'], bool):
            self._fiscal_invoice_error(_('Fiscal invoice confirmation contract version is unsupported.'))
        if not isinstance(payload['company'], dict) or set(payload['company']) != {'odoo_id'}:
            self._fiscal_invoice_error(_('Fiscal invoice confirmation company fields are invalid.'))
        invoices = payload['invoices']
        if not isinstance(invoices, list) or not 1 <= len(invoices) <= 500:
            self._fiscal_invoice_error(_('Fiscal invoice confirmation requires 1 to 500 invoices.'))
        contract = self._api_request('GET', '/api/internal/odoo/fiscal-configuration')
        if payload['contract_version'] == 2:
            self._require_fiscal_v2_zero_value_capability(contract)
        configuration = self._current_fiscal_invoice_configuration(contract)
        company, journal, customer, _rate = configuration
        if (
            not self._positive_int(payload['company']['odoo_id'])
            or payload['company']['odoo_id'] != company.id
        ):
            self._fiscal_invoice_error(_('Fiscal invoice confirmation company does not match Odoo.'))
        platform_ids = set()
        move_ids = set()
        for row in invoices:
            if not isinstance(row, dict) or set(row) != {
                    'platform_invoice_id', 'odoo_move_id', 'invoice_payload_sha256'}:
                self._fiscal_invoice_error(_('Fiscal invoice confirmation fields are invalid.'))
            try:
                valid_uuid = (
                    isinstance(row['platform_invoice_id'], str)
                    and str(uuid.UUID(row['platform_invoice_id'])) == row['platform_invoice_id']
                )
            except (ValueError, AttributeError):
                valid_uuid = False
            if (
                not valid_uuid or row['platform_invoice_id'] in platform_ids
                or not self._positive_int(row['odoo_move_id']) or row['odoo_move_id'] in move_ids
                or not isinstance(row['invoice_payload_sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', row['invoice_payload_sha256'])
            ):
                self._fiscal_invoice_error(_('Fiscal invoice confirmation identity is invalid.'))
            platform_ids.add(row['platform_invoice_id'])
            move_ids.add(row['odoo_move_id'])
        if payload['contract_version'] == 2:
            provisional_moves = self.env['account.move'].sudo().browse(list(move_ids)).exists()
            if set(provisional_moves.ids) != move_ids:
                self._fiscal_invoice_error(_('Fiscal invoice confirmation contains an unknown move.'))
            local_months = set()
            for move in provisional_moves:
                snapshot = move.softlife_fiscal_invoice_snapshot
                local_month = snapshot.get('local_month') if isinstance(snapshot, dict) else None
                if not isinstance(local_month, str) or not re.fullmatch(
                        r'\d{4}-(0[1-9]|1[0-2])', local_month):
                    self._fiscal_invoice_error(_('Stored fiscal invoice month is invalid.'))
                local_months.add(local_month)
            for local_month in sorted(local_months):
                scope_month = datetime.date.fromisoformat(local_month + '-01')
                softlife_company_month_advisory_lock(
                    self.env.cr, company.id, scope_month,
                )
        self.env.cr.execute(
            'SELECT id FROM account_move WHERE id IN %s FOR UPDATE',
            [tuple(move_ids)],
        )
        locked_ids = {row[0] for row in self.env.cr.fetchall()}
        if locked_ids != move_ids:
            self._fiscal_invoice_error(_('Fiscal invoice confirmation contains an unknown move.'))
        moves = self.env['account.move'].sudo().browse(list(move_ids))
        moves.invalidate_recordset()
        moves_by_id = {move.id: move for move in moves}
        ordered = []
        snapshots = {}
        v2_coverage = {}
        for row in invoices:
            move = moves_by_id[row['odoo_move_id']]
            if (
                move.softlife_fiscal_invoice_id != row['platform_invoice_id']
                or move.softlife_fiscal_payload_sha256 != row['invoice_payload_sha256']
                or move.company_id != company or move.journal_id != journal
                or payload['contract_version'] == 1 and move.partner_id != customer
                or move.move_type != 'out_invoice'
                or move.currency_id != company.currency_id or move.state not in ('draft', 'posted')
            ):
                self._fiscal_invoice_error(_('Fiscal invoice confirmation move does not match Odoo.'))
            snapshot = self._validate_fiscal_invoice_snapshot(move)
            tax_ids = {line.get('odoo_tax_id') for line in snapshot.get('lines', [])}
            if len(tax_ids) != 1:
                self._fiscal_invoice_error(_('Stored fiscal invoice snapshot tax is invalid.'))
            tax = self.env['account.tax'].sudo().with_context(active_test=False).browse(
                tax_ids.pop(),
            ).exists()
            if not tax:
                self._fiscal_invoice_error(_('Stored fiscal invoice snapshot tax is unavailable.'))
            tax = self._validate_fiscal_invoice_tax({
                'odoo_tax_id': tax.id,
                'rate': tax.amount,
                'country_code': tax.country_id.code if tax.country_id else None,
                'type_tax_use': tax.type_tax_use,
                'amount_type': tax.amount_type,
                'price_include': bool(tax.price_include),
            }, company, configuration[3])
            if payload['contract_version'] == 2:
                if snapshot.get('invoice_mode') not in (
                        'individual_requested', 'monthly_consolidated'):
                    self._fiscal_invoice_error(_('Fiscal invoice confirmation contract does not match its snapshot.'))
                customer_info = self._prepare_fiscal_v2_customer(
                    snapshot.get('customer'), customer, company,
                )
                if not customer_info['partner'] or customer_info['partner'] != move.partner_id:
                    self._fiscal_invoice_error(_('Fiscal invoice named customer no longer matches Odoo.'))
                prepared_snapshot = copy.deepcopy(snapshot)
                if len(prepared_snapshot.get('lines', [])) != len(
                        prepared_snapshot.get('allocations', [])):
                    self._fiscal_invoice_error(_('Stored fiscal invoice allocation coverage is invalid.'))
                scope = (company.id, prepared_snapshot.get('local_month'))
                supporting_sale_ids = {
                    allocation.get('odoo_sale_order_id')
                    for allocation in prepared_snapshot['allocations']
                }
                if supporting_sale_ids:
                    self.env.cr.execute(
                        'SELECT id FROM sale_order WHERE id IN %s ORDER BY id FOR UPDATE',
                        [tuple(sorted(supporting_sale_ids))],
                    )
                for line, allocation in zip(
                        prepared_snapshot.get('lines', []),
                        prepared_snapshot.get('allocations', [])):
                    sale_line = self._validate_fiscal_v2_supporting_sale(
                        prepared_snapshot, line, allocation, company, allowed_move=move,
                    )
                    line['_sale_line_id'] = sale_line.id
                    line['_source_order_id'] = allocation['source_order_id']
                    line['_supporting_sale_order_id'] = allocation['supporting_sale_order_id']
                    line['_supporting_source_sha256'] = allocation[
                        'supporting_sale_order_source_sha256'
                    ]
                    coverage = v2_coverage.setdefault(scope, {
                        'source_ids': set(), 'sale_ids': set(),
                    })
                    if allocation['source_order_id'] in coverage['source_ids']:
                        self._fiscal_invoice_error(_(
                            'Fiscal invoice confirmation contains duplicate source coverage.'
                        ))
                    coverage['source_ids'].add(allocation['source_order_id'])
                    coverage['sale_ids'].add(allocation['odoo_sale_order_id'])
                prepared_snapshot['_customer_info'] = {'partner': move.partner_id}
                prepared_snapshot['_accounting_key'] = move.softlife_fiscal_accounting_key
                self._prepare_fiscal_v2_accounts(
                    prepared_snapshot, company, tax, contract, move.partner_id,
                )
                self._validate_fiscal_invoice_move_v2(
                    move, prepared_snapshot, company, journal, tax, move.partner_id,
                )
            else:
                if snapshot.get('invoice_mode'):
                    self._fiscal_invoice_error(_('Fiscal invoice confirmation contract does not match its snapshot.'))
                self._validate_fiscal_invoice_move(move, snapshot, configuration, tax)
            snapshots[move.id] = (snapshot, tax)
            ordered.append(move)
        for scope, coverage in v2_coverage.items():
            self._validate_fiscal_v2_included_sales(
                company, scope[1], coverage['source_ids'], coverage['sale_ids'],
            )
        was_draft = {move.id: move.state == 'draft' for move in ordered}
        with self.env.cr.savepoint():
            drafts = self.env['account.move'].browse([
                move.id for move in ordered if was_draft[move.id]
            ])
            if drafts:
                with _softlife_fiscal_internal_scope():
                    drafts.action_post()
            for move in ordered:
                snapshot, tax = snapshots[move.id]
                if payload['contract_version'] == 2:
                    prepared_snapshot = copy.deepcopy(snapshot)
                    for line, allocation in zip(
                            prepared_snapshot.get('lines', []),
                            prepared_snapshot.get('allocations', [])):
                        sale_line = self._validate_fiscal_v2_supporting_sale(
                            prepared_snapshot, line, allocation, company, allowed_move=move,
                        )
                        line['_sale_line_id'] = sale_line.id
                        line['_source_order_id'] = allocation['source_order_id']
                        line['_supporting_sale_order_id'] = allocation['supporting_sale_order_id']
                        line['_supporting_source_sha256'] = allocation[
                            'supporting_sale_order_source_sha256'
                        ]
                    prepared_snapshot['_customer_info'] = {'partner': move.partner_id}
                    prepared_snapshot['_accounting_key'] = move.softlife_fiscal_accounting_key
                    self._prepare_fiscal_v2_accounts(
                        prepared_snapshot, company, tax, contract, move.partner_id,
                    )
                    self._validate_fiscal_invoice_move_v2(
                        move, prepared_snapshot, company, journal, tax, move.partner_id,
                    )
                else:
                    self._validate_fiscal_invoice_move(move, snapshot, configuration, tax)
                if move.state != 'posted' or not move.name:
                    self._fiscal_invoice_error(_(
                        'Fiscal invoice confirmation did not post every listed move.'
                    ))
        return {
            'accepted': True,
            'summary': _('Confirmed or verified %s fiscal invoice(s).') % len(ordered),
            'contract_version': payload['contract_version'],
            'invoices': [{
                'platform_invoice_id': row['platform_invoice_id'],
                'odoo_move_id': move.id,
                'invoice_payload_sha256': row['invoice_payload_sha256'],
                'state': 'posted',
                'name': move.name,
                'confirmed': was_draft[move.id],
            } for row, move in zip(invoices, ordered)],
        }

    @api.model
    def _link_fiscal_invoices_to_sales(self, payload):
        if not isinstance(payload, dict) or set(payload) != {
                'contract_version', 'local_month', 'links'}:
            self._fiscal_invoice_error(_('Fiscal sale-link payload fields are invalid.'))
        if payload['contract_version'] != 1 or isinstance(payload['contract_version'], bool):
            self._fiscal_invoice_error(_('Fiscal sale-link contract version is unsupported.'))
        if not isinstance(payload['local_month'], str) or not re.fullmatch(
                r'\d{4}-(0[1-9]|1[0-2])', payload['local_month']):
            self._fiscal_invoice_error(_('Fiscal sale-link month is invalid.'))
        links = payload['links']
        if not isinstance(links, list) or not 1 <= len(links) <= 500:
            self._fiscal_invoice_error(_('Fiscal sale-link request requires 1 to 500 links.'))

        invoice_ids = set()
        move_ids = set()
        sale_ids = set()
        for link in links:
            required = {
                'platform_invoice_id', 'odoo_move_id',
                'source_order_id', 'export_id', 'recipe_version_id', 'odoo_warehouse_id',
                'odoo_sale_order_id', 'odoo_product_id', 'quantity',
            }
            if not isinstance(link, dict) or set(link) != required:
                self._fiscal_invoice_error(_('Fiscal sale-link row fields are invalid.'))
            try:
                valid_invoice_id = str(uuid.UUID(link['platform_invoice_id'])) == link['platform_invoice_id']
                valid_source_id = str(uuid.UUID(link['source_order_id'])) == link['source_order_id']
                valid_export_id = str(uuid.UUID(link['export_id'])) == link['export_id']
                valid_recipe_version_id = str(uuid.UUID(
                    link['recipe_version_id'])) == link['recipe_version_id']
            except (ValueError, TypeError, AttributeError):
                valid_invoice_id = valid_source_id = valid_export_id = \
                    valid_recipe_version_id = False
            if (
                not valid_invoice_id or not valid_source_id or not valid_export_id
                or not valid_recipe_version_id
                or link['platform_invoice_id'] in invoice_ids
                or not self._positive_int(link['odoo_move_id'])
                or link['odoo_move_id'] in move_ids
                or not self._positive_int(link['odoo_warehouse_id'])
                or not self._positive_int(link['odoo_sale_order_id'])
                or not self._positive_int(link['odoo_product_id'])
                or not self._positive_int(link['quantity'])
            ):
                self._fiscal_invoice_error(_('Fiscal sale-link identity is invalid.'))
            invoice_ids.add(link['platform_invoice_id'])
            move_ids.add(link['odoo_move_id'])
            sale_ids.add(link['odoo_sale_order_id'])

        self.env.cr.execute('SELECT id FROM account_move WHERE id IN %s FOR UPDATE', [tuple(move_ids)])
        if {row[0] for row in self.env.cr.fetchall()} != move_ids:
            self._fiscal_invoice_error(_('Fiscal sale-link request contains an unknown invoice.'))
        self.env.cr.execute('SELECT id FROM sale_order WHERE id IN %s FOR UPDATE', [tuple(sale_ids)])
        if {row[0] for row in self.env.cr.fetchall()} != sale_ids:
            self._fiscal_invoice_error(_('Fiscal sale-link request contains an unknown sales order.'))

        moves = self.env['account.move'].sudo().browse(list(move_ids))
        sales = self.env['sale.order'].sudo().browse(list(sale_ids))
        moves.invalidate_recordset()
        sales.invalidate_recordset()
        moves_by_id = {move.id: move for move in moves}
        sales_by_id = {sale.id: sale for sale in sales}
        prepared = []
        planned_by_line = {}
        for link in links:
            move = moves_by_id[link['odoo_move_id']]
            sale = sales_by_id[link['odoo_sale_order_id']]
            if (
                move.softlife_fiscal_invoice_id != link['platform_invoice_id']
                or move.state != 'posted' or move.move_type != 'out_invoice'
                or sale.state not in ('sale', 'done')
                or sale.softlife_export_id != link['export_id']
                or sale.softlife_warehouse_id.id != link['odoo_warehouse_id']
                or sale.company_id != move.company_id or sale.currency_id != move.currency_id
                or sale.partner_id != move.partner_id
            ):
                self._fiscal_invoice_error(_('Fiscal invoice and sales order identities do not match.'))
            snapshot = self._validate_fiscal_invoice_snapshot(move)
            if not str(snapshot.get('invoice_date') or '').startswith(payload['local_month'] + '-'):
                self._fiscal_invoice_error(_('Fiscal invoice date does not match the reconciliation month.'))
            sources = sale.softlife_source_orders or []
            if link['source_order_id'] not in {
                    str(source.get('platform_order_id') or '')
                    for source in sources if isinstance(source, dict)}:
                self._fiscal_invoice_error(_('Sales order does not contain the fiscal invoice source order.'))
            invoice_lines = move.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
            if len(invoice_lines) != 1:
                self._fiscal_invoice_error(_('Fiscal invoice must contain exactly one product line.'))
            invoice_line = invoice_lines[0]
            sale_lines = sale.order_line.filtered(
                lambda line: not line.display_type and not line.softlife_rounding_adjustment
                and line.product_id.id == link['odoo_product_id'])
            if (
                len(sale_lines) != 1
                or invoice_line.product_id.id != link['odoo_product_id']
                or invoice_line.quantity != link['quantity']
            ):
                self._fiscal_invoice_error(_('Fiscal invoice product or quantity does not match one sales line.'))
            sale_line = sale_lines[0]
            if invoice_line.sale_line_ids and invoice_line.sale_line_ids != sale_line:
                self._fiscal_invoice_error(_('Fiscal invoice line is already linked to another sales line.'))
            planned_by_line.setdefault(sale_line, []).append(invoice_line)
            prepared.append((link, move, sale, invoice_line, sale_line, bool(invoice_line.sale_line_ids)))

        for sale_line, invoice_lines in planned_by_line.items():
            existing = sale_line.invoice_lines.filtered(lambda line: line.move_id.state != 'cancel')
            planned = self.env['account.move.line'].browse([line.id for line in invoice_lines]) - existing
            combined = existing | planned
            if combined.filtered(
                    lambda line: line.product_uom_id.category_id != sale_line.product_uom.category_id):
                self._fiscal_invoice_error(_('Fiscal invoice and sales line units of measure are incompatible.'))
            invoiced = sum(
                (-1 if line.move_id.move_type == 'out_refund' else 1)
                * line.product_uom_id._compute_quantity(
                    line.quantity, sale_line.product_uom, round=False)
                for line in combined
                if line.move_id.move_type in ('out_invoice', 'out_refund')
            )
            if float_compare(
                    invoiced, sale_line.product_uom_qty,
                    precision_rounding=sale_line.product_uom.rounding) > 0:
                self._fiscal_invoice_error(_('Fiscal invoice links would exceed the sales-order quantity.'))

        with self.env.cr.savepoint(), _softlife_fiscal_internal_scope():
            for _link, _move, _sale, invoice_line, sale_line, already_linked in prepared:
                if not already_linked:
                    invoice_line.write({'sale_line_ids': [Command.link(sale_line.id)]})
            sales.invalidate_recordset(['invoice_ids', 'invoice_status'])
            sales.mapped('order_line').invalidate_recordset([
                'invoice_lines', 'qty_invoiced', 'qty_to_invoice', 'invoice_status',
            ])
            results = []
            for link, move, sale, invoice_line, sale_line, already_linked in prepared:
                if invoice_line.sale_line_ids != sale_line or move not in sale.invoice_ids:
                    self._fiscal_invoice_error(_('Odoo did not retain the fiscal invoice sales link.'))
                results.append({
                    'platform_invoice_id': link['platform_invoice_id'],
                    'odoo_move_id': move.id,
                    'odoo_sale_order_id': sale.id,
                    'odoo_sale_order_line_id': sale_line.id,
                    'linked': True,
                    'already_linked': already_linked,
                    'sale_order_status': sale.invoice_status,
                })
        fully_invoiced = len(sales.filtered(lambda sale: sale.invoice_status == 'invoiced'))
        return {
            'accepted': True,
            'summary': _(
                'Linked %(invoices)s existing fiscal invoice(s) to sales orders; '
                '%(orders)s of %(total)s affected sales orders are fully invoiced.',
                invoices=len(results), orders=fully_invoiced, total=len(sales),
            ),
            'contract_version': 1,
            'links': results,
        }

    @api.model
    def _rest_get(self, table, params=None):
        import requests
        base = self._param('softlife.sync.supabase_url').rstrip('/')
        key = self._param('softlife.sync.supabase_key')
        url = f'{base}/rest/v1/{table}'
        headers = {'apikey': key, 'Authorization': f'Bearer {key}'}
        r = requests.get(url, headers=headers, params=params or {}, timeout=60)
        if r.status_code != 200:
            raise UserError(_('Supabase GET %s failed: %s %s') % (table, r.status_code, r.text[:200]))
        return r.json()

    @api.model
    def _rest_upsert(self, table, rows, on_conflict):
        """Bulk upsert rows into a Supabase table, keyed on `on_conflict` (a column name)."""
        import requests
        if not rows:
            return
        base = self._param('softlife.sync.supabase_url').rstrip('/')
        key = self._param('softlife.sync.supabase_key')
        url = f'{base}/rest/v1/{table}'
        headers = {
            'apikey': key,
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
            'Prefer': 'resolution=merge-duplicates,return=minimal',
        }
        r = requests.post(url, headers=headers, params={'on_conflict': on_conflict}, json=rows, timeout=60)
        if r.status_code not in (200, 201, 204):
            raise UserError(_('Supabase upsert %s failed: %s %s') % (table, r.status_code, r.text[:200]))

    @api.model
    def _rest_delete_missing(self, table, id_column, present_ids, retain_referenced=False):
        """Delete rows from a Supabase table whose id_column isn't in present_ids —
        i.e. records removed/archived on the Odoo side since the last sync.
        No-ops on an empty present_ids so a transient empty read can't wipe the table.

        Product identities frozen into immutable recipe versions must remain in the
        mirror. For those rows, prune individually and retain only FK-blocked rows.
        """
        import requests
        if not present_ids:
            return
        base = self._param('softlife.sync.supabase_url').rstrip('/')
        key = self._param('softlife.sync.supabase_key')
        url = f'{base}/rest/v1/{table}'
        headers = {'apikey': key, 'Authorization': f'Bearer {key}', 'Prefer': 'return=minimal'}
        if retain_referenced:
            mirrored = self._rest_get(table, {'select': id_column, 'limit': 10000})
            present = set(present_ids)
            missing = [row.get(id_column) for row in mirrored if row.get(id_column) not in present]
            for missing_id in missing:
                r = requests.delete(url, headers=headers, params={id_column: f'eq.{missing_id}'}, timeout=60)
                if r.status_code in (200, 204):
                    continue
                try:
                    error_code = r.json().get('code')
                except (TypeError, ValueError):
                    error_code = None
                if r.status_code == 409 and error_code == '23503':
                    _logger.info(
                        'softlife_sync: retained historical %s row %s=%s because it is still referenced',
                        table, id_column, missing_id,
                    )
                    continue
                raise UserError(_('Supabase delete-missing %s failed: %s %s') % (table, r.status_code, r.text[:200]))
            return
        ids_csv = ','.join(str(i) for i in present_ids)
        r = requests.delete(url, headers=headers, params={id_column: f'not.in.({ids_csv})'}, timeout=60)
        if r.status_code not in (200, 204):
            raise UserError(_('Supabase delete-missing %s failed: %s %s') % (table, r.status_code, r.text[:200]))

    @api.model
    def _rest_rpc(self, function, payload):
        import requests
        base = self._param('softlife.sync.supabase_url').rstrip('/')
        key = self._param('softlife.sync.supabase_key')
        r = requests.post(
            f'{base}/rest/v1/rpc/{function}',
            headers={'apikey': key, 'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json=payload, timeout=60,
        )
        if r.status_code not in (200, 201, 204):
            raise UserError(_('Supabase RPC %s failed: %s %s') % (function, r.status_code, r.text[:200]))

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------
    @api.model
    def _acquire_sync_lock(self):
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s)', [self._sync_lock_id])
        return bool(self.env.cr.fetchone()[0])

    @api.model
    def sync_partners(self):
        rows = self._rest_get('tenants', {'select': 'id,name,kind'})
        Partner = self.env['res.partner']
        n = 0
        for row in rows:
            sid = row.get('id')
            if not sid:
                continue
            vals = {'name': row.get('name') or 'SoftLife tenant', 'supabase_id': sid, 'is_company': True}
            existing = Partner.search([('supabase_id', '=', sid)], limit=1)
            if existing:
                existing.write(vals)
            else:
                Partner.create(vals)
            n += 1
        return n

    @api.model
    def sync_products(self):
        """Platform -> Odoo. Creates/updates product.template by supabase_id.
        Does NOT touch products.odoo_id — linking a platform ingredient to an
        Odoo SKU is a deliberate choice made on the platform (see /odoo and
        /products), never inferred automatically. An earlier version of this
        method auto-linked by writing back the newly-created product's id,
        which silently created duplicate Odoo products for ingredients that
        already had a real match under a different id (matched by name only
        in the human's head, not by any field this code could see) and linked
        to the wrong one. Don't repeat that."""
        rows = self._rest_get('products', {'select': 'id,name,type'})
        Template = self.env['product.template']
        n = 0
        for row in rows:
            sid = row.get('id')
            if not sid:
                continue
            vals = {'name': row.get('name') or 'SoftLife product', 'supabase_id': sid, 'type': 'consu'}
            existing = Template.search([('supabase_id', '=', sid)], limit=1)
            if existing:
                existing.write(vals)
            else:
                Template.create(vals)
            n += 1
        return n

    # ------------------------------------------------------------------
    # Odoo -> Supabase (master-data mirror the platform reads)
    # ------------------------------------------------------------------
    @api.model
    def sync_odoo_warehouses(self):
        Warehouse = self.env['stock.warehouse']
        rows = [
            {'odoo_id': w.id, 'name': w.name, 'code': w.code,
             'stock_location_id': w.lot_stock_id.id if w.lot_stock_id else None}
            for w in Warehouse.search([])
        ]
        self._rest_upsert('odoo_warehouses', rows, on_conflict='odoo_id')
        self._rest_delete_missing('odoo_warehouses', 'odoo_id', [r['odoo_id'] for r in rows])
        return len(rows)

    @api.model
    def sync_odoo_products(self):
        Product = self.env['product.product']
        rows = []
        for p in Product.search([('active', '=', True)]):
            rows.append({
                'odoo_id': p.id,
                'name': p.display_name,
                'sku': p.default_code or None,
                'barcode': p.barcode or None,
                'category': p.categ_id.display_name if p.categ_id else None,
                'uom': p.uom_id.name if p.uom_id else None,
                'uom_rounding': p.uom_id.rounding if p.uom_id else 0.01,
                'tracking': p.tracking,
                'qty_available': p.qty_available,
                'package_content_quantity': p.package_content_quantity or None,
                'package_content_uom': p.package_content_uom or None,
            })
        self._rest_upsert('odoo_products', rows, on_conflict='odoo_id')
        # Delete archived products only when no immutable recipe or production
        # record still depends on their historical Odoo identity.
        self._rest_delete_missing(
            'odoo_products', 'odoo_id', [r['odoo_id'] for r in rows],
            retain_referenced=True,
        )
        return len(rows)

    @api.model
    def sync_odoo_lots(self):
        # lot.location_id / product_qty are Odoo's own computed snapshot of where
        # a lot currently sits and how much remains (aggregated across quants).
        # A lot split across multiple locations collapses to one row here —
        # fine for a "what lots exist and roughly where" mirror, not for
        # location-level stock accounting.
        Lot = self.env['stock.lot']
        rows = []
        for lot in Lot.search([]):
            warehouse = lot.location_id.warehouse_id if lot.location_id else None
            rows.append({
                'odoo_id': lot.id,
                'name': lot.name,
                'odoo_product_id': lot.product_id.id or None,
                'product_name': lot.product_id.display_name if lot.product_id else None,
                'qty': lot.product_qty,
                'expiration_date': lot.expiration_date.date().isoformat() if lot.expiration_date else None,
                'odoo_warehouse_id': warehouse.id if warehouse else None,
                'warehouse_name': warehouse.name if warehouse else None,
            })
        self._rest_upsert('odoo_lots', rows, on_conflict='odoo_id')
        self._rest_delete_missing('odoo_lots', 'odoo_id', [r['odoo_id'] for r in rows])
        return len(rows)

    @api.model
    def sync_odoo_lot_stock(self):
        reflected_references = []
        for picking in self.env['stock.picking'].search([
            ('state', '=', 'done'),
            ('softlife_stock_movement_group_id', '!=', False),
        ]):
            reflected_references.extend(filter(None, (
                picking.softlife_stock_movement_out_reference,
                picking.softlife_stock_movement_in_reference,
            )))
        lot_quantities = {}
        product_quantities = {}
        for quant in self.env['stock.quant'].search([
            ('location_id.usage', '=', 'internal'),
            ('product_id.active', '=', True),
        ]):
            warehouse = quant.location_id.warehouse_id
            if not warehouse:
                continue
            product_key = (quant.product_id.id, warehouse.id)
            current = product_quantities.get(product_key, [0.0, 0.0, 0.0])
            current[0] += quant.quantity
            current[1] += quant.reserved_quantity
            current[2] = max(0.0, current[0] - current[1])
            product_quantities[product_key] = current
            if quant.lot_id:
                lot_key = (quant.lot_id.id, warehouse.id)
                lot_current = lot_quantities.get(lot_key, [0.0, 0.0])
                lot_current[0] += quant.quantity
                lot_current[1] += quant.reserved_quantity
                lot_quantities[lot_key] = lot_current
        rows = [
            {
                'odoo_lot_id': lot_id,
                'odoo_warehouse_id': warehouse_id,
                'qty': quantities[0],
                'available_qty': max(0.0, quantities[0] - quantities[1]),
            }
            for (lot_id, warehouse_id), quantities in sorted(lot_quantities.items())
            if all(math.isfinite(quantity) for quantity in quantities) and quantities[0] > 0
        ]
        product_rows = [
            {
                'odoo_product_id': product_id,
                'odoo_warehouse_id': warehouse_id,
                'quantity': quantities[0],
                'reserved_quantity': quantities[1],
                'available_quantity': quantities[2],
            }
            for (product_id, warehouse_id), quantities in sorted(product_quantities.items())
            if all(math.isfinite(quantity) for quantity in quantities) and quantities[0] >= 0
        ]
        self._api_request(
            'POST',
            '/api/internal/odoo/lot-stock-snapshot',
            payload={
                'rows': rows, 'product_rows': product_rows,
                'reflected_references': sorted(set(reflected_references)),
                'observed_at': fields.Datetime.now().isoformat() + 'Z',
            },
        )
        return len(product_rows)

    @api.model
    def _stock_movement_worker(self):
        identity = self._param('database.uuid') or self.env.cr.dbname
        return 'softlife-odoo:%s' % identity

    @api.model
    def _group_claimed_stock_movements(self, movements):
        if not isinstance(movements, list):
            raise SoftlifeAPIError(_('Platform returned invalid stock movements.'), code='invalid_response')
        groups = OrderedDict()
        seen_ids = set()
        for movement in movements:
            if not isinstance(movement, dict):
                raise SoftlifeAPIError(_('Platform returned an invalid stock movement.'), code='invalid_response')
            movement_id = movement.get('movement_id')
            lease_token = movement.get('lease_token')
            if (
                not isinstance(movement_id, str) or not movement_id.strip()
                or movement_id in seen_ids
                or not isinstance(lease_token, str) or not lease_token.strip()
            ):
                raise SoftlifeAPIError(_('Platform stock movement identity is invalid.'), code='invalid_response')
            seen_ids.add(movement_id)
            operation_id = movement.get('movement_group_id') or movement_id
            if not isinstance(operation_id, str) or not operation_id.strip():
                raise SoftlifeAPIError(_('Platform stock movement group is invalid.'), code='invalid_response')
            groups.setdefault(operation_id, []).append(movement)
        for rows in groups.values():
            if len({row['lease_token'] for row in rows}) != 1:
                raise SoftlifeAPIError(
                    _('Platform stock movement group has inconsistent leases.'),
                    code='invalid_response',
                )
        return list(groups.values())

    @api.model
    def _stock_transfer_values(self, rows):
        if len(rows) != 2 or {row.get('movement_kind') for row in rows} != {
                'transfer_out', 'transfer_in'}:
            kinds = sorted(str(row.get('movement_kind') or '<missing>') for row in rows)
            if not set(kinds).issubset({'transfer_out', 'transfer_in'}):
                raise SoftlifeAPIError(
                    _('Unsupported platform stock movement kind(s): %s') % ', '.join(kinds),
                    code='unsupported_stock_movement',
                )
            raise SoftlifeAPIError(
                _('A platform transfer must contain exactly one outbound and one inbound movement.'),
                code='invalid_stock_transfer',
            )
        outbound = next(row for row in rows if row['movement_kind'] == 'transfer_out')
        inbound = next(row for row in rows if row['movement_kind'] == 'transfer_in')
        group_id = outbound.get('movement_group_id')
        if (
            not isinstance(group_id, str) or not group_id.strip()
            or inbound.get('movement_group_id') != group_id
        ):
            raise SoftlifeAPIError(
                _('Paired platform transfers must share a movement group.'),
                code='invalid_stock_transfer',
            )
        references = (outbound.get('external_reference'), inbound.get('external_reference'))
        if any(not isinstance(reference, str) or not reference.strip() for reference in references) \
                or references[0] == references[1]:
            raise SoftlifeAPIError(
                _('Paired platform transfers require distinct external references.'),
                code='invalid_stock_transfer',
            )
        lot_id = outbound.get('odoo_lot_id')
        source_id = outbound.get('odoo_warehouse_id')
        destination_id = inbound.get('odoo_warehouse_id')
        if (
            not self._positive_int(lot_id) or inbound.get('odoo_lot_id') != lot_id
            or not self._positive_int(source_id) or not self._positive_int(destination_id)
            or source_id == destination_id
        ):
            raise SoftlifeAPIError(
                _('Paired platform transfer lot or warehouses are invalid.'),
                code='invalid_stock_transfer',
            )
        try:
            outbound_quantity = Decimal(str(outbound.get('quantity')))
            inbound_quantity = Decimal(str(inbound.get('quantity')))
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise SoftlifeAPIError(
                _('Paired platform transfer quantity is invalid.'), code='invalid_stock_transfer',
            ) from exc
        if (
            not outbound_quantity.is_finite() or not inbound_quantity.is_finite()
            or outbound_quantity >= 0 or inbound_quantity <= 0
            or -outbound_quantity != inbound_quantity
        ):
            raise SoftlifeAPIError(
                _('Paired platform transfer requires equal negative outbound and positive inbound quantities.'),
                code='invalid_stock_transfer',
            )
        occurred_at = outbound.get('occurred_at')
        if not isinstance(occurred_at, str) or inbound.get('occurred_at') != occurred_at:
            raise SoftlifeAPIError(
                _('Paired platform transfers require one shared occurrence time.'),
                code='invalid_stock_transfer',
            )
        try:
            parsed_at = datetime.datetime.fromisoformat(occurred_at.replace('Z', '+00:00'))
            if parsed_at.tzinfo:
                parsed_at = parsed_at.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            occurred_at = fields.Datetime.to_datetime(parsed_at)
        except (TypeError, ValueError) as exc:
            raise SoftlifeAPIError(
                _('Paired platform transfer occurrence time is invalid.'),
                code='invalid_stock_transfer',
            ) from exc
        source = self.env['stock.warehouse'].browse(source_id).exists()
        destination = self.env['stock.warehouse'].browse(destination_id).exists()
        lot = self.env['stock.lot'].browse(lot_id).exists()
        if (
            not source or not destination or source.company_id != destination.company_id
            or source.company_id != self.env.company or not source.lot_stock_id
            or not destination.lot_stock_id or not source.int_type_id
        ):
            raise SoftlifeAPIError(
                _('Paired platform transfer warehouses are unavailable or incompatible.'),
                code='invalid_stock_transfer',
            )
        if not lot or not lot.product_id or lot.product_id.tracking == 'none':
            raise SoftlifeAPIError(
                _('Paired platform transfer lot is unavailable or untracked.'),
                code='invalid_stock_transfer',
            )
        quantity = float(inbound_quantity)
        if lot.product_id.tracking == 'serial' and inbound_quantity != Decimal('1'):
            raise SoftlifeAPIError(
                _('Serial-tracked platform transfers require quantity exactly 1.'),
                code='invalid_stock_transfer',
            )
        if float_compare(quantity, 0.0, precision_rounding=lot.product_id.uom_id.rounding) <= 0:
            raise SoftlifeAPIError(
                _('Paired platform transfer quantity is below the product precision.'),
                code='invalid_stock_transfer',
            )
        return {
            'group_id': group_id, 'outbound_reference': references[0],
            'inbound_reference': references[1], 'source': source, 'destination': destination,
            'lot': lot, 'product': lot.product_id, 'uom': lot.product_id.uom_id,
            'quantity': quantity, 'occurred_at': occurred_at,
        }

    @api.model
    def _validate_existing_stock_transfer(self, picking, values):
        move = picking.move_ids
        actual_lots = {}
        move_line_date_mismatch = False
        for line in move.move_line_ids:
            if line.lot_id:
                actual_lots[line.lot_id.id] = actual_lots.get(line.lot_id.id, 0.0) + line.quantity
            if 'date' in line._fields and line.date != values['occurred_at']:
                move_line_date_mismatch = True
        if (
            picking.state != 'done' or len(move) != 1
            or picking.softlife_stock_movement_group_id != values['group_id']
            or picking.softlife_stock_movement_out_reference != values['outbound_reference']
            or picking.softlife_stock_movement_in_reference != values['inbound_reference']
            or picking.picking_type_id != values['source'].int_type_id
            or picking.location_id != values['source'].lot_stock_id
            or picking.location_dest_id != values['destination'].lot_stock_id
            or picking.date_done != values['occurred_at']
            or move.product_id != values['product'] or move.product_uom != values['uom']
            or move.date != values['occurred_at'] or move_line_date_mismatch
            or float_compare(
                move.product_uom_qty, values['quantity'], precision_rounding=values['uom'].rounding,
            )
            or set(actual_lots) != {values['lot'].id}
            or float_compare(
                actual_lots.get(values['lot'].id, 0.0), values['quantity'],
                precision_rounding=values['uom'].rounding,
            )
        ):
            raise SoftlifeAPIError(
                _('Existing Odoo transfer does not match the immutable platform movement.'),
                code='stock_transfer_conflict',
            )

    @api.model
    def _process_stock_movement_group(self, rows):
        values = self._stock_transfer_values(rows)
        Picking = self.env['stock.picking']
        existing = Picking.search([
            '|', '|',
            ('softlife_stock_movement_group_id', '=', values['group_id']),
            ('softlife_stock_movement_out_reference', '=', values['outbound_reference']),
            ('softlife_stock_movement_in_reference', '=', values['inbound_reference']),
        ], limit=1)
        if existing:
            self._validate_existing_stock_transfer(existing, values)
            return existing

        quant_ids = self.env['stock.quant'].search([
            ('product_id', '=', values['product'].id),
            ('location_id', 'child_of', values['source'].lot_stock_id.id),
            ('lot_id', '=', values['lot'].id),
        ]).ids
        if quant_ids:
            self.env.cr.execute('SELECT id FROM stock_quant WHERE id IN %s FOR UPDATE', [tuple(quant_ids)])
        available = self.env['stock.quant']._get_available_quantity(
            values['product'], values['source'].lot_stock_id,
            lot_id=values['lot'], strict=False,
        )
        if float_compare(
                available, values['quantity'], precision_rounding=values['uom'].rounding) < 0:
            raise SoftlifeAPIError(
                _('Platform transfer lot has %(available)s available; %(required)s is required.',
                  available=available, required=values['quantity']),
                code='insufficient_stock', retryable=True,
            )
        picking = Picking.create({
            'picking_type_id': values['source'].int_type_id.id,
            'location_id': values['source'].lot_stock_id.id,
            'location_dest_id': values['destination'].lot_stock_id.id,
            'scheduled_date': values['occurred_at'],
            'origin': 'Platform stock transfer %s' % values['group_id'],
            'softlife_stock_movement_group_id': values['group_id'],
            'softlife_stock_movement_out_reference': values['outbound_reference'],
            'softlife_stock_movement_in_reference': values['inbound_reference'],
            'move_ids': [Command.create({
                'name': values['product'].display_name,
                'product_id': values['product'].id,
                'product_uom_qty': values['quantity'],
                'product_uom': values['uom'].id,
                'location_id': values['source'].lot_stock_id.id,
                'location_dest_id': values['destination'].lot_stock_id.id,
                'date': values['occurred_at'],
            })],
        })
        picking.action_confirm()
        move = picking.move_ids
        if move.move_line_ids:
            move._do_unreserve()
        reserved = move._update_reserved_quantity(
            values['quantity'], values['source'].lot_stock_id,
            lot_id=values['lot'], strict=False,
        )
        if float_compare(reserved, values['quantity'], precision_rounding=values['uom'].rounding):
            raise SoftlifeAPIError(
                _('Odoo could not reserve the complete platform transfer lot.'),
                code='reservation_failed', retryable=True,
            )
        if 'picked' in move._fields:
            move.picked = True
        result = picking.with_context(
            skip_backorder=True, cancel_backorder=False,
            force_period_date=values['occurred_at'].date(),
        ).button_validate()
        if picking.state != 'done' and isinstance(result, dict):
            raise SoftlifeAPIError(
                _('Odoo transfer requires an unsupported validation wizard.'),
                code='validation_wizard', retryable=True,
            )
        if picking.state != 'done' or Picking.search([('backorder_id', '=', picking.id)]):
            raise SoftlifeAPIError(
                _('Odoo did not complete the platform transfer without a backorder.'),
                code='incomplete_transfer', retryable=True,
            )
        picking.write({'date_done': values['occurred_at']})
        move.write({'date': values['occurred_at']})
        if 'date' in move.move_line_ids._fields:
            move.move_line_ids.write({'date': values['occurred_at']})
        self._validate_existing_stock_transfer(picking, values)
        return picking

    @api.model
    def _stock_movement_error_is_retryable(self, exc):
        if isinstance(exc, SoftlifeAPIError):
            return exc.retryable
        if getattr(exc, 'pgcode', None) in ('40001', '40P01', '55P03'):
            return True
        try:
            import requests
            if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
                return True
        except ImportError:
            pass
        try:
            from psycopg2 import OperationalError
            if isinstance(exc, OperationalError):
                return True
        except ImportError:
            pass
        return False

    @api.model
    def process_platform_stock_movements(self, limit=20):
        worker = self._stock_movement_worker()
        processed = 0
        for _index in range(max(1, min(int(limit), 100))):
            claimed = self._api_request(
                'POST', '/api/internal/odoo/stock-movements/claim',
                payload={'worker': worker, 'limit': 1},
            )
            groups = self._group_claimed_stock_movements(claimed.get('movements'))
            if not groups:
                break
            if len(groups) != 1:
                raise SoftlifeAPIError(
                    _('Platform returned more than one stock operation for a single claim.'),
                    code='invalid_response',
                )
            rows = groups[0]
            movement_id = rows[0]['movement_id']
            lease_token = rows[0]['lease_token']
            try:
                with self.env.cr.savepoint():
                    picking = self._process_stock_movement_group(rows)
                self.env.cr.commit()
                result = {
                    'movement_id': movement_id, 'worker': worker, 'lease_token': lease_token,
                    'accepted': True, 'odoo_external_id': str(picking.id),
                }
            except Exception as exc:
                _logger.exception('SoftLife stock movement group %s failed', movement_id)
                result = {
                    'movement_id': movement_id, 'worker': worker, 'lease_token': lease_token,
                    'accepted': False, 'error': str(exc),
                }
                if self._stock_movement_error_is_retryable(exc):
                    result['retry_at'] = (
                        fields.Datetime.now()
                        + datetime.timedelta(minutes=self._stock_movement_retry_minutes)
                    ).isoformat() + 'Z'
            self._api_request(
                'POST', '/api/internal/odoo/stock-movements/result', payload=result,
            )
            processed += 1
        return processed

    @api.model
    def sync_machines(self):
        rows = self._rest_get('machines', {
            'select': 'id,name,ref,device_imei,device_id_huaxin,state,customer_id',
        })
        Machine = self.env['softlife.machine']
        Partner = self.env['res.partner']
        n = 0
        warnings = []
        for row in rows:
            imei = row.get('device_imei')
            if not imei:
                continue
            vals = {
                'name': row.get('name') or imei,
                'ref': row.get('ref'),
                'device_imei': imei,
                'device_id_huaxin': row.get('device_id_huaxin'),
                'state': row.get('state') or 'active',
            }
            cust = row.get('customer_id')
            if cust:
                partner = Partner.search([('supabase_id', '=', cust)], limit=1)
                if partner:
                    vals['partner_id'] = partner.id
            machine = Machine.search([('device_imei', '=', imei)], limit=1)
            if machine:
                machine.write(vals)
            else:
                machine = Machine.create(vals)

            # Hoppers / ingredients (positions: solid_1..3, liquid_1..3)
            try:
                with self.env.cr.savepoint():
                    ing_rows = self._rest_get('machine_ingredients', {
                        'select': 'position,product_id,product_type,enabled,products(odoo_id)',
                        'machine_id': f'eq.{row.get("id")}',
                    })
                    issues = self._apply_ingredients(machine, ing_rows)
                    warnings.extend(f'{imei} ingredients: {issue}' for issue in issues)
            except Exception as e:
                warnings.append(f'{imei} ingredients: {e}')
                _logger.warning('softlife_sync ingredients for %s: %s', imei, e)
            n += 1
        return (n, warnings) if warnings else n

    @api.model
    def _apply_ingredients(self, machine, ing_rows):
        """Merge Supabase hopper config into Odoo ingredient lines by position
        (preserves portion size / cycled on existing lines; Supabase is source of truth)."""
        Product = self.env['product.product']
        pos_to_line = {ln.position: ln for ln in machine.ingredient_line_ids}
        desired = set()
        issues = []
        for row in ing_rows:
            pos = row.get('position')
            if not pos:
                continue
            vals = {
                'position': pos,
                'product_type': row.get('product_type') or 'topping',
                'enabled': bool(row.get('enabled', True)),
            }
            relation = row.get('products') or {}
            if isinstance(relation, list):
                relation = relation[0] if relation else {}
            odoo_id = relation.get('odoo_id') if isinstance(relation, dict) else None
            product = Product.browse(int(odoo_id)).exists() if odoo_id else Product
            if not product:
                issues.append(f'{pos} has no linked Odoo product')
                continue
            desired.add(pos)
            vals['product_id'] = product.id
            if pos in pos_to_line:
                pos_to_line[pos].write(vals)
            else:
                machine.write({'ingredient_line_ids': [(0, 0, vals)]})
        for pos, ln in pos_to_line.items():
            if pos not in desired:
                ln.unlink()
        return issues

    @api.model
    def sync_orders(self):
        icp = self.env['ir.config_parameter'].sudo()
        since = icp.get_param('softlife.sync.orders_since') or ''
        params = {
            'select': 'order_code,order_time,price,product_name,order_state',
            'order': 'order_time.asc',
        }
        if since:
            params['order_time'] = f'gt.{since}'
        rows = self._rest_get('huaxin_orders', params)

        Move = self.env['account.move']
        pid = self._param('softlife.sync.default_partner_id')
        prod_id = self._param('softlife.sync.default_product_id')
        partner = self.env['res.partner'].browse(int(pid)).exists() if pid else self.env['res.partner']
        product = self.env['product.product'].browse(int(prod_id)).exists() if prod_id else self.env['product.product']
        if not partner or not product:
            _logger.warning(
                'softlife_sync: default partner/product not set; skipping %s order(s)', len(rows)
            )
            return 0
        income_account = (
            product.property_account_income_id or product.categ_id.property_account_income_categ_id
        )

        n = 0
        max_ts = since
        warnings = []
        for row in rows:
            code = row.get('order_code')
            ts = row.get('order_time') or ''
            if ts and ts > max_ts:
                max_ts = ts
            if not code:
                warnings.append(f'order at {ts or "unknown time"}: missing order code')
                continue
            if Move.search([('supabase_order_code', '=', code)], limit=1):
                continue
            try:
                inv_date = (
                    fields.Date.to_date(datetime.datetime.fromisoformat(ts.replace('Z', '+00:00')).date())
                    if ts else fields.Date.today()
                )
            except Exception:
                inv_date = fields.Date.today()
            try:
                with self.env.cr.savepoint():
                    Move.create({
                        'move_type': 'out_invoice',
                        'partner_id': partner.id,
                        'invoice_date': inv_date,
                        'supabase_order_code': code,
                        'invoice_line_ids': [(0, 0, {
                            'product_id': product.id,
                            'name': row.get('product_name') or product.name,
                            'quantity': 1,
                            'price_unit': float(row.get('price') or 0.0),
                            'account_id': income_account.id if income_account else False,
                        })],
                    })
                n += 1
            except Exception as e:
                warnings.append(f'order {code}: {e}')
                _logger.warning('softlife_sync: failed order %s: %s', code, e)

        if not warnings and max_ts and max_ts != since:
            icp.set_param('softlife.sync.orders_since', max_ts)
        return (n, warnings) if warnings else n

    @api.model
    def sync_all(self):
        if not self._is_configured():
            return 'Skipped: Supabase URL / key not configured.'
        if not self._acquire_sync_lock():
            return 'Skipped: another SoftLife full sync is already running.'
        results = {}
        errors = []
        warnings = []
        for name, fn in (('partners', self.sync_partners),
                         ('products', self.sync_products),
                         ('machines', self.sync_machines),
                         ('odoo_warehouses', self.sync_odoo_warehouses),
                         ('odoo_products', self.sync_odoo_products),
                         ('odoo_lots', self.sync_odoo_lots),
                         ('odoo_lot_stock', self.sync_odoo_lot_stock)):
            try:
                with self.env.cr.savepoint():
                    result = fn()
                    if isinstance(result, tuple):
                        results[name] = result[0]
                        warnings.extend(f'{name}: {warning}' for warning in result[1])
                    else:
                        results[name] = result
            except Exception as e:
                results[name] = 0
                errors.append(f'{name}: {e}')
                _logger.exception('softlife_sync %s failed', name)
        def count(name):
            result = results.get(name, 0)
            return result if isinstance(result, int) else 0
        msg = (
            f"Synced {count('partners')} customer(s), "
            f"{count('products')} product(s), "
            f"{count('machines')} machine(s); "
            f"mirrored {count('odoo_products')} Odoo SKU(s), "
            f"{count('odoo_lots')} lot(s), "
            f"{count('odoo_lot_stock')} warehouse-product balance(s), "
            f"{count('odoo_warehouses')} warehouse(s) to Supabase."
        )
        if errors:
            shown = errors[:10]
            if len(errors) > len(shown):
                shown.append(f'{len(errors) - len(shown)} more error(s); see Odoo logs')
            msg += f" Errors: {'; '.join(shown)}"
        if warnings:
            shown = warnings[:10]
            if len(warnings) > len(shown):
                shown.append(f'{len(warnings) - len(shown)} more warning(s); see Odoo logs')
            msg += f" Warnings: {'; '.join(shown)}"
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('softlife.sync.last_sync', fields.Datetime.now())
        icp.set_param('softlife.sync.last_sync_summary', msg)
        return msg

    @api.model
    def process_platform_sync_request(self):
        pending_result = self._retry_pending_platform_sync_result()
        if pending_result:
            return pending_result
        if not self._acquire_sync_lock():
            return False
        claimed = self._api_request('GET', '/api/internal/odoo/sync-requests')
        request = claimed.get('request')
        if not request:
            return False
        request_id = str(request.get('id') or '')
        claim_token = str(request.get('claim_token') or '')
        if not request_id or not claim_token:
            raise SoftlifeAPIError(_('Platform sync request omitted its ID or lease.'), code='invalid_response')
        try:
            result = self._dispatch_platform_sync_request(request)
            result.update({
                'claim_token': claim_token,
                'error': None if result['accepted'] else result.get('error') or result['summary'],
                'finished_at': fields.Datetime.now().isoformat() + 'Z',
            })
        except Exception as exc:
            self.env.cr.rollback()
            self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', [self._sync_lock_id])
            _logger.exception('SoftLife platform-requested sync %s failed', request_id)
            result = {
                'accepted': False, 'summary': 'Platform-requested Odoo operation failed.', 'claim_token': claim_token,
                'error': str(exc), 'finished_at': fields.Datetime.now().isoformat() + 'Z',
            }
        pending = {'request_id': request_id, 'result': result}
        self.env['ir.config_parameter'].sudo().set_param(
            self._pending_sync_result_key, json.dumps(pending),
        )
        self.env.cr.commit()
        return self._retry_pending_platform_sync_result()

    @api.model
    def _dispatch_platform_sync_request(self, request):
        kind = request.get('kind')
        if kind == 'stock_snapshot':
            summary = self.sync_all()
            accepted = not summary.startswith('Skipped:') and ' Errors:' not in summary
            return {'accepted': accepted, 'summary': summary}
        fiscal_handlers = {
            'fiscal_product_remediation': self.remediate_fiscal_products,
            'fiscal_invoice_draft_creation': self._create_fiscal_invoice_drafts,
            'fiscal_invoice_bulk_confirmation': self._confirm_fiscal_invoices,
        }
        if kind == 'fiscal_invoice_sale_link':
            return self._link_fiscal_invoices_to_sales(request.get('payload'))
        if kind in fiscal_handlers:
            payload = request.get('payload')
            payload_hash = request.get('payload_sha256')
            if (
                not isinstance(payload_hash, str)
                or not re.fullmatch(r'[0-9a-f]{64}', payload_hash)
                or self._canonical_sha256(payload) != payload_hash
            ):
                return {
                    'accepted': False,
                    'summary': _('Rejected fiscal operation with an invalid frozen payload hash.'),
                    'error': _('Fiscal operation payload hash mismatch.'),
                }
            return fiscal_handlers[kind](payload)
        return {
            'accepted': False,
            'summary': _('Rejected unsupported platform request kind: %s') % (kind or '<missing>'),
            'error': _('Unsupported platform request kind.'),
        }

    @api.model
    def _retry_pending_platform_sync_result(self):
        icp = self.env['ir.config_parameter'].sudo()
        raw = icp.get_param(self._pending_sync_result_key)
        if not raw:
            return False
        try:
            pending = json.loads(raw)
            request_id = str(pending['request_id'])
            result = pending['result']
        except (KeyError, TypeError, ValueError) as exc:
            raise SoftlifeAPIError(_('Stored platform sync result is invalid.'), code='invalid_response') from exc
        try:
            self._api_request(
                'POST', f'/api/internal/odoo/sync-requests/{request_id}/result', payload=result,
            )
        except SoftlifeAPIError as exc:
            if exc.status != 409:
                raise
            icp.set_param(self._pending_sync_result_key, '')
            self.env.cr.commit()
            return False
        icp.set_param(self._pending_sync_result_key, '')
        self.env.cr.commit()
        return result

    @api.model
    def _cron_sync_requests(self):
        if not self._api_is_configured():
            return
        try:
            self.process_platform_sync_request()
        except Exception as exc:
            _logger.warning('SoftLife sync request polling failed: %s', exc)
        try:
            self.process_platform_stock_movements()
        except Exception as exc:
            _logger.warning('SoftLife stock movement polling failed: %s', exc)

    @api.model
    def _cron_fiscal_configuration(self):
        if not self._fiscal_configuration_reporting_enabled() or not self._api_is_configured():
            return
        try:
            self.report_fiscal_configuration()
        except Exception as exc:
            _logger.exception('SoftLife fiscal configuration report failed: %s', exc)
            raise

    @api.model
    def _cron_sync(self):
        try:
            with self.env.cr.savepoint():
                self.sync_all()
        except Exception as e:
            _logger.warning('SoftLife cron sync failed: %s', e)
