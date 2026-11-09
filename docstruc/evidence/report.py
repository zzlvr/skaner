"""A self-contained HTML report: each document next to its extracted fields, every value linked to the place
it was found, with the checks that passed or failed. One file, no network, nothing leaves the machine.

Everything printed in it comes from documents, so all text is escaped here and the page runs no data as code.
"""

import base64
from html import escape
from io import BytesIO
from pathlib import Path

from docstruc.core.models import Document, ExtractionResult
from docstruc.evidence import ocr_available, read_pages

IMAGE_MAX_SIDE = 1500

_CSS = """
:root{--bg:#f6f8fb;--card:#fff;--ink:#14213d;--muted:#5b6b82;--line:#dfe6f0;--ok:#1a8f55;--warn:#c27c00;--err:#d1343c;--none:#8a97a8;--hl:#2f6fed}
@media (prefers-color-scheme:dark){:root{--bg:#0b1626;--card:#12233a;--ink:#e8eefc;--muted:#9fb2d4;--line:#24395a;--ok:#46c982;--warn:#ffb020;--err:#ff6b73;--none:#7f8fa8;--hl:#7aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 Inter,"Segoe UI",Arial,sans-serif}
main{max-width:1280px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 20px}
section.doc{background:var(--card);border:1px solid var(--line);border-radius:14px;margin:0 0 24px;padding:18px}
.head{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}.head h2{font-size:18px;margin:0;word-break:break-all}
.badge{font-size:13px;font-weight:700;padding:3px 11px;border-radius:99px;border:1px solid currentColor}.badge.ok{color:var(--ok)}.badge.review{color:var(--err)}
.meta{color:var(--muted);font-size:13px}
.grid{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:18px;align-items:start}
@media (max-width:860px){.grid{grid-template-columns:1fr}}
.pages{display:flex;flex-direction:column;gap:12px}.page{position:relative;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fff}
.page img{display:block;width:100%;height:auto}.box{position:absolute;border:2px solid var(--none);border-radius:3px;background:transparent;cursor:pointer;opacity:.0;transition:opacity .12s}
.box.ok{border-color:var(--ok);background:color-mix(in srgb,var(--ok) 14%,transparent)}.box.warning{border-color:var(--warn);background:color-mix(in srgb,var(--warn) 22%,transparent)}
.box.error{border-color:var(--err);background:color-mix(in srgb,var(--err) 24%,transparent)}
.showall .box,.box.flag,.box.sel{opacity:1}.box.sel{outline:3px solid var(--hl);outline-offset:1px;z-index:2}
.tools{display:flex;gap:14px;align-items:center;color:var(--muted);font-size:13px;margin:0 0 10px}
h3{font-size:14px;margin:16px 0 8px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
ul.issues{list-style:none;margin:0;padding:0}.issues li{display:flex;gap:8px;padding:7px 10px;border-radius:8px;margin-bottom:6px;border:1px solid var(--line);cursor:pointer}
.issues li.error{border-color:var(--err)}.issues li.warning{border-color:var(--warn)}.issues .tag{font-weight:700;font-size:12px;text-transform:uppercase}
.issues .error .tag{color:var(--err)}.issues .warning .tag{color:var(--warn)}.none{color:var(--ok);font-weight:600}
table{width:100%;border-collapse:collapse}td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}tr.f{cursor:pointer}
tr.f:hover,tr.f.sel{background:color-mix(in srgb,var(--hl) 12%,transparent)}td.k{color:var(--muted);width:38%;word-break:break-word}td.v{font-weight:600;word-break:break-word}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;background:var(--none)}.dot.ok{background:var(--ok)}.dot.warning{background:var(--warn)}.dot.error{background:var(--err)}
.note{color:var(--muted);font-size:12.5px;margin-top:14px}
"""

_JS = """
document.querySelectorAll('section.doc').forEach(function(sec){
  function pick(id){
    sec.querySelectorAll('.sel').forEach(function(e){e.classList.remove('sel')});
    sec.querySelectorAll('[data-f="'+id+'"]').forEach(function(e){e.classList.add('sel')});
    var b=sec.querySelector('.box[data-f="'+id+'"]'); if(b){b.scrollIntoView({block:'center',behavior:'smooth'})}
  }
  sec.addEventListener('click',function(ev){var t=ev.target.closest('[data-f]'); if(t&&t.dataset.f){pick(t.dataset.f)}});
  var all=sec.querySelector('input.all'); if(all){all.addEventListener('change',function(){sec.querySelector('.pages').classList.toggle('showall',all.checked)})}
});
"""


def _image_uri(jpeg: bytes) -> str:
    from PIL import Image

    with Image.open(BytesIO(jpeg)) as image:
        if max(image.size) > IMAGE_MAX_SIDE:
            image = image.copy()
            image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        out = BytesIO()
        image.convert("RGB").save(out, "JPEG", quality=78)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def _label(path: str) -> str:
    parts = path.split(".")
    if parts[0] == "items" and len(parts) == 3 and parts[1].isdigit():
        return f"Line {int(parts[1]) + 1} · {parts[2].replace('_', ' ')}"
    return " · ".join(p.replace("_", " ") for p in parts)


def _flat(node, path: str = ""):
    if isinstance(node, dict):
        for k, v in node.items():
            if k != "uncertain_fields":
                yield from _flat(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for n, v in enumerate(node):
            yield from _flat(v, f"{path}.{n}" if path else str(n))
    elif node is not None and node != "":
        yield path, node


def _status(result: ExtractionResult, path: str) -> str:
    kinds = {i.severity for i in result.issues if i.field == path}
    return "error" if "error" in kinds else "warning" if "warning" in kinds else "ok"


def _section(result: ExtractionResult, parts: list[Document]) -> str:
    pages = read_pages(parts)
    fields = list(_flat(result.data))
    ids = {path: n for n, (path, _v) in enumerate(fields)}
    for i in result.issues:  # an issue on a field that has no value still deserves an id
        ids.setdefault(i.field, len(ids))

    page_html = []
    for page in pages:
        boxes = []
        for path, ev in result.evidence.items():
            if (ev.part, ev.page) != (page.part, page.index):
                continue
            x0, y0, x1, y1 = ev.box
            status = _status(result, path)
            flag = " flag" if status != "ok" else ""
            boxes.append(
                f'<div class="box {status}{flag}" data-f="{ids[path]}" title="{escape(_label(path))}" '
                f'style="left:{x0 * 100:.2f}%;top:{y0 * 100:.2f}%;width:{(x1 - x0) * 100:.2f}%;height:{(y1 - y0) * 100:.2f}%"></div>'
            )
        page_html.append(f'<div class="page"><img alt="" src="{_image_uri(page.image)}">{"".join(boxes)}</div>')

    rows = []
    for path, value in fields:
        status = _status(result, path)
        located = path in result.evidence
        hint = "" if located else ' <span class="meta">(position unknown)</span>'
        rows.append(
            f'<tr class="f" data-f="{ids[path]}"><td class="k"><span class="dot {status}"></span>{escape(_label(path))}</td>'
            f'<td class="v">{escape(str(value))}{hint}</td></tr>'
        )

    issue_html = "".join(
        f'<li class="{i.severity}" data-f="{ids[i.field]}"><span class="tag">{i.severity}</span>'
        f"<span><b>{escape(_label(i.field))}</b>: {escape(i.message)}</span></li>"
        for i in result.issues
    )
    status_badge = '<span class="badge review">Needs review</span>' if result.needs_review else '<span class="badge ok">Checks passed</span>'
    located = f"{len(result.evidence)} of {len(fields)} values located on the page"
    return (
        f'<section class="doc"><div class="head"><h2>{escape(result.source)}</h2>{status_badge}'
        f'<span class="meta">{escape(result.doctype)} · {escape(result.model)} · {located}</span></div>'
        f'<div class="grid"><div><div class="tools"><label><input class="all" type="checkbox"> Show every located value</label></div>'
        f'<div class="pages">{"".join(page_html)}</div></div>'
        f'<div><h3>Checks</h3>{"<ul class=issues>" + issue_html + "</ul>" if issue_html else "<p class=none>All checks passed.</p>"}'
        f'<h3>Values</h3><table>{"".join(rows)}</table>'
        f'<p class="note">Click a value or a mark to see it on the page. Positions come from the PDF text or from OCR and '
        f"are approximate; OCR can misread, so a missing position is a prompt to look, not proof of an error.</p></div></div></section>"
    )


def build_report(entries: list[tuple[ExtractionResult, list[Document]]], title: str = "Extraction report") -> str:
    """One HTML page for a batch: `entries` pairs each result with the file(s) it was read from."""
    note = "" if ocr_available() else " OCR is not installed, so photos have no positions (pip install \"docstruc[locate]\")."
    body = "".join(_section(result, parts) for result, parts in entries)
    reviewed = sum(r.needs_review for r, _ in entries)
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body><main><h1>{escape(title)}</h1>"
        f'<p class="sub">{len(entries)} documents, {reviewed} need review.{escape(note)}</p>{body}</main><script>{_JS}</script></body></html>'
    )


def write_report(entries: list[tuple[ExtractionResult, list[Document]]], path: str | Path, title: str = "Extraction report") -> Path:
    path = Path(path)
    path.write_text(build_report(entries, title), encoding="utf-8")
    return path
