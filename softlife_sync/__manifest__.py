{
    'name': 'SoftLife Platform Sync',
    'version': '18.0.1.0.0',
    'summary': 'Pull operational data (customers, products, orders -> invoices) from the SoftLife platform (Supabase) into Odoo.',
    'description': """
SoftLife Platform Sync
======================
Downstream ERP connector: reads the SoftLife platform (Supabase REST, the
system of record operated by the middleware) and mirrors into Odoo:

  tenants        -> res.partner (customers)
  products       -> product.template
  huaxin_orders  -> account.move (draft customer invoices -> feeds VeriFactu)

...and mirrors Odoo's own SKU/lot/warehouse master data back out to Supabase
(read-only mirror tables the platform consumes — see softlife-platform/README):

  product.product   -> odoo_products
  stock.lot          -> odoo_lots
  stock.warehouse     -> odoo_warehouses

Linking a platform ingredient to an Odoo SKU (products.odoo_id) is never done
by this module automatically — it's a deliberate choice made on the platform
(see /odoo and /products there). Records removed/archived in Odoo are pruned
from the mirror tables on the next sync; any ingredient linked to a pruned
row is automatically unlinked (FK is ON DELETE SET NULL), never re-pointed.

Odoo no longer talks to Huaxin directly — the middleware owns Huaxin.
Idempotent by Supabase id / order code / odoo_id. Run via Settings, the
"Platform Sync" menu, or an hourly cron.
""",
    'author': 'SoftLife',
    'website': 'https://softlife.es',
    'category': 'Accounting/Accounting',
    'license': 'OPL-1',
    'depends': ['softlife_machine', 'account', 'stock'],
    'data': [
        'security/ir.model.access.csv',
        'data/softlife_sync_data.xml',
        'data/ir_cron.xml',
        'views/res_config_settings_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'application': False,
}
