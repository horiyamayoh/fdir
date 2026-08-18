
.assertions
| map(select(.status == "accepted"))
| sort_by(.unitId, .predicate, .assertionId)
| map({assertionId, unitId, predicate, value, occurrenceIds})
