// Free-text notes linked to the graph, hybrid search, and the memory editor.

// name: add_note
MATCH (u:User {id: $uid})
CREATE (n:Note {id: $id, text: $text, ts: $now, trust: $trust, archived: false})
CREATE (u)-[:WROTE]->(n)
WITH n
OPTIONAL MATCH (src:Event {id: $source_event})
FOREACH (_ IN CASE WHEN src IS NULL THEN [] ELSE [1] END | CREATE (n)-[:DERIVED_FROM]->(src))
RETURN n.id AS id

// name: note_about_person
MATCH (n:Note {id: $note_id}), (p:Person {id: $person_id})
MERGE (n)-[:ABOUT_ENTITY]->(p)

// name: note_about_place
MATCH (n:Note {id: $note_id}), (pl:Place {id: $place_id})
MERGE (n)-[:ABOUT_ENTITY]->(pl)

// name: notes_about_person
MATCH (n:Note)-[:ABOUT_ENTITY]->(p:Person)
WHERE coalesce(n.archived, false) = false
  AND (toLower(p.name) CONTAINS toLower($person)
       OR any(al IN coalesce(p.aliases, []) WHERE toLower(al) = toLower($person)))
RETURN n.id AS id, n.text AS text, n.ts AS ts, p.name AS person
ORDER BY ts DESC
LIMIT 5

// name: list_notes
MATCH (n:Note)
WHERE coalesce(n.archived, false) = false
OPTIONAL MATCH (n)-[:ABOUT_ENTITY]->(x)
RETURN n.id AS id, n.text AS text, n.ts AS ts, n.trust AS trust, collect(x.name) AS about
ORDER BY ts DESC
LIMIT 50

// name: search_notes_text
MATCH (n:Note)
WHERE coalesce(n.archived, false) = false AND toLower(n.text) CONTAINS toLower($q)
OPTIONAL MATCH (n)-[:ABOUT_ENTITY]->(x)
RETURN n.id AS id, n.text AS text, n.ts AS ts, collect(x.name) AS about
LIMIT 5

// name: search_events_fulltext
CALL db.idx.fulltext.queryNodes('Event', $q) YIELD node
WHERE coalesce(node.archived, false) = false AND node.id <> $exclude
RETURN node.id AS id, node.type AS type, node.text AS text, node.ts AS ts, node.trust AS trust
ORDER BY ts DESC
LIMIT 8

// name: search_events_contains
MATCH (e:Event)
WHERE coalesce(e.archived, false) = false AND e.id <> $exclude AND toLower(e.text) CONTAINS toLower($q)
RETURN e.id AS id, e.type AS type, e.text AS text, e.ts AS ts, e.trust AS trust
ORDER BY ts DESC
LIMIT 8

// name: vector_events
CALL db.idx.vector.queryNodes('Event', 'embedding', 8, vecf32($embedding)) YIELD node, score
WHERE coalesce(node.archived, false) = false AND node.id <> $exclude
RETURN node.id AS id, node.type AS type, node.text AS text, node.ts AS ts, node.trust AS trust, score
ORDER BY score DESC

// name: archive_fact
MATCH (f:Fact {id: $id})
SET f.archived = true, f.archived_at = $now
REMOVE f.embedding
RETURN f.id AS id

// name: archive_note
MATCH (n:Note {id: $id})
SET n.archived = true, n.archived_at = $now
RETURN n.id AS id

// name: list_people_full
MATCH (p:Person)
OPTIONAL MATCH (u:User {id: $uid})-[r:RELATION]->(p)
OPTIONAL MATCH (p)-[:SENT]->(m:Message)
RETURN p.id AS id, p.name AS name, p.aliases AS aliases, r.type AS relation, count(m) AS messages
ORDER BY messages DESC, name
LIMIT 100

// name: update_person
MATCH (p:Person {id: $id}), (u:User {id: $uid})
SET p.name = coalesce($name, p.name), p.aliases = coalesce($aliases, p.aliases)
MERGE (u)-[r:RELATION]->(p)
SET r.type = coalesce($relation, r.type)
RETURN p.id AS id

// name: merge_person_out
MATCH (old:Person {id: $old})-[r:{rel}]->(x), (keep:Person {id: $keep})
WHERE x <> keep
MERGE (keep)-[:{rel}]->(x)
DELETE r

// name: merge_person_in
MATCH (x)-[r:{rel}]->(old:Person {id: $old}), (keep:Person {id: $keep})
WHERE x <> keep
MERGE (x)-[:{rel}]->(keep)
DELETE r

// name: merge_person_finish
MATCH (old:Person {id: $old}), (keep:Person {id: $keep})
OPTIONAL MATCH (u:User {id: $uid})-[ro:RELATION]->(old)
OPTIONAL MATCH (u)-[rk:RELATION]->(keep)
FOREACH (_ IN CASE WHEN ro IS NOT NULL AND rk IS NULL THEN [1] ELSE [] END |
  MERGE (u)-[nr:RELATION]->(keep) SET nr.type = ro.type)
SET keep.aliases = coalesce(keep.aliases, []) + [old.name] + coalesce(old.aliases, [])
WITH old, keep
DETACH DELETE old
RETURN keep.id AS id, keep.aliases AS aliases
