// Triage: every live notification is judged against the user's open requests and context.

// name: recent_utterances
MATCH (u:Event:Utterance)
WHERE u.ts >= $since AND coalesce(u.speaker, 'user') = 'user'
RETURN u.text AS text, u.ts AS ts
ORDER BY ts DESC
LIMIT 10

// name: set_triage
MATCH (e:Event {id: $event_id})
SET e.importance = $importance, e.triage_reason = $reason, e.triage_by = $by, e.triage_ts = $now
