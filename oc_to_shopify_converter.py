"""
OpenCart to Shopify Migration Tool  v2.0  (optimized)
======================================================
Rebuilt on the same unified pattern as the Squarespace v2 converter:
complete Shopify product columns, one shipping/tax/transaction row per
ORDER (not per line item), customer cross-referencing for order billing/
shipping, duplicate-email detection, and an argparse CLI.

USAGE:
    python oc_to_shopify_converter_v2.py --type products
    python oc_to_shopify_converter_v2.py --type orders
    python oc_to_shopify_converter_v2.py --type customers
    python oc_to_shopify_converter_v2.py --type all
    python oc_to_shopify_converter_v2.py                     (interactive menu)

Optional custom paths (single-type only):
    python oc_to_shopify_converter_v2.py --type products --input input/my_products.csv --output output/my_result.csv

Orders need customer + product lookups even when run standalone:
    --customers-file input/other_customers.csv
    --products-file  input/other_products.csv

------------------------------------------------------------------------------
IMPORTANT ASSUMPTIONS / LIMITATIONS (read before running on real data)
------------------------------------------------------------------------------
1. VARIANTS: true OpenCart option data lives in oc_product_option /
   oc_product_option_value, which aren't in a flat product export. This
   script groups rows by master_id (falling back to product_id when
   master_id is 0/blank), and looks for Option1/2/3 Name+Value columns.
   If those aren't present, it falls back to a generic "Variant" / N label
   on the "variant" column, same as before. Send oc_product_option(_value)
   exports when you have them and I'll wire in real option names.
2. PRODUCT NAME: uses a real name/title column if present (name,
   product_name, title); falls back to "model" only if none exist.
3. IMAGES: OpenCart gives relative paths — edit OPENCART_STORE_IMAGE_BASE
   below to your store's real image base URL.
4. WEIGHT: weight_class_id mapping (1=kg, 2=lb, 3=g, 4=oz) is OpenCart's
   common default — verify against Setup > Localisation > Weight Classes.
5. ORDERS: now grouped by order_id, so multiple oc_order_product rows per
   order (same order_id repeated) are combined into one order with several
   line items, one shipping line, and one transaction — instead of assuming
   one line item per order.
------------------------------------------------------------------------------

REQUIREMENTS:
  pip install pandas
"""

import argparse
import csv
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

# ============================================================
# CONFIG - EDIT THESE FOR YOUR STORE
# ============================================================

OPENCART_STORE_IMAGE_BASE = "https://yourstore.com/image/"

WEIGHT_CLASS_TO_GRAMS = {
    "1": 1000.0,       # kilogram
    "2": 453.592,      # pound
    "3": 1.0,          # gram
    "4": 28.3495,      # ounce
}

ORDER_STATUS_TO_FINANCIAL_STATUS = {
    "complete": "paid", "processing": "pending", "pending": "pending",
    "shipped": "paid", "refunded": "refunded", "cancelled": "voided",
    "canceled": "voided", "denied": "voided", "failed": "voided",
}

SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_DIR  = SCRIPT_DIR / "input"
OUTPUT_DIR = SCRIPT_DIR / "shopify_output"

DEFAULTS = {
    "products":  INPUT_DIR / "oc_products.csv",
    "orders":    INPUT_DIR / "oc_orders.csv",
    "customers": INPUT_DIR / "oc_customers.csv",
}
DEFAULT_OUTPUTS = {
    "products":  OUTPUT_DIR / "shopify_products.csv",
    "orders":    OUTPUT_DIR / "shopify_orders.csv",
    "customers": OUTPUT_DIR / "shopify_customers.csv",
}

# ── Unified Shopify headers (same schema as the Squarespace v2 script,
#    so every migration tool now feeds Shopify identically) ────────────
PRODUCT_HEADERS = [
    "Handle","Title","Body (HTML)","Vendor","Type","Tags","Published",
    "Option1 Name","Option1 Value","Option2 Name","Option2 Value",
    "Option3 Name","Option3 Value",
    "Variant SKU","Variant Grams","Variant Inventory Tracker",
    "Variant Inventory Qty","Variant Inventory Policy",
    "Variant Fulfillment Service","Variant Price","Variant Compare At Price",
    "Variant Requires Shipping","Variant Taxable","Variant Barcode",
    "Image Src","Image Alt Text","SEO Title","SEO Description","Status",
]  # 29 columns — every inventory/fulfillment column populated.

CUSTOMER_HEADERS = [
    "First Name","Last Name","Email","Accepts Email Marketing",
    "Default Address Company","Default Address Address1",
    "Default Address Address2","Default Address City",
    "Default Address Province Code","Default Address Country Code",
    "Default Address Zip","Default Address Phone","Phone",
    "Accepts SMS Marketing","Note","Tax Exempt","Tags",
]  # 17 columns

ORDER_HEADERS = [
    "Name","Email","Phone","Currency","Payment: Status","Processed At",
    "Send Receipt","Inventory Behaviour","Note","Tags",
    "Billing: First Name","Billing: Last Name","Billing: Company","Billing: Phone",
    "Billing: Address 1","Billing: Address 2","Billing: City",
    "Billing: Province","Billing: Province Code","Billing: Country",
    "Billing: Country Code","Billing: Zip",
    "Shipping: First Name","Shipping: Last Name","Shipping: Company","Shipping: Phone",
    "Shipping: Address 1","Shipping: Address 2","Shipping: City",
    "Shipping: Province","Shipping: Province Code","Shipping: Country",
    "Shipping: Country Code","Shipping: Zip",
    "Line: Type","Line: Title","Line: Quantity","Line: Price","Line: SKU",
    "Line: Grams","Line: Requires Shipping","Line: Taxable","Line: Discount",
    "Tax 1: Title","Tax 1: Price","Tax 1: Rate",
    "Transaction: Amount","Transaction: Currency","Transaction: Kind",
    "Transaction: Status","Transaction: Gateway",
]  # 51 columns — one Shipping Line + one Transaction row PER ORDER.


# ═══════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════

def log(msg, level="INFO"):
    tag = {"INFO":"i ","OK":"OK","WARN":"! ","ERROR":"X ","STEP":">>"}
    print("  [{}]  {}".format(tag.get(level,"  "), msg))


def write_csv(rows, headers, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    clean = [[v.replace("\n"," ").replace("\r"," ").strip() if isinstance(v, str) else v
              for v in row] for row in rows]
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f, quoting=csv.QUOTE_ALL).writerows([headers] + clean)
    log("Saved → {}  ({:,} bytes)".format(output_path.resolve(), output_path.stat().st_size), "OK")


def read_file(path):
    path = Path(path)
    if not path.exists():
        log("File not found: {}".format(path.resolve()), "ERROR")
        return None
    try:
        try:    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
        except: df = pd.read_csv(path, encoding="latin-1", dtype=str)
    except Exception as e:
        log("Cannot read {}: {}".format(path.name, e), "ERROR")
        return None
    df = df.fillna("")
    if df.empty:
        log("Input CSV has no rows: {}".format(path.name), "ERROR")
        return None
    return df


def cv(val, default=""):
    """clean_value — strip whitespace / stray literal quotes."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    s = str(val).strip()
    if s.startswith('"') and s.endswith('"') and len(s) > 1:
        s = s[1:-1].strip()
    return s if s.lower() not in ("nan", "none", "") else default


def sfloat(val, default=""):
    try:
        if cv(val) == "": return default
        return "{:.2f}".format(float(val))
    except Exception:
        return default


def sint(val, default=0):
    try:
        if cv(val) == "": return default
        return int(float(val))
    except Exception:
        return default


def find_col(df, candidates):
    lower_map = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c in df.columns: return c
        if c.lower() in lower_map: return lower_map[c.lower()]
    return None


def gv(row, col, default=""):
    if col is None: return default
    return cv(row.get(col, default), default)


def sanitize_handle(text):
    text = cv(text)
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return re.sub(r"-+", "-", text).strip("-")


def clean_phone(phone):
    phone = cv(phone)
    if not phone: return ""
    d = re.sub(r"[^\d+]", "", phone)
    if d and not d.startswith("+"):
        d = "+1" + d if len(d) == 10 else "+" + d
    return d


def fmt_date(raw):
    try:
        return pd.to_datetime(raw).strftime("%Y-%m-%d %H:%M:%S +0000")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S +0000")


def build_image_url(relative_path):
    """OpenCart gives a relative catalog path — prepend the configured
    store image base unless it's already a full URL."""
    path = cv(relative_path)
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    return OPENCART_STORE_IMAGE_BASE.rstrip("/") + "/" + path.lstrip("/")


def weight_to_grams(weight, weight_class_id):
    weight = cv(weight)
    if not weight:
        return ""
    try:
        weight_val = float(weight)
    except ValueError:
        return ""
    factor = WEIGHT_CLASS_TO_GRAMS.get(cv(weight_class_id), 1000.0)
    return str(round(weight_val * factor, 2))


def map_financial_status(order_status):
    return ORDER_STATUS_TO_FINANCIAL_STATUS.get(cv(order_status).lower(), "pending")


def marketing_to_yesno(value):
    return "yes" if cv(value).lower() in ("1", "true", "yes") else "no"


# ═══════════════════════════════════════════════════════════════
# CUSTOMERS
# ═══════════════════════════════════════════════════════════════

def load_customers_lookup(path):
    df = read_file(path)
    if df is None:
        return [], {}

    c_id      = find_col(df, ["customer_id"])
    c_first   = find_col(df, ["firstname","first_name"])
    c_last    = find_col(df, ["lastname","last_name"])
    c_email   = find_col(df, ["email"])
    c_phone   = find_col(df, ["telephone","phone"])
    c_company = find_col(df, ["company"])
    c_addr1   = find_col(df, ["address_1","address1"])
    c_addr2   = find_col(df, ["address_2","address2"])
    c_city    = find_col(df, ["city"])
    c_zone    = find_col(df, ["zone_code","zone","province","state"])
    c_zip     = find_col(df, ["postcode","zip"])
    c_country = find_col(df, ["country_code","country"])
    c_news    = find_col(df, ["newsletter"])
    c_created = find_col(df, ["date_added","created_at"])

    if c_email:
        emails = df[c_email].astype(str).str.strip()
        dupes = emails[(emails != "") & emails.duplicated(keep=False)]
        if not dupes.empty:
            log("{} duplicate email(s) found — Shopify keeps only the LAST row "
                "per duplicate on import:".format(dupes.nunique()), "WARN")
            for email in dupes.unique():
                log("  - {}".format(email), "WARN")

    rows, lookup = [], {}
    skipped = 0

    for _, row in df.iterrows():
        email = gv(row, c_email)
        if not email or "@" not in email:
            skipped += 1
            continue

        zone_raw    = gv(row, c_zone)
        prov_code   = zone_raw.upper() if zone_raw and len(zone_raw) <= 4 else ""
        prov_name   = zone_raw if zone_raw and len(zone_raw) > 4 else ""
        country_raw = gv(row, c_country, "US")
        cc          = country_raw.upper() if len(country_raw) <= 2 else country_raw[:2].upper()
        phone = clean_phone(gv(row, c_phone))

        note_parts = []
        cust_id_val = gv(row, c_id)
        if cust_id_val:
            note_parts.append("OpenCart customer_id: {}".format(cust_id_val))
        joined = gv(row, c_created)
        if joined:
            note_parts.append("OpenCart joined date: {}".format(joined))

        rows.append([
            gv(row, c_first), gv(row, c_last), email,
            marketing_to_yesno(gv(row, c_news)),
            gv(row, c_company), gv(row, c_addr1), gv(row, c_addr2), gv(row, c_city),
            prov_code, cc, gv(row, c_zip), phone, phone,
            "no", " | ".join(note_parts), "no", "",
        ])

        if cust_id_val:
            lookup[cust_id_val] = {
                "first": gv(row, c_first), "last": gv(row, c_last),
                "email": email, "phone": phone, "company": gv(row, c_company),
                "addr1": gv(row, c_addr1), "addr2": gv(row, c_addr2),
                "city": gv(row, c_city),
                "prov_name": prov_name, "prov_code": prov_code,
                "country_name": country_raw if len(country_raw) > 2 else "",
                "cc": cc, "zip": gv(row, c_zip),
            }

    log("Customers converted : {}".format(len(rows)), "OK")
    log("Customers skipped   : {} (no valid email)".format(skipped),
        "OK" if skipped == 0 else "WARN")
    return rows, lookup


# ═══════════════════════════════════════════════════════════════
# PRODUCTS
# ═══════════════════════════════════════════════════════════════

def get_group_id(row, c_master, c_pid):
    master = gv(row, c_master, "0")
    pid    = gv(row, c_pid)
    if master and master != "0":
        return master
    return pid


def load_products(path):
    df = read_file(path)
    if df is None:
        return [], {}

    c_pid     = find_col(df, ["product_id"])
    c_master  = find_col(df, ["master_id"])
    c_model   = find_col(df, ["model"])
    c_name    = find_col(df, ["name","product_name","title"])
    c_desc    = find_col(df, ["description","product_description"])
    c_manu    = find_col(df, ["manufacturer","manufacturer_name","brand"])
    c_status  = find_col(df, ["status"])
    c_sku     = find_col(df, ["sku"])
    c_price   = find_col(df, ["price"])
    c_qty     = find_col(df, ["quantity"])
    c_weight  = find_col(df, ["weight"])
    c_wclass  = find_col(df, ["weight_class_id"])
    c_image   = find_col(df, ["image"])
    c_variant = find_col(df, ["variant"])
    c_ean     = find_col(df, ["ean"])
    c_upc     = find_col(df, ["upc"])
    c_mpn     = find_col(df, ["mpn"])
    c_isbn    = find_col(df, ["isbn"])

    if not c_pid:
        log("No 'product_id' column found — cannot group variants.", "ERROR")
        return [], {}

    opt_cols = []
    for i in (1, 2, 3):
        n_col = find_col(df, ["Option{} Name".format(i)])
        v_col = find_col(df, ["Option{} Value".format(i)])
        if n_col and v_col:
            opt_cols.append((n_col, v_col))

    group_order, groups = [], {}
    for _, row in df.iterrows():
        gid = get_group_id(row, c_master, c_pid)
        if not gid:
            continue
        if gid not in groups:
            groups[gid] = []
            group_order.append(gid)
        groups[gid].append(row)

    all_rows, sku_lookup = [], {}
    fallback_variant_count = 0
    multi_variant_groups = 0
    converted = 0

    for gid in group_order:
        rows = groups[gid]
        parent = next((r for r in rows if gv(r, c_pid) == gid), rows[0])
        is_multi = len(rows) > 1
        if is_multi:
            multi_variant_groups += 1

        handle = sanitize_handle(gv(parent, c_model)) or sanitize_handle(gid)
        title  = gv(parent, c_name) or gv(parent, c_model)
        body   = gv(parent, c_desc)
        vendor = gv(parent, c_manu)
        status_raw = gv(parent, c_status, "1")
        published = "TRUE" if status_raw == "1" else "FALSE"
        status = "active" if published == "TRUE" else "draft"

        for i, row in enumerate(rows):
            is_first = (i == 0)

            if opt_cols:
                opt_names = [gv(row, opt_cols[j][0]) for j in range(len(opt_cols))]
                opt_vals  = [gv(row, opt_cols[j][1]) for j in range(len(opt_cols))]
            elif is_multi:
                raw_variant = gv(row, c_variant)
                if raw_variant:
                    opt_vals = [raw_variant]
                else:
                    opt_vals = ["Variant {}".format(i + 1)]
                    fallback_variant_count += 1
                opt_names = ["Variant"]
            else:
                opt_names, opt_vals = [], []

            while len(opt_names) < 3: opt_names.append("")
            while len(opt_vals)  < 3: opt_vals.append("")
            if not opt_names[0]:
                opt_names[0], opt_vals[0] = "Title", "Default Title"

            sku = gv(row, c_sku) or gv(row, c_model)
            price = sfloat(gv(row, c_price))
            qty   = sint(gv(row, c_qty))
            grams = weight_to_grams(gv(row, c_weight), gv(row, c_wclass))
            barcode = gv(row, c_ean) or gv(row, c_upc) or gv(row, c_mpn) or gv(row, c_isbn)
            image_url = build_image_url(gv(row, c_image))

            all_rows.append([
                handle,
                title if is_first else "", body if is_first else "",
                vendor if is_first else "", "" , "" if is_first else "",
                published if is_first else "",
                opt_names[0], opt_vals[0], opt_names[1], opt_vals[1],
                opt_names[2], opt_vals[2],
                sku, grams, "shopify", qty, "deny", "manual",
                price, "", "TRUE", "TRUE", barcode,
                image_url, title if image_url else "",
                title if is_first else "", body if is_first else "",
                status,
            ])
            if sku:
                sku_lookup[sku] = {"title": title, "price": price or "0.00"}

        converted += 1

    log("Products converted  : {} groups → {} CSV rows".format(converted, len(all_rows)), "OK")
    if multi_variant_groups:
        log("Groups with variants detected: {}".format(multi_variant_groups), "OK")
    else:
        log("No multi-row (variant) groups detected — every product came through "
            "as standalone.", "INFO")
    if fallback_variant_count:
        log("{} variant row(s) had no option data — labeled generically as "
            "'Variant N'. Provide oc_product_option(_value) to fix this.".format(
                fallback_variant_count), "WARN")
    return all_rows, sku_lookup


# ═══════════════════════════════════════════════════════════════
# ORDERS
# ═══════════════════════════════════════════════════════════════

def _blank_order_row():
    return [""] * len(ORDER_HEADERS)


def load_orders(path, customer_lookup, sku_lookup):
    df = read_file(path)
    if df is None:
        return []

    c_id       = find_col(df, ["order_id"])
    c_custid   = find_col(df, ["customer_id"])
    c_email    = find_col(df, ["email"])
    c_date     = find_col(df, ["date_added","created_at"])
    c_status   = find_col(df, ["order_status"])
    c_currency = find_col(df, ["currency"])
    c_shipping = find_col(df, ["shipping"])
    c_tax      = find_col(df, ["tax"])
    c_total    = find_col(df, ["total"])
    c_product  = find_col(df, ["product"])
    c_model    = find_col(df, ["model"])
    c_sku      = find_col(df, ["sku"])
    c_qty      = find_col(df, ["quantity"])
    c_price    = find_col(df, ["price"])
    c_shipmeth = find_col(df, ["shipping_method"])
    c_paymeth  = find_col(df, ["payment_method"])
    c_custname = find_col(df, ["customer"])
    c_phone    = find_col(df, ["telephone"])
    c_invoice  = find_col(df, ["invoice_no"])

    if not c_id:
        log("No 'order_id' column found — cannot group line items.", "ERROR")
        return []

    order_order, groups = [], {}
    for _, row in df.iterrows():
        oid = gv(row, c_id)
        if not oid:
            continue
        if oid not in groups:
            groups[oid] = []
            order_order.append(oid)
        groups[oid].append(row)

    all_rows = []
    missing_customer = 0
    missing_sku = 0

    for oid in order_order:
        rows  = groups[oid]
        first = rows[0]

        order_name = "#" + oid
        email      = gv(first, c_email)
        currency   = gv(first, c_currency, "USD")
        processed  = fmt_date(gv(first, c_date))
        pay_status = map_financial_status(gv(first, c_status))
        invoice    = gv(first, c_invoice)

        cust_id = gv(first, c_custid)
        cust = customer_lookup.get(cust_id)
        if not cust:
            missing_customer += 1
            name_parts = gv(first, c_custname).split(" ", 1)
            cust = {
                "first": name_parts[0] if name_parts else "",
                "last": name_parts[1] if len(name_parts) > 1 else "",
                "phone": clean_phone(gv(first, c_phone)),
                "company": "", "addr1": "", "addr2": "", "city": "",
                "prov_name": "", "prov_code": "", "country_name": "",
                "cc": "US", "zip": "",
            }

        shipping = sfloat(gv(first, c_shipping))
        tax      = sfloat(gv(first, c_tax))
        total    = sfloat(gv(first, c_total))

        for li_idx, li_row in enumerate(rows):
            sku = gv(li_row, c_sku) or gv(li_row, c_model)
            qty = sint(gv(li_row, c_qty), 1)
            prod = sku_lookup.get(sku)
            if not prod:
                missing_sku += 1
            item_title = gv(li_row, c_product) or (prod["title"] if prod else sku or "Item")
            item_price = sfloat(gv(li_row, c_price)) or (prod["price"] if prod else "0.00")

            row_out = _blank_order_row()
            if li_idx == 0:
                row_out[ORDER_HEADERS.index("Name")]  = order_name
                row_out[ORDER_HEADERS.index("Email")] = email
                row_out[ORDER_HEADERS.index("Phone")] = cust["phone"]
                if invoice:
                    row_out[ORDER_HEADERS.index("Note")] = "Imported from OpenCart invoice #{}".format(invoice)

            row_out[ORDER_HEADERS.index("Currency")]         = currency
            row_out[ORDER_HEADERS.index("Payment: Status")]  = pay_status
            row_out[ORDER_HEADERS.index("Processed At")]     = processed
            row_out[ORDER_HEADERS.index("Send Receipt")]     = "FALSE"
            row_out[ORDER_HEADERS.index("Inventory Behaviour")] = "bypass"

            row_out[ORDER_HEADERS.index("Billing: First Name")] = cust["first"]
            row_out[ORDER_HEADERS.index("Billing: Last Name")]  = cust["last"]
            row_out[ORDER_HEADERS.index("Billing: Company")]    = cust.get("company","")
            row_out[ORDER_HEADERS.index("Billing: Phone")]      = cust["phone"]
            row_out[ORDER_HEADERS.index("Billing: Address 1")]  = cust["addr1"]
            row_out[ORDER_HEADERS.index("Billing: Address 2")]  = cust["addr2"]
            row_out[ORDER_HEADERS.index("Billing: City")]       = cust["city"]
            row_out[ORDER_HEADERS.index("Billing: Province")]      = cust["prov_name"]
            row_out[ORDER_HEADERS.index("Billing: Province Code")] = cust["prov_code"]
            row_out[ORDER_HEADERS.index("Billing: Country")]        = cust["country_name"]
            row_out[ORDER_HEADERS.index("Billing: Country Code")]  = cust["cc"]
            row_out[ORDER_HEADERS.index("Billing: Zip")]            = cust["zip"]
            row_out[ORDER_HEADERS.index("Shipping: First Name")] = cust["first"]
            row_out[ORDER_HEADERS.index("Shipping: Last Name")]  = cust["last"]
            row_out[ORDER_HEADERS.index("Shipping: Company")]    = cust.get("company","")
            row_out[ORDER_HEADERS.index("Shipping: Address 1")]  = cust["addr1"]
            row_out[ORDER_HEADERS.index("Shipping: Address 2")]  = cust["addr2"]
            row_out[ORDER_HEADERS.index("Shipping: City")]       = cust["city"]
            row_out[ORDER_HEADERS.index("Shipping: Province")]      = cust["prov_name"]
            row_out[ORDER_HEADERS.index("Shipping: Province Code")] = cust["prov_code"]
            row_out[ORDER_HEADERS.index("Shipping: Country")]        = cust["country_name"]
            row_out[ORDER_HEADERS.index("Shipping: Country Code")]  = cust["cc"]
            row_out[ORDER_HEADERS.index("Shipping: Zip")]           = cust["zip"]

            row_out[ORDER_HEADERS.index("Line: Type")]              = "Line Item"
            row_out[ORDER_HEADERS.index("Line: Title")]             = item_title
            row_out[ORDER_HEADERS.index("Line: Quantity")]          = qty
            row_out[ORDER_HEADERS.index("Line: Price")]             = item_price
            row_out[ORDER_HEADERS.index("Line: SKU")]               = sku
            row_out[ORDER_HEADERS.index("Line: Requires Shipping")] = "TRUE"
            row_out[ORDER_HEADERS.index("Line: Taxable")]           = "TRUE"
            if tax and li_idx == 0:
                row_out[ORDER_HEADERS.index("Tax 1: Title")] = "Tax"
                row_out[ORDER_HEADERS.index("Tax 1: Price")] = tax

            all_rows.append(row_out)

        # ONE shipping-line row and ONE transaction row per order.
        if shipping:
            ship_row = _blank_order_row()
            ship_row[ORDER_HEADERS.index("Currency")]        = currency
            ship_row[ORDER_HEADERS.index("Payment: Status")] = pay_status
            ship_row[ORDER_HEADERS.index("Processed At")]    = processed
            ship_row[ORDER_HEADERS.index("Line: Type")]  = "Shipping Line"
            ship_row[ORDER_HEADERS.index("Line: Title")] = gv(first, c_shipmeth, "Shipping")
            ship_row[ORDER_HEADERS.index("Line: Price")] = shipping
            all_rows.append(ship_row)

        if total and pay_status == "paid":
            txn_row = _blank_order_row()
            txn_row[ORDER_HEADERS.index("Currency")]        = currency
            txn_row[ORDER_HEADERS.index("Payment: Status")] = pay_status
            txn_row[ORDER_HEADERS.index("Processed At")]    = processed
            txn_row[ORDER_HEADERS.index("Line: Type")]            = "Transaction"
            txn_row[ORDER_HEADERS.index("Transaction: Amount")]   = total
            txn_row[ORDER_HEADERS.index("Transaction: Currency")] = currency
            txn_row[ORDER_HEADERS.index("Transaction: Kind")]     = "sale"
            txn_row[ORDER_HEADERS.index("Transaction: Status")]   = "success"
            txn_row[ORDER_HEADERS.index("Transaction: Gateway")]  = gv(first, c_paymeth, "Custom Gateway")
            all_rows.append(txn_row)

    log("Orders converted    : {}".format(len(order_order)), "OK")
    log("Total CSV rows      : {} (line + shipping + transaction rows)".format(len(all_rows)), "OK")
    if missing_customer:
        log("Orders with no matching Customer ID: {} (billing built from 'customer' name only)".format(
            missing_customer), "WARN")
    if missing_sku:
        log("Line items with no matching product SKU: {} (price fell back)".format(missing_sku), "WARN")
    return all_rows


# ═══════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def run_products(input_path=None, output_path=None):
    rows, sku_lookup = load_products(input_path or DEFAULTS["products"])
    if rows:
        write_csv(rows, PRODUCT_HEADERS, output_path or DEFAULT_OUTPUTS["products"])
    return sku_lookup


def run_customers(input_path=None, output_path=None):
    rows, lookup = load_customers_lookup(input_path or DEFAULTS["customers"])
    if rows:
        write_csv(rows, CUSTOMER_HEADERS, output_path or DEFAULT_OUTPUTS["customers"])
    return lookup


def run_orders(input_path=None, output_path=None, customers_file=None, products_file=None):
    _, sku_lookup = load_products(products_file or DEFAULTS["products"])
    _, cust_lookup = load_customers_lookup(customers_file or DEFAULTS["customers"])
    rows = load_orders(input_path or DEFAULTS["orders"], cust_lookup, sku_lookup)
    if rows:
        write_csv(rows, ORDER_HEADERS, output_path or DEFAULT_OUTPUTS["orders"])


def interactive_menu():
    print("OpenCart to Shopify Migration Tool")
    print("1. Products")
    print("2. Orders")
    print("3. Customers")
    print("4. All three")
    choice = input("Select an option (1-4): ").strip()
    mapping = {"1": ["products"], "2": ["orders"], "3": ["customers"],
               "4": ["products", "customers", "orders"]}
    selected = mapping.get(choice)
    if not selected:
        print("Invalid choice.")
        return
    for m in selected:
        print()
        print("=" * 60)
        print(m.upper())
        print("=" * 60)
        if m == "products":  run_products()
        elif m == "customers": run_customers()
        elif m == "orders":   run_orders()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenCart to Shopify CSV Migration Tool")
    parser.add_argument("--type", choices=["products", "orders", "customers", "all"],
                         help="Which migration to run")
    parser.add_argument("--input", help="Custom input CSV path (single-type only)")
    parser.add_argument("--output", help="Custom output CSV path (single-type only)")
    parser.add_argument("--customers-file", help="Override customer lookup source (orders only)")
    parser.add_argument("--products-file", help="Override product/SKU lookup source (orders only)")
    args = parser.parse_args()

    if not args.type:
        interactive_menu()
    elif args.type == "all":
        run_products()
        run_customers()
        run_orders()
    elif args.type == "products":
        run_products(args.input, args.output)
    elif args.type == "customers":
        run_customers(args.input, args.output)
    elif args.type == "orders":
        run_orders(args.input, args.output, args.customers_file, args.products_file)