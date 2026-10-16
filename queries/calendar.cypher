// Meetings from ICS calendars, and the graph walk behind meeting prep packs.

// name: upsert_meeting
MATCH (u:User {id: $uid})
MERGE (m:Meeting {id: $id})
SET m.title = $title, m.start_ts = $start, m.end_ts = $end, m.location = $location,
    m.description = $description, m.updated = $now
MERGE (u)-[:ATTENDS]->(m)

// name: link_meeting_person
MATCH (m:Meeting {id: $meeting_id}), (p:Person {id: $person_id})
MERGE (m)-[:WITH]->(p)

// name: link_meeting_place
MATCH (m:Meeting {id: $meeting_id})
MERGE (pl:Place {name: $place})
ON CREATE SET pl.id = $place_id, pl.kind = 'meeting'
MERGE (m)-[:AT]->(pl)

// name: upcoming_meetings
MATCH (u:User {id: $uid})-[:ATTENDS]->(m:Meeting)
WHERE m.end_ts >= $since AND m.start_ts <= $until
OPTIONAL MATCH (m)-[:WITH]->(p:Person)
RETURN m.id AS id, m.title AS title, m.start_ts AS start_ts, m.end_ts AS end_ts, m.location AS location,
       m.prepped AS prepped, collect(p.name) AS people
ORDER BY start_ts
LIMIT 20

// name: mark_meeting_prepped
MATCH (m:Meeting {id: $id})
SET m.prepped = $now

// name: meeting_context
MATCH (m:Meeting {id: $meeting_id})-[:WITH]->(p:Person)
OPTIONAL MATCH (p)-[:SENT]->(msg:Message)
WHERE msg.ts >= $since AND coalesce(msg.archived, false) = false
OPTIONAL MATCH (msg)-[:CONTAINS]->(:Link)-[:DOWNLOADED_AS]->(:Download)-[:RESULTED_IN]->(f:File)
OPTIONAL MATCH (t:Task)-[:WITH]->(p)
WHERE t.status = 'open'
RETURN p.name AS person, collect(DISTINCT msg.subject)[..3] AS subjects, collect(DISTINCT f.name)[..3] AS files,
       collect(DISTINCT t.title)[..3] AS tasks
