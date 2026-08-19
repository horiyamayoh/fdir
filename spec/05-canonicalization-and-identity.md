# 5. Canonicalization, identity, and lineage

## 5.1 Current authority

FDIR 2.1 identity material is canonical JSON encoded as UTF-8. Canonical CBOR is not an FDIR 2.1 identity authority. Another encoding may be used as a transport only when it is explicitly non-authoritative, or may become authoritative through a separately versioned normative change.

The canonicalization contract is identified as `fdir-canonical-json/1`. The pinned Python implementation in `tools/canonical_json.py`, the Rust implementation in `fdir-canonical`, and `fixtures/canonical/vector.json` are required to agree byte for byte.

## 5.2 Canonical JSON rules

A supported value is serialized with these rules:

1. Objects have string keys only. Keys are ordered lexicographically by their unchanged Unicode scalar-value sequence. Duplicate keys are rejected before canonicalization.
2. Strings are emitted as UTF-8 without Unicode normalization. Quotation mark, reverse solidus, and control characters use JSON escapes; non-ASCII text is not converted to `\u` escapes.
3. Arrays retain their source order.
4. `null`, `true`, and `false` use their lowercase JSON spellings.
5. No insignificant whitespace, byte-order mark, trailing data, locale-dependent text, or platform-dependent line ending is emitted.

Precomposed and decomposed Unicode strings are therefore distinct identity material. Filesystem order, hash-map iteration order, locale, timezone, and operational clocks cannot affect canonical output.

## 5.3 Numeric domain and spelling

The cross-language numeric domain is intentionally narrower than every value accepted by every host language:

- Integers are exact decimal values from `-9223372036854775808` through `18446744073709551615`, inclusive. Leading zeroes are invalid JSON. Integer `-0` normalizes to `0`.
- Floating values are finite IEEE 754 binary64 values. NaN, positive or negative infinity, overflow, and non-zero values that underflow to zero are rejected.
- Floating spelling follows the pinned Python JSON authority: shortest round-tripping decimal digits, lowercase `e`, an explicit exponent sign, and at least two exponent digits. Scientific notation is used when the base-10 exponent is below `-4` or at least `16`; otherwise fixed notation is used. An integral floating value in fixed notation retains `.0`.
- Floating negative zero is preserved as `-0.0`; positive floating zero is `0.0`.
- Lexical variants such as `1.2500E+03` may be accepted as input but normalize to the single canonical spelling `1250.0`.

A value outside this domain is rejected with a stable diagnostic rather than rounded, saturated, or silently converted to a different semantic value.

## 5.4 Content digests

The FDIR 2.1 content digest is the lowercase algorithm-qualified string

```text
sha256:<64 lowercase hexadecimal digits>
```

where the SHA-256 input is exactly the canonical JSON byte sequence. This plain content digest is retained for compatibility with the frozen 2.1 vectors. It is distinct from an entity identity digest.

## 5.5 Domain-separated entity identity

Every entity identity uses the preimage

```text
FDIR-ID NUL fdir-identity/1 NUL <identity-kind> NUL <canonical-envelope-bytes>
```

and the resulting SHA-256 value is written with the same `sha256:` prefix. The NUL separators and the identity kind prevent equal canonical material in different identity domains from colliding by construction.

The canonical envelope has schema `fdir/identity-material/1` and contains exactly:

- `kind`: the identity domain;
- `material`: stable semantic or evidentiary material;
- `references`: dependency identities represented by semantic role, target kind, and target digest;
- `schema`: the envelope schema identifier.

Repository-local node keys are resolution handles and are not included in identity material.

## 5.6 Acyclic identity DAG

Identity references must point strictly from a later construction layer to an earlier layer:

| Layer | Identity kinds |
| --- | --- |
| 0 | artifact, selector, information-unit |
| 1 | carrier, referenced-object |
| 2 | occurrence |
| 3 | evidence |
| 4 | record-assertion |
| 5 | snapshot |

The implementation detects graph cycles before enforcing layer order so that cycle diagnostics are reproducible. Missing targets, duplicate edges, cycles, and forward or same-layer identity references are rejected. Reference arrays are sorted by semantic role, target kind, and target digest before canonicalization.

A change to any stable node material changes that node digest and every dependent digest. A change to the identity kind also changes the digest even when the envelope material is otherwise equal.

## 5.7 Excluded material

The following are excluded from identity unless a future normative version explicitly promotes a field into stable material:

- rebuildable projections and indexes;
- transport envelopes and compression choices;
- worker receipts, process identifiers, retry counters, and scheduling state;
- operational timestamps, elapsed durations, cache state, and telemetry;
- repository-local keys used only to resolve the in-memory DAG.

An implementation may retain such metadata beside an identity node, but it must not serialize it into the identity envelope. Tests must demonstrate that changing excluded metadata leaves identities unchanged.

Unit identity, cross-format equivalence, and cross-revision continuity remain separate claims. A `LineageCertificate` may connect predecessor and successor units without declaring them identical or cross-format equivalent.

## 5.8 Diagnostics and independent reproduction

Canonicalization and identity failures expose stable codes and a path or node key. Published vectors cover positive, negative, boundary, collision-domain, and determinism cases. Any implementation can reproduce a content digest by canonicalizing the vector value and applying SHA-256, and can reproduce an identity digest by assembling the documented preimage without product-private code.
