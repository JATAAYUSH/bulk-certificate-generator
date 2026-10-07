"""Certificate rendering: one fixed template, drawn with reportlab.

The template is code (a landscape A4 page with a border, title, recipient name,
event, date and issuer). Text is drawn with canvas.drawString rather than
Paragraph, so user-supplied text is never interpreted as markup.

Limitation: the built-in Helvetica font covers Western European characters but
not e.g. CJK/Arabic. Supporting those would mean registering a TTF font.
"""
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = landscape(A4)
NAVY = colors.HexColor("#1f3a5f")
GOLD = colors.HexColor("#b8923a")


def _fit_font_size(text, font, max_size, max_width, min_size=10):
    """Shrink the font until the text fits the available width."""
    size = max_size
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def _centered(c, text, y, font, size, color=colors.black, max_width=None):
    if max_width:
        size = _fit_font_size(text, font, size, max_width)
    c.setFont(font, size)
    c.setFillColor(color)
    c.drawCentredString(PAGE_W / 2, y, text)


def render_certificate(path, *, recipient_name, event_name, issue_date, issued_by=None):
    """Write a single-page PDF certificate to `path`."""
    c = canvas.Canvas(str(path), pagesize=(PAGE_W, PAGE_H))
    c.setTitle(f"Certificate - {recipient_name}")
    c.setAuthor(issued_by or "Certificate Generator")

    # Double border
    c.setStrokeColor(NAVY)
    c.setLineWidth(6)
    c.rect(25, 25, PAGE_W - 50, PAGE_H - 50)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.5)
    c.rect(40, 40, PAGE_W - 80, PAGE_H - 80)

    usable = PAGE_W - 140
    _centered(c, "CERTIFICATE", PAGE_H - 140, "Helvetica-Bold", 44, NAVY)
    _centered(c, "OF PARTICIPATION", PAGE_H - 170, "Helvetica", 16, GOLD)
    _centered(c, "This certificate is proudly presented to", PAGE_H - 230,
              "Helvetica-Oblique", 15, colors.grey)
    _centered(c, recipient_name, PAGE_H - 285, "Helvetica-Bold", 38, colors.black, usable)

    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.line(PAGE_W / 2 - 160, PAGE_H - 297, PAGE_W / 2 + 160, PAGE_H - 297)

    _centered(c, "for successfully participating in", PAGE_H - 335, "Helvetica", 15, colors.grey)
    _centered(c, event_name, PAGE_H - 375, "Helvetica-Bold", 24, NAVY, usable)

    try:
        pretty_date = date.fromisoformat(issue_date).strftime("%d %B %Y")
    except (TypeError, ValueError):
        pretty_date = str(issue_date)
    c.setFont("Helvetica", 12)
    c.setFillColor(colors.black)
    c.drawString(90, 85, f"Date: {pretty_date}")
    if issued_by:
        c.drawRightString(PAGE_W - 90, 85, f"Issued by: {issued_by}")

    c.showPage()
    c.save()
