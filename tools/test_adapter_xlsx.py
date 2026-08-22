"""Independent authored-package regression tests for the bounded XLSX lane."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

try:
    from convert_document import convert_path
    from adapter_xlsx import _xlsx_condition_matches
except ImportError:  # pragma: no cover
    from tools.convert_document import convert_path
    from tools.adapter_xlsx import _xlsx_condition_matches


def _write_package(path: Path, parts: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, value in parts.items():
            package.writestr(name, value)


def _parts(*, formula: str = '<f>1+1</f>') -> dict[str, str]:
    return {
        "[Content_Types].xml": """
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
  <Override PartName="/xl/tables/table1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/>
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
  <Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rIdShared" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>
""",
        "xl/worksheets/_rels/sheet1.xml.rels": """
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdTable" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table" Target="../tables/table1.xml"/>
</Relationships>
""",
        "xl/workbook.xml": """
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Data" sheetId="1" r:id="rIdSheet"/></sheets>
</workbook>
""",
        "xl/worksheets/sheet1.xml": f"""
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="B1:C3"/>
  <sheetData>
    <row r="1"><c r="B1" t="s"><v>0</v></c><c r="C1" t="inlineStr"><is><t>Inline</t></is></c></row>
    <row r="2"><c r="B2" s="1"><v>45292</v></c><c r="C2" s="2"><v>12.5</v></c></row>
    <row r="3"><c r="B3">{formula}</c><c r="C3" t="s"><v>99</v></c></row>
  </sheetData>
</worksheet>
""",
        "xl/sharedStrings.xml": """
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="1" uniqueCount="1"><si><t>Shared</t></si></sst>
""",
        "xl/styles.xml": """
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="0.00"/></numFmts>
  <fonts count="1"><font><sz val="11"/><name val="Aptos"/></font></fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="thin"><color rgb="FF0000FF"/></left><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="3">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="14" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyNumberFormat="1"/>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyNumberFormat="1"/>
  </cellXfs>
</styleSheet>
""",
        "xl/tables/table1.xml": """
<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" id="1" name="DataTable" displayName="DataTable" ref="B1:C2" headerRowCount="1">
  <autoFilter ref="B1:C2"/>
  <tableColumns count="2"><tableColumn id="1" name="Shared"/><tableColumn id="2" name="Number"/></tableColumns>
</table>
""",
    }


def _cell(document: dict, address: str) -> dict:
    return next(item for item in document["nodes"] if item.get("kind") == "cell" and item.get("address", {}).get("row") == int(address[1:]) and item.get("address", {}).get("column") == ord(address[0]) - 64)


def _text(document: dict, cell: dict, representation: str) -> dict:
    return next(item for item in document["texts"] if item["textId"] in cell.get("textIds", []) and item["representation"] == representation)


class AdapterXlsxFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fdir-xlsx-regression-")
        self.addCleanup(self._tmp.cleanup)

    def _convert(self, name: str, parts: dict[str, str]) -> dict:
        path = Path(self._tmp.name) / f"adapter-xlsx-{name}.xlsx"
        try:
            _write_package(path, parts)
            document, evidence = convert_path(path, "xlsx")
            self.assertEqual(evidence["outcome"], "success", document.get("diagnostics"))
            return document
        finally:
            path.unlink(missing_ok=True)

    def test_shared_inline_and_invalid_shared_values_are_distinct_and_fail_closed(self) -> None:
        document = self._convert("value-lanes", _parts())
        shared = _cell(document, "B1")
        inline = _cell(document, "C1")
        invalid = _cell(document, "C3")
        self.assertEqual(shared["value"], {"type": "string", "value": "Shared", "status": "preserved", "sourceRepresentation": "shared-string-index"})
        self.assertEqual(inline["value"], {"type": "string", "value": "Inline", "status": "preserved", "sourceRepresentation": "inline-string"})
        self.assertEqual(invalid["value"]["status"], "unavailable")
        self.assertIsNone(invalid["value"]["value"])
        source_text = _text(document, invalid, "source")
        self.assertEqual(source_text["representation"], "source")
        self.assertEqual(source_text["provenance"], "authored")
        self.assertEqual(source_text["value"], "99")
        self.assertEqual(source_text["status"], "unavailable")
        self.assertIn("DFIR-XLSX-SHARED-STRING-MISSING", {item["code"] for item in document["diagnostics"]})

    def test_date_numeric_display_and_style_border_are_exact(self) -> None:
        document = self._convert("date-display-style", _parts())
        date_cell = _cell(document, "B2")
        number_cell = _cell(document, "C2")
        self.assertEqual(date_cell["value"], {"type": "date", "value": "2024-01-01", "status": "preserved", "sourceRepresentation": "worksheet-number"})
        self.assertEqual(_text(document, date_cell, "displayed")["value"], "1/1/24")
        self.assertEqual(_text(document, number_cell, "displayed")["value"], "12.50")
        style = next(item for item in document["styles"] if item.get("styleId") == "style-xlsx-cell-1")
        self.assertEqual(style["resolved"]["borders"]["left"]["dash"], "thin")
        self.assertEqual(style["resolved"]["borders"]["left"]["color"], {"kind": "rgb", "r": 0, "g": 0, "b": 255, "a": 1})

    def test_formula_without_cache_and_unknown_formula_shape_remain_unavailable(self) -> None:
        document = self._convert("formula-lanes", _parts(formula='<f t="futureType" ref="C3:B2">1+1</f>'))
        formula = next(item for item in document["formulas"] if item["ownerAddress"] == "B3")
        self.assertEqual(formula["values"]["raw"]["sourceRepresentation"], "formula-expression")
        self.assertEqual(formula["values"]["stored"]["status"], "unavailable")
        self.assertEqual(formula["values"]["cached"]["status"], "unavailable")
        self.assertEqual(formula["values"]["computed"]["status"], "unavailable")
        self.assertEqual(formula["values"]["displayed"], {"text": "", "status": "unavailable"})
        self.assertEqual(formula["status"], "ambiguous")
        self.assertNotIn("formulaType", formula)
        self.assertFalse(formula["range"]["balanced"])
        codes = {item["code"] for item in document["diagnostics"]}
        self.assertIn("DFIR-XLSX-FORMULA-TYPE-UNSUPPORTED", codes)
        self.assertIn("DFIR-XLSX-FORMULA-RANGE-INVALID", codes)

    def test_non_a_table_columns_and_relationship_occurrence_are_preserved(self) -> None:
        document = self._convert("grid-relationship", _parts())
        structured = next(item for item in document["tables"] if item.get("scope") == "structured-table")
        self.assertEqual(structured["range"], "B1:C2")
        self.assertEqual(structured["columnIds"], ["node-xlsx-column-0-2", "node-xlsx-column-0-3"])
        relation = next(item for item in document["relations"] if item.get("sourceRelationshipId") == "rIdTable")
        self.assertEqual(relation["sourceOccurrenceId"], "xlsx-sheet-table")
        self.assertEqual(relation["type"], "http://schemas.openxmlformats.org/officeDocument/2006/relationships/table")
        table_part = next(item for item in document["parts"] if item.get("name") == "xl/tables/table1.xml")
        self.assertEqual(table_part["contentType"], "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml")

    def test_unsupported_package_occurrence_targets_its_part(self) -> None:
        parts = _parts()
        parts["xl/calcChain.xml"] = '<calcChain xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
        document = self._convert("unsupported-occurrence", parts)
        part = next(item for item in document["parts"] if item.get("name") == "xl/calcChain.xml")
        self.assertEqual(part["status"], "unsupported")
        diagnostic = next(item for item in document["diagnostics"] if item.get("code") == "DFIR-XLSX-FEATURE-UNSUPPORTED" and "calcChain.xml" in item.get("message", ""))
        self.assertEqual(diagnostic["targetId"], part["partId"])

    def test_empty_or_missing_conditional_operator_is_schema_valid_and_defaults_for_cell_is(self) -> None:
        for suffix, operator_attribute in (("missing", ""), ("empty", ' operator=""')):
            parts = _parts()
            worksheet = parts["xl/worksheets/sheet1.xml"]
            conditional = f'<conditionalFormatting sqref="B2"><cfRule type="expression" priority="1"{operator_attribute}><formula>B2&gt;0</formula></cfRule></conditionalFormatting>'
            parts["xl/worksheets/sheet1.xml"] = worksheet.replace("</worksheet>", f"{conditional}</worksheet>")
            path = Path(self._tmp.name) / f"adapter-xlsx-conditional-operator-{suffix}.xlsx"
            try:
                _write_package(path, parts)
                document, evidence = convert_path(path, "xlsx")
                self.assertEqual(evidence["outcome"], "success", document.get("diagnostics"))
                extension = next(item for item in document["extensions"] if item.get("type") == "conditional-formatting")
                self.assertIsNone(extension["payload"]["rules"][0]["operator"])
            finally:
                path.unlink(missing_ok=True)
        rule = ET.fromstring('<cfRule xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" type="cellIs" operator=""><formula>1</formula></cfRule>')
        self.assertTrue(_xlsx_condition_matches(rule, "1"))

    def test_conditional_format_without_formula_is_schema_valid(self) -> None:
        parts = _parts()
        worksheet = parts["xl/worksheets/sheet1.xml"]
        conditional = '<conditionalFormatting sqref="B2"><cfRule type="colorScale" priority="1"><colorScale><cfvo type="min"/></colorScale></cfRule></conditionalFormatting>'
        parts["xl/worksheets/sheet1.xml"] = worksheet.replace("</worksheet>", f"{conditional}</worksheet>")
        path = Path(self._tmp.name) / "adapter-xlsx-conditional-no-formula.xlsx"
        try:
            _write_package(path, parts)
            document, evidence = convert_path(path, "xlsx")
            self.assertEqual(evidence["outcome"], "success", document.get("diagnostics"))
            extension = next(item for item in document["extensions"] if item.get("type") == "conditional-formatting")
            self.assertEqual(extension["payload"]["rules"][0]["formula"], [])
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
