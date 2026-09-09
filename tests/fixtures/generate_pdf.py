"""Regenerate the small, synthetic PDF viewer fixtures; not required to run tests.

uv run --with reportlab --with pypdf tests/fixtures/generate_pdf.py
"""

from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

out = Path(__file__).parent
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
pdf = canvas.Canvas(str(out / "preview.pdf"), pagesize=A4, invariant=True)
pdf.setTitle("Ava PDF preview fixture")
for page, (title, subtitle) in enumerate(
    [
        ("A clearer workspace", "中文文件预览"),
        ("Room to explore", "横向页面与不同尺寸"),
        ("Review notes", "第三页：检查分页、缩放和文本选择"),
    ], 1,
):
    size = landscape(A4) if page == 2 else A4
    pdf.setPageSize(size)
    w, h = size
    pdf.bookmarkPage(f"page-{page}", fit="XYZ", left=0, top=h, zoom=1)
    pdf.setFillColor(HexColor("#386ac6"))
    pdf.roundRect(40, h - 76, 112, 24, 7, fill=1, stroke=0)
    pdf.setFillColor(HexColor("#ffffff"))
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(52, h - 68, "AVA / DOCUMENTS")
    pdf.setFillColor(HexColor("#202123"))
    pdf.setFont("Helvetica-Bold", 28)
    pdf.drawString(40, h - 132, title)
    pdf.setFont("STSong-Light", 19)
    pdf.drawString(40, h - 170, subtitle)
    pdf.setFont("Helvetica", 12)
    pdf.drawString(40, h - 220, "Pages stay sharp as you zoom. Select text to copy it.")
    pdf.setFont("STSong-Light", 14)
    pdf.drawString(40, h - 252, "清晰的中文内容，不需要跳转外部软件即可阅读。")
    pdf.setFillColor(HexColor("#f3f6fc"))
    pdf.roundRect(40, h - 360, w - 80, 70, 12, fill=1, stroke=0)
    pdf.setFillColor(HexColor("#386ac6"))
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(58, h - 322, "Jump to review notes" if page == 1 else "Back to the first page")
    pdf.linkRect("", "page-3" if page == 1 else "page-1", (40, h - 360, w - 40, h - 290), thickness=0)
    pdf.setFillColor(HexColor("#76767d"))
    pdf.setFont("Helvetica", 10)
    pdf.drawString(40, 32, "Generated acceptance fixture - no private content")
    pdf.drawRightString(w - 40, 32, f"{page} / 3")
    pdf.showPage()
pdf.save()
reader = PdfReader(out / "preview.pdf")
assert len(reader.pages) == 3 and "中文文件预览" in reader.pages[0].extract_text()
writer = PdfWriter(clone_from=reader)
writer.encrypt(user_password="ava-test", owner_password="fixture-owner", algorithm="RC4-128")
with (out / "preview-locked.pdf").open("wb") as stream:
    writer.write(stream)
