
-- Rebuildable SQLite projection query: assertions and their occurrence evidence.
SELECT
  json_extract(a.json, '$.assertionId') AS assertion_id,
  json_extract(a.json, '$.unitId') AS unit_id,
  json_extract(o.value, '$') AS occurrence_id
FROM assertions AS a,
     json_each(a.json, '$.occurrenceIds') AS o
ORDER BY assertion_id, occurrence_id;
