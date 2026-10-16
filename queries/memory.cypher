// Semantic + procedural memory (Q5/Q6 family). Facts are versioned, never overwritten.

// name: assert_fact
MATCH (u:User {id: $uid})
OPTIONAL MATCH (u)-[:ASSERTS]->(old:Fact {key: $key})
WHERE old.valid_to IS NULL
CREATE (new:Fact {id: $id, key: $key, statement: $statement, valid_from: $now,
                  confidence: $conf, strength: 1.0, archived: false})
CREATE (u)-[:ASSERTS]->(new)
FOREACH (_ IN CASE WHEN old IS NULL THEN [] ELSE [1] END |
  SET old.valid_to = $now
  CREATE (new)-[:SUPERSEDES]->(old))
WITH new
OPTIONAL MATCH (src:Event {id: $source_event})
FOREACH (_ IN CASE WHEN src IS NULL THEN [] ELSE [1] END | CREATE (new)-[:DERIVED_FROM]->(src))
RETURN new.id AS id

// name: current_fact
MATCH (u:User {id: $uid})-[:ASSERTS]->(f:Fact {key: $key})
WHERE f.valid_to IS NULL AND coalesce(f.archived, false) = false
RETURN f.id AS id, f.statement AS statement, f.valid_from AS valid_from

// name: fact_history
MATCH (u:User {id: $uid})-[:ASSERTS]->(f:Fact {key: $key})
WHERE coalesce(f.archived, false) = false
RETURN f.statement AS statement, f.valid_from AS valid_from, f.valid_to AS valid_to
ORDER BY valid_from DESC

// name: search_facts_text
MATCH (u:User {id: $uid})-[:ASSERTS]->(f:Fact)
WHERE f.valid_to IS NULL AND coalesce(f.archived, false) = false
  AND toLower(f.statement) CONTAINS toLower($q)
RETURN f.id AS id, f.key AS key, f.statement AS statement
LIMIT 5

// name: forget_topic
MATCH (n)
WHERE (n:Fact OR n:Message OR n:Event)
  AND coalesce(n.archived, false) = false
  AND toLower(coalesce(n.statement, n.subject, n.text, '')) CONTAINS toLower($topic)
SET n.archived = true, n.archived_at = $now
REMOVE n.embedding
RETURN count(n) AS n

// name: detect_trip_habits
MATCH (t:Trip)-[:TO]->(pl:Place)
WHERE t.ts >= $since
WITH pl, CASE WHEN t.weekday < 5 THEN 'weekdays' ELSE 'weekends' END AS days, t.hour AS hr, collect(t) AS trips
WHERE size(trips) >= $min_count
MERGE (h:Habit {id: 'trip_to:' + pl.name + ':' + days + ':' + toString(hr)})
ON CREATE SET h.kind = 'trip', h.place = pl.name, h.days = days, h.hour = hr, h.provider = trips[0].provider,
              h.strength = CASE WHEN 0.3 + 0.04 * size(trips) > 0.9 THEN 0.9 ELSE 0.3 + 0.04 * size(trips) END,
              h.archived = false,
              h.description = 'trip to ' + pl.name + ' on ' + days + ' around ' + toString(hr) + ':00'
ON MATCH SET h.strength = h.strength + 0.1 * (1 - h.strength)
SET h.count = size(trips), h.last_reinforced = $now, h.last_decayed = $now, h.archived = false
WITH h, trips
UNWIND trips AS t
MERGE (h)-[:REINFORCED_BY]->(t)
RETURN DISTINCT h.id AS id, h.strength AS strength, h.count AS count

// name: decay_habits
MATCH (h:Habit)
WHERE coalesce(h.archived, false) = false
WITH h, ($now - coalesce(h.last_decayed, h.last_reinforced)) / 86400000.0 AS days
SET h.strength = h.strength * exp(-1.0 * $lambda * days), h.last_decayed = $now
WITH h
WHERE h.strength < $threshold
SET h.archived = true
RETURN count(h) AS archived

// name: list_habits
MATCH (h:Habit)
WHERE coalesce(h.archived, false) = false AND h.strength >= $min_strength
RETURN h.id AS id, h.kind AS kind, h.place AS place, h.days AS days, h.hour AS hour, h.provider AS provider,
       h.strength AS strength, h.count AS count, h.last_suggested AS last_suggested
ORDER BY h.strength DESC
LIMIT 5

// name: mark_habit_suggested
MATCH (h:Habit {id: $id})
SET h.last_suggested = $now
