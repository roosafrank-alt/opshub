"""Generates a label image for a shop part and sends it straight to the
Brother QL-810W thermal label printer over USB, no browser print dialog
involved. Mirrors the QR-vs-barcode logic already used client-side in
part_detail.html / labels.html: our own "SHOP-..." codes print as a QR
(forgiving for a phone camera), anything else (a manufacturer barcode)
prints as a Code128 barcode.
"""
import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

# DK-1201-equivalent 1.1" x 3.5" (29mm x 90mm) die-cut label, matching what's
# loaded in the shop's QL-810W.
LABEL_SIZE = "29x90"
LABEL_PX = (306, 991)  # width x height in px, per brother_ql's own label table

PRINTER_MODEL = "QL-810W"
PRINTER_IDENTIFIER = "usb://0x04f9:0x209c"
BACKEND = "pyusb"

_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _font(size, bold=False):
    try:
        return ImageFont.truetype(_FONT_BOLD if bold else _FONT_REGULAR, size)
    except Exception:
        return ImageFont.load_default()


def _resize_bilevel(im, new_w, new_h):
    """Resize a 1-bit image cleanly by resizing in grayscale then
    re-thresholding, instead of letting PIL nearest-neighbor a bitmap."""
    im = im.convert("L").resize((new_w, new_h), Image.LANCZOS)
    im = im.point(lambda x: 0 if x < 128 else 255)
    return im.convert("1")


def _draw_centered(draw, text, y, font, width, fill=0):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(((width - tw) // 2, y), text, font=font, fill=fill)
    return th


def generate_label_image(name, code, location=None, force_qr=False):
    w, h = LABEL_PX
    img = Image.new("1", (w, h), 1)
    draw = ImageDraw.Draw(img)

    if force_qr or code.startswith("SHOP-"):
        import qrcode
        # High error correction (~30% of the code can be smudged/torn and
        # still scan) - these get stuck on tools and job boards in a shop,
        # not kept pristine like a shipping label.
        qr = qrcode.QRCode(border=2, box_size=6, error_correction=qrcode.constants.ERROR_CORRECT_H)
        qr.add_data(code)
        qr.make(fit=True)
        code_img = qr.make_image(fill_color="black", back_color="white").convert("1")
        side = min(w - 40, code_img.size[0])
        code_img = _resize_bilevel(code_img, side, side)
        img.paste(code_img, ((w - side) // 2, 20))
        y = 20 + side + 15
    else:
        import barcode
        from barcode.writer import ImageWriter
        bc = barcode.get("code128", code, writer=ImageWriter())
        buf = io.BytesIO()
        bc.write(buf, options={
            "module_width": 0.3, "module_height": 12,
            "write_text": False, "quiet_zone": 2,
        })
        buf.seek(0)
        code_img = Image.open(buf).convert("1")
        bc_w = min(w - 20, code_img.size[0])
        bc_h = int(code_img.size[1] * (bc_w / code_img.size[0]))
        code_img = _resize_bilevel(code_img, bc_w, bc_h)
        img.paste(code_img, ((w - bc_w) // 2, 20))
        y = 20 + bc_h + 15

    font_name = _font(26, bold=True)
    font_small = _font(18)

    for line in (textwrap.wrap(name, width=18) or [name])[:3]:
        th = _draw_centered(draw, line, y, font_name, w)
        y += th + 6

    y += 6
    th = _draw_centered(draw, code, y, font_small, w)
    y += th + 20

    if location:
        _draw_centered(draw, location, y, font_small, w)

    return img


def print_label_image(img):
    from brother_ql.conversion import convert
    from brother_ql.backends.helpers import send
    from brother_ql.raster import BrotherQLRaster

    qlr = BrotherQLRaster(PRINTER_MODEL)
    qlr.exception_on_warning = True
    instructions = convert(
        qlr=qlr, images=[img], label=LABEL_SIZE, rotate="0",
        threshold=70, dither=False, compress=False, red=False,
        dpi_600=False, hq=True, cut=True,
    )
    send(instructions=instructions, printer_identifier=PRINTER_IDENTIFIER,
         backend_identifier=BACKEND, blocking=True)


def print_part_label(part):
    """part: a sqlite3.Row (or dict) with name, barcode, and optionally location."""
    location = part["location"] if "location" in part.keys() else None
    img = generate_label_image(part["name"], part["barcode"], location)
    print_label_image(img)


def print_project_label(project, asset_tag=None):
    """project: a sqlite3.Row (or dict) with name and code. asset_tag: the
    aircraft's tail number/tag, if known, shown as the subtitle line.
    Always a QR (matches the on-screen project_label.html page), since a
    plain project code like "26-001" doesn't start with "SHOP-"."""
    img = generate_label_image(project["name"], project["code"], asset_tag, force_qr=True)
    print_label_image(img)


def print_laborer_label(laborer):
    """laborer: a sqlite3.Row (or dict) with name and code. Always a QR -
    a LABOR-xxxx code doesn't start with "SHOP-" either."""
    img = generate_label_image(laborer["name"], laborer["code"], force_qr=True)
    print_label_image(img)


def print_task_label(title, subtitle, code):
    """A project's General/task or sub-area QR code (TASK-...), or the
    General Shop clock-in code. Always a QR."""
    img = generate_label_image(title, code, subtitle, force_qr=True)
    print_label_image(img)
