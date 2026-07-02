"""Connector: pulls operational data from the SoftLife platform (Supabase REST)
into Odoo. Odoo is the downstream ERP; the middleware is the system of record.
"""
import datetime
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SoftlifeSyncClient(models.TransientModel):
    _name = 'softlife.sync.client'
    _description = 'SoftLife Platform Sync (Supabase connector)'

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

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------
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

    @api.model
    def sync_machines(self):
        rows = self._rest_get('machines', {
            'select': 'id,name,ref,device_imei,device_id_huaxin,state,customer_id',
        })
        Machine = self.env['softlife.machine']
        Partner = self.env['res.partner']
        n = 0
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
            existing = Machine.search([('device_imei', '=', imei)], limit=1)
            if existing:
                existing.write(vals)
            else:
                Machine.create(vals)
            n += 1
        return n

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
        for row in rows:
            code = row.get('order_code')
            if not code:
                continue
            if Move.search([('supabase_order_code', '=', code)], limit=1):
                continue
            ts = row.get('order_time') or ''
            try:
                inv_date = (
                    fields.Date.to_date(datetime.datetime.fromisoformat(ts.replace('Z', '+00:00')).date())
                    if ts else fields.Date.today()
                )
            except Exception:
                inv_date = fields.Date.today()
            try:
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
                if ts and ts > max_ts:
                    max_ts = ts
            except Exception as e:
                _logger.warning('softlife_sync: failed order %s: %s', code, e)

        if max_ts and max_ts != since:
            icp.set_param('softlife.sync.orders_since', max_ts)
        return n

    @api.model
    def sync_all(self):
        if not self._is_configured():
            return 'Skipped: Supabase URL / key not configured.'
        results = {}
        for name, fn in (('partners', self.sync_partners),
                         ('products', self.sync_products),
                         ('machines', self.sync_machines),
                         ('orders', self.sync_orders)):
            try:
                results[name] = fn()
            except Exception as e:
                results[name] = f'error: {e}'
                _logger.exception('softlife_sync %s failed', name)
        self.env['ir.config_parameter'].sudo().set_param('softlife.sync.last_sync', fields.Datetime.now())
        return (
            f"Synced {results.get('partners', 0)} customer(s), "
            f"{results.get('products', 0)} product(s), "
            f"{results.get('machines', 0)} machine(s), "
            f"{results.get('orders', 0)} order(s)."
        )

    @api.model
    def _cron_sync(self):
        try:
            self.sync_all()
        except Exception as e:
            _logger.warning('SoftLife cron sync failed: %s', e)
