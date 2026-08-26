PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memory_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    status TEXT NOT NULL CHECK (status IN ('success', 'fail')),
    item_type TEXT NOT NULL CHECK (item_type IN ('strategy', 'meta_heuristic')),
    task_query TEXT NOT NULL CHECK (length(trim(task_query)) > 0),
    session_id TEXT NOT NULL CHECK (length(trim(session_id)) > 0),
    template_key TEXT,
    success_count INTEGER NOT NULL DEFAULT 0 CHECK (success_count >= 0),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    confidence REAL NOT NULL DEFAULT 0.5
        CHECK (confidence >= 0.25 AND confidence <= 1.0),
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE INDEX IF NOT EXISTS idx_memory_items_created_at
    ON memory_items (created_at, id);
CREATE INDEX IF NOT EXISTS idx_memory_items_provenance
    ON memory_items (status, item_type, template_key);

CREATE TABLE IF NOT EXISTS memory_feedback (
    session_id TEXT NOT NULL CHECK (length(trim(session_id)) > 0),
    memory_id INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure')),
    source TEXT NOT NULL CHECK (source IN ('terminal', 'judge')),
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    PRIMARY KEY (session_id, memory_id),
    FOREIGN KEY (memory_id) REFERENCES memory_items(id) ON DELETE CASCADE
);

-- MemoryStore replaces 384 when a custom embedder dimension is configured.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_embeddings USING vec0(
    memory_id INTEGER PRIMARY KEY,
    embedding FLOAT[384] distance_metric=cosine
);
