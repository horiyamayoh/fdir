"""Focused regressions for the bounded Markdown adapter (#102)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

try:
    from tools.adapter_markdown import convert
    from tools.ir_validation import validate_document
except ModuleNotFoundError:  # direct ``python tools/test_adapter_markdown.py`` execution
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from adapter_markdown import convert
    from ir_validation import validate_document


ROOT = Path(__file__).resolve().parents[1]


class MarkdownAdapterRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fdir-markdown-regression-")
        self.addCleanup(self._tmp.cleanup)

    def _convert(self, name: str, source: bytes, *, profile: str | None = None) -> dict:
        path = Path(self._tmp.name) / name
        path.write_bytes(source)
        document = convert(path, profile=profile)
        validate_document(document)
        return document

    @staticmethod
    def _maps(document: dict, target_id: str) -> list[dict]:
        return [item["locator"] for item in document.get("sourceMaps", []) if item.get("targetId") == target_id]

    def test_gfm_separator_is_metadata_not_a_data_row(self) -> None:
        document = self._convert(
            "table-separator.md",
            "| Name | Value |\r\n| --- | ---: |\r\n| answer | 42 |\r\n".encode("utf-8"),
        )
        table = document["tables"][0]
        self.assertEqual(len(table["rowIds"]), 2)
        row_lines = [self._maps(document, row_id)[-1]["lineStart"] for row_id in table["rowIds"]]
        self.assertEqual(row_lines, [1, 3])
        table_authoring = next(item for item in document["extensions"] if item["type"] == "table-authoring")
        self.assertEqual(table_authoring["payload"]["separatorLine"], 2)
        self.assertEqual(table_authoring["payload"]["dataRowLines"], [3])

    def test_multiline_span_preserves_crlf_unicode_tab_and_byte_offsets(self) -> None:
        source = "α\tfirst\r\nsecond e\u0301\n".encode("utf-8")
        document = self._convert("multiline-span.md", source)
        paragraph = next(item for item in document["nodes"] if item["kind"] == "paragraph")
        locator = max(self._maps(document, paragraph["nodeId"]), key=lambda item: item.get("byteEnd", -1))
        content = "α\tfirst\r\nsecond e\u0301"
        self.assertEqual(locator["lineStart"], 1)
        self.assertEqual(locator["lineEnd"], 2)
        self.assertEqual(locator["columnEnd"], len("second e\u0301") + 1)
        self.assertEqual(locator["byteStart"], 0)
        self.assertEqual(locator["byteEnd"], len(content.encode("utf-8")))
        self.assertEqual(locator["codePointEnd"], len(content))
        self.assertEqual(locator["coordinateUnit"], "unicode-code-point")
        self.assertTrue(locator["endExclusive"])
        self.assertEqual(locator["lineEnding"], "CRLF")
        self.assertEqual(locator["tokenEnd"], len(content))

    def test_mixed_line_endings_keep_unicode_byte_and_code_point_spans_exact(self) -> None:
        crlf = chr(13) + chr(10)
        lf = chr(10)
        first = chr(0xFF8E) + chr(0xFF71) + "\tfirst"
        second = "second e" + chr(0x301)
        source = (first + crlf + second + lf + "third" + crlf).encode("utf-8")
        document = self._convert("mixed-span.md", source)
        paragraph = next(item for item in document["nodes"] if item["kind"] == "paragraph")
        locator = max(self._maps(document, paragraph["nodeId"]), key=lambda item: item.get("byteEnd", -1))
        content = first + crlf + second + lf + "third"
        self.assertEqual(locator["lineEnd"], 3)
        self.assertEqual(locator["columnEnd"], len("third") + 1)
        self.assertEqual(locator["byteEnd"], len(content.encode("utf-8")))
        self.assertEqual(locator["codePointEnd"], len(content))
        self.assertEqual(locator["lineEnding"], "mixed")
        self.assertTrue(locator["endExclusive"])

    def test_reference_definition_and_use_share_resource_and_close_relations(self) -> None:
        document = self._convert(
            "reference-closure.md",
            b"[ref][] and [ref]\n\n[ref]: https://example.invalid/a (Title)\n",
        )
        definition = next(item for item in document["annotations"] if item["kind"] == "bookmark")
        uses = [item for item in document["annotations"] if item["kind"] == "hyperlink"]
        self.assertEqual(len(uses), 2)
        self.assertTrue(all(item["status"] == "preserved" for item in uses))
        self.assertEqual({item["body"] for item in uses}, {"https://example.invalid/a"})
        self.assertEqual(len(document["resources"]), 1)
        reference_relations = [item for item in document["relations"] if item["kind"] == "references"]
        self.assertEqual(len(reference_relations), 2)
        self.assertEqual({item["toId"] for item in reference_relations}, {definition["annotationId"]})
        self.assertEqual(len(document["observations"]), 1)
        self.assertEqual(document["observations"][0]["status"], "unavailable")
        facts = [item["payload"] for item in document["extensions"] if item["type"] == "authoring-facts"]
        self.assertEqual(sum(1 for item in facts if item.get("referenceDefinitionId") == definition["annotationId"]), 2)

    def test_issue96_markdown_closure_preserves_projection_facts(self) -> None:
        document = self._convert(
            "issue96-markdown-closure.md",
            (
                "# Link closure\n\n"
                "External [outer **link**](https://example.invalid/doc) and [reference][ref].\n"
                "![local image](missing.png)\n"
                "![external image](https://example.invalid/image.png)\n"
                "Autolink <https://example.invalid/auto>\n"
                "[^note]\n\n"
                "[ref]: local.md \"Local\"\n"
                "[^note]: Footnote body\n"
            ).encode("utf-8"),
        )

        relations = {
            item["sourceOccurrenceId"]: item
            for item in document["relations"]
            if item.get("sourceOccurrenceId")
        }
        self.assertEqual(
            {
                source_id: (
                    relation["type"],
                    relation["targetMode"],
                    relation["status"],
                )
                for source_id, relation in relations.items()
            },
            {
                "md-inline-external": ("inline-link", "external", "unavailable"),
                "md-reference-link": ("reference-link", "internal", "unavailable"),
                "md-local-image": ("image", "internal", "unavailable"),
                "md-external-image": ("image", "external", "unavailable"),
                "md-autolink": ("autolink", "external", "unavailable"),
            },
        )
        relationship_owner = next(item for item in document["parts"] if item.get("name") == "markdown-run")
        self.assertTrue(all(item["relationId"] in relationship_owner["relationshipIds"] for item in relations.values()))

        resources = {item["derivedHandle"]: item for item in document["resources"]}
        self.assertEqual(resources["local.md"]["mediaType"], "application/octet-stream")
        self.assertEqual(resources["local.md"]["embeddedOrExternal"], "linked")
        self.assertEqual(resources["https://example.invalid/doc"]["networkAvailability"], "unknown")
        self.assertTrue(all(item["packagePresence"] is False and item["rawPayloadAvailable"] is False for item in resources.values()))

        footnote = next(item for item in document["annotations"] if item.get("kind") == "footnote" and item.get("targetIds", [])[0] != "node-markdown-document")
        self.assertEqual(footnote["sourceSubtype"], "markdown:footnote")
        self.assertEqual(footnote["anchor"], {"kind": "reference", "label": "note", "resolved": True})

        link = next(item for item in document["annotations"] if item.get("destination") == "https://example.invalid/doc")
        self.assertEqual(link["displayText"], "outer **link**")

    def test_front_matter_requires_scalar_entry_and_thematic_break_stays_a_break(self) -> None:
        front = self._convert("front-matter.md", b"---\r\ntitle: Example\r\n---\r\nBody\r\n")
        front_payload = next(item["payload"] for item in front["extensions"] if item["type"] == "front-matter")
        self.assertEqual(front_payload["status"], "preserved")
        self.assertFalse(any(item["kind"] == "thematicBreak" for item in front["nodes"]))

        thematic = self._convert("thematic-break.md", b"---\r\nBody\r\n")
        self.assertTrue(any(item["kind"] == "thematicBreak" for item in thematic["nodes"]))
        self.assertFalse(any(item["type"] == "front-matter" for item in thematic["extensions"]))

    def test_setext_heading_preserves_marker_and_multiline_span(self) -> None:
        document = self._convert("setext-heading.md", "Title\r\n---\r\n".encode("utf-8"))
        heading = next(item for item in document["nodes"] if item["kind"] == "heading")
        locator = max(self._maps(document, heading["nodeId"]), key=lambda item: item.get("lineEnd", -1))
        self.assertEqual(locator["lineStart"], 1)
        self.assertEqual(locator["lineEnd"], 2)
        self.assertEqual(locator["lineEnding"], "CRLF")
        authoring = next(item["payload"] for item in document["extensions"] if item["type"] == "heading-authoring")
        self.assertEqual(authoring, {"level": 2, "marker": "-"})
        self.assertFalse(any(item["type"] == "front-matter" for item in document["extensions"]))

    def test_disabled_syntax_is_partial_with_diagnostic(self) -> None:
        document = self._convert("unsupported-syntax.md", b"~~strike~~\n\n    indented code\n")
        self.assertEqual(document["conversion"]["status"], "partial")
        codes = {item["code"] for item in document["diagnostics"]}
        self.assertIn("DFIR-MD-STRIKETHROUGH-UNSUPPORTED", codes)
        self.assertIn("DFIR-MD-INDENTED-CODE-UNSUPPORTED", codes)
        self.assertTrue(any(item["status"] == "unsupported" for item in document["nodes"]))

    def test_gfm_profile_preserves_task_state_and_strikethrough(self) -> None:
        document = self._convert(
            "gfm-profile.md",
            b"~~strike~~\n- [x] task\n",
            profile="gfm-0.29",
        )
        self.assertFalse(any(item["code"] == "DFIR-MD-STRIKETHROUGH-UNSUPPORTED" for item in document["diagnostics"]))
        self.assertFalse(any(item["code"] == "DFIR-MD-TASK-LIST-UNSUPPORTED" for item in document["diagnostics"]))
        self.assertIn(
            {"feature": "strikethrough", "status": "preserved"},
            [{key: item[key] for key in ("feature", "status")} for item in document["conversion"]["features"] if item["feature"] == "strikethrough"],
        )
        task = next(item for item in document["annotations"] if item.get("sourceSubtype") == "markdown:task-list-item")
        self.assertEqual(task["anchor"], {"kind": "checkbox", "checked": True, "marker": "[x]"})
        self.assertEqual(task["body"], "checked")
        normalized = [item["value"] for item in document["texts"] if item["representation"] == "normalized"]
        self.assertIn("strike", normalized)

    def test_bounded_gfm_table_profile_preserves_tables(self) -> None:
        document = self._convert(
            "gfm-table-extension.md",
            b"| Name \\| key | Value |\n| :--- | ---: |\n| `a|b` | 1 |\n",
            profile="gfm-table-extension",
        )
        self.assertEqual(len(document["tables"]), 1)
        self.assertEqual(len(document["tables"][0]["rowIds"]), 2)
        self.assertEqual(document["conversion"]["status"], "complete")

    def test_commonmark_core_does_not_complete_gfm_or_footnote_extensions(self) -> None:
        document = self._convert(
            "commonmark-core-boundary.md",
            b"~~strike~~\n- [ ] task\n[^1]: note\n",
            profile="commonmark-0.31.2-core",
        )
        self.assertEqual(document["conversion"]["status"], "partial")
        codes = {item["code"] for item in document["diagnostics"]}
        self.assertTrue({"DFIR-MD-STRIKETHROUGH-UNSUPPORTED", "DFIR-MD-TASK-LIST-UNSUPPORTED", "DFIR-MD-FOOTNOTE-UNSUPPORTED"} <= codes)
        self.assertFalse(any(item.get("sourceSubtype") == "markdown:task-list-item" for item in document["annotations"]))

    def test_atx_and_directive_node_spans_include_authored_delimiters(self) -> None:
        heading = self._convert("atx-span.md", b"# Heading\n")
        heading_node = next(item for item in heading["nodes"] if item["kind"] == "heading")
        heading_map = self._maps(heading, heading_node["nodeId"])[0]
        self.assertEqual((heading_map["columnStart"], heading_map["columnEnd"], heading_map["byteStart"], heading_map["byteEnd"]), (1, 10, 0, 9))

        directive = self._convert("directive-span.md", b":::note\nbody\n:::\n")
        paragraph = next(item for item in directive["nodes"] if item["kind"] == "paragraph")
        directive_map = self._maps(directive, paragraph["nodeId"])[0]
        self.assertEqual((directive_map["lineStart"], directive_map["lineEnd"], directive_map["columnStart"]), (1, 3, 1))
        self.assertEqual(next(item for item in directive["extensions"] if item["type"] == "unsupported-directive")["payload"]["opening"], ":::note")

    def test_unsafe_resource_is_preserved_without_execution(self) -> None:
        document = self._convert("unsafe-resource.md", b"[unsafe](javascript:alert(1))\n")
        resource = document["resources"][0]
        self.assertEqual(resource["derivedHandle"], "javascript:alert(1)")
        self.assertEqual(resource["availability"], "unavailable")
        self.assertEqual(document["observations"][0]["status"], "unavailable")
        self.assertIn("DFIR-MD-UNSAFE-URI-PRESERVED", {item["code"] for item in document["diagnostics"]})

    def test_gfm_header_separator_mismatch_is_not_a_table(self) -> None:
        document = self._convert("invalid-gfm-table.md", b"| a | b |\n| --- |\n", profile="gfm-0.29")
        self.assertFalse(document["tables"])
        root = next(item for item in document["nodes"] if item["kind"] == "document")
        by_id = {item["nodeId"]: item for item in document["nodes"]}
        self.assertEqual([by_id[item_id]["kind"] for item_id in root["childIds"]], ["paragraph"])

    def test_profile_declares_exact_bounded_scope(self) -> None:
        profile = json.loads((ROOT / "machine" / "capability-profile.json").read_text(encoding="utf-8"))
        markdown = next(item for item in profile["profiles"] if item["format"] == "markdown")
        dialect = markdown["dialect"]
        self.assertEqual(dialect["baseSpecVersion"], "0.31.2")
        self.assertEqual(dialect["name"], "fdir-commonmark-bounded")
        self.assertEqual(dialect["gfm"]["tables"], "enabled")
        self.assertEqual(dialect["gfm"]["strikethrough"], "disabled")
        self.assertEqual(dialect["sourceSpan"]["end"], "exclusive")


if __name__ == "__main__":
    unittest.main()
