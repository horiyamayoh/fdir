# Format mapping

FDIR maps DOCX, XLSX, PDF, and Markdown source constructs into the common
Document Form IR. Each adapter preserves source facts, records normalized
values separately, and emits a diagnostic when a construct is unavailable,
ambiguous, approximated, unsupported, or rejected by a resource limit.

## Shared mapping rules

| source construct | common IR mapping | product rule |
| --- | --- | --- |
| document/package | `Document`, `Part`, and root `Node` | retain source format and profile |
| paragraph, run, or text | `Node` and `Text` | keep authored and normalized representations |
| table, row, or cell | `TableGrid` and typed `Node` | retain coordinates and merge relationships |
| style and inheritance | `Style` with authored/inherited/resolved views | do not replace authored values with renderer output |
| relationship or hyperlink | typed `Relation` | preserve source occurrence identity and target mode |
| geometry or layout | `Geometry`, `Layout`, `CoordinateSpace` | use explicit units and status |
| annotation, comment, or revision | `Annotation` | retain authoring metadata when present |
| unsupported construct | `Extension` or partial node | emit a diagnostic and keep the disposition visible |

## DOCX

DOCX package parts map to typed document, section, paragraph, table, style,
numbering, annotation, relationship, drawing, and resource entities. DrawingML
geometry and anchors use common units; unsupported compatibility markup remains
an extension or diagnostic rather than being silently discarded.

## XLSX

XLSX workbooks map worksheets, sparse cells, formulas, styles, merges, names,
tables, validation rules, comments, hyperlinks, drawings, and external links.
Raw, stored, cached, computed, and displayed formula values remain distinct.
The adapter does not claim recalculation when a calculation engine is absent.

## PDF

PDF objects, pages, fonts, glyphs, annotations, actions, resources, and page
geometry map to parts, nodes, relations, and observations. Renderer and OCR
results are observations linked to source text or geometry; they never replace
source-decoded content.

## Markdown

Markdown lines, blocks, inline tokens, links, reference definitions, footnotes,
raw HTML, and authoring delimiters map to source maps, nodes, text, relations,
and format extensions. A malformed source line remains addressable through its
source map and diagnostic.

## Expected statuses

`complete` and `complete-with-warnings` indicate that conversion completed;
`partial` retains a documented unsupported or ambiguous construct; `failed`
indicates that the input or configured resource limit prevented conversion.
The status and diagnostics are part of the product contract and are asserted by
the standard unittest discovery command.
