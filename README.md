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

`sync_products` still pushes every platform ingredient to Odoo as a
`product.template` (matched idempotently by a `supabase_id` stamp) — but it
**never writes `products.odoo_id`**. Linking an ingredient to a specific Odoo
SKU is always a deliberate action taken on the platform (`/odoo` or
`/products`), never inferred by name. An earlier version of this auto-linked
by writing back the id of whatever product it had just pushed/matched — which
silently created a duplicate Odoo product for an ingredient that already had
a real match under a different id, and linked to the wrong one. Don't repeat
that mistake if you touch this file again.

Records deleted/archived in Odoo are pruned from the mirror tables on the
next sync. If a platform ingredient was linked to a pruned record, the link
is automatically cleared (`products.odoo_id` has `ON DELETE SET NULL`) — it's
never silently re-pointed at something else.

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
