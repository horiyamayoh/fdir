
-- Rebuildable SQLite projection query: accepted text assertions.
SELECT
  json_extract(json, '$.unitId') AS unit_id,
  json_extract(json, '$.value') AS recorded_text
FROM assertions
WHERE json_extract(json, '$.predicate') = 'text'
  AND json_extract(json, '$.status') = 'accepted'
ORDER BY unit_id;
