## Scope

- Product behavior:
- Affected formats or commands:
- Changed paths:

## Boundary review

- [ ] No business meaning or semantic equivalence was added to the IR.
- [ ] Core fields remain typed and closed.
- [ ] Source, normalized, stored, cached, computed, displayed, rendered, and observed facts remain distinct.
- [ ] Unsupported, partial, ambiguous, unavailable, and failed outcomes remain explicit.
- [ ] Query indexes and renderer outputs remain non-authoritative.

## Verification

Command:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Result and known limitation:
