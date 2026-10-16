// Permission graph (Q8), audit trail and device routing (Q7).

// name: check_permission
MATCH (d:Device {id: $device_id})-[a:ALLOWED]->(c:Capability {name: $capability})
RETURN a.mode AS mode, c.risk AS risk, a.trusted AS trusted

// name: trust_permission
MATCH (d:Device {id: $device_id})-[a:ALLOWED]->(c:Capability {name: $capability})
SET a.mode = 'auto', a.trusted = true, a.trusted_at = $now
RETURN a.mode AS mode

// name: create_action
CREATE (a:Action)
SET a += $props
WITH a
OPTIONAL MATCH (c:Capability {name: $capability})
FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END | CREATE (a)-[:USED_CAPABILITY]->(c))
RETURN a.id AS id

// name: link_cause
MATCH (a:Action {id: $action_id}), (c:{label} {id: $cause_id})
MERGE (a)-[:CAUSED_BY]->(c)

// name: link_influence
MATCH (a:Action {id: $action_id}), (e:Event {id: $event_id})
MERGE (a)-[:INFLUENCED_BY]->(e)

// name: update_action
MATCH (a:Action {id: $id})
SET a.status = $status, a.result = $result, a.updated = $now
RETURN a.id AS id

// name: pending_action
MATCH (a:Action {status: 'pending_confirmation'})
WHERE a.ts >= $since
RETURN a.id AS id, a.tool AS tool, a.args_json AS args_json, a.summary AS summary, a.device_id AS device_id
ORDER BY a.ts DESC
LIMIT 1

// name: expire_pending_actions
MATCH (a:Action {status: 'pending_confirmation'})
WHERE a.ts < $since
SET a.status = 'expired'
RETURN count(a) AS n

// name: last_action
MATCH (a:Action)
WHERE a.status = 'done' AND a.tool IN $tools
RETURN a.id AS id
ORDER BY a.ts DESC
LIMIT 1

// name: set_device_online
MERGE (d:Device {id: $device_id})
SET d.online = $online, d.last_seen = $now

// name: set_all_devices_online
MATCH (u:User {id: $uid})-[:OWNS]->(d:Device)
SET d.online = $online

// name: list_devices
MATCH (u:User {id: $uid})-[:OWNS]->(d:Device)
RETURN d.id AS id, d.kind AS kind, d.name AS name, d.online AS online

// name: route_device
MATCH (u:User {id: $uid})-[:OWNS]->(d:Device)
WHERE d.online = true
OPTIONAL MATCH (d)<-[:ON_DEVICE]-(e:Event)
WHERE e.ts > $since
RETURN d.id AS device_id, d.kind AS kind, count(e) AS recent
ORDER BY recent DESC
LIMIT 1
