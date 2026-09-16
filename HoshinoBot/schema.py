"""Core SQLite schema shared by the plugin and offline migration tool."""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gvg_snapshots (
    cuid INTEGER NOT NULL,
    kind TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (cuid, kind, snapshot_date)
);
CREATE TABLE IF NOT EXISTS gvg_members (
    cuid INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    avatar_role_id TEXT,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS gvg_current_members (
    cuid INTEGER PRIMARY KEY,
    snapshot_date TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    upper_1_role_id TEXT,
    upper_2_role_id TEXT,
    upper_3_role_id TEXT,
    lower_1_role_id TEXT,
    lower_2_role_id TEXT,
    lower_3_role_id TEXT,
    FOREIGN KEY (cuid) REFERENCES gvg_members(cuid)
);
CREATE TABLE IF NOT EXISTS gvg_rounds (
    battle_id TEXT NOT NULL,
    round_idx INTEGER NOT NULL,
    start_ts INTEGER NOT NULL,
    atk_cuid INTEGER,
    atk_name TEXT,
    atk_guild TEXT,
    def_cuid INTEGER,
    def_name TEXT,
    def_guild TEXT,
    win INTEGER NOT NULL,
    PRIMARY KEY (battle_id, round_idx)
);
CREATE TABLE IF NOT EXISTS gvg_units (
    battle_id TEXT NOT NULL,
    round_idx INTEGER NOT NULL,
    side TEXT NOT NULL,
    pos INTEGER NOT NULL,
    role_id TEXT NOT NULL,
    star INTEGER,
    awaken INTEGER,
    imprint INTEGER,
    dead INTEGER NOT NULL,
    PRIMARY KEY (battle_id, round_idx, side, pos)
);
CREATE TABLE IF NOT EXISTS plugin_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gvg_rounds_recent
    ON gvg_rounds(start_ts);
CREATE INDEX IF NOT EXISTS idx_gvg_rounds_defender
    ON gvg_rounds(def_cuid, atk_guild, start_ts);
CREATE INDEX IF NOT EXISTS idx_gvg_units_role
    ON gvg_units(side, role_id);
CREATE INDEX IF NOT EXISTS idx_gvg_units_round_side
    ON gvg_units(battle_id, round_idx, side, role_id, dead);
"""
