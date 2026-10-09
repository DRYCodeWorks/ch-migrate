-- Users table v1
-- Example schema for ch-migrate-cli

CREATE TABLE {db}.users (
    id UInt64,
    email String,
    name String,
    created_at DateTime DEFAULT now()
)
ENGINE = SharedMergeTree
ORDER BY id
