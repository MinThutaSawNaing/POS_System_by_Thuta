"""Seed demo products (with generated photos) into the POS database.

Usage:
    python seed.py                  # seed into the default branch, with photos
    python seed.py --branch-id 2    # seed into a specific branch
    python seed.py --no-photos      # skip photo generation

The script is idempotent: products whose name or barcode already exists in
the target branch are skipped, so it is safe to re-run at any time.
Photos are generated locally with Pillow (no external assets needed) and
stored in uploads/products exactly like real uploads.
"""

import argparse
import os
import uuid

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:  # seed.py still works without Pillow, just without photos
    PIL_AVAILABLE = False

from app import app, db, Branch, Category, Product, get_default_branch_id

PHOTO_SIZE = 480
UPLOAD_FOLDER = app.config['UPLOAD_FOLDER']

# (name, category, price MMK, cost MMK, stock)
DEMO_PRODUCTS = [
    ('Coca-Cola 500ml', 'Drinks', 1400, 1050, 48),
    ('Myanmar Beer 330ml', 'Drinks', 2800, 2100, 36),
    ('Green Tea 500ml', 'Drinks', 1200, 850, 60),
    ('Energy Drink 250ml', 'Drinks', 1800, 1300, 40),
    ('Mineral Water 600ml', 'Drinks', 500, 300, 120),
    ('Instant Coffee 3-in-1', 'Drinks', 800, 550, 90),
    ('Potato Chips BBQ 60g', 'Snacks', 3200, 2300, 45),
    ('Chocolate Bar 45g', 'Snacks', 2500, 1750, 30),
    ('Instant Noodles Chicken', 'Snacks', 900, 600, 100),
    ('Salted Biscuits 120g', 'Snacks', 1500, 1000, 55),
    ('Dried Mango 100g', 'Snacks', 4500, 3200, 20),
    ('Jasmine Rice 5kg', 'Grocery', 14500, 11500, 25),
    ('Cooking Oil 1L', 'Grocery', 5200, 4200, 35),
    ('Fish Sauce 500ml', 'Grocery', 2800, 2000, 40),
    ('Sugar 1kg', 'Grocery', 2200, 1700, 50),
    ('Eggs (10 pcs)', 'Grocery', 3500, 2800, 30),
    ('Beef Curry Set', 'Food & Beverage', 8500, 6500, 15),
    ('Chicken Biryani Pack', 'Food & Beverage', 7500, 5600, 18),
    ('Pork Pack 1kg', 'Food & Beverage', 9800, 7800, 12),
    ('USB-C Cable 1m', 'Electronic', 8500, 5000, 25),
    ('Wireless Mouse', 'Electronic', 18000, 12000, 14),
    ('Power Bank 10000mAh', 'Electronic', 45000, 32000, 10),
    ('Bluetooth Earbuds', 'Electronic', 35000, 24000, 8),
    ('Cotton T-Shirt (M)', 'Shirts', 12000, 7500, 20),
    ('Polo Shirt Navy', 'Shirts', 28000, 19000, 12),
    ('Formal Shirt White', 'Shirts', 32000, 22000, 10),
    ('Canvas Sneakers', 'Shoes', 42000, 28000, 8),
    ('Running Shoes Pro', 'Shoes', 95000, 68000, 5),
    ('Leather Sandals', 'Shoes', 25000, 16000, 10),
]

CATEGORY_PALETTE = {
    'Drinks': (47, 128, 237),
    'Snacks': (242, 153, 74),
    'Grocery': (39, 174, 96),
    'Food & Beverage': (235, 87, 87),
    'Electronic': (45, 156, 219),
    'Shirts': (155, 81, 224),
    'Shoes': (86, 107, 125),
}
FALLBACK_HUES = [
    (52, 96, 150), (150, 90, 60), (90, 130, 70), (140, 80, 130),
]

FONT_CANDIDATES = [
    'C:/Windows/Fonts/segoeuib.ttf',
    'C:/Windows/Fonts/arialbd.ttf',
    'C:/Windows/Fonts/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
]


def load_font(size):
    """Bold-ish TTF when available, Pillow's scalable default otherwise."""
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def category_color(name):
    if name in CATEGORY_PALETTE:
        return CATEGORY_PALETTE[name]
    # Stable pseudo-random pick for categories we do not know.
    return FALLBACK_HUES[(sum(ord(ch) for ch in name) or 0) % len(FALLBACK_HUES)]


def mix(color, other, factor):
    return tuple(round(a + (b - a) * factor) for a, b in zip(color, other))


def wrap_text(draw, text, font, max_width, max_lines=3):
    words = text.split()
    lines = []
    current = ''
    for word in words:
        trial = f'{current} {word}'.strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + '\u2026'
    return lines


def make_product_photo(name, category_name, folder):
    """Render a clean gradient tile with the product name; return filename."""
    base = category_color(category_name)
    top = mix(base, (255, 255, 255), 0.38)
    image = Image.new('RGB', (PHOTO_SIZE, PHOTO_SIZE), base)
    draw = ImageDraw.Draw(image)
    for y in range(PHOTO_SIZE):
        draw.line([(0, y), (PHOTO_SIZE, y)], fill=mix(top, base, y / PHOTO_SIZE))

    # Subtle inner frame.
    draw.rounded_rectangle(
        [16, 16, PHOTO_SIZE - 17, PHOTO_SIZE - 17],
        radius=24, outline=(255, 255, 255), width=3,
    )

    category_font = load_font(26)
    name_font = load_font(52)

    category_label = (category_name or 'General').upper()
    draw.text((PHOTO_SIZE / 2, 74), category_label, font=category_font,
              fill=(255, 255, 255), anchor='mm')

    lines = wrap_text(draw, name, name_font, PHOTO_SIZE - 96)
    line_height = 62
    total_height = line_height * len(lines)
    start_y = (PHOTO_SIZE - total_height) / 2 + line_height / 2 + 12
    for index, line in enumerate(lines):
        y = start_y + index * line_height
        # Soft shadow keeps white text readable on light gradient tops.
        draw.text((PHOTO_SIZE / 2 + 3, y + 3), line, font=name_font,
                  fill=mix(base, (0, 0, 0), 0.55), anchor='mm')
        draw.text((PHOTO_SIZE / 2, y), line, font=name_font,
                  fill=(255, 255, 255), anchor='mm')

    filename = f'{uuid.uuid4().hex}.png'
    image.save(os.path.join(folder, filename), optimize=True)
    return filename


def ensure_category(name, branch_id):
    category = Category.query.filter_by(name=name, branch_id=branch_id).first()
    if category:
        return category, False
    color = '#%02x%02x%02x' % category_color(name)
    category = Category(
        name=name, branch_id=branch_id, color=color,
        description=f'{name} (demo seed)', is_active=True,
    )
    db.session.add(category)
    db.session.flush()
    return category, True


def next_barcode(taken):
    candidate = 900001
    while str(candidate) in taken:
        candidate += 1
    taken.add(str(candidate))
    return str(candidate)


def seed_products(branch_id, with_photos=True):
    branch = db.session.get(Branch, branch_id)
    if not branch:
        raise SystemExit(f'Branch {branch_id} not found')

    existing_names = {
        name.lower() for (name,) in db.session.query(Product.name)
        .filter(Product.branch_id == branch_id)
    }
    taken_barcodes = {
        barcode for (barcode,) in db.session.query(Product.barcode)
        .filter(Product.branch_id == branch_id, Product.barcode.isnot(None))
    }

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    if with_photos and not PIL_AVAILABLE:
        print('Pillow is not installed - seeding without photos.')
        print('  Install it with:  pip install Pillow')
        with_photos = False
    created = skipped = categories_added = photos = 0

    for name, category_name, price, cost, stock in DEMO_PRODUCTS:
        if name.lower() in existing_names:
            skipped += 1
            continue
        category, was_created = ensure_category(category_name, branch_id)
        categories_added += int(was_created)

        photo_filename = None
        if with_photos:
            photo_filename = make_product_photo(name, category_name, UPLOAD_FOLDER)
            photos += 1

        db.session.add(Product(
            barcode=next_barcode(taken_barcodes),
            name=name,
            price=float(price),
            cost=float(cost),
            stock=int(stock),
            category=category_name,       # legacy display field
            category_id=category.id,
            tax_rate=0.0,
            photo_filename=photo_filename,
            reorder_point=10,
            reorder_quantity=40,
            reorder_enabled=True,
            branch_id=branch_id,
        ))
        existing_names.add(name.lower())
        created += 1

    db.session.commit()
    print(f'Branch {branch_id} ({branch.name}):')
    print(f'  products created : {created}')
    print(f'  products skipped : {skipped} (name already present)')
    print(f'  categories added : {categories_added}')
    print(f'  photos generated : {photos}')


def main():
    parser = argparse.ArgumentParser(description='Seed demo products.')
    parser.add_argument('--branch-id', type=int, default=None,
                        help='target branch (default: the default branch)')
    parser.add_argument('--no-photos', action='store_true',
                        help='skip generating product photos')
    args = parser.parse_args()

    with app.app_context():
        branch_id = args.branch_id or get_default_branch_id()
        if not branch_id:
            raise SystemExit('No default branch found; pass --branch-id.')
        seed_products(branch_id, with_photos=not args.no_photos)


if __name__ == '__main__':
    main()

