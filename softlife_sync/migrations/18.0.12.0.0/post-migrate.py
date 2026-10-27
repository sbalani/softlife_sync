from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    report = env.ref('softlife_sync.action_report_payout_self_bill', raise_if_not_found=False)
    if report:
        report.unlink()
    template = env.ref('softlife_sync.report_payout_self_bill', raise_if_not_found=False)
    if template:
        template.unlink()
    env['ir.config_parameter'].search([
        ('key', 'in', [
            'softlife.sync.payout_company_id',
            'softlife.sync.payout_purchase_journal_id',
            'softlife.sync.payout_expense_product_id',
            'softlife.sync.payout_purchase_tax_id',
        ]),
    ]).unlink()
    cr.execute('ALTER TABLE res_partner DROP COLUMN IF EXISTS softlife_payout_tenant_id')
    cr.execute('ALTER TABLE res_partner DROP COLUMN IF EXISTS softlife_payout_vendor_sha256')
    cr.execute('ALTER TABLE res_partner DROP COLUMN IF EXISTS softlife_payout_vendor_key')
