"""Synthetic receipts with exact ground truth, in Russian, Kazakh and English, at three image qualities.

    python bench/generate.py bench/data --n 60 --seed 1

Writes JPG files and `truth.json` in the format of `eval/compare.py`. Everything is made up (BINs are generated with
a valid checksum, they belong to nobody). A value hidden on purpose (a finger over a price) is left out of the truth,
so the document is expected to be sent to review: that is how silent errors are measured.

Quality levels: `clean` (a flat scan), `photo` (skew, perspective, uneven light, noise, JPEG), `damaged` (a photo with a
fold and a finger over one amount), `faded` (old thermal paper: low contrast, heavy blur, low resolution).
"""

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_PATHS = [
    "C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "DejaVuSansMono.ttf",
]
_W1 = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
_W2 = (3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2)

ITEMS = {
    "ru": ["Капучино 0.3", "Латте 0.4", "Круассан", "Хлеб белый", "Молоко 1л", "Сыр Российский", "Вода б/г 0.5",
           "Чай чёрный", "Яблоки", "Сахар 1кг", "Печенье овсяное", "Йогурт"],
    "kk": ["Нан", "Сүт 1л", "Қант 1кг", "Шай", "Алма", "Ірімшік", "Су 0.5", "Кофе", "Құрма", "Ет", "Печенье", "Ақ нан"],
    "en": ["Organic Bananas", "Almond Milk", "Sourdough Loaf", "Free Range Eggs", "Greek Yogurt", "Orange Juice",
           "Green Tea", "Rice 2lb", "Cheddar", "Olive Oil", "Pasta", "Coffee Beans"],
}
SHOPS = {
    "ru": ["ТОО «Кофейня Жибек»", "ИП Сериков", "ТОО «Продмаг Алтын»", "ТОО «Аптека Здоровье»", "ТОО «Мастер Сервис»"],
    "kk": ["ЖШС «Алтын Дүкен»", "ЖК Нұрғалиев", "ЖШС «Ақ Бұлақ»", "ЖШС «Достық Маркет»", "ЖШС «Бәйтерек Кафе»"],
    "en": ["GREENLEAF MARKET", "HARBOR CAFE", "CORNER PHARMACY", "BLUE DOOR BAKERY", "NORTHSIDE GROCERS"],
}
STREETS = {
    "ru": ["г. Алматы, пр. Абая, {n}", "г. Астана, ул. Кенесары, {n}", "г. Шымкент, ул. Тауке хана, {n}"],
    "kk": ["Алматы қ., Абай даңғ., {n}", "Астана қ., Кенесары к., {n}", "Шымкент қ., Төле би к., {n}"],
    "en": ["{n} Harbor Rd, Portland, OR", "{n} Main St, Austin, TX", "{n} Oak Ave, Denver, CO"],
}
LABELS = {
    "ru": dict(tax_id="БИН", head="ФИСКАЛЬНЫЙ ЧЕК № {num}", date="Дата: {d}", time="Время: {t}", total="ИТОГО:",
               vat="в т.ч. НДС 16%:", pay="Оплата: банковская карта", thanks="Спасибо за покупку!"),
    "kk": dict(tax_id="БСН", head="ФИСКАЛДЫҚ ЧЕК № {num}", date="Күні: {d}", time="Уақыты: {t}", total="БАРЛЫҒЫ:",
               vat="ҚҚС 16%:", pay="Төлем: банк картасы", thanks="Сатып алғаныңызға рахмет!"),
    "en": dict(tax_id="TAX ID", head="Receipt #{num}", date="{d}", time="{t}", total="TOTAL", vat="TAX 8%",
               pay="VISA **** 4417", thanks="Thank you for shopping!"),
}


def font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def valid_bin(rng: random.Random) -> str:
    while True:
        digits = [rng.randint(0, 9) for _ in range(11)]
        control = sum(d * w for d, w in zip(digits, _W1)) % 11
        if control == 10:
            control = sum(d * w for d, w in zip(digits, _W2)) % 11
        if control < 10:
            return "".join(map(str, digits)) + str(control)


def money(value: float, lang: str) -> str:
    text = f"{value:,.2f}"
    if lang == "en":
        return text
    whole, frac = text.split(".")
    whole = whole.replace(",", " ")
    return whole if frac == "00" else f"{whole},{frac}"


def make_receipt(rng: random.Random, lang: str) -> dict:
    """The receipt data: this is the ground truth."""
    when = date(2026, 1, 1) + timedelta(days=rng.randint(0, 300))
    items = []
    for name in rng.sample(ITEMS[lang], rng.randint(2, 6)):
        weighed = "кг" in name or "lb" in name
        qty = rng.choice([0.5, 1.25, 2]) if weighed else rng.choice([1, 1, 2, 3, 4])
        base = rng.choice([150, 290, 350, 480, 650, 790, 1200, 1850, 2400])
        price = round(base * 0.004, 2) if lang == "en" else float(base)
        items.append({"description": name, "quantity": qty, "unit_price": price, "total": round(qty * price, 2)})
    subtotal = round(sum(i["total"] for i in items), 2)
    if lang == "en":
        tax = round(subtotal * 0.08, 2)
        total = round(subtotal + tax, 2)
    else:
        total = subtotal
        tax = round(total * 16 / 116, 2)  # VAT included in the printed prices
    truth = {
        "merchant_name": rng.choice(SHOPS[lang]),
        "receipt_number": f"A-{rng.randint(10000, 99999)}" if lang == "en" else f"{rng.randint(1, 99999):06d}",
        "date": when.isoformat(),
        "time": f"{rng.randint(8, 21):02d}:{rng.randint(0, 59):02d}",
        "total": total,
        "tax_amount": tax,
        "currency": "USD" if lang == "en" else "KZT",
        "items": items,
    }
    if lang != "en":
        truth["merchant_tax_id"] = valid_bin(rng)
    return truth


def render(truth: dict, lang: str, rng: random.Random, hide: str | None):
    """Draw the receipt. Returns the image and the truth keys hidden on purpose."""
    labels = LABELS[lang]
    f = font(26)
    symbol = "$" if lang == "en" else " ₸"
    printed_date = date.fromisoformat(truth["date"]).strftime("%m/%d/%Y" if lang == "en" else "%d.%m.%Y")
    lines: list[tuple[str, str | None]] = [
        (truth["merchant_name"], None),
        (rng.choice(STREETS[lang]).format(n=rng.randint(1, 180)), None),
    ]
    if "merchant_tax_id" in truth:
        lines.append((f"{labels['tax_id']} {truth['merchant_tax_id']}", "merchant_tax_id"))
    lines += [
        ("", None),
        (labels["head"].format(num=truth["receipt_number"]), "receipt_number"),
        (f"{labels['date'].format(d=printed_date)}    {labels['time'].format(t=truth['time'])}", "date"),
        ("-" * 34, None),
    ]
    for n, item in enumerate(truth["items"]):
        left = item["description"][:16].ljust(16)
        lines.append((f"{left} {item['quantity']:g} x {money(item['unit_price'], lang)} = {money(item['total'], lang)}", f"items.{n}"))
    lines += [
        ("-" * 34, None),
        (f"{labels['total']}  {money(truth['total'], lang)}{symbol}", "total"),
        (f"{labels['vat']}  {money(truth['tax_amount'], lang)}", "tax_amount"),
        (labels["pay"], None),
        ("", None),
        (labels["thanks"], None),
    ]
    img = Image.new("RGB", (680, 40 + 36 * len(lines)), "white")
    draw = ImageDraw.Draw(img)
    boxes = {}
    for n, (text, key) in enumerate(lines):
        xy = (24, 20 + 36 * n)
        draw.text(xy, text, fill="black", font=f)
        if key:
            boxes[key] = draw.textbbox(xy, text, font=f)
    hidden: set[str] = set()
    if hide and hide in boxes:  # a "finger" over the right part of that line's amount
        _x0, y0, x1, y1 = boxes[hide]
        draw.ellipse((x1 - 150, y0 - 14, x1 + 40, y1 + 14), fill=(224, 172, 140))
        hidden.add(hide)
    return img, hidden


def degrade(img: Image.Image, level: str, rng: random.Random) -> Image.Image:
    if level == "clean":
        return img.rotate(rng.uniform(-1, 1), expand=True, fillcolor="white", resample=Image.BICUBIC).filter(
            ImageFilter.GaussianBlur(0.5))
    import numpy as np

    paper = (235, 232, 226)
    padded = Image.new("RGB", (img.width + 100, img.height + 100), paper)  # room, so skew and perspective cut nothing
    padded.paste(img, (50, 50))
    img = padded.rotate(rng.uniform(-4, 4), expand=True, fillcolor=paper, resample=Image.BICUBIC)
    w, h = img.size
    dx = rng.randint(10, 40)  # perspective: the top edge is narrower than the bottom
    img = img.transform((w, h), Image.QUAD, (dx, 0, 0, h, w, h - dx, w - dx, 0), Image.BICUBIC, fillcolor=paper)
    arr = np.asarray(img).astype("float32")
    ys, xs = np.mgrid[0:h, 0:w]
    shade = 1 - 0.35 * (xs / w * rng.uniform(0, 1) + ys / h * rng.uniform(0, 1))  # uneven light
    noise = np.random.default_rng(rng.randint(0, 10**6)).normal(0, 7, arr.shape)
    img = Image.fromarray((arr * shade[..., None] + noise).clip(0, 255).astype("uint8"))
    img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.7, 1.3)))
    if level == "faded":
        small = img.resize((int(w * 0.45), int(h * 0.45)), Image.BILINEAR)  # a cheap camera, far away
        img = small.resize((w, h), Image.BICUBIC).point(lambda v: int(150 + v * 0.38))  # grey ink on grey paper
    if level == "damaged":
        draw = ImageDraw.Draw(img, "RGBA")
        y = rng.randint(h // 3, h * 2 // 3)
        draw.rectangle((0, y, w, y + 8), fill=(255, 255, 255, 110))  # a fold
        draw.rectangle((0, y + 8, w, y + 12), fill=(90, 90, 90, 60))
    return img


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out")
    parser.add_argument("--n", type=int, default=60, help="number of receipts")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    levels = ["clean", "photo", "damaged", "faded"]
    langs = ["ru", "kk", "en"]
    truth_all = {}
    for n in range(args.n):
        lang, level = langs[n % 3], levels[(n // 3) % len(levels)]
        truth = make_receipt(rng, lang)
        hide = None
        if level == "damaged":
            hide = rng.choice(["total", "tax_amount", f"items.{rng.randrange(len(truth['items']))}"])
        img, hidden = render(truth, lang, rng, hide)
        name = f"{n + 1:03d}_{lang}_{level}.jpg"
        degrade(img, level, rng).convert("RGB").save(out / name, quality={"clean": 85, "photo": 55, "damaged": 50, "faded": 45}[level])
        expected = {k: v for k, v in truth.items() if k not in hidden and k != "items"}
        hidden_line = int(hide.split(".")[1]) if hide and hide.startswith("items.") else None
        expected["items"] = [
            [None, None, None] if k == hidden_line else [i["quantity"], i["unit_price"], i["total"]]
            for k, i in enumerate(truth["items"])
        ]
        expected["_hidden"] = sorted(hidden)
        truth_all[name] = expected
    (out / "truth.json").write_text(json.dumps(truth_all, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{args.n} receipts written to {out}")


if __name__ == "__main__":
    main()
