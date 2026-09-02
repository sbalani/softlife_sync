# softlife_sync

Custom **Odoo 18** module — downstream ERP connector for the SoftLife platform.

The **middleware** (Supabase + the platform app) is the system of record for
machines, recipes, sales and manufacturing export payloads. This module mirrors
master data and turns confirmed period exports into Odoo manufacturing and
warehouse sales documents.

| Platform (Supabase) | → Odoo |
|---|---|
| `tenants` | `res.partner` (customers) |
| `products` | `product.template` |
| `machines` | `softlife.machine` (matched by IMEI; customer linked) |
| ready manufacturing period | completed `mrp.production` per warehouse/recipe version |
| ready manufacturing period | confirmed `sale.order` and validated delivery per warehouse |

For SKUs, lots and warehouses, **Odoo is the system of record** instead — it
already owns real stock/traceability data. This module mirrors that data back
out so the platform can read it, closing the loop:

| Odoo | → Platform (Supabase) |
|---|---|
| `product.product` | `odoo_products` |
| `stock.lot` | `odoo_lots` |
| `stock.warehouse` | `odoo_warehouses` |
| positive internal `stock.quant` by lot/warehouse | `odoo_lot_stock` |

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

Direct vending-order invoice import is no longer part of `sync_all`. The old
`sync_orders` method and existing invoices are left intact for audit/history,
but manufacturing-period sales orders are now the only automatic revenue sync.

## Configure
**Settings → SoftLife Sync**:
- **Supabase URL** — `https://<project>.supabase.co`
- **Service Role Key** — bypasses RLS for server-to-server reads (kept in Odoo config, like the old Huaxin keys)
- **Platform App URL** — application origin hosting `/api/internal/odoo/*`; this is not the Supabase URL
- **Odoo Sync Secret** — shared `ODOO_SYNC_SECRET`, sent only as `x-odoo-sync-secret`

Use **SoftLife → Create Manufacturing Period** for an inclusive date range and
timezone. Review the preview and blocked details, confirm Odoo-initiated drafts,
then process. A ten-minute cron refreshes catalog/runs, processes only `ready`
runs, and retries callbacks. Generated records commit in one cron/action pass;
the `/result` callback is attempted only in a later pass.

Idempotency is enforced in PostgreSQL: one finished product per recipe, one BOM
per recipe version, one MO per export/warehouse/version/currency, and one sales
order per export/warehouse. Sales orders are confirmed and deliveries validated;
this module never creates an invoice from a manufacturing period.

### Package content and recipe dosage

Ingredient inventory remains in its existing Odoo UoM (normally **Units**).
Configure **Net Content per Unit** and **Content UoM** on an ingredient, for
example `1120 g`. The module derives OCA's inverse secondary-UoM factor and
hides that implementation detail. It does not rewrite existing quants, lots,
purchase orders, or stock moves.

Manufacturing accepts only payload contract v2. It verifies the platform's
frozen physical dosage, package snapshot, `stock_quantity_per_unit`, and
`stock_total_quantity`, then writes the fractional Unit quantity to the BOM.
For example, `100 g / 1120 g = 0.0892857143 Units`. The calculation and frozen
values remain visible on the BOM line for audit; later product configuration
changes do not alter an existing recipe version. Standard **Units** and Product
Unit of Measure display precision are set to six decimal places. This preserves
the inventory UoM and numeric values of all existing quants, lots, purchase
orders, and stock moves while allowing fractional package consumption.

## Install
Clone into your Odoo addons path named `softlife_sync`:
```bash
git clone https://github.com/sbalani/softlife_sync.git softlife_sync
```
**Apps → Update Apps List → install "SoftLife Platform Sync"**
(requires `softlife_machine`, `account`, `stock`, `mrp`, and `sale_management`).

## Depends on
`softlife_machine`, `account`, `stock`, `mrp`, `sale_management`. Add OCA
`l10n_es_*` VeriFactu modules for the fiscal submission itself.
