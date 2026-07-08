# softlife_sync

Custom **Odoo 18** module — downstream ERP connector for the SoftLife platform.

The **middleware** (Supabase + Vercel) is the system of record for machines,
franchisees, orders and telemetry. This module reads it and mirrors the
data Odoo needs for accounting / **Spanish VeriFactu**:

| Platform (Supabase) | → Odoo |
|---|---|
| `tenants` | `res.partner` (customers) |
| `products` | `product.template` |
| `machines` | `softlife.machine` (matched by IMEI; customer linked) |
| `huaxin_orders` | `account.move` — draft customer invoices (→ VeriFactu) |

For SKUs, lots and warehouses, **Odoo is the system of record** instead — it
already owns real stock/traceability data. This module mirrors that data back
out so the platform can read it, closing the loop:

| Odoo | → Platform (Supabase) |
|---|---|
| `product.product` | `odoo_products` |
| `stock.lot` | `odoo_lots` |
| `stock.warehouse` | `odoo_warehouses` |

New products created on the platform (`products.odoo_id is null`) are created
in Odoo by `sync_products`, which then writes the resulting Odoo id back onto
the Supabase row — so `products.odoo_id` always points at the matching
`odoo_products.odoo_id` once a sync has run, in either direction.

Odoo **no longer talks to Huaxin directly**; `softlife_huaxin` is retired.

## Configure
**Settings → SoftLife Sync**:
- **Supabase URL** — `https://<project>.supabase.co`
- **Service Role Key** — bypasses RLS for server-to-server reads (kept in Odoo config, like the old Huaxin keys)
- **Default customer for orders** — partner set on vending invoices (B2C retail)
- **Default product for order lines** — provides the revenue account

Click **Sync now**, or let the hourly cron run. Mirrors are **idempotent** by
Supabase id / order code; orders track a high-water timestamp so only new rows
pull each run.

## Install
Clone into your Odoo addons path named `softlife_sync`:
```bash
git clone https://github.com/sbalani/softlife_sync.git softlife_sync
```
**Apps → Update Apps List → install "SoftLife Platform Sync"**
(requires `softlife_machine` and `account`).

## Depends on
`softlife_machine`, `account`, `stock` (product comes transitively). Add OCA
`l10n_es_*` VeriFactu modules for the fiscal submission itself.
