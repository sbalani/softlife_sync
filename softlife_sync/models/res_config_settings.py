from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    softlife_supabase_url = fields.Char(
        string='Supabase URL', config_parameter='softlife.sync.supabase_url',
    )
    softlife_supabase_key = fields.Char(
        string='Service Role Key', config_parameter='softlife.sync.supabase_key',
    )
    softlife_platform_url = fields.Char(
        string='Platform App URL', config_parameter='softlife.sync.platform_url',
    )
    softlife_odoo_sync_secret = fields.Char(
        string='Odoo Sync Secret', config_parameter='softlife.sync.odoo_sync_secret',
    )
    softlife_default_partner_id = fields.Many2one(
        'res.partner', string='Vending Customer / Consumidor Final',
        config_parameter='softlife.sync.default_partner_id',
        help='Accounting customer used on Sales Orders created by manufacturing runs.',
    )
    softlife_default_product_id = fields.Many2one(
        'product.product', string='Default product for order lines',
        config_parameter='softlife.sync.default_product_id',
        help='Product used on the invoice line (provides the revenue account).',
    )
    softlife_fiscal_reporting_enabled = fields.Boolean(
        string='Fiscal configuration reporting',
        config_parameter='softlife.sync.fiscal_reporting_enabled',
        default=False,
        help='Allow the read-only hourly Odoo fiscal configuration report to the platform.',
    )
    softlife_fiscal_product_remediation_enabled = fields.Boolean(
        string='Allow platform-requested fiscal product configuration repairs',
        config_parameter='softlife.sync.fiscal_product_remediation_enabled',
        default=False,
        help='Allow narrowly scoped platform requests to repair income accounts and sales taxes on '
             'existing SoftLife products. This is independent from read-only fiscal reporting.',
    )
    softlife_fiscal_invoice_draft_creation_enabled = fields.Boolean(
        string='Allow platform-requested fiscal invoice draft creation',
        config_parameter='softlife.sync.fiscal_invoice_draft_creation_enabled',
        default=False,
        help='Allow validated platform requests to create fiscal customer invoice drafts.',
    )
    softlife_fiscal_invoice_confirmation_enabled = fields.Boolean(
        string='Allow platform-requested fiscal invoice confirmation',
        config_parameter='softlife.sync.fiscal_invoice_confirmation_enabled',
        default=False,
        help='Allow validated platform requests to post only their explicitly listed fiscal invoices.',
    )
    softlife_fiscal_company_id = fields.Many2one(
        'res.company', string='Fiscal issuing company',
        config_parameter='softlife.sync.fiscal_company_id',
        help='Legal entity whose company, journal, customer, taxes, and products are checked.',
    )
    softlife_payout_bill_draft_creation_enabled = fields.Boolean(
        string='Allow payout self-bill draft creation',
        config_parameter='softlife.sync.payout_bill_draft_creation_enabled',
        default=False,
    )
    softlife_payout_bill_posting_enabled = fields.Boolean(
        string='Allow payout self-bill posting',
        config_parameter='softlife.sync.payout_bill_posting_enabled',
        default=False,
    )
    softlife_payout_bill_pdf_export_enabled = fields.Boolean(
        string='Allow payout self-bill PDF export',
        config_parameter='softlife.sync.payout_bill_pdf_export_enabled',
        default=False,
    )
    softlife_payout_company_id = fields.Many2one(
        'res.company', string='Payout self-billing company',
        config_parameter='softlife.sync.payout_company_id',
    )
    softlife_payout_purchase_journal_id = fields.Many2one(
        'account.journal', string='Payout purchase journal',
        config_parameter='softlife.sync.payout_purchase_journal_id',
    )
    softlife_payout_expense_product_id = fields.Many2one(
        'product.product', string='Payout expense product',
        config_parameter='softlife.sync.payout_expense_product_id',
    )
    softlife_payout_purchase_tax_id = fields.Many2one(
        'account.tax', string='Payout purchase IVA',
        config_parameter='softlife.sync.payout_purchase_tax_id',
    )
    softlife_last_sync = fields.Char(string='Last sync', compute='_compute_softlife_last_sync')
    softlife_last_sync_summary = fields.Char(string='Last result', compute='_compute_softlife_last_sync')

    @api.depends('softlife_supabase_url')
    def _compute_softlife_last_sync(self):
        icp = self.env['ir.config_parameter'].sudo()
        last = icp.get_param('softlife.sync.last_sync')
        summary = icp.get_param('softlife.sync.last_sync_summary')
        for rec in self:
            rec.softlife_last_sync = last or ''
            rec.softlife_last_sync_summary = summary or ''

    def set_values(self):
        if (self.softlife_fiscal_reporting_enabled
                or self.softlife_fiscal_product_remediation_enabled
                or self.softlife_fiscal_invoice_draft_creation_enabled
                or self.softlife_fiscal_invoice_confirmation_enabled) \
                and not self.softlife_fiscal_company_id:
            raise UserError(_(
                'Select the Fiscal issuing company before enabling a fiscal integration.'
            ))
        payout_enabled = (
            self.softlife_payout_bill_draft_creation_enabled
            or self.softlife_payout_bill_posting_enabled
            or self.softlife_payout_bill_pdf_export_enabled
        )
        if payout_enabled:
            company = self.softlife_payout_company_id
            journal = self.softlife_payout_purchase_journal_id
            product = self.softlife_payout_expense_product_id
            tax = self.softlife_payout_purchase_tax_id
            if not all((company, journal, product, tax)):
                raise UserError(_(
                    'Select the payout company, purchase journal, expense product, and purchase tax.'
                ))
            account = product.with_company(company).product_tmpl_id.get_product_accounts().get(
                'expense'
            )
            account_company_valid = not account or (
                company in account.company_ids if 'company_ids' in account._fields
                else not account.company_id or account.company_id == company
            )
            supplier_taxes = product.with_company(company).supplier_taxes_id._filter_taxes_by_company(
                company
            )
            repartition = tax.invoice_repartition_line_ids
            base_repartition = repartition.filtered(lambda line: line.repartition_type == 'base')
            tax_repartition = repartition.filtered(lambda line: line.repartition_type == 'tax')
            valid_repartition = (
                sum(base_repartition.mapped('factor_percent')) == 100
                and sum(tax_repartition.mapped('factor_percent')) == 100
                and tax_repartition
                and all(line.account_id and line.account_id.account_type == 'asset_current' and (
                    company in line.account_id.company_ids if 'company_ids' in line.account_id._fields
                    else not line.account_id.company_id or line.account_id.company_id == company
                ) for line in tax_repartition)
                and any(line.tag_ids for line in base_repartition)
                and any(line.tag_ids for line in tax_repartition)
            )
            if (
                not company.active or company.currency_id.name != 'EUR'
                or not company.account_fiscal_country_id
                or company.account_fiscal_country_id.code != 'ES'
                or not journal.active or journal.company_id != company or journal.type != 'purchase'
                or not product.active or not product.purchase_ok
                or not account or account.account_type not in ('expense', 'expense_direct_cost')
                or not account_company_valid or supplier_taxes != tax
                or not tax.active or tax.company_id != company
                or tax.type_tax_use != 'purchase' or tax.amount_type != 'percent'
                or tax.amount != 21 or not tax.price_include
                or tax.country_id != company.account_fiscal_country_id
                or tax.tax_exigibility != 'on_invoice' or not valid_repartition
            ):
                raise UserError(_(
                    'Payout self-billing requires a Spanish EUR company, its purchase journal, '
                    'an active purchasable product with an expense account, and exactly the '
                    'configured price-included 21% purchase tax.'
                ))
        result = super().set_values()
        cron = self.env.ref('softlife_sync.cron_softlife_fiscal_configuration', raise_if_not_found=False)
        if cron:
            cron.active = bool(self.softlife_fiscal_reporting_enabled)
        return result

    def action_sync_now(self):
        msg = self.env['softlife.sync.client'].sudo().sync_all()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': 'SoftLife Sync', 'message': msg, 'type': 'success', 'sticky': False},
        }
