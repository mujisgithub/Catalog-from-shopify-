"""
catalog_maker.py
================
This is the "engine" of the website. It takes a Shopify product CSV and
turns it into one or more PDF catalogs (one per discount level).

It is your original script, reorganised into functions so the website
(app.py) can call it, plus these upgrades:

  1. DISCOUNTS  - pass a list like [0, 10, 20] and you get one PDF per
                  level. Discounted cards show the old price crossed out.
  2. SPEED      - product images download 12 at a time instead of 1 at a
                  time, and are downloaded ONCE even if you make 3 PDFs.
  3. PROGRESS   - reports "Downloading images 40/120..." back to the
                  browser so customers see a live progress bar.
  4. SAFETY     - only downloads real public image URLs, never crashes
                  the whole job because one image failed.

You should not need to touch app.py to change how the PDF LOOKS -
all the design knobs are in the SETTINGS block right below.
"""

import os
import re
import socket
import ipaddress
import zipfile
from io import BytesIO
from datetime import date
from urllib.parse import quote, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

# =========================================================
# SETTINGS - tweak the look of every catalog here
# =========================================================
COLUMNS = 3                 # product cards per row
ROWS = 4                    # rows per page  (3 x 4 = 12 products/page)

IMAGE_AREA_RATIO = 0.50     # top half of each card is the photo
IMAGE_PADDING_MM = 2.5

PAGE_MARGIN_MM = 10
GUTTER_MM = 5

TITLE_FONT = "Helvetica-Bold"
TITLE_FONT_SIZE = 8.5
TITLE_MAX_LINES = 2
TITLE_LINE_GAP = 9

META_FONT = "Helvetica"
META_FONT_SIZE = 7.5

PRICE_FONT = "Helvetica-Bold"
PRICE_FONT_SIZE = 10

STRIKE_FONT = "Helvetica"
STRIKE_FONT_SIZE = 8

# Colours (red, green, blue - each 0.0 to 1.0)
ZINC_BG = (0.97, 0.97, 0.98)
CARD_BG = (1, 1, 1)
CARD_BORDER = (0.82, 0.82, 0.86)
SHADOW = (0.86, 0.86, 0.90)
HEADER_BAR = (0.10, 0.12, 0.16)
HEADER_TEXT = (1, 1, 1)
ACCENT = (0.85, 0.65, 0.15)

PRICE_BADGE_BG = (0.10, 0.12, 0.16)
PRICE_BADGE_TEXT = (1, 1, 1)

META_PILL_BG = (0.93, 0.93, 0.95)
META_PILL_TEXT = (0.22, 0.24, 0.28)
META_PILL_BORDER = (0.86, 0.86, 0.90)

STRIKE_COLOR = (0.45, 0.47, 0.52)

CARD_RADIUS = 10
CARD_BORDER_WIDTH = 1.1
SHADOW_OFFSET = 2.0

DOWNLOAD_THREADS = 12       # how many images to download at the same time
DOWNLOAD_TIMEOUT = 20       # seconds before giving up on one image

REQUIRED_COLUMNS = [
    "Handle", "Title", "Variant SKU",
    "Variant Inventory Qty", "Variant Price", "Image Src",
]


# =========================================================
# Small helpers
# =========================================================
def clean_str(x):
    s = str(x).strip()
    return "" if s.lower() in ("nan", "none") else s


def parse_price(v):
    """'£12.50' or '12.5' -> 12.5 as a number. Returns None if unreadable."""
    s = clean_str(v).replace("£", "").replace("$", "").replace("€", "").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def format_money(amount, symbol):
    return f"{symbol}{amount:,.2f}"


def to_int_qty(v):
    s = clean_str(v)
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def safe_filename(s):
    s = clean_str(s)
    s = re.sub(r"[^\w\-\. ]+", "", s)
    s = re.sub(r"\s+", "_", s)
    return s[:120] if s else "item"


def product_url(store_domain, handle):
    handle = clean_str(handle)
    if not handle or not store_domain:
        return ""
    return f"{store_domain.rstrip('/')}/products/{quote(handle)}"


# =========================================================
# Image safety + downloading
# =========================================================
_host_check_cache = {}

def is_safe_image_url(url):
    """
    Only allow normal public http/https image links.
    Blocks links that point at private/internal network addresses
    (a standard safety measure for any website that downloads URLs
    supplied inside an uploaded file).
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname
        if host in _host_check_cache:
            return _host_check_cache[host]
        ok = True
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast):
                ok = False
                break
        _host_check_cache[host] = ok
        return ok
    except Exception:
        return False


def flatten_to_white(img):
    """Transparent PNGs turn black with a plain convert('RGB').
    This pastes them onto white first, which looks right in the catalog."""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        return background
    return img.convert("RGB")


def download_image(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CatalogMaker/1.0)",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    r = requests.get(url, timeout=DOWNLOAD_TIMEOUT, headers=headers)
    r.raise_for_status()
    return flatten_to_white(Image.open(BytesIO(r.content)))


def predownload_images(items, cache_dir, progress=None):
    """
    Download every product photo BEFORE drawing any PDF.
    Runs 12 downloads at once, so 200 photos take seconds not minutes.
    Each item gets item["img_path"] = local file (or None if it failed).
    """
    os.makedirs(cache_dir, exist_ok=True)

    def fetch_one(item):
        url = item.get("image", "")
        if not url:
            return None
        cache_path = os.path.join(
            cache_dir, safe_filename(item["sku"] or item["title"]) + ".jpg"
        )
        if os.path.exists(cache_path):
            return cache_path
        if not is_safe_image_url(url):
            return None
        try:
            img = download_image(url)
            img.save(cache_path, "JPEG", quality=88)
            return cache_path
        except Exception:
            return None   # one bad image never kills the whole catalog

    total = len(items)
    done = 0
    with ThreadPoolExecutor(max_workers=DOWNLOAD_THREADS) as pool:
        futures = {pool.submit(fetch_one, it): it for it in items}
        for fut in as_completed(futures):
            futures[fut]["img_path"] = fut.result()
            done += 1
            if progress:
                progress(done / total, f"Downloading product photos ({done}/{total})")


# =========================================================
# Reading the Shopify CSV
# =========================================================
def load_products(csv_path, store_domain):
    """
    Reads a standard Shopify 'Export products' CSV.
    Returns one entry per product (the first variant that has a
    title + SKU + price - exactly like your original script).
    """
    df = pd.read_csv(csv_path, dtype=str).fillna("")

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            "This doesn't look like a Shopify product export. "
            f"Missing columns: {', '.join(missing)}"
        )

    for col in REQUIRED_COLUMNS:
        df[col] = df[col].astype(str).str.strip()

    # Shopify sometimes puts the product photo on a *different* row than
    # the price row. Build a lookup: handle -> first image URL found.
    image_by_handle = {}
    for _, r in df.iterrows():
        h, img = clean_str(r["Handle"]), clean_str(r["Image Src"]).split(",")[0].strip()
        if h and img and h not in image_by_handle:
            image_by_handle[h] = img

    df = df[(df["Title"] != "") & (df["Variant SKU"] != "") & (df["Variant Price"] != "")]
    df = df.drop_duplicates(subset=["Variant SKU"], keep="first")

    items = []
    for _, r in df.iterrows():
        price = parse_price(r["Variant Price"])
        if price is None:
            continue
        handle = clean_str(r["Handle"])
        img = clean_str(r["Image Src"]).split(",")[0].strip() or image_by_handle.get(handle, "")
        items.append({
            "url": product_url(store_domain, handle),
            "title": clean_str(r["Title"]),
            "sku": clean_str(r["Variant SKU"]),
            "qty": to_int_qty(r["Variant Inventory Qty"]),
            "price": price,
            "image": img,
            "img_path": None,
        })
    return items


# =========================================================
# Drawing helpers (same style as your original)
# =========================================================
def resize_contain(img, target_w, target_h, bg=(255, 255, 255)):
    iw, ih = img.size
    scale = min(target_w / iw, target_h / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)
    canvas_img = Image.new("RGB", (target_w, target_h), bg)
    canvas_img.paste(resized, ((target_w - nw) // 2, (target_h - nh) // 2))
    return canvas_img


def wrap_title(c, text, max_width, font, size, max_lines):
    text = clean_str(text)
    if not text:
        return [""]
    words, lines, current = text.split(), [], ""
    for w in words:
        trial = (current + " " + w).strip()
        if c.stringWidth(trial, font, size) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = w
            while c.stringWidth(current + "…", font, size) > max_width and len(current) > 1:
                current = current[:-1]
            if c.stringWidth(current, font, size) > max_width:
                current += "…"
        if len(lines) >= max_lines:
            break
    if len(lines) < max_lines and current:
        lines.append(current)
    if len(lines) == max_lines and " ".join(lines).split() != words and not lines[-1].endswith("…"):
        last = lines[-1]
        while c.stringWidth(last + "…", font, size) > max_width and len(last) > 1:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines[:max_lines]


def draw_image_aspect(c, pil_img, x, y, box_w, box_h):
    iw, ih = pil_img.size
    if iw <= 0 or ih <= 0:
        return
    scale = min(box_w / iw, box_h / ih)
    dw, dh = iw * scale, ih * scale
    c.drawImage(ImageReader(pil_img), x + (box_w - dw) / 2, y + (box_h - dh) / 2,
                dw, dh, mask="auto")


def add_link(c, url, rect):
    if not url:
        return
    x1, y1, x2, y2 = rect
    c.linkURL(url, (x1 - 1.5, y1 - 1.5, x2 + 1.5, y2 + 1.5), relative=0)


def draw_badge_center(c, cx, y, text, font, size, pad_x, pad_y, bg, fg,
                      radius, border=None):
    text = clean_str(text)
    if not text:
        return (0, 0, 0, 0, 0)
    c.setFont(font, size)
    tw = c.stringWidth(text, font, size)
    bw, bh = tw + 2 * pad_x, size + 2 * pad_y
    x = cx - bw / 2
    c.setFillColorRGB(*bg)
    if border:
        c.setStrokeColorRGB(*border)
        c.setLineWidth(0.8)
        c.roundRect(x, y, bw, bh, radius, stroke=1, fill=1)
    else:
        c.roundRect(x, y, bw, bh, radius, stroke=0, fill=1)
    c.setFillColorRGB(*fg)
    c.drawString(x + pad_x, y + pad_y, text)
    return (x, y, x + bw, y + bh, bh)


def load_logo(path):
    path = clean_str(path)
    if not path or not os.path.exists(path):
        return None
    try:
        return flatten_to_white(Image.open(path)) if False else Image.open(path).convert("RGBA")
    except Exception:
        return None


# =========================================================
# Building ONE pdf at ONE discount level
# =========================================================
def build_pdf(items, out_path, *, business_name, discount=0.0,
              currency="£", logo_path="", progress=None):
    W, H = A4
    c = canvas.Canvas(out_path, pagesize=A4)
    logo_img = load_logo(logo_path)
    today = date.today().strftime("%d %B %Y")

    discount_label = f"{discount:g}% OFF" if discount > 0 else ""

    def page_bg():
        c.setFillColorRGB(*ZINC_BG)
        c.rect(0, 0, W, H, stroke=0, fill=1)

    def header(page_no):
        header_h = 16 * mm
        c.setFillColorRGB(*HEADER_BAR)
        c.rect(0, H - header_h, W, header_h, stroke=0, fill=1)
        if logo_img:
            box = 12 * mm
            draw_image_aspect(c, logo_img, 10 * mm,
                              H - header_h + (header_h - box) / 2, box, box)
        c.setFillColorRGB(*HEADER_TEXT)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(10 * mm + (16 * mm if logo_img else 0),
                     H - header_h + 5.3 * mm, f"{business_name} Catalog")
        if discount_label:
            c.setFillColorRGB(*ACCENT)
            c.setFont("Helvetica-Bold", 9)
            lbl = f"{discount_label} ALL ITEMS"
            c.drawCentredString(W / 2 + 20 * mm, H - header_h + 5.3 * mm, lbl)
        c.setFillColorRGB(*HEADER_TEXT)
        c.setFont("Helvetica", 9)
        c.drawRightString(W - 10 * mm, H - header_h + 5.2 * mm, f"Page {page_no}")

    def cover():
        page_bg()
        c.setFillColorRGB(*HEADER_BAR)
        c.rect(0, H - 40 * mm, W, 40 * mm, stroke=0, fill=1)
        if logo_img:
            draw_image_aspect(c, logo_img, (W - 45 * mm) / 2,
                              H - 35 * mm - 22.5 * mm, 45 * mm, 45 * mm)

        title = f"{business_name} Product Catalog {date.today().year}"
        c.setFillColorRGB(0.10, 0.12, 0.16)
        c.setFont("Helvetica-Bold", 26)
        c.drawCentredString(W / 2, H / 2 + 10 * mm, title)

        subtitle = (f"Special price list — {discount:g}% off every item"
                    if discount > 0 else "Full price list")
        c.setFont("Helvetica", 12)
        c.setFillColorRGB(0.25, 0.27, 0.30)
        c.drawCentredString(W / 2, H / 2 - 2 * mm, subtitle)

        c.setFont("Helvetica", 10)
        c.setFillColorRGB(0.45, 0.47, 0.52)
        c.drawCentredString(W / 2, H / 2 - 10 * mm, f"Generated {today}")

        if discount > 0:
            draw_badge_center(c, W / 2, H / 2 - 26 * mm,
                              f"  {discount:g}% OFF EVERYTHING  ",
                              "Helvetica-Bold", 12, 10, 6,
                              ACCENT, (1, 1, 1), 10)

        c.setStrokeColorRGB(*ACCENT)
        c.setLineWidth(2)
        c.line(W * 0.25, H / 2 - 16 * mm, W * 0.75, H / 2 - 16 * mm)
        c.showPage()

    def card_frame(x, y, w, h):
        c.setFillColorRGB(*SHADOW)
        c.roundRect(x + SHADOW_OFFSET, y - SHADOW_OFFSET, w, h, CARD_RADIUS, stroke=0, fill=1)
        c.setFillColorRGB(*CARD_BG)
        c.setStrokeColorRGB(*CARD_BORDER)
        c.setLineWidth(CARD_BORDER_WIDTH)
        c.roundRect(x, y, w, h, CARD_RADIUS, stroke=1, fill=1)

    # ---- layout maths -------------------------------------------------
    cover()
    margin, gutter = PAGE_MARGIN_MM * mm, GUTTER_MM * mm
    header_h = 16 * mm
    usable_h = H - header_h - 2 * margin
    tile_w = (W - 2 * margin - (COLUMNS - 1) * gutter) / COLUMNS
    tile_h = (usable_h - (ROWS - 1) * gutter) / ROWS
    img_area_h = tile_h * IMAGE_AREA_RATIO
    pad = IMAGE_PADDING_MM * mm

    dpi_scale = 150 / 72
    img_box_w_px = int((tile_w - 2 * pad) * dpi_scale)
    img_box_h_px = int((img_area_h - 2 * pad) * dpi_scale)

    per_page = COLUMNS * ROWS
    page_no, page_items = 1, []
    total = len(items)

    for idx, item in enumerate(items):
        page_items.append(item)
        if len(page_items) == per_page or idx == total - 1:
            page_bg()
            header(page_no)
            top_y = H - header_h - margin

            for n, p in enumerate(page_items):
                r, col = n // COLUMNS, n % COLUMNS
                x = margin + col * (tile_w + gutter)
                y = top_y - (r + 1) * tile_h - r * gutter
                url = p["url"]

                card_frame(x, y, tile_w, tile_h)
                add_link(c, url, (x, y, x + tile_w, y + tile_h))

                # ---------- product photo ----------
                img_x, img_y = x + pad, y + tile_h - img_area_h + pad
                img_w, img_h = tile_w - 2 * pad, img_area_h - 2 * pad
                if p.get("img_path"):
                    try:
                        img = Image.open(p["img_path"]).convert("RGB")
                        tile = resize_contain(img, img_box_w_px, img_box_h_px)
                        c.drawImage(ImageReader(tile), img_x, img_y, img_w, img_h,
                                    preserveAspectRatio=False, mask="auto")
                    except Exception:
                        pass
                else:
                    # tidy placeholder instead of an ugly empty gap
                    c.setFillColorRGB(0.955, 0.955, 0.965)
                    c.roundRect(img_x, img_y, img_w, img_h, 6, stroke=0, fill=1)
                    c.setFillColorRGB(0.62, 0.64, 0.68)
                    c.setFont("Helvetica", 7.5)
                    c.drawCentredString(img_x + img_w / 2, img_y + img_h / 2 - 3,
                                        "Image unavailable")

                # ---------- title ----------
                text_top_y = y + tile_h - img_area_h - 9
                title_lines = wrap_title(c, p["title"], tile_w - 14,
                                         TITLE_FONT, TITLE_FONT_SIZE, TITLE_MAX_LINES)
                c.setFillColorRGB(0.10, 0.12, 0.16)
                c.setFont(TITLE_FONT, TITLE_FONT_SIZE)
                for i, line in enumerate(title_lines):
                    c.drawCentredString(x + tile_w / 2,
                                        text_top_y - i * TITLE_LINE_GAP, line)
                add_link(c, url, (x + 6,
                                  text_top_y - len(title_lines) * TITLE_LINE_GAP - 2,
                                  x + tile_w - 6, text_top_y + 6))

                # ---------- bottom stack: price badge / old price / info pill ----------
                final_price = round(p["price"] * (1 - discount / 100.0), 2)
                badge_y = y + 6
                bx1, by1, bx2, by2, badge_h = draw_badge_center(
                    c, x + tile_w / 2, badge_y, format_money(final_price, currency),
                    PRICE_FONT, PRICE_FONT_SIZE, 7, 4,
                    PRICE_BADGE_BG, PRICE_BADGE_TEXT, 8)
                add_link(c, url, (bx1, by1, bx2, by2))

                next_y = badge_y + badge_h + 3
                if discount > 0:
                    old = format_money(p["price"], currency)
                    c.setFont(STRIKE_FONT, STRIKE_FONT_SIZE)
                    c.setFillColorRGB(*STRIKE_COLOR)
                    tw = c.stringWidth(old, STRIKE_FONT, STRIKE_FONT_SIZE)
                    sx = x + tile_w / 2 - tw / 2
                    c.drawString(sx, next_y, old)
                    c.setStrokeColorRGB(*STRIKE_COLOR)
                    c.setLineWidth(0.9)
                    c.line(sx - 1, next_y + STRIKE_FONT_SIZE * 0.32,
                           sx + tw + 1, next_y + STRIKE_FONT_SIZE * 0.32)
                    next_y += STRIKE_FONT_SIZE + 4

                # one pill with SKU (and stock when we know it)
                pill_text = f"SKU {p['sku']}"
                if p["qty"] > 0:
                    pill_text += f"  •  {p['qty']} in stock"
                c.setFont(META_FONT, META_FONT_SIZE)
                if c.stringWidth(pill_text, META_FONT, META_FONT_SIZE) > tile_w - 24:
                    pill_text = f"SKU {p['sku']}"
                px1, py1, px2, py2, _ = draw_badge_center(
                    c, x + tile_w / 2, next_y, pill_text,
                    META_FONT, META_FONT_SIZE, 6, 3,
                    META_PILL_BG, META_PILL_TEXT, 7, border=META_PILL_BORDER)
                add_link(c, url, (px1, py1, px2, py2))

            c.showPage()
            page_items = []
            page_no += 1
            if progress:
                progress(min(idx + 1, total) / total, None)

    c.save()
    return out_path


# =========================================================
# The one function app.py calls
# =========================================================
def build_catalogs(items, job_dir, *, business_name, discounts,
                   currency="£", logo_path="", progress=None):
    """
    items      : list from load_products()  (photos not yet downloaded)
    job_dir    : private folder for this customer's job
    discounts  : e.g. [0, 10, 20]  -> makes 3 PDFs
    Returns (path_to_give_customer, list_of_pdf_paths)
    """
    def report(pct, msg):
        if progress:
            progress(pct, msg)

    # 1) download all photos once (0% -> 65% of the progress bar)
    cache_dir = os.path.join(job_dir, "image_cache")
    report(0.02, "Reading your product list")
    predownload_images(
        items, cache_dir,
        progress=lambda frac, msg: report(0.02 + frac * 0.63, msg),
    )

    # 2) build one PDF per discount level (65% -> 95%)
    safe_name = safe_filename(business_name) or "catalog"
    pdf_paths = []
    n = len(discounts)
    for i, d in enumerate(discounts):
        label = "full_price" if d == 0 else f"{d:g}pct_off"
        nice = "Full Price" if d == 0 else f"{d:g}% Off"
        report(0.65 + (i / n) * 0.30, f"Building catalog {i + 1} of {n} ({nice})")
        out = os.path.join(job_dir, f"{safe_name}_catalog_{label}.pdf")
        build_pdf(items, out, business_name=business_name, discount=d,
                  currency=currency, logo_path=logo_path,
                  progress=lambda frac, _m, base=0.65 + (i / n) * 0.30, span=0.30 / n:
                      report(base + frac * span, None))
        pdf_paths.append(out)

    # 3) one PDF -> hand it straight over; several -> zip them together
    if len(pdf_paths) == 1:
        report(1.0, "Done")
        return pdf_paths[0], pdf_paths

    report(0.97, "Zipping your catalogs")
    zip_path = os.path.join(job_dir, f"{safe_name}_catalogs.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in pdf_paths:
            z.write(p, arcname=os.path.basename(p))
    report(1.0, "Done")
    return zip_path, pdf_paths
