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
    def _rest_delete_missing(self, table, id_column, present_ids):
        """Delete rows from a Supabase table whose id_column isn't in present_ids —
        i.e. records removed/archived on the Odoo side since the last sync.
        No-ops on an empty present_ids so a transient empty read can't wipe the table."""
        import requests
        if not present_ids:
            return
        base = self._param('softlife.sync.supabase_url').rstrip('/')
        key = self._param('softlife.sync.supabase_key')
        url = f'{base}/rest/v1/{table}'
        headers = {'apikey': key, 'Authorization': f'Bearer {key}', 'Prefer': 'return=minimal'}
        ids_csv = ','.join(str(i) for i in present_ids)
        r = requests.delete(url, headers=headers, params={id_column: f'not.in.({ids_csv})'}, timeout=60)
        if r.status_code not in (200, 204):
            raise UserError(_('Supabase delete-missing %s failed: %s %s') % (table, r.status_code, r.text[:200]))

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
            {'odoo_id': w.id, 'name': w.name, 'code': w.code}
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
                'qty_available': p.qty_available,
            })
        self._rest_upsert('odoo_products', rows, on_conflict='odoo_id')
        # Deleted/archived in Odoo -> drop from the mirror. Any platform ingredient
        # linked to it (products.odoo_id) is auto-unlinked (FK is ON DELETE SET NULL),
        # never silently re-pointed at something else.
        self._rest_delete_missing('odoo_products', 'odoo_id', [r['odoo_id'] for r in rows])
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
            machine = Machine.search([('device_imei', '=', imei)], limit=1)
            if machine:
                machine.write(vals)
            else:
                machine = Machine.create(vals)

            # Hoppers / ingredients (positions: solid_1..3, liquid_1..3)
            try:
                ing_rows = self._rest_get('machine_ingredients', {
                    'select': 'position,product_id,product_type,enabled',
                    'machine_id': f'eq.{row.get("id")}',
                })
                self._apply_ingredients(machine, ing_rows)
            except Exception as e:
                _logger.warning('softlife_sync ingredients for %s: %s', imei, e)
            n += 1
        return n

    @api.model
    def _apply_ingredients(self, machine, ing_rows):
        """Merge Supabase hopper config into Odoo ingredient lines by position
        (preserves portion size / cycled on existing lines; Supabase is source of truth)."""
        Template = self.env['product.template']
        pos_to_line = {ln.position: ln for ln in machine.ingredient_line_ids}
        desired = set()
        for row in ing_rows:
            pos = row.get('position')
            if not pos:
                continue
            desired.add(pos)
            vals = {
                'position': pos,
                'product_type': row.get('product_type') or 'topping',
                'enabled': bool(row.get('enabled', True)),
            }
            pid = row.get('product_id')
            if pid:
                tmpl = Template.search([('supabase_id', '=', pid)], limit=1)
                if tmpl and tmpl.product_variant_id:
                    vals['product_id'] = tmpl.product_variant_id.id
            if pos in pos_to_line:
                pos_to_line[pos].write(vals)
            else:
                machine.write({'ingredient_line_ids': [(0, 0, vals)]})
        for pos, ln in pos_to_line.items():
            if pos not in desired:
                ln.unlink()

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
                         ('orders', self.sync_orders),
                         ('odoo_warehouses', self.sync_odoo_warehouses),
                         ('odoo_products', self.sync_odoo_products),
                         ('odoo_lots', self.sync_odoo_lots)):
            try:
                results[name] = fn()
            except Exception as e:
                results[name] = f'error: {e}'
                _logger.exception('softlife_sync %s failed', name)
        msg = (
            f"Synced {results.get('partners', 0)} customer(s), "
            f"{results.get('products', 0)} product(s), "
            f"{results.get('machines', 0)} machine(s), "
            f"{results.get('orders', 0)} order(s); "
            f"mirrored {results.get('odoo_products', 0)} Odoo SKU(s), "
            f"{results.get('odoo_lots', 0)} lot(s), "
            f"{results.get('odoo_warehouses', 0)} warehouse(s) to Supabase."
        )
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('softlife.sync.last_sync', fields.Datetime.now())
        icp.set_param('softlife.sync.last_sync_summary', msg)
        return msg

    @api.model
    def _cron_sync(self):
        try:
            self.sync_all()
        except Exception as e:
            _logger.warning('SoftLife cron sync failed: %s', e)
