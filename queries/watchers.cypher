// Watchers (Q4 family): "tell me when..." requests stored as graph patterns.
// One small matcher per condition_type, all run for every new event.

// name: create_watcher
CREATE (w:Watcher)
SET w += $props
WITH w
OPTIONAL MATCH (src:Event {id: $utterance_id})
FOREACH (_ IN CASE WHEN src IS NULL THEN [] ELSE [1] END | CREATE (w)-[:CREATED_BY]->(src))
WITH w
OPTIONAL MATCH (d:Device {id: $notify_device})
FOREACH (_ IN CASE WHEN d IS NULL THEN [] ELSE [1] END | CREATE (w)-[:NOTIFY_VIA]->(d))
RETURN w.id AS id

// name: watch_target
MATCH (w:Watcher {id: $watcher_id}), (t:{label} {id: $target_id})
MERGE (w)-[:WATCHES]->(t)

// name: match_entity_state
MATCH (e:Event {id: $event_id})-[:UPDATES|ABOUT]->(target)<-[:WATCHES]-(w:Watcher)
WHERE w.status = 'active' AND w.condition_type = 'entity_state' AND e.status = w.target_status
CREATE (w)-[:FIRED_BY {ts: e.ts}]->(e)
SET w.status = CASE w.mode WHEN 'once' THEN 'done' ELSE 'active' END, w.last_fired = e.ts
WITH w, e
OPTIONAL MATCH (w)-[:NOTIFY_VIA]->(d:Device)
RETURN w.id AS watcher_id, w.description AS description, d.id AS notify_device, e.text AS event_text

// name: match_event_type
MATCH (e:Event {id: $event_id})
MATCH (w:Watcher)
WHERE w.status = 'active' AND w.condition_type = 'event_type' AND w.event_type = e.type
CREATE (w)-[:FIRED_BY {ts: e.ts}]->(e)
SET w.status = CASE w.mode WHEN 'once' THEN 'done' ELSE 'active' END, w.last_fired = e.ts
WITH w, e
OPTIONAL MATCH (w)-[:NOTIFY_VIA]->(d:Device)
RETURN w.id AS watcher_id, w.description AS description, d.id AS notify_device, e.text AS event_text

// name: match_trip_eta
MATCH (e:Event {id: $event_id})-[:ABOUT]->(t:Trip)
MATCH (w:Watcher)
WHERE w.status = 'active' AND w.condition_type = 'trip_eta'
  AND (w.provider IS NULL OR toLower(t.provider) = toLower(w.provider))
  AND ((t.eta_min IS NOT NULL AND t.eta_min <= w.eta_threshold) OR t.status = 'arrived')
MERGE (w)-[:WATCHES]->(t)
CREATE (w)-[:FIRED_BY {ts: e.ts}]->(e)
SET w.status = CASE w.mode WHEN 'once' THEN 'done' ELSE 'active' END, w.last_fired = e.ts
WITH w, e
OPTIONAL MATCH (w)-[:NOTIFY_VIA]->(d:Device)
RETURN w.id AS watcher_id, w.description AS description, d.id AS notify_device, e.text AS event_text

// name: match_app_text
MATCH (e:Event {id: $event_id})-[:FROM_APP]->(a:App)
MATCH (w:Watcher)
WHERE w.status = 'active' AND w.condition_type = 'app_text'
  AND toLower(a.name) = toLower(w.app)
  AND (w.keyword IS NULL OR toLower(e.text) CONTAINS toLower(w.keyword))
CREATE (w)-[:FIRED_BY {ts: e.ts}]->(e)
SET w.status = CASE w.mode WHEN 'once' THEN 'done' ELSE 'active' END, w.last_fired = e.ts
WITH w, e
OPTIONAL MATCH (w)-[:NOTIFY_VIA]->(d:Device)
RETURN w.id AS watcher_id, w.description AS description, d.id AS notify_device, e.text AS event_text

// name: match_place
MATCH (e:Event {id: $event_id})
WHERE e.type IN ['place.entered', 'place.left']
MATCH (w:Watcher)
WHERE w.status = 'active' AND w.condition_type = 'place'
  AND toLower(w.place) = toLower(e.place) AND e.type = 'place.' + w.transition
CREATE (w)-[:FIRED_BY {ts: e.ts}]->(e)
SET w.status = CASE w.mode WHEN 'once' THEN 'done' ELSE 'active' END, w.last_fired = e.ts
WITH w, e
OPTIONAL MATCH (w)-[:NOTIFY_VIA]->(d:Device)
RETURN w.id AS watcher_id, w.description AS description, d.id AS notify_device,
       coalesce(w.message, e.text) AS event_text

// name: due_time_watchers
MATCH (w:Watcher)
WHERE w.status = 'active' AND w.condition_type = 'time' AND w.due_ts <= $now
SET w.status = 'done', w.last_fired = $now
WITH w
OPTIONAL MATCH (w)-[:NOTIFY_VIA]->(d:Device)
RETURN w.id AS watcher_id, w.description AS description, d.id AS notify_device, w.message AS event_text

// name: semantic_watchers
MATCH (w:Watcher {status: 'active'})
WHERE w.condition_type = 'semantic'
OPTIONAL MATCH (w)-[:NOTIFY_VIA]->(d:Device)
RETURN w.id AS id, w.description AS description, d.id AS notify_device, w.created_at AS created_at
ORDER BY created_at DESC
LIMIT 20

// name: fire_watcher
MATCH (w:Watcher {id: $watcher_id}), (e:Event {id: $event_id})
WHERE w.status = 'active'
CREATE (w)-[:FIRED_BY {ts: e.ts, reason: $reason}]->(e)
SET w.status = CASE w.mode WHEN 'once' THEN 'done' ELSE 'active' END, w.last_fired = e.ts
WITH w, e
OPTIONAL MATCH (w)-[:NOTIFY_VIA]->(d:Device)
RETURN w.id AS watcher_id, w.description AS description, d.id AS notify_device, e.text AS event_text

// name: list_active_watchers
MATCH (w:Watcher {status: 'active'})
OPTIONAL MATCH (w)-[:NOTIFY_VIA]->(d:Device)
RETURN w.id AS id, w.description AS description, w.condition_type AS condition_type, d.id AS notify_device, w.created_at AS created_at
ORDER BY w.created_at DESC

// name: cancel_watcher
MATCH (w:Watcher {id: $id})
WHERE w.status = 'active'
SET w.status = 'cancelled', w.cancelled_at = $now
RETURN w.id AS id

// name: cancel_all_watchers
MATCH (w:Watcher {status: 'active'})
SET w.status = 'cancelled', w.cancelled_at = $now
RETURN count(w) AS n
