import pymupdf

from docstruc.core.models import Document
from docstruc.digitize import DigitalPage, digitize, render_pdf, sanitize_html


def test_sanitizer_keeps_structure_and_drops_unsafe_content():
    raw = ('<h1 style="color:red; text-align: center" onclick="x()">Title</h1>'
           '<script>alert(1)</script><img src="http://evil/x.png"><a href="http://x">link</a>'
           '<table><tr><td colspan="2" style="text-align:right">1 &amp; 2</td></tr></table>')
    clean = sanitize_html(raw)
    assert '<h1 style="text-align: center">Title</h1>' in clean
    assert "script" not in clean and "alert" not in clean and "img" not in clean and "href" not in clean
    assert "link" in clean  # text of a dropped tag is kept
    assert '<td colspan="2" style="text-align: right">1 &amp; 2</td>' in clean


def test_placeholders_are_marked():
    assert sanitize_html("<p>Директор ____ [подпись]</p>") == '<p>Директор ____ <span class="ph">[подпись]</span></p>'


def test_render_keeps_real_text_and_starts_each_source_page_on_a_new_page():
    pages = [DigitalPage(html="<h1>СПРАВКА</h1><p>Выдана <b>Иванову</b>.</p>"),
             DigitalPage(html="<p>Second page ₸ 350 000</p>")]
    content, count = render_pdf(pages)
    with pymupdf.open(stream=content, filetype="pdf") as pdf:
        assert count == pdf.page_count == 2
        assert "СПРАВКА" in pdf[0].get_text() and "Иванову" in pdf[0].get_text()
        assert "350 000" in pdf[1].get_text()


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.calls = []

    def extract(self, parts, schema, instructions):
        self.calls.append((parts[0].name, schema.__name__))
        assert "Never reproduce signatures" in instructions
        return schema(html=f"<p>Page from {parts[0].name}</p>")


def test_digitize_transcribes_each_page_in_order():
    backend = FakeBackend()
    docs = [Document(name=n, mime_type="image/jpeg", data=b"x") for n in ("p1.jpg", "p2.jpg")]
    content, pages = digitize(docs, backend)
    assert backend.calls == [("p1.jpg", "DigitalPage"), ("p2.jpg", "DigitalPage")]
    with pymupdf.open(stream=content, filetype="pdf") as pdf:
        assert pages == 2 and "p2.jpg" in pdf[1].get_text()



def test_wide_table_is_not_cut_off_at_the_page_edge():
    """MuPDF does not shrink a table wider than the page; the right columns of a 9-column
    waybill З-2 (the sums) ran off A4 and were lost, also from the text layer. Wide pages go landscape."""
    nbsp = " "  # the model keeps the document's non-breaking thousands separators, so numbers cannot wrap
    head = "".join(f"<th>{h}</th>" for h in (
        "Номер по порядку", "Наименование, характеристика", "Номенклатурный номер", "Единица измерения",
        "Количество подлежит отпуску", "Количество отпущено", "Цена за единицу, в KZT", "Сумма с НДС, в KZT",
        "Сумма НДС, в KZT"))
    cells = ("1", "Картридж лазерный HP 85A (CE285A)", "659-6136", "шт", "25", "25", "18 900,00", "472 500,00",
             "50 625,00")
    row = "".join(f"<td>{v.replace(' ', nbsp) if v[0].isdigit() else v}</td>" for v in cells)
    wide = DigitalPage(html=f"<table><tr>{head}</tr><tr>{row}</tr></table>")
    narrow = DigitalPage(html="<p>Акт № 5</p><table><tr><td>Итого</td><td>1 000,00</td></tr></table>")
    pdf_bytes, count = render_pdf([wide, narrow])
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        text = " ".join(" ".join(p.get_text().replace(nbsp, " ").split()) for p in pdf)
        sizes = [(round(p.rect.width), round(p.rect.height)) for p in pdf]
    assert "472 500,00" in text and "50 625,00" in text and "Сумма НДС" in text
    assert sizes[0] == (842, 595) and sizes[-1] == (595, 842)  # landscape only where needed
