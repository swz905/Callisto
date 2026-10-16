// Recall (Q2/Q3 family): multi-hop questions answered by walking the graph.

// name: recall_file_from_person
MATCH (p:Person)-[:SENT]->(m:Message)-[:CONTAINS]->(l:Link)-[:DOWNLOADED_AS]->(dl:Download)-[:RESULTED_IN]->(f:File)
WHERE (toLower(p.name) CONTAINS toLower($person)
       OR any(al IN coalesce(p.aliases, []) WHERE toLower(al) = toLower($person)))
  AND ($ext IS NULL OR f.ext = $ext)
  AND m.ts >= $since
  AND coalesce(m.archived, false) = false
OPTIONAL MATCH (dl)<-[:UPDATES]-(:Event)-[:ON_DEVICE]->(dev:Device)
RETURN f.path AS path, f.name AS name, m.subject AS subject, m.ts AS sent_at, p.name AS person,
       l.url AS url, dl.id AS download_id, collect(DISTINCT dev.id)[0] AS device,
       [p.name, m.subject, l.url, dl.filename, f.path] AS evidence_path
ORDER BY sent_at DESC
LIMIT 3

// name: recall_links_from_person
MATCH (p:Person)-[:SENT]->(m:Message)-[:CONTAINS]->(l:Link)
WHERE (toLower(p.name) CONTAINS toLower($person)
       OR any(al IN coalesce(p.aliases, []) WHERE toLower(al) = toLower($person)))
  AND m.ts >= $since
  AND coalesce(m.archived, false) = false
OPTIONAL MATCH (l)-[:DOWNLOADED_AS]->(dl:Download)
RETURN l.url AS url, m.subject AS subject, m.ts AS sent_at, p.name AS person, m.channel AS channel,
       collect(dl.status) AS download_statuses
ORDER BY sent_at DESC
LIMIT 5

// name: recall_messages_from_person
MATCH (p:Person)-[:SENT]->(m:Message)
WHERE (toLower(p.name) CONTAINS toLower($person)
       OR any(al IN coalesce(p.aliases, []) WHERE toLower(al) = toLower($person)))
  AND m.ts >= $since
  AND coalesce(m.archived, false) = false
OPTIONAL MATCH (m)-[:CONTAINS]->(l:Link)
RETURN m.subject AS subject, m.snippet AS snippet, m.channel AS channel, m.ts AS sent_at, p.name AS person,
       collect(l.url) AS urls
ORDER BY sent_at DESC
LIMIT 5

// name: find_file_by_name
MATCH (f:File)
WHERE toLower(f.name) CONTAINS toLower($q)
OPTIONAL MATCH (f)<-[:RESULTED_IN]-(:Download)<-[:UPDATES]-(:Event)-[:ON_DEVICE]->(dev:Device)
RETURN f.path AS path, f.name AS name, collect(DISTINCT dev.id)[0] AS device
LIMIT 5

// name: latest_link
MATCH (m:Message)-[:CONTAINS]->(l:Link)
WHERE ($channel IS NULL OR m.channel = $channel) AND coalesce(m.archived, false) = false
OPTIONAL MATCH (p:Person)-[:SENT]->(m)
RETURN l.url AS url, m.channel AS channel, p.name AS person, m.ts AS ts
ORDER BY ts DESC
LIMIT 1

// name: person_context
MATCH (p:Person)
WHERE toLower(p.name) CONTAINS toLower($person)
   OR any(al IN coalesce(p.aliases, []) WHERE toLower(al) = toLower($person))
OPTIONAL MATCH (u:User {id: $uid})-[r:RELATION]->(p)
OPTIONAL MATCH (p)-[:SENT]->(m:Message)
WITH p, r, m
ORDER BY m.ts DESC
RETURN p.name AS name, r.type AS relation, collect(m.subject)[..3] AS recent_subjects,
       count(m) AS messages, max(m.ts) AS last_ts
LIMIT 1

// name: recent_activity
MATCH (e:Event)-[:ON_DEVICE]->(d:Device)
WHERE e.ts >= $since AND e.ts < $until AND e.type <> 'utterance' AND coalesce(e.archived, false) = false
RETURN e.type AS type, e.text AS text, e.ts AS ts, d.id AS device
ORDER BY ts DESC
LIMIT 25

// name: recent_browser_visits
MATCH (e:Event:BrowserVisit)-[:MENTIONS]->(l:Link)
WHERE e.ts >= $since AND e.ts < $until AND coalesce(e.archived, false) = false
  AND ($q = '' OR toLower(coalesce(l.title, '') + ' ' + l.url) CONTAINS toLower($q))
RETURN l.url AS url, l.title AS title, e.ts AS ts, e.source AS browser
ORDER BY ts DESC
LIMIT 10

// name: list_places
MATCH (pl:Place)
RETURN pl.id AS id, pl.name AS name, pl.kind AS kind, pl.lat IS NOT NULL AS has_geo
ORDER BY name

// name: event_counts_since
MATCH (e:Event)
WHERE e.ts >= $since AND e.type <> 'utterance'
RETURN e.type AS type, count(e) AS n

// name: find_path
MATCH (a)
WHERE (a:Person OR a:Place OR a:File OR a:App) AND toLower(coalesce(a.name, '')) CONTAINS toLower($a)
MATCH (b)
WHERE (b:Person OR b:Place OR b:File OR b:App) AND toLower(coalesce(b.name, '')) CONTAINS toLower($b) AND a <> b
WITH a, b
LIMIT 1
MATCH p = (a)-[*1..4]-(b)
RETURN [n IN nodes(p) | coalesce(n.name, n.subject, n.title, n.text, n.description, n.url)] AS names,
       [r IN relationships(p) | type(r)] AS rels
ORDER BY length(p)
LIMIT 1

// name: vector_facts
CALL db.idx.vector.queryNodes('Fact', 'embedding', 8, vecf32($embedding)) YIELD node AS f, score
WHERE f.valid_to IS NULL AND coalesce(f.archived, false) = false
OPTIONAL MATCH (f)-[:ABOUT_ENTITY]->(x)
RETURN f.statement AS statement, f.key AS key, score, collect(DISTINCT coalesce(x.name, x.title)) AS about
ORDER BY score DESC
