-- Phase 0 placeholder. Phase 4 replaces this with the reviewed ReasoningBank schema.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON memories (updated_at);

-- The vector index shape is retained only as a Phase 4 design note.
-- CREATE VIRTUAL TABLE IF NOT EXISTS memory_embeddings USING vec0(
--     memory_id INTEGER PRIMARY KEY,
--     embedding FLOAT[384]
-- );
