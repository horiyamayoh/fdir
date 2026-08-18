
# 5. Canonicalization, identity, and lineage

Canonical JSON uses UTF-8, lexicographically sorted object keys, no insignificant whitespace, finite JSON numbers, and unchanged array order. Content digests are computed over these bytes. Identity construction must be acyclic and must not incorporate rebuildable projections.

Unit identity, cross-format equivalence, and cross-revision continuity are separate claims. A `LineageCertificate` may connect predecessor and successor units without declaring them identical or cross-format equivalent.
