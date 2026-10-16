// Indexes and constraints. Each statement is applied separately; "already exists" errors are ignored.
// Verify syntax against the FalkorDB version you run.

// name: schema
CREATE INDEX FOR (e:Event) ON (e.id);
CREATE INDEX FOR (e:Event) ON (e.ts);
CREATE INDEX FOR (e:Event) ON (e.hash);
CREATE INDEX FOR (u:User) ON (u.id);
CREATE INDEX FOR (d:Device) ON (d.id);
CREATE INDEX FOR (p:Person) ON (p.id);
CREATE INDEX FOR (p:Person) ON (p.name);
CREATE INDEX FOR (pl:Place) ON (pl.name);
CREATE INDEX FOR (a:App) ON (a.name);
CREATE INDEX FOR (f:File) ON (f.path);
CREATE INDEX FOR (l:Link) ON (l.url);
CREATE INDEX FOR (m:Message) ON (m.id);
CREATE INDEX FOR (dl:Download) ON (dl.id);
CREATE INDEX FOR (t:Trip) ON (t.id);
CREATE INDEX FOR (f:Fact) ON (f.id);
CREATE INDEX FOR (f:Fact) ON (f.key);
CREATE INDEX FOR (h:Habit) ON (h.id);
CREATE INDEX FOR (w:Watcher) ON (w.id);
CREATE INDEX FOR (w:Watcher) ON (w.status);
CREATE INDEX FOR (a:Action) ON (a.id);
CREATE INDEX FOR (a:Action) ON (a.status);
CREATE INDEX FOR (c:Capability) ON (c.name);
CREATE INDEX FOR (t:Task) ON (t.id);
CREATE INDEX FOR (t:Task) ON (t.status);
CREATE INDEX FOR (m:Meeting) ON (m.id);
CREATE INDEX FOR (m:Meeting) ON (m.start_ts);
CREATE INDEX FOR (d:Digest) ON (d.day);
CREATE INDEX FOR (s:Session) ON (s.id);
CREATE INDEX FOR (n:Note) ON (n.id);
CREATE INDEX FOR (pl:Place) ON (pl.id);
CALL db.idx.fulltext.createNodeIndex('Event', 'text');
CALL db.idx.fulltext.createNodeIndex('Person', 'name');

// name: schema_vectors
CREATE VECTOR INDEX FOR (f:Fact) ON (f.embedding) OPTIONS {dimension: 384, similarityFunction: 'cosine'};
CREATE VECTOR INDEX FOR (e:Event) ON (e.embedding) OPTIONS {dimension: 384, similarityFunction: 'cosine'};
CREATE VECTOR INDEX FOR (n:Note) ON (n.embedding) OPTIONS {dimension: 384, similarityFunction: 'cosine'}
