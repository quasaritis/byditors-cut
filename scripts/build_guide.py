"""Build "Byditors Cut - Setup Guide.pdf" from docs/SETUP_GUIDE.md.

Needs: pip install markdown, and Google Chrome or Chromium.  Run:  python scripts/build_guide.py
"""
import pathlib
import re
import shutil
import subprocess
import tempfile

import markdown

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = """
@page { size: A4; margin: 18mm 17mm 18mm 17mm; @bottom-right { content: counter(page); } }
:root { --ink:#16151a; --mute:#5b5966; --line:#e4e2ea; --accent:#5b3df5; --tint:#f4f2fe; }
* { box-sizing: border-box; }
html { font: 10.4pt/1.55 "Inter", system-ui, sans-serif; color: var(--ink); -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin: 0; }
.cover { padding: 6mm 0 8mm; border-bottom: 2px solid var(--ink); margin-bottom: 7mm; }
.cover .brand { font: 600 9pt/1 "Inter"; letter-spacing: .02em; color: var(--accent); margin: 0 0 10mm; }
.cover h1 { font: 700 30pt/1.05 "Inter Display","Inter"; letter-spacing: -0.03em; margin: 0 0 4mm; max-width: 15ch; }
.cover p { color: var(--mute); margin: 0; max-width: 62ch; font-size: 11pt; }
h2 { font: 700 17pt/1.15 "Inter Display","Inter"; letter-spacing: -0.02em; margin: 0 0 4mm; padding-top: 2mm; }
h2.part { margin-top: 10mm; padding-top: 6mm; border-top: 1px solid var(--line); break-after: avoid; }
table, ol { break-before: avoid; }
p, li { max-width: 72ch; }
p { margin: 0 0 3mm; }
h3 { font: 650 11.2pt/1.3 "Inter"; margin: 5mm 0 2mm; break-after: avoid; }
ol, ul { margin: 0 0 3.5mm; padding-left: 6mm; }
li { margin: 0 0 1.6mm; }
li > ul { margin-top: 1.4mm; }
code { font: 500 9pt/1.3 ui-monospace, "DejaVu Sans Mono", monospace; background: var(--tint); color: #2b1d8f; padding: .4mm 1.4mm; border-radius: 1.2mm; white-space: nowrap; }
a { color: var(--accent); text-decoration: none; }
table { width: 100%; border-collapse: collapse; margin: 2mm 0 5mm; font-size: 9.4pt; break-inside: auto; }
th { text-align: left; font-weight: 600; color: var(--mute); border-bottom: 1.5px solid var(--ink); padding: 1.6mm 2mm; }
td { border-bottom: 1px solid var(--line); padding: 1.8mm 2mm; vertical-align: top; }
tr { break-inside: avoid; }
td:first-child { font-weight: 600; width: 34%; }
h2 + p { font-size: 10.8pt; }
li strong { font-weight: 650; }
"""
COVER = """<p class="brand">BYDITORS CUT · SETUP GUIDE</p><h1>From unzip to clips that post themselves.</h1>
<p>Install, free AI, YouTube, Instagram, streamers, daily uploads, phone alerts and fixes, step by step. Only part 1 is required.</p>"""


def build() -> pathlib.Path:
    src = (ROOT / "docs" / "SETUP_GUIDE.md").read_text(encoding="utf-8")
    body = src.split("\n", 1)[1]
    lines = []
    for line in body.split("\n"):          # 3-space sub-lists → 4 spaces, as python-markdown expects
        if re.match(r"^ {3}- ", line):
            line = " " + line
        elif re.match(r"^ {5}\S", line):
            line = "      " + line.lstrip()
        lines.append(line)
    html = markdown.markdown("\n".join(lines), extensions=["tables", "sane_lists"]).replace("<hr />", "")
    html = html.replace("<h2>", '<h2 class="part">')
    html = re.sub(r"<p><strong>([^<]+)</strong></p>", r"<h3>\1</h3>", html)
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Byditors Cut — Setup guide</title>'
           f'<style>{CSS}</style></head><body><header class="cover">{COVER}</header>{html}</body></html>')
    tmp = pathlib.Path(tempfile.mkdtemp()) / "guide.html"
    tmp.write_text(doc, encoding="utf-8")
    out = ROOT / "Byditors Cut - Setup Guide.pdf"
    chrome = next(filter(None, map(shutil.which, ["google-chrome", "chromium", "chromium-browser", "chrome"])), None)
    if not chrome:
        raise SystemExit("Install Google Chrome or Chromium first")
    subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={out}", tmp.as_uri()], check=True, capture_output=True)
    return out


if __name__ == "__main__":
    print(build())
