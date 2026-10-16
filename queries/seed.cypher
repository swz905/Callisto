// Persona / policy seeding. All idempotent (MERGE).

// name: seed_user
MERGE (u:User {id: $uid})
SET u.name = $name, u.tz = $tz

// name: seed_device
MATCH (u:User {id: $uid})
MERGE (d:Device {id: $id})
SET d.kind = $kind, d.name = $name
MERGE (u)-[:OWNS]->(d)

// name: seed_person
MATCH (u:User {id: $uid})
MERGE (p:Person {id: $id})
SET p.name = $name, p.aliases = $aliases
MERGE (u)-[r:RELATION]->(p)
SET r.type = $relation

// name: seed_place
MERGE (pl:Place {id: $id})
SET pl.name = $name, pl.kind = $kind, pl.lat = $lat, pl.lon = $lon, pl.radius_m = $radius_m

// name: seed_capability
MERGE (c:Capability {name: $name})
SET c.risk = $risk, c.description = $description

// name: seed_permission
MATCH (d:Device {id: $device_id}), (c:Capability {name: $capability})
MERGE (d)-[a:ALLOWED]->(c)
SET a.mode = $mode
