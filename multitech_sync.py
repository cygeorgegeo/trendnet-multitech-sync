#!/usr/bin/env python3
"""
Multitech B2B -> Trendnet Odoo daily sync.

Crawls https://b2b.multitech.com.cy/en/products (logged in), reads SKU, dealer
price (excl. VAT) and stock level for every product, and updates the live Odoo
at trendnet.com.cy:

  * product.supplierinfo (vendor = Multitech, partner 18): price
  * product.template: x_mt_availability ("In stock" / "Limited stock" /
    "Out of stock" / "Not listed"), x_mt_synced (timestamp)

x_supply_state (the existing computed "In stock / Out of stock / No feed"
field) picks up x_mt_availability once `setup` has been run.

Commands
  python multitech_sync.py setup   one-time: create the 2 fields, extend x_supply_state
  python multitech_sync.py sync    daily crawl + update (DRY_RUN=1 -> report only)

Environment
  MT_USER, MT_PASS                 Multitech B2B login (e-mail + password)
  ODOO_URL                         default https://trendnet.com.cy
  ODOO_DB                          default trendnet-security-ltd1
  ODOO_USER                        default george@trendnet.com.cy
  ODOO_API_KEY                     Odoo API key (Preferences > Account Security)
  MT_PARTNER_ID                    default 18
  DRY_RUN                          "1" (default) = no writes, "0" = write
  REPORT_DIR                       default ./report
"""
import csv
import datetime as dt
import os
import re
import sys
import time
import xmlrpc.client
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

B2B = "https://b2b.multitech.com.cy"
UA = "Mozilla/5.0 (X11; Linux x86_64) TrendnetStockSync/1.0 (+https://trendnet.com.cy)"

ODOO_URL = os.environ.get("ODOO_URL", "https://trendnet.com.cy").rstrip("/")
ODOO_DB = os.environ.get("ODOO_DB", "trendnet-security-ltd1")
ODOO_USER = os.environ.get("ODOO_USER", "george@trendnet.com.cy")
ODOO_KEY = os.environ.get("ODOO_API_KEY", "")
PARTNER_ID = int(os.environ.get("MT_PARTNER_ID", "18"))
DRY_RUN = os.environ.get("DRY_RUN", "1").strip().lower() not in ("0", "false", "no")
REPORT_DIR = os.environ.get("REPORT_DIR", "report")

# Safety: never mark products "Not listed" unless the crawl looks complete.
MIN_ITEMS = 1500
WORKERS = int(os.environ.get("MT_WORKERS", "6"))  # parallel B2B page requests

# Greek look-alike letters appear in the site's labels ("In Stοck" with Greek omicron)
_GREEK = str.maketrans("ΑΒΕΖΗΙΚΜΝΟΡΤΥΧαοιρνκτ", "ABEZHIKMNOPTYXaoipvkt")


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- B2B

def b2b_login(user, password):
    s = requests.Session()
    s.headers["User-Agent"] = UA
    r = s.get(f"{B2B}/en/user/login", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    form = soup.select_one("form#user-login") or soup.select_one("form[id^=user-login]")
    if not form:
        raise RuntimeError("B2B login form not found")
    data = {i["name"]: i.get("value", "") for i in form.select("input[name]") if i.get("type") in ("hidden", "submit")}
    data.update({"name": user, "pass": password})
    action = form.get("action") or "/en/user/login"
    r = s.post(B2B + action if action.startswith("/") else action, data=data, timeout=30)
    r.raise_for_status()
    if "/user/logout" not in r.text:
        # Drupal sometimes lands on another page; check a protected one
        if "/user/logout" not in s.get(f"{B2B}/en/user", timeout=30).text:
            raise RuntimeError("B2B login failed - check MT_USER / MT_PASS")
    return s


def parse_price(txt):
    if not txt:
        return None
    t = re.sub(r"[^\d,.]", "", txt)
    if not t:
        return None
    if "," in t:  # European format 1.234,56
        t = t.replace(".", "").replace(",", ".")
    try:
        return round(float(t), 4)
    except ValueError:
        return None


def normalize_stock(el):
    """Map the stock-level badge to a canonical availability string."""
    if el is None:
        return "Unknown"
    classes = el.get("class", [])
    title = (el.get("title") or " ".join(classes)).translate(_GREEK).strip()
    tl = title.lower()
    if "out" in tl or any(c in classes for c in ("stock-out", "stock-bad", "stock-danger", "stock-none")):
        return "Out of stock"
    if "limited" in tl or "stock-warning" in classes:
        return "Limited stock"
    if "in stock" in tl or "stock-good" in classes:
        return "In stock"
    return title[:40] or "Unknown"


def parse_listing(html):
    soup = BeautifulSoup(html, "lxml")
    items = []
    for title in soup.select(".field-name-title-field"):
        card = title.parent
        sku = ""
        for el in card.select(".commerce-product-sku"):
            txt = el.get_text(" ", strip=True)
            if txt.upper().startswith("SKU"):
                sku = re.sub(r"^SKU:?\s*", "", txt, flags=re.I).strip()
                break
        if not sku:
            continue
        price_el = card.select_one(".commerce-product-field-commerce-price")
        items.append({
            "sku": sku,
            "name": title.get_text(" ", strip=True),
            "price": parse_price(price_el.get_text(" ", strip=True) if price_el else ""),
            "stock": normalize_stock(card.select_one(".stock-level")),
        })
    last = None
    a = soup.select_one(".pager-last a, li.pager-last a")
    if a and (m := re.search(r"[?&]page=(\d+)", a.get("href", ""))):
        last = int(m.group(1))
    return items, last


def b2b_crawl(s):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    t0 = time.time()

    def get(page):
        url = f"{B2B}/en/products" + (f"?page={page}" if page else "")
        last_err = ""
        for attempt in range(3):
            try:
                t = time.time()
                r = s.get(url, timeout=40)
                if r.status_code == 200:
                    if page and page % 20 == 0:
                        log(f"  page {page}: {time.time() - t:.1f}s")
                    return r.text
                last_err = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                last_err = type(e).__name__
            log(f"  page {page} attempt {attempt + 1} failed ({last_err})")
            time.sleep(3 + attempt * 5)
        raise RuntimeError(f"B2B page {page} failed: {last_err}")

    first, last = parse_listing(get(0))
    if last is None:
        raise RuntimeError("Could not read pager on /en/products (layout changed or not logged in)")
    log(f"B2B: {last + 1} pages, first page {time.time() - t0:.1f}s")
    items = list(first)
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(get, p): p for p in range(1, last + 1)}
        for f in as_completed(futs):
            page_items, _ = parse_listing(f.result())
            items.extend(page_items)
            done += 1
            if done % 25 == 0:
                log(f"  {done}/{last} pages, {len(items)} items, {time.time() - t0:.0f}s")
    by_sku = {}
    for it in items:
        by_sku.setdefault(it["sku"].strip().upper(), it)
    log(f"B2B: {len(items)} cards, {len(by_sku)} unique SKUs in {time.time() - t0:.0f}s")
    return by_sku


# -------------------------------------------------------------------------- Odoo

class Odoo:
    def __init__(self):
        if not ODOO_KEY:
            raise RuntimeError("ODOO_API_KEY not set")
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
        self.uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_KEY, {})
        if not self.uid:
            raise RuntimeError("Odoo authentication failed")
        self.m = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

    def __call__(self, model, method, *args, **kw):
        return self.m.execute_kw(ODOO_DB, self.uid, ODOO_KEY, model, method, list(args), kw)


SUPPLY_STATE_COMPUTE = """for record in self:
    a = (record['x_asbis_availability'] or '').strip()
    m = (record['x_mst_availability'] or '').strip().lower()
    s = (record['x_sb_availability'] or '').strip().lower()
    sb = record['x_sb_stock'] or 0
    mt = (record['x_mt_availability'] or '').strip().lower()
    has_stock = False
    if a and a != '0':
        has_stock = True
    if m == 'in stock':
        has_stock = True
    if sb > 0:
        has_stock = True
    if mt in ('in stock', 'limited stock'):
        has_stock = True
    if has_stock:
        record['x_supply_state'] = 'available'
    elif a == '0' or m == 'out of stock' or s == 'out of stock' or mt in ('out of stock', 'not listed'):
        record['x_supply_state'] = 'out'
    else:
        record['x_supply_state'] = 'not_fed'"""
SUPPLY_STATE_DEPENDS = "x_asbis_availability,x_mst_availability,x_sb_availability,x_sb_stock,x_mt_availability"


def setup():
    od = Odoo()
    model_id = od("ir.model", "search", [("model", "=", "product.template")])[0]
    for name, label, ttype in (("x_mt_availability", "Multitech availability", "char"),
                               ("x_mt_synced", "Multitech last sync", "datetime")):
        if od("ir.model.fields", "search", [("model", "=", "product.template"), ("name", "=", name)]):
            log(f"{name}: exists")
        else:
            fid = od("ir.model.fields", "create", {"model_id": model_id, "name": name, "field_description": label,
                                                   "ttype": ttype, "store": True, "copied": False})
            log(f"{name}: created ({fid})")
    fid = od("ir.model.fields", "search", [("model", "=", "product.template"), ("name", "=", "x_supply_state")])
    if not fid:
        log("x_supply_state not found - skipped")
        return
    cur = od("ir.model.fields", "read", fid, fields=["compute", "depends"])[0]
    if "x_mt_availability" in (cur["depends"] or ""):
        log("x_supply_state: already includes Multitech")
    else:
        os.makedirs(REPORT_DIR, exist_ok=True)
        with open(os.path.join(REPORT_DIR, "x_supply_state_compute_backup.py"), "w") as f:
            f.write(f"# depends: {cur['depends']}\n{cur['compute']}\n")
        od("ir.model.fields", "write", fid, {"depends": SUPPLY_STATE_DEPENDS, "compute": SUPPLY_STATE_COMPUTE})
        log("x_supply_state: Multitech added (old compute backed up in report/)")


def chunks(seq, n=200):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


RANK = {"In stock": 4, "Limited stock": 3, "Unknown": 2, "Out of stock": 1, "Not listed": 0}


def sync():
    user, pw = os.environ.get("MT_USER"), os.environ.get("MT_PASS")
    if not (user and pw):
        raise RuntimeError("MT_USER / MT_PASS not set")
    log(f"Mode: {'DRY RUN (no writes)' if DRY_RUN else 'LIVE'}")

    feed = b2b_crawl(b2b_login(user, pw))
    complete = len(feed) >= MIN_ITEMS

    od = Odoo()
    tf = od("ir.model.fields", "search_read", [("model", "=", "product.template"),
                                                ("name", "in", ["x_mt_availability", "x_mt_synced"])], fields=["name"])
    have_fields = {f["name"] for f in tf}
    if not DRY_RUN and len(have_fields) < 2:
        raise RuntimeError("Run `setup` first - x_mt_availability / x_mt_synced missing in Odoo")

    rows = od("product.supplierinfo", "search_read", [("partner_id", "=", PARTNER_ID)],
              fields=["id", "product_code", "price", "product_tmpl_id"])
    log(f"Odoo: {len(rows)} Multitech supplier rows")
    tmpl_ids = sorted({r["product_tmpl_id"][0] for r in rows if r["product_tmpl_id"]})
    cur_avail = {}
    if "x_mt_availability" in have_fields:
        for ch in chunks(tmpl_ids, 1000):
            for t in od("product.template", "read", ch, fields=["x_mt_availability"]):
                cur_avail[t["id"]] = t["x_mt_availability"] or ""

    price_changes, tmpl_target = [], {}
    matched_skus = set()
    for r in rows:
        code = (r["product_code"] or "").strip().upper()
        tid = r["product_tmpl_id"][0] if r["product_tmpl_id"] else None
        it = feed.get(code) if code else None
        if it:
            matched_skus.add(code)
            if it["price"] is not None and abs((r["price"] or 0) - it["price"]) > 0.005:
                price_changes.append((r["id"], code, r["price"], it["price"]))
            avail = it["stock"]
        else:
            avail = "Not listed" if (code and complete) else None
        if tid and avail and RANK.get(avail, 2) >= RANK.get(tmpl_target.get(tid, "Not listed"), 0):
            tmpl_target[tid] = avail

    avail_changes = {tid: a for tid, a in tmpl_target.items() if cur_avail.get(tid, "") != a}
    new_skus = [feed[k] for k in feed if k not in matched_skus]

    # ---------------------------------------------------------------- report
    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d_%H%M")
    with open(os.path.join(REPORT_DIR, f"price_changes_{stamp}.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["supplierinfo_id", "sku", "old_price", "new_price", "diff_pct"])
        for sid, code, old, new in sorted(price_changes, key=lambda x: x[1]):
            w.writerow([sid, code, old, new, round((new - old) / old * 100, 1) if old else ""])
    with open(os.path.join(REPORT_DIR, f"stock_changes_{stamp}.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product_tmpl_id", "old", "new"])
        for tid, a in sorted(avail_changes.items()):
            w.writerow([tid, cur_avail.get(tid, ""), a])
    with open(os.path.join(REPORT_DIR, f"not_in_odoo_{stamp}.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sku", "name", "price", "stock"])
        for it in sorted(new_skus, key=lambda x: x["sku"]):
            w.writerow([it["sku"], it["name"], it["price"], it["stock"]])

    dist = defaultdict(int)
    for a in tmpl_target.values():
        dist[a] += 1
    summary = [
        f"## Multitech sync - {'DRY RUN' if DRY_RUN else 'LIVE'}",
        f"- B2B products: **{len(feed)}** ({'complete' if complete else 'INCOMPLETE - Not listed marking skipped'})",
        f"- Odoo Multitech rows: {len(rows)}, matched by SKU: **{len(matched_skus)}**",
        f"- Price changes: **{len(price_changes)}**",
        f"- Availability changes: **{len(avail_changes)}**",
        f"- Multitech SKUs not in Odoo: {len(new_skus)} (see not_in_odoo CSV)",
        "- Availability now: " + ", ".join(f"{k} {v}" for k, v in sorted(dist.items(), key=lambda x: -x[1])),
    ]
    big = sorted(price_changes, key=lambda x: -abs((x[3] - x[2]) / x[2]) if x[2] else 0)[:15]
    if big:
        summary += ["", "| SKU | old € | new € |", "|---|---:|---:|"] + [f"| {c} | {o:.2f} | {n:.2f} |" for _, c, o, n in big]
    text = "\n".join(summary)
    log(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")

    if DRY_RUN:
        log("Dry run - nothing written.")
        return

    # ---------------------------------------------------------------- writes
    by_price = defaultdict(list)
    for sid, _, _, new in price_changes:
        by_price[new].append(sid)
    for price, ids in by_price.items():
        for ch in chunks(ids):
            od("product.supplierinfo", "write", ch, {"price": price})
    by_avail = defaultdict(list)
    for tid, a in avail_changes.items():
        by_avail[a].append(tid)
    for a, ids in by_avail.items():
        for ch in chunks(ids):
            od("product.template", "write", ch, {"x_mt_availability": a})
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for ch in chunks(list(tmpl_target), 500):
        od("product.template", "write", ch, {"x_mt_synced": now})
    log(f"Written: {len(price_changes)} prices, {len(avail_changes)} availabilities, {len(tmpl_target)} sync stamps.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    try:
        {"setup": setup, "sync": sync}[cmd]()
    except KeyError:
        sys.exit("usage: multitech_sync.py [setup|sync]")
    except Exception as e:  # make the GitHub run fail visibly (GitHub e-mails on failure)
        log(f"ERROR: {e}")
        sys.exit(1)
