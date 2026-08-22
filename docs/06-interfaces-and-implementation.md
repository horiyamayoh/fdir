# Interfaces and implementation boundaries

This chapter describes the product interfaces implemented by FDIR. The local
unit, acceptance, E2E, and regression test command is the development and
release decision point.

## Typed query surface

The query layer works on the validated Document Form IR and exposes these
operations:

```text
list_nodes(document_id, kind?, part_id?, status?) -> Node[]
get_text(node_id, representation: source|normalized|displayed) -> Text
get_styles(node_id, stage: authored|inherited|theme|direct|resolved) -> StyleView
find_relations(kind, from_id?, to_id?) -> Relation[]
find_extensions(namespace?, type?, target_id?) -> Extension[]
find_observation_differences(target_id?, observation_kind?) -> Difference[]
```

The index is rebuildable from the IR. Tests compare direct collection access
with the rebuilt index so a query result never becomes a second authority.

## Module boundaries

| module | responsibility | should not own |
| --- | --- | --- |
| `form-schema` | JSON Schema and generated contracts | parser behavior |
| `form-core` | typed entities, IDs, status, invariants | raw package bytes |
| `form-canonical` | canonical JSON and digest | semantic comparison |
| `adapter-contract` | inspect/convert protocol and capabilities | format-specific parsing |
| `adapter-docx` | DOCX parsing and mapping | business meaning |
| `adapter-xlsx` | XLSX parsing and mapping | recalculation authority |
| `adapter-pdf` | PDF parsing and mapping | OCR inference as source fact |
| `adapter-markdown` | Markdown parsing and mapping | business meaning |
| `observation-worker` | renderer/OCR observations | source fact mutation |
| `form-validation` | schema, invariant, and compatibility checks | workflow state |
| `form-index` | rebuildable query projection | canonical authority mutation |
| `form-cli` | `inspect`, `convert`, and `validate` entry points | vendor-specific bypass |
| `test-support` | fixtures and malformed-input harnesses | hidden source-byte oracle |

## Parser boundary

Adapters preserve source-declared facts and identify library-derived
normalization. Parser failures become `unsupported`, `partial`, or `failed`
conversion results with diagnostics; parser output alone is not promoted to
semantic business meaning.

## Renderer and OCR boundary

Renderer and OCR output is represented as `Observation` data linked to the
source page, surface, or node. Engine, method, settings, resource, status,
and precision remain explicit so an observation cannot silently replace source
text or geometry.

## CLI boundary

The public entry point is `tools/convert_document.py`:

```text
python tools/convert_document.py inspect <input> [--format <kind>] [--profile <id>]
python tools/convert_document.py convert <input> --out <document-form.json> [--format <kind>] [--profile <id>] [--evidence <execution.json>]
python tools/convert_document.py validate <document-form.json>
```

The optional `--evidence` file is product conversion metadata. It reports the
input consumed and the conversion outcome and is not a development completion
record.

`tools/run_e2e.py --all` runs transient four-format cases through this public
boundary, including malformed, unsupported, and resource-limit inputs. It is
also invoked by the standard unittest discovery command and leaves no generated
files in the repository.
