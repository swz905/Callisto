// Explainability (Q9): walk an Action back to the events, watchers, habits and utterances that caused it.

// name: explain_action
MATCH (a:Action {id: $action_id})
OPTIONAL MATCH p = (a)-[:CAUSED_BY|FIRED_BY|CREATED_BY|INFLUENCED_BY*1..3]->(n)
RETURN a.tool AS tool, a.summary AS summary, a.ts AS ts, a.status AS status,
       a.tainted AS tainted, a.taint_sources AS taint_sources,
       labels(n) AS labels, coalesce(n.description, n.text, n.title, n.statement, n.id) AS text,
       coalesce(n.ts, n.created_at, n.valid_from, n.last_reinforced) AS node_ts, length(p) AS hops, [r IN relationships(p) | type(r)] AS rels
ORDER BY hops
