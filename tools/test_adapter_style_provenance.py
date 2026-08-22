"""Focused authored-fixture tests for the bounded DOCX/XLSX style lanes."""

from __future__ import annotations

from copy import deepcopy
import re
from pathlib import Path
import tempfile
import unittest
import zipfile

try:
    from convert_document import convert_path
except ImportError:  # pragma: no cover
    from tools.convert_document import convert_path


def _write_package(path: Path, parts: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, value in parts.items():
            package.writestr(name, value)


def _style(document: dict, style_id: str) -> dict:
    return next(item for item in document["styles"] if item.get("styleId") == style_id)


def _node(document: dict, node_id: str) -> dict:
    return next(item for item in document["nodes"] if item.get("nodeId") == node_id)


def _provenance(style: dict) -> dict[str, str]:
    return {
        item["property"]: item["source"]
        for item in style.get("propertyProvenance", [])
        if isinstance(item, dict) and isinstance(item.get("property"), str) and isinstance(item.get("source"), str)
    }


def _provenance_is_coherent(style: dict, style_ids: set[str]) -> bool:
    resolved = style.get("resolved")
    if not isinstance(resolved, dict):
        return False
    provenance = _provenance(style)
    return set(resolved) == set(provenance) and all(source in style_ids for source in provenance.values())


def _docx_parts(*, missing_parent: bool = False) -> dict[str, str]:
    if missing_parent:
        styles = """
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Aptos"/><w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults>
  <w:style w:type="paragraph" w:styleId="Child"><w:basedOn w:val="MissingParent"/><w:rPr><w:b/></w:rPr></w:style>
</w:styles>
"""
        document = """
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
  <w:p><w:pPr><w:pStyle w:val="Child"/></w:pPr><w:r><w:t>missing parent</w:t></w:r></w:p>
  <w:sectPr/>
</w:body></w:document>
"""
    else:
        styles = """
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Aptos"/><w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults>
  <w:style w:type="paragraph" w:styleId="Base"><w:rPr><w:sz w:val="22"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Mid"><w:basedOn w:val="Base"/><w:rPr><w:b/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Leaf"><w:basedOn w:val="Mid"/><w:rPr><w:color w:themeColor="accent1"/></w:rPr></w:style>
</w:styles>
"""
        document = """
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
  <w:p><w:pPr><w:pStyle w:val="Leaf"/></w:pPr><w:r><w:rPr><w:color w:themeColor="accent2"/><w:i/></w:rPr><w:t>direct override</w:t></w:r></w:p>
  <w:sectPr/>
</w:body></w:document>
"""
    return {
        "[Content_Types].xml": """
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
</Types>
""",
        "_rels/.rels": """
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdDocument" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
""",
        "word/_rels/document.xml.rels": """
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rIdTheme" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>
</Relationships>
""",
        "word/document.xml": document,
        "word/styles.xml": styles,
        "word/theme/theme1.xml": """
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:themeElements><a:clrScheme name="Test">
  <a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>
  <a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>
  <a:accent1><a:srgbClr val="1F4E79"/></a:accent1>
  <a:accent2><a:srgbClr val="ED7D31"/></a:accent2>
</a:clrScheme></a:themeElements></a:theme>
""",
    }


def _xlsx_parts() -> dict[str, str]:
    return {
        "[Content_Types].xml": """
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/xl/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
</Types>
""",
        "_rels/.rels": """
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdWorkbook" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
""",
        "xl/_rels/workbook.xml.rels": """
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdSheet" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rIdTheme" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>
</Relationships>
""",
        "xl/workbook.xml": """
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Data" sheetId="1" r:id="rIdSheet"/></sheets>
</workbook>
""",
        "xl/worksheets/sheet1.xml": """
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="B1:B2"/><sheetData>
  <row r="1"><c r="B1" s="1" t="n"><v>1</v></c></row>
  <row r="2"><c r="B2" s="1" t="n"><v>9</v></c></row>
</sheetData><conditionalFormatting sqref="B1:B2"><cfRule type="cellIs" dxfId="0" priority="1" stopIfTrue="1" operator="greaterThan"><formula>8</formula></cfRule></conditionalFormatting></worksheet>
""",
        "xl/styles.xml": """
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="0.00"/></numFmts>
  <fonts count="2"><font><sz val="11"/><name val="Aptos"/><color theme="1"/></font><font><b/><sz val="12"/><name val="Aptos Display"/><color rgb="80FF0000"/></font></fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="solid"><fgColor theme="4" tint="0.4"/></patternFill></fill></fills>
  <cellStyleXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/><xf numFmtId="164" fontId="1" fillId="1" borderId="1"/></cellStyleXfs>
  <cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/><xf numFmtId="164" fontId="1" fillId="1" borderId="1" xfId="1" applyNumberFormat="1" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="right"/></xf></cellXfs>
  <cellStyles count="1"><cellStyle name="AccentValue" xfId="1"/></cellStyles>
  <dxfs count="1"><dxf><fill><patternFill patternType="solid"><fgColor rgb="FFFFFF00"/></patternFill></fill></dxf></dxfs>
</styleSheet>
""",
        "xl/theme/theme1.xml": """
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:themeElements><a:clrScheme name="Test">
  <a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>
  <a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>
  <a:accent4><a:srgbClr val="70AD47"/></a:accent4>
</a:clrScheme></a:themeElements></a:theme>
""",
    }


class AdapterStyleProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fdir-style-provenance-")
        self.addCleanup(self._tmp.cleanup)

    def _convert(self, suffix: str, parts: dict[str, str]) -> dict:
        path = Path(self._tmp.name) / f"style-provenance-fixture.{suffix}"
        try:
            _write_package(path, parts)
            document, evidence = convert_path(path, suffix)
            self.assertEqual(evidence["outcome"], "success", document.get("diagnostics"))
            self.assertNotEqual(document.get("conversion", {}).get("status"), "failed")
            return document
        finally:
            path.unlink(missing_ok=True)

    def test_docx_property_cascade_direct_override_and_theme_color(self) -> None:
        document = self._convert("docx", _docx_parts())
        resolved = _style(document, "style-docx-resolved-Leaf")
        self.assertEqual(resolved["resolved"]["fontFamily"], "Aptos")
        self.assertEqual(resolved["resolved"]["fontSize"], {"value": "11", "unit": "pt"})
        self.assertEqual(resolved["resolved"]["weight"], 700)
        self.assertEqual(resolved["resolved"]["foreground"], {"kind": "rgb", "r": 31, "g": 78, "b": 121, "a": 1})
        self.assertEqual(_provenance(resolved), {
            "fontFamily": "docx-docDefaults",
            "fontSize": "style-docx-Base",
            "weight": "style-docx-Mid",
            "foreground": "theme-accent1",
        })
        self.assertEqual(resolved["cascadeTrace"], [
            {"property": "fontFamily", "source": "docx-docDefaults", "action": "default"},
            {"property": "fontSize", "source": "style-docx-Base", "action": "inherit"},
            {"property": "weight", "source": "style-docx-Mid", "action": "inherit"},
            {"property": "foreground", "source": "theme-accent1", "action": "theme"},
        ])
        direct = next(item for item in document["styles"] if item.get("origin") == "direct" and "italic" in item.get("direct", {}))
        self.assertEqual(direct["direct"]["italic"], True)
        self.assertEqual(direct["direct"]["foreground"], {"kind": "rgb", "r": 237, "g": 125, "b": 49, "a": 1})
        self.assertEqual(direct["cascadeTrace"], [
            {"property": "italic", "source": "direct-formatting", "action": "direct"},
            {"property": "foreground", "source": "theme-accent2", "action": "theme"},
        ])
        run = next(item for item in document["nodes"] if item.get("kind") == "run")
        run_resolved = _style(document, run["resolvedStyleId"])
        self.assertEqual(run_resolved["resolved"]["weight"], 700)
        self.assertEqual(_provenance(run_resolved)["weight"], "style-docx-Mid")
        self.assertEqual(_provenance(run_resolved)["foreground"], "theme-accent2")

    def test_docx_missing_parent_is_ambiguous_and_diagnosed(self) -> None:
        document = self._convert("docx", _docx_parts(missing_parent=True))
        self.assertEqual(document["conversion"]["status"], "partial")
        self.assertIn("DFIR-DOCX-STYLE-PARENT-MISSING", {item["code"] for item in document["diagnostics"]})
        child = _style(document, "style-docx-resolved-Child")
        placeholder = _style(document, "style-docx-MissingParent")
        self.assertEqual(child["status"], "ambiguous")
        self.assertEqual(placeholder["status"], "unavailable")
        self.assertIn("style-docx-MissingParent", child["resolvedFrom"])

    def test_xlsx_cellxf_cascade_conditional_cell_overlay_theme_tint_and_alpha(self) -> None:
        document = self._convert("xlsx", _xlsx_parts())
        base = _style(document, "style-xlsx-cell-1")
        self.assertEqual(base["resolved"]["fill"], {"kind": "solid", "color": {"kind": "rgb", "r": 169, "g": 209, "b": 142, "a": 1}})
        self.assertEqual(base["resolved"]["foreground"]["a"], "0.5019607843137254901960784314")
        self.assertEqual(_provenance(base)["fill"], "xlsx-cellXfs-1-fill")
        self.assertEqual([step["property"] for step in base["cascadeTrace"]], ["numberFormat", "fontFamily", "fontSize", "weight", "foreground", "fill", "paragraphAlignment"])
        self.assertEqual(_provenance(base).get("numberFormat"), "xlsx-cellStyleXfs-AccentValue")
        cells = {item["nodeId"]: item for item in document["nodes"] if item.get("kind") == "cell"}
        b1 = next(item for item in cells.values() if item["nodeId"].endswith("-B1"))
        b2 = next(item for item in cells.values() if item["nodeId"].endswith("-B2"))
        self.assertEqual(b1["resolvedStyleId"], "style-xlsx-cell-1")
        self.assertNotEqual(b2["resolvedStyleId"], "style-xlsx-cell-1")
        conditional = _style(document, b2["resolvedStyleId"])
        self.assertEqual(conditional["origin"], "conditional")
        self.assertEqual(conditional["resolved"]["fill"], {"kind": "solid", "color": {"kind": "rgb", "r": 255, "g": 255, "b": 0, "a": 1}})
        self.assertEqual(_provenance(conditional)["fill"], "xlsx-dxf-0-priority-1")
        self.assertEqual(next(step for step in conditional["cascadeTrace"] if step["property"] == "fill"), {"property": "fill", "source": "xlsx-dxf-0-priority-1", "action": "conditional"})
        self.assertTrue(any(item.get("styleId") == "xlsx-dxf-0-priority-1" for item in document["styles"]))
        self.assertTrue(any(
            item.get("targetId") == conditional["styleId"]
            and item.get("locator", {}).get("cell") == "B2"
            for item in document["sourceMaps"]
        ))

    def test_provenance_mutation_is_detected_and_missing_count_is_zero(self) -> None:
        document = self._convert("xlsx", _xlsx_parts())
        style_ids = {item["styleId"] for item in document["styles"]}
        for style in document["styles"]:
            if style.get("resolved"):
                self.assertTrue(_provenance_is_coherent(style, style_ids), style["styleId"])
        missing = sum(
            1
            for style in document["styles"]
            for property_name in style.get("resolved", {})
            if property_name not in _provenance(style)
        )
        self.assertEqual(missing, 0)
        mutated = deepcopy(_style(document, "style-xlsx-cell-1"))
        mutated["propertyProvenance"][0]["source"] = "missing-provenance-source"
        self.assertFalse(_provenance_is_coherent(mutated, style_ids))
        self.assertTrue(re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:/-]{0,127}", mutated["propertyProvenance"][0]["source"]))


if __name__ == "__main__":
    unittest.main()
