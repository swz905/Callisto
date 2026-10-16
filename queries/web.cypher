// Web memory: what was searched, which results came back, which pages were read.
//   (WebSearch event)-[:RESULT {rank, snippet}]->(Link)
//   (WebRead event)-[:MENTIONS]->(Link {title, excerpt, read_at})

// name: link_search_result
MATCH (e:Event {id: $event_id})
MERGE (l:Link {url: $url})
ON CREATE SET l.domain = $domain
SET l.title = coalesce($title, l.title)
CREATE (e)-[:RESULT {rank: $rank, snippet: $snippet}]->(l)

// name: set_link_excerpt
MATCH (l:Link {url: $url})
SET l.excerpt = $excerpt, l.read_at = $now, l.title = coalesce($title, l.title)

// name: link_known
MATCH (l:Link {url: $url})
RETURN l.url AS url
LIMIT 1

// name: recent_web_reads
MATCH (e:Event:WebRead)-[:MENTIONS]->(l:Link)
WHERE e.ts >= $since AND coalesce(e.archived, false) = false
  AND ($q = '' OR toLower(coalesce(l.title, '') + ' ' + coalesce(l.excerpt, '') + ' ' + l.url) CONTAINS toLower($q))
RETURN l.url AS url, l.title AS title, l.excerpt AS excerpt, e.ts AS ts
ORDER BY ts DESC
LIMIT 10
