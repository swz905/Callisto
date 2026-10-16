// Read models for the web dashboard and the LLM context card.

// name: recent_events
MATCH (e:Event)
OPTIONAL MATCH (e)-[:ON_DEVICE]->(d:Device)
RETURN e.id AS id, e.type AS type, e.text AS text, e.ts AS ts, d.id AS device,
       e.importance AS importance, e.triage_reason AS triage_reason, e.triage_by AS triage_by
ORDER BY ts DESC
LIMIT 40

// name: recent_actions
MATCH (a:Action)
RETURN a.id AS id, a.tool AS tool, a.status AS status, a.summary AS summary, a.ts AS ts,
       a.device_id AS device, a.result AS result, a.tainted AS tainted, a.taint_sources AS taint_sources
ORDER BY ts DESC
LIMIT 40

// name: get_action
MATCH (a:Action {id: $id})
RETURN a.id AS id, a.tool AS tool, a.args_json AS args_json, a.summary AS summary,
       a.device_id AS device_id, a.status AS status, a.origin_device AS origin_device

// name: list_people
MATCH (u:User {id: $uid})-[r:RELATION]->(p:Person)
RETURN p.name AS name, r.type AS relation
ORDER BY name

// name: current_facts
MATCH (u:User {id: $uid})-[:ASSERTS]->(f:Fact)
WHERE f.valid_to IS NULL AND coalesce(f.archived, false) = false
RETURN f.key AS key, f.statement AS statement, f.valid_from AS valid_from
ORDER BY key
LIMIT 20

// name: all_facts
MATCH (u:User {id: $uid})-[:ASSERTS]->(f:Fact)
WHERE coalesce(f.archived, false) = false
RETURN f.id AS id, f.key AS key, f.statement AS statement, f.valid_from AS valid_from, f.valid_to AS valid_to
ORDER BY key, valid_from DESC

// name: graph_snapshot
MATCH (n)-[r]->(m)
WHERE type(r) <> 'NEXT'
  AND (NOT n:Event OR n.ts >= $since)
  AND (NOT n:Action OR n.ts >= $since)
  AND (NOT m:Event OR m.ts >= $since)
RETURN id(n) AS src, labels(n) AS src_labels,
       coalesce(n.name, n.description, n.statement, n.subject, n.filename, n.title, n.text, n.url, n.summary, n.id) AS src_name,
       id(m) AS dst, labels(m) AS dst_labels,
       coalesce(m.name, m.description, m.statement, m.subject, m.filename, m.title, m.text, m.url, m.summary, m.id) AS dst_name,
       type(r) AS rel
LIMIT 600

// name: graph_counts
MATCH (n)
RETURN labels(n)[0] AS label, count(n) AS n
