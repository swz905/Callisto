// Shared family graph (separate FalkorDB graph, e.g. kairos:family).

// name: family_share
MERGE (m:Member {id: $member})
SET m.name = $name
CREATE (f:SharedFact {id: $id, statement: $statement, key: $key, ts: $now})
CREATE (m)-[:SHARED]->(f)
WITH f
OPTIONAL MATCH (old:SharedFact)
WHERE $key IS NOT NULL AND old.key = $key AND old.id <> f.id AND old.valid_to IS NULL
FOREACH (_ IN CASE WHEN old IS NULL THEN [] ELSE [1] END |
  SET old.valid_to = $now
  CREATE (f)-[:SUPERSEDES]->(old))
RETURN f.id AS id

// name: family_search
MATCH (m:Member)-[:SHARED]->(f:SharedFact)
WHERE f.valid_to IS NULL AND ($q = '' OR toLower(f.statement) CONTAINS toLower($q))
RETURN f.id AS id, f.statement AS statement, f.ts AS ts, m.name AS member
ORDER BY ts DESC
LIMIT 10
