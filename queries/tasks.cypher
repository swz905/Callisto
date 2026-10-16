// Commitments / open loops.

// name: create_task
MATCH (u:User {id: $uid})
CREATE (t:Task {id: $id, title: $title, status: 'open', direction: $direction, due_ts: $due_ts,
                created_at: $now, source: $source, trust: $trust})
CREATE (u)-[:HAS_TASK]->(t)
WITH t
OPTIONAL MATCH (p:Person {id: $person_id})
FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END | CREATE (t)-[:WITH]->(p))
WITH t
OPTIONAL MATCH (e:Event {id: $event_id})
FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END | CREATE (t)-[:DERIVED_FROM]->(e))
RETURN t.id AS id

// name: open_tasks
MATCH (u:User {id: $uid})-[:HAS_TASK]->(t:Task)
WHERE t.status = 'open'
OPTIONAL MATCH (t)-[:WITH]->(p:Person)
WITH t, p
WHERE $person IS NULL
   OR (p IS NOT NULL AND (toLower(p.name) CONTAINS toLower($person)
       OR any(al IN coalesce(p.aliases, []) WHERE toLower(al) = toLower($person))))
RETURN t.id AS id, t.title AS title, t.direction AS direction, t.due_ts AS due_ts, t.created_at AS created_at,
       t.source AS source, t.trust AS trust, p.name AS person
ORDER BY coalesce(due_ts, 9000000000000) ASC, created_at DESC
LIMIT 25

// name: complete_task
MATCH (t:Task {id: $id})
WHERE t.status = 'open'
SET t.status = 'done', t.done_at = $now
RETURN t.id AS id

// name: due_tasks
MATCH (u:User {id: $uid})-[:HAS_TASK]->(t:Task)
WHERE t.status = 'open' AND t.due_ts IS NOT NULL AND t.due_ts <= $until AND t.reminded_at IS NULL
OPTIONAL MATCH (t)-[:WITH]->(p:Person)
SET t.reminded_at = $now
RETURN t.id AS id, t.title AS title, t.direction AS direction, t.due_ts AS due_ts, p.name AS person
