from odoo import fields


def softlife_company_month_advisory_lock(cr, company_id, document_date):
    date = fields.Date.to_date(document_date)
    if not date:
        raise ValueError('A document date is required for the company/month lock.')
    month_key = date.year * 100 + date.month
    cr.execute(
        'SELECT pg_advisory_xact_lock(%s, %s)',
        [company_id, month_key],
    )
    return month_key
