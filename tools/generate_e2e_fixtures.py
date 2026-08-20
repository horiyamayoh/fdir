"""Generate small real document fixtures used by the FDIR E2E gate.

The fixtures are intentionally constructed from the public container formats,
not from pre-authored IR.  The adapter must open the generated DOCX/XLSX/PDF
and derive the IR itself.  Keeping generation deterministic makes the suite
portable while avoiding binary blobs in the source review.
"""

from __future__ import annotations

from pathlib import Path
import re
import struct
from typing import Iterable
import zipfile


ROOT = Path(__file__).resolve().parents[1] / "e2e" / "fixtures"
NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"


def write_zip(path: Path, parts: dict[str, str | bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in sorted(parts.items()):
            archive.writestr(name, value.encode("utf-8") if isinstance(value, str) else value)


def docx_parts() -> dict[str, str | bytes]:
    content_types = '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>
</Types>'''
    package_rels = '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
    document_rels = '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rIdComments" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
<Relationship Id="rIdFootnotes" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>
</Relationships>'''
    document = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{NS_W}" xmlns:wp="{NS_WP}" xmlns:a="{NS_A}" xmlns:wps="{NS_WPS}">
 <w:body>
  <w:p>
   <w:pPr><w:pStyle w:val="Heading1"/><w:spacing w:after="120"/></w:pPr>
   <w:r><w:rPr><w:b/><w:color w:val="1F4E79"/></w:rPr><w:t>FDIR DOCX E2E</w:t></w:r>
  </w:p>
  <w:p>
   <w:commentRangeStart w:id="0"/><w:r><w:t xml:space="preserve">A paragraph with </w:t></w:r>
   <w:r><w:rPr><w:b/></w:rPr><w:t>bold</w:t></w:r><w:commentRangeEnd w:id="0"/>
   <w:r><w:commentReference w:id="0"/></w:r>
  </w:p>
  <w:p><w:r><w:t>Revision </w:t></w:r><w:ins w:id="1" w:author="FDIR"><w:r><w:t>inserted</w:t></w:r></w:ins><w:del w:id="2" w:author="FDIR"><w:r><w:delText>deleted</w:delText></w:r></w:del></w:p>
  <w:p><w:fldSimple w:instr=" PAGE "><w:r><w:t>1</w:t></w:r></w:fldSimple><w:footnoteReference w:id="1"/></w:p>
  <w:tbl>
   <w:tblPr><w:tblStyle w:val="TableGrid"/></w:tblPr>
   <w:tr><w:tc><w:p><w:r><w:t>Key</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>
   <w:tr><w:tc><w:p><w:r><w:t>answer</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>42</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
  <w:p>
   <w:r><w:t>Callout:</w:t></w:r>
   <w:drawing><wp:inline><wp:extent cx="1524000" cy="762000"/><wp:docPr id="1" name="Callout"/><a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"><wps:wsp><wps:cNvSpPr/><wps:spPr><a:prstGeom prst="roundRect"/></wps:spPr><wps:txbx><w:txbxContent><w:p><w:r><w:t>Shape text</w:t></w:r></w:p></w:txbxContent></wps:txbx></wps:wsp></a:graphicData></a:graphic></wp:inline></w:drawing>
  </w:p>
  <w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
 </w:body>
</w:document>'''
    styles = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="{NS_W}">
 <w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Aptos"/></w:rPr></w:style>
 <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
 <w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/></w:style>
</w:styles>'''
    comments = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:comments xmlns:w="{NS_W}"><w:comment w:id="0" w:author="E2E" w:date="2026-01-01T00:00:00Z"><w:p><w:r><w:t>Review comment</w:t></w:r></w:p></w:comment></w:comments>'''
    footnotes = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:footnotes xmlns:w="{NS_W}"><w:footnote w:id="-1"/><w:footnote w:id="0"/><w:footnote w:id="1"><w:p><w:r><w:t>Footnote text</w:t></w:r></w:p></w:footnote></w:footnotes>'''
    return {
        "[Content_Types].xml": content_types,
        "_rels/.rels": package_rels,
        "word/_rels/document.xml.rels": document_rels,
        "word/document.xml": document,
        "word/styles.xml": styles,
        "word/comments.xml": comments,
        "word/footnotes.xml": footnotes,
    }


def unsupported_docx_parts() -> dict[str, str | bytes]:
    parts = docx_parts()
    document = str(parts["word/document.xml"])
    parts["word/document.xml"] = document.replace(
        " </w:body>",
        '  <w:customXml w:element="unsupported-extension"/>\n </w:body>',
    )
    return parts


def xlsx_parts() -> dict[str, str | bytes]:
    content_types = '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/><Override PartName="/xl/tables/table1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/><Override PartName="/xl/charts/chart1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>
</Types>'''
    rels = '''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'''
    workbook_rels = '''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/><Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table" Target="tables/table1.xml"/></Relationships>'''
    workbook = '''<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><workbookPr date1904="0"/><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets><calcPr calcId="191029" calcMode="auto"/></workbook>'''
    shared = '''<?xml version="1.0" encoding="UTF-8"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="4" uniqueCount="4"><si><t>Name</t></si><si><t>Alpha</t></si><si><t>Beta</t></si><si><t>Result</t></si></sst>'''
    sheet = '''<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><dimension ref="A1:C4"/><sheetViews><sheetView workbookViewId="0"/></sheetViews><sheetData>
<row r="1"><c r="A1" t="s" s="1"><v>0</v></c><c r="B1" t="s" s="1"><v>3</v></c><c r="C1" t="s" s="1"><v>3</v></c></row>
<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2" t="n"><v>42</v></c><c r="C2"><f>SUM(B2:B3)</f><v>84</v></c></row>
<row r="3"><c r="A3" t="s"><v>2</v></c><c r="B3" t="n"><v>42</v></c><c r="C3" t="str"><v>cached</v></c></row>
<row r="4"><c r="A4" t="s"><v>3</v></c><c r="B4" t="n" s="2"><v>45292</v></c></row>
</sheetData><mergeCells count="1"><mergeCell ref="A1:A2"/></mergeCells><conditionalFormatting sqref="B2:B3"><cfRule type="cellIs" dxfId="0" priority="1" operator="greaterThan"><formula>40</formula></cfRule></conditionalFormatting><tableParts count="1"><tablePart r:id="rIdTable"/></tableParts></worksheet>'''
    styles = '''<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/></numFmts><fonts count="1"><font><sz val="11"/><name val="Aptos"/></font></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills><borders count="1"><border/></borders><cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="0" applyFont="1"/><xf numFmtId="164" applyNumberFormat="1"/></cellXfs><dxfs count="1"><dxf><fill><patternFill patternType="solid"><fgColor rgb="FFFF00"/></patternFill></fill></dxf></dxfs></styleSheet>'''
    table = '''<?xml version="1.0" encoding="UTF-8"?><table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" id="1" name="DataTable" displayName="DataTable" ref="A1:C4" headerRowCount="1"><autoFilter ref="A1:C4"/><tableColumns count="3"><tableColumn id="1" name="Name"/><tableColumn id="2" name="Result"/><tableColumn id="3" name="Result"/></tableColumns><tableStyleInfo name="TableStyleMedium2" showFirstColumn="0" showLastColumn="0" showRowStripes="1" showColumnStripes="0"/></table>'''
    chart = '''<?xml version="1.0" encoding="UTF-8"?><chartSpace xmlns="http://schemas.openxmlformats.org/drawingml/2006/chart"><chart><plotArea><layout/><barChart><barDir val="col"/><grouping val="clustered"/><ser><idx val="0"/><order val="0"/></ser></barChart></plotArea></chart></chartSpace>'''
    return {
        "[Content_Types].xml": content_types,
        "_rels/.rels": rels,
        "xl/_rels/workbook.xml.rels": workbook_rels,
        "xl/workbook.xml": workbook,
        "xl/sharedStrings.xml": shared,
        "xl/worksheets/sheet1.xml": sheet,
        "xl/styles.xml": styles,
        "xl/tables/table1.xml": table,
        "xl/charts/chart1.xml": chart,
    }


def unsupported_xlsx_parts() -> dict[str, str | bytes]:
    parts = xlsx_parts()
    parts["xl/pivotTables/pivot1.xml"] = "<?xml version=\"1.0\"?><pivotTableDefinition xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" name=\"UnsupportedPivot\"/>"
    return parts


def pdf_bytes(*, unsupported: bool = False) -> bytes:
    stream = b"BT /F1 18 Tf 72 720 Td (FDIR PDF E2E) Tj ET\n"
    if unsupported:
        stream += b"/XUnsupported Do\n"
    stream += b"0 0 m 120 120 l 120 0 l h W n\n72 680 m 200 680 l S\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{number} 0 obj\n".encode("ascii"))
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects)+1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(out)


MARKDOWN = """---
title: FDIR Markdown E2E
dialect: commonmark
---

# FDIR Markdown E2E

A paragraph with **bold**, *emphasis*, `inline code`, and a [link][ref].  \\
The hard break is authored.

- Alpha
- Beta

| Name | Value |
| --- | ---: |
| answer | 42 |

![diagram](image.png "alt")

```python
print('e2e')
```

<span data-fdir="raw">raw HTML</span>

[^1]: Footnote definition.

[ref]: https://example.invalid/reference "Reference"
"""

UNSUPPORTED_MARKDOWN = MARKDOWN + """
::: unsupported-extension
This directive is intentionally outside the bounded Markdown dialect.
:::
"""


def write_fixtures(root: Path = ROOT) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    paths = {
        "docx": root / "sample.docx",
        "xlsx": root / "sample.xlsx",
        "pdf": root / "sample.pdf",
        "markdown": root / "sample.md",
        "malformed_docx": root / "malformed.docx",
        "malformed_xlsx": root / "malformed.xlsx",
        "malformed_pdf": root / "malformed.pdf",
        "malformed_markdown": root / "malformed.md",
        "unsupported_docx": root / "unsupported.docx",
        "unsupported_xlsx": root / "unsupported.xlsx",
        "unsupported_pdf": root / "unsupported.pdf",
        "unsupported_markdown": root / "unsupported.md",
    }
    write_zip(paths["docx"], docx_parts())
    write_zip(paths["xlsx"], xlsx_parts())
    paths["pdf"].write_bytes(pdf_bytes())
    paths["markdown"].write_text(MARKDOWN, encoding="utf-8", newline="\n")
    paths["malformed_docx"].write_bytes(b"not a zip document")
    paths["malformed_xlsx"].write_bytes(b"not a zip workbook")
    paths["malformed_pdf"].write_bytes(b"%PDF-1.7\nnot a valid xref")
    paths["malformed_markdown"].write_text("# malformed\n\x00\x00\n", encoding="utf-8", newline="\n")
    write_zip(paths["unsupported_docx"], unsupported_docx_parts())
    write_zip(paths["unsupported_xlsx"], unsupported_xlsx_parts())
    paths["unsupported_pdf"].write_bytes(pdf_bytes(unsupported=True))
    paths["unsupported_markdown"].write_text(UNSUPPORTED_MARKDOWN, encoding="utf-8", newline="\n")
    return paths


if __name__ == "__main__":
    generated = write_fixtures()
    for name, path in generated.items():
        print(f"{name}: {path}")
