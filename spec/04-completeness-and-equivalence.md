
# 4. Completeness and equivalence

Completeness is a vector of `GuaranteeStatus` records. A snapshot can be complete for recorded text while partial for geometry and unsupported for executable embedded objects. No reducer may turn partial, unsupported, unresolved, cancelled, or failed into an unqualified success boolean.

Equivalence is profile-scoped. An `EquivalenceCertificate` identifies both snapshots, the profile, coverage statuses, evidence digest, and outcome. `equivalent` is allowed only when every required coverage status is complete. Insufficient coverage produces `indeterminate`, never `equivalent`.
