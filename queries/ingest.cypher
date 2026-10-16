// Episodic ingestion (Q1 family). Events are immutable; mutable state lives on entities
// (Download, Trip) and every change is recorded as an Event that UPDATES / is ABOUT that entity.
// `{label}` placeholders are filled from a fixed allowlist in Python, never from user input.

// name: event_by_hash
MATCH (e:Event {hash: $hash})
RETURN e.id AS id
LIMIT 1

// name: create_event
MERGE (d:Device {id: $device_id})
WITH d
CREATE (e:Event:{label})
SET e += $props
CREATE (e)-[:ON_DEVICE]->(d)
WITH e, d
OPTIONAL MATCH (prev:Event)
WHERE prev.id = d.last_event_id
FOREACH (_ IN CASE WHEN prev IS NULL THEN [] ELSE [1] END | CREATE (prev)-[:NEXT]->(e))
SET d.last_event_id = e.id, d.last_seen = e.ts
RETURN e.id AS id

// name: link_app
MATCH (e:Event {id: $event_id})
MERGE (a:App {name: $app})
ON CREATE SET a.category = $category
CREATE (e)-[:FROM_APP]->(a)

// name: resolve_person
MATCH (p:Person)
WHERE toLower(p.name) = toLower($name)
   OR any(al IN coalesce(p.aliases, []) WHERE toLower(al) = toLower($name))
RETURN p.id AS id, p.name AS name
LIMIT 1

// name: create_person
MERGE (p:Person {name: $name})
ON CREATE SET p.id = $id, p.aliases = []
RETURN p.id AS id, p.name AS name

// name: link_mentions_person
MATCH (e:Event {id: $event_id}), (p:Person {id: $person_id})
MERGE (e)-[:MENTIONS]->(p)

// name: create_message
MATCH (e:Event {id: $event_id}), (p:Person {id: $person_id}), (u:User {id: $uid})
CREATE (m:Message {id: $id, channel: $channel, subject: $subject, snippet: $snippet, ts: $ts, archived: false})
CREATE (p)-[:SENT]->(m), (m)-[:RECEIVED]->(u), (e)-[:ABOUT]->(m)
WITH m
UNWIND $links AS link
MERGE (l:Link {url: link.url})
ON CREATE SET l.domain = link.domain
CREATE (m)-[:CONTAINS]->(l)

// name: active_trip
MATCH (t:Trip)
WHERE toLower(t.provider) = toLower($provider) AND t.status <> 'completed' AND t.ts >= $since
RETURN t.id AS id
ORDER BY t.ts DESC
LIMIT 1

// name: upsert_trip
MATCH (e:Event {id: $event_id})
MERGE (t:Trip {id: $trip_id})
ON CREATE SET t.provider = $provider, t.ts = $ts, t.weekday = $weekday, t.hour = $hour
SET t.status = $status,
    t.eta_min = $eta_min,
    t.driver = coalesce($driver, t.driver),
    t.updated = $ts
CREATE (e)-[:ABOUT]->(t)

// name: link_trip_destination
MATCH (t:Trip {id: $trip_id})
MERGE (pl:Place {name: $place})
ON CREATE SET pl.id = $place_id, pl.kind = 'other'
MERGE (t)-[:TO]->(pl)

// name: create_download_request
MERGE (dl:Download {id: $download_id})
ON CREATE SET dl.url = $url, dl.filename = $filename, dl.status = 'requested', dl.requested_ts = $ts
WITH dl
OPTIONAL MATCH (l:Link {url: $url})
FOREACH (_ IN CASE WHEN l IS NULL THEN [] ELSE [1] END | MERGE (l)-[:DOWNLOADED_AS]->(dl))
RETURN dl.id AS id

// name: upsert_download
MATCH (e:Event {id: $event_id})
MERGE (dl:Download {id: $download_id})
ON CREATE SET dl.started_ts = $ts
SET dl.status = $status,
    dl.filename = coalesce($filename, dl.filename),
    dl.url = coalesce($url, dl.url),
    dl.bytes = coalesce($bytes, dl.bytes)
CREATE (e)-[:UPDATES]->(dl)
WITH dl
WHERE dl.url IS NOT NULL
MERGE (l:Link {url: dl.url})
ON CREATE SET l.domain = $domain
MERGE (l)-[:DOWNLOADED_AS]->(dl)

// name: download_result_file
MATCH (dl:Download {id: $download_id})
MERGE (f:File {path: $path})
SET f.name = $name, f.ext = $ext, f.size = $size
MERGE (fo:Folder {path: $folder})
MERGE (f)-[:SAVED_IN]->(fo)
MERGE (dl)-[:RESULTED_IN]->(f)

// name: latest_open_download
MATCH (dl:Download)
WHERE dl.status IN ['requested', 'in_progress'] AND coalesce(dl.started_ts, dl.requested_ts) >= $since
RETURN dl.id AS id, dl.filename AS filename
ORDER BY coalesce(dl.started_ts, dl.requested_ts) DESC
LIMIT 1

// name: link_file
MATCH (e:Event {id: $event_id})
MERGE (f:File {path: $path})
ON CREATE SET f.name = $name, f.ext = $ext
CREATE (e)-[:MENTIONS]->(f)

// name: link_url
MATCH (e:Event {id: $event_id})
MERGE (l:Link {url: $url})
ON CREATE SET l.domain = $domain
SET l.title = coalesce($title, l.title)
CREATE (e)-[:MENTIONS]->(l)

// name: link_session
MATCH (e:Event {id: $event_id})
MERGE (s:Session {id: $session_id})
ON CREATE SET s.started = $ts, s.device = $device_id
SET s.last = $ts
CREATE (e)-[:IN_SESSION]->(s)

// name: set_embedding
MATCH (n:{label} {id: $id})
SET n.embedding = vecf32($embedding)
