-- Sorta initial schema.
-- Lives in `<HD root>/sorta.db`.

CREATE TABLE IF NOT EXISTS media (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id         INTEGER NOT NULL,
    media_type      TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
    title           TEXT NOT NULL,
    original_title  TEXT,
    runtime_minutes INTEGER,
    poster_path     TEXT,           -- relative path inside HD root, e.g. "poster/27205.jpg"
    poster_url      TEXT,           -- TMDB URL fallback
    folder_path     TEXT NOT NULL,  -- relative to HD root, e.g. "Movies/Action/Inception [tmdb-27205]"
    UNIQUE (tmdb_id, media_type)
);

CREATE INDEX IF NOT EXISTS idx_media_folder ON media(folder_path);

CREATE TABLE IF NOT EXISTS genres (
    id              INTEGER NOT NULL,            -- TMDB genre id
    media_type      TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
    canonical_name  TEXT NOT NULL,               -- English name from TMDB
    translated_name TEXT,                        -- user override (display name)
    PRIMARY KEY (id, media_type)
);

CREATE TABLE IF NOT EXISTS media_genres (
    media_id   INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    genre_id   INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (media_id, genre_id),
    FOREIGN KEY (genre_id, media_type) REFERENCES genres(id, media_type)
);

CREATE INDEX IF NOT EXISTS idx_media_genres_genre ON media_genres(genre_id, media_type);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Default kind-root labels. The user can overwrite them via the Settings UI.
INSERT OR IGNORE INTO settings(key, value) VALUES
    ('movies_folder_label', 'Movies'),
    ('series_folder_label', 'Series');
