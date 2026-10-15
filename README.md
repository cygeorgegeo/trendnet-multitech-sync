# Multitech → Odoo daily stock sync

Every day GitHub Actions logs into **b2b.multitech.com.cy**, reads all ~2,500 products
(`/en/products`, SKU + dealer price excl. VAT + stock badge) and updates **trendnet.com.cy**:

| Odoo | What changes |
|---|---|
| `product.supplierinfo` (vendor Multitech, id 18) | `price` = current B2B dealer price |
| `product.template.x_mt_availability` | In stock / Limited stock / Out of stock / Not listed |
| `product.template.x_mt_synced` | timestamp of last sync |
| `x_supply_state` (existing computed field) | now also counts Multitech stock |

Matching is by supplier code (B2B SKU = `product_code` on the Multitech supplier row).
Cost (`standard_price`) and sale prices are **not** touched.
"Not listed" is only set when the crawl returned ≥1,500 products, so a broken crawl can't wipe stock.

## Setup (once, ~5 min)

1. **Create a private GitHub repo** (e.g. `trendnet-multitech-sync`) and push these files.
2. **Odoo API key**: trendnet.com.cy → avatar → My Preferences → Account Security → New API Key.
3. Repo → **Settings → Secrets and variables → Actions**:
   - Secrets: `MT_USER` (B2B e-mail), `MT_PASS` (B2B password), `ODOO_API_KEY`
   - Variables (optional): `ODOO_USER` (default george@trendnet.com.cy), `DRY_RUN`
4. **Actions → Multitech stock sync → Run workflow**, command `setup`, untick dry run.
   Creates the 2 fields and adds Multitech to `x_supply_state` (old compute saved in the run's report artifact).
5. Run again: command `sync`, **dry run ticked**. Check the run summary + `report` artifact
   (price_changes / stock_changes / not_in_odoo CSVs).
6. Happy with it → set repo variable `DRY_RUN` = `0`. From then on it runs daily at 07:17 (summer) / 06:17 (winter) Cyprus time.

A failed run (bad login, site layout change, Odoo down) turns red and GitHub e-mails you; nothing is written in that case.

## Run locally

```bash
pip install -r requirements.txt
export MT_USER=... MT_PASS=... ODOO_API_KEY=... DRY_RUN=1
python multitech_sync.py sync
```
