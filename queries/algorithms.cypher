// Graph algorithms used as agent tools.

// name: pagerank_people
CALL algo.pageRank(NULL, NULL) YIELD node, score
WITH node, score
WHERE node:Person
RETURN node.id AS id, node.name AS name, score
ORDER BY score DESC
LIMIT 50

// name: person_interactions
MATCH (p:Person)
OPTIONAL MATCH (p)-[:SENT]->(m:Message)
WHERE m.ts >= $since
WITH p, count(m) AS msgs, max(m.ts) AS last_ts
OPTIONAL MATCH (e:Event)-[:MENTIONS]->(p)
WHERE e.ts >= $since
WITH p, msgs, last_ts, count(e) AS mentions
OPTIONAL MATCH (t:Task)-[:WITH]->(p)
WHERE t.status = 'open'
WITH p, msgs, last_ts, mentions, count(t) AS open_tasks
OPTIONAL MATCH (mt:Meeting)-[:WITH]->(p)
WHERE mt.start_ts >= $now
WITH p, msgs, last_ts, mentions, open_tasks, count(mt) AS meetings
OPTIONAL MATCH (:User {id: $uid})-[r:RELATION]->(p)
RETURN p.id AS id, p.name AS name, r.type AS relation, msgs, mentions, open_tasks, meetings, last_ts
