// Nightly memory digest (kairos/digest.py): one Digest node per local day of conversation.
// Facts, notes and tasks the digest files away point back to it with DERIVED_FROM, so "why do you think that?"
// has an answer: (Fact)-[:DERIVED_FROM]->(Digest)-[:COVERS]->(Session)<-[:IN_SESSION]-(Utterance).

// name: create_digest
MATCH (u:User {id: $uid})
MERGE (d:Digest {day: $day})
ON CREATE SET d.id = $id
SET d.summary = $summary, d.topics = $topics, d.ts = $now, d.turns = $turns, d.method = $method
MERGE (u)-[:DIGESTED]->(d)
WITH d
OPTIONAL MATCH (s:Session)
WHERE s.last >= $since AND s.started < $until
FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END | MERGE (d)-[:COVERS]->(s))
RETURN DISTINCT d.id AS id

// name: derived_from_digest
MATCH (n:{label} {id: $id}), (d:Digest {id: $digest_id})
MERGE (n)-[:DERIVED_FROM]->(d)

// name: digest_mentions_person
MATCH (d:Digest {id: $digest_id}), (p:Person {id: $person_id})
MERGE (d)-[:MENTIONS]->(p)

// name: digested_days
MATCH (:User {id: $uid})-[:DIGESTED]->(d:Digest)
RETURN d.day AS day

// name: recent_digests
MATCH (:User {id: $uid})-[:DIGESTED]->(d:Digest)
OPTIONAL MATCH (n)-[:DERIVED_FROM]->(d)
RETURN d.id AS id, d.day AS day, d.summary AS summary, d.topics AS topics, d.method AS method,
       d.turns AS turns, count(n) AS items
ORDER BY day DESC
LIMIT $limit

// name: search_digests_text
MATCH (d:Digest)
WHERE toLower(d.summary) CONTAINS toLower($q)
   OR any(t IN coalesce(d.topics, []) WHERE toLower(t) CONTAINS toLower($q))
RETURN d.id AS id, d.day AS day, d.summary AS summary
ORDER BY day DESC
LIMIT 3
