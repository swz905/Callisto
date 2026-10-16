// Live device state: the latest snapshot lives on the Device node, open apps as RUNNING edges.
// Only *changes* (app opened/closed/focused, battery low, network changed) become Events.

// name: get_device_status
MATCH (d:Device {id: $device_id})
OPTIONAL MATCH (d)-[:RUNNING]->(a:App)
RETURN d.status_ts AS status_ts, d.foreground_app AS foreground_app, d.foreground_title AS foreground_title,
       d.battery AS battery, d.charging AS charging, d.network AS network, d.place_name AS place_name,
       collect(a.name) AS apps

// name: geo_places
MATCH (pl:Place)
WHERE pl.lat IS NOT NULL AND pl.lon IS NOT NULL
RETURN pl.id AS id, pl.name AS name, pl.lat AS lat, pl.lon AS lon, pl.radius_m AS radius_m

// name: link_event_place
MATCH (e:Event {id: $event_id})
MERGE (pl:Place {name: $place})
ON CREATE SET pl.id = $place_id, pl.kind = 'other'
CREATE (e)-[:ABOUT]->(pl)

// name: set_device_status
MERGE (d:Device {id: $device_id})
SET d += $props

// name: stop_running_apps
MATCH (d:Device {id: $device_id})-[r:RUNNING]->(a:App)
WHERE NOT a.name IN $apps
DELETE r

// name: start_running_apps
MATCH (d:Device {id: $device_id})
UNWIND $apps AS name
MERGE (a:App {name: name})
MERGE (d)-[r:RUNNING]->(a)
ON CREATE SET r.since = $now

// name: all_device_status
MATCH (u:User {id: $uid})-[:OWNS]->(d:Device)
OPTIONAL MATCH (d)-[r:RUNNING]->(a:App)
WITH d, a, r
ORDER BY r.since DESC
RETURN d.id AS id, d.kind AS kind, d.name AS name, d.online AS online, d.status_ts AS status_ts,
       d.foreground_app AS foreground_app, d.foreground_title AS foreground_title, d.battery AS battery,
       d.charging AS charging, d.network AS network, d.cpu AS cpu, d.mem AS mem, d.idle_s AS idle_s,
       d.downloads_active AS downloads_active, d.place_name AS place_name, collect(a.name) AS apps
