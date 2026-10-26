import logging
from contextlib import contextmanager
from contextvars import ContextVar

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_FISCAL_PARTNER_AUTHORITY = object()
_fiscal_partner_authority = ContextVar('softlife_fiscal_partner_authority', default=None)
_PAYOUT_PARTNER_AUTHORITY = object()
_payout_partner_authority = ContextVar('softlife_payout_partner_authority', default=None)
_logger = logging.getLogger(__name__)


@contextmanager
def _softlife_fiscal_partner_internal_scope():
    token = _fiscal_partner_authority.set(_FISCAL_PARTNER_AUTHORITY)
    try:
        yield
    finally:
        _fiscal_partner_authority.reset(token)


@contextmanager
def _softlife_payout_partner_internal_scope():
    token = _payout_partner_authority.set(_PAYOUT_PARTNER_AUTHORITY)
    try:
        yield
    finally:
        _payout_partner_authority.reset(token)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    supabase_id = fields.Char(string='SoftLife ID', index=True, copy=False)
    softlife_fiscal_customer_id = fields.Char(index=True, copy=False)
    softlife_fiscal_customer_sha256 = fields.Char(copy=False)
    softlife_payout_tenant_id = fields.Char(index=True, copy=False)
    softlife_payout_vendor_sha256 = fields.Char(copy=False)
    softlife_payout_vendor_key = fields.Char(index=True, copy=False)

    _sql_constraints = [
        ('softlife_fiscal_customer_id_unique', 'unique(softlife_fiscal_customer_id)',
         'A SoftLife fiscal customer can only be linked to one Odoo partner.'),
        ('softlife_fiscal_customer_provenance_paired',
         "CHECK ((softlife_fiscal_customer_id IS NULL AND softlife_fiscal_customer_sha256 IS NULL) OR "
         "(softlife_fiscal_customer_id IS NOT NULL AND softlife_fiscal_customer_sha256 IS NOT NULL))",
         'SoftLife fiscal customer identity and snapshot hash must be stored together.'),
        ('softlife_fiscal_customer_id_format',
         "CHECK (softlife_fiscal_customer_id IS NULL OR softlife_fiscal_customer_id ~ "
         "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')",
         'The SoftLife fiscal customer ID must be a canonical UUID.'),
        ('softlife_fiscal_customer_sha256_format',
         "CHECK (softlife_fiscal_customer_sha256 IS NULL OR softlife_fiscal_customer_sha256 ~ "
         "'^[0-9a-f]{64}$')",
         'The SoftLife fiscal customer snapshot hash must be lowercase SHA-256.'),
        ('softlife_payout_vendor_key_unique', 'unique(softlife_payout_vendor_key)',
         'A SoftLife payout vendor identity can only exist once.'),
        ('softlife_payout_vendor_provenance_paired',
         "CHECK ((softlife_payout_tenant_id IS NULL AND softlife_payout_vendor_sha256 IS NULL "
         "AND softlife_payout_vendor_key IS NULL) OR (softlife_payout_tenant_id IS NOT NULL "
         "AND softlife_payout_vendor_sha256 IS NOT NULL AND softlife_payout_vendor_key IS NOT NULL))",
         'SoftLife payout vendor provenance must be stored together.'),
        ('softlife_payout_vendor_hash_format',
         "CHECK (softlife_payout_vendor_sha256 IS NULL OR "
         "softlife_payout_vendor_sha256 ~ '^[0-9a-f]{64}$')",
         'The SoftLife payout vendor hash must be lowercase SHA-256.'),
    ]

    def init(self):
        super().init()
        self.env.cr.execute("""
            SELECT 1
              FROM res_partner
             WHERE supabase_id IS NOT NULL
             GROUP BY supabase_id
            HAVING count(*) > 1
             LIMIT 1
        """)
        if self.env.cr.fetchone():
            _logger.warning(
                'Cannot enforce unique non-null res.partner.supabase_id until duplicates are resolved.'
            )
            return
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS res_partner_supabase_id_not_null_uniq
                ON res_partner (supabase_id)
             WHERE supabase_id IS NOT NULL
        """)

    @api.model_create_multi
    def create(self, vals_list):
        provenance = {'softlife_fiscal_customer_id', 'softlife_fiscal_customer_sha256'}
        if _fiscal_partner_authority.get() is not _FISCAL_PARTNER_AUTHORITY and any(
                provenance.intersection(vals) for vals in vals_list):
            raise UserError(_('SoftLife fiscal customer provenance is managed internally.'))
        payout_provenance = {
            'softlife_payout_tenant_id', 'softlife_payout_vendor_sha256',
            'softlife_payout_vendor_key',
        }
        if _payout_partner_authority.get() is not _PAYOUT_PARTNER_AUTHORITY and any(
                payout_provenance.intersection(vals) for vals in vals_list):
            raise UserError(_('SoftLife payout vendor provenance is managed internally.'))
        return super().create(vals_list)

    def write(self, vals):
        provenance = {'softlife_fiscal_customer_id', 'softlife_fiscal_customer_sha256'}
        snapshot_fields = {
            'name', 'vat', 'email', 'street', 'city', 'zip', 'state_id', 'country_id',
            'company_id', 'softlife_fiscal_customer_id', 'softlife_fiscal_customer_sha256',
        }
        if _fiscal_partner_authority.get() is not _FISCAL_PARTNER_AUTHORITY:
            if provenance.intersection(vals):
                raise UserError(_('SoftLife fiscal customer provenance cannot be changed.'))
            if snapshot_fields.intersection(vals) and self.filtered('softlife_fiscal_customer_id'):
                raise UserError(_('SoftLife fiscal customer snapshot fields are frozen.'))
        payout_snapshot_fields = {
            'name', 'vat', 'email', 'street', 'street2', 'city', 'zip', 'state_id',
            'country_id', 'company_id', 'company_type', 'is_company', 'supabase_id',
            'active', 'supplier_rank',
        }
        if (
            _payout_partner_authority.get() is not _PAYOUT_PARTNER_AUTHORITY
            and payout_snapshot_fields.intersection(vals)
            and self.filtered('softlife_payout_vendor_key')
        ):
            raise UserError(_('SoftLife payout vendor snapshot fields are frozen.'))
        if (
            _payout_partner_authority.get() is not _PAYOUT_PARTNER_AUTHORITY
            and {'softlife_payout_tenant_id', 'softlife_payout_vendor_sha256',
                 'softlife_payout_vendor_key'}.intersection(vals)
        ):
            raise UserError(_('SoftLife payout vendor provenance cannot be changed.'))
        return super().write(vals)

    def unlink(self):
        if self.filtered('softlife_fiscal_customer_id'):
            raise UserError(_('SoftLife fiscal customers cannot be deleted.'))
        if self.filtered('softlife_payout_vendor_key'):
            raise UserError(_('SoftLife payout vendors cannot be deleted.'))
        return super().unlink()
