SCHEMA_V1 = """
PRAGMA foreign_keys = ON;

CREATE TABLE schema_version (
    version INTEGER NOT NULL
);

INSERT INTO schema_version(version) VALUES (1);

CREATE TABLE shipping_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE delivery_warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    address TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE fba_warehouses (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    last_successful_channel_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE processing_batches (
    id TEXT PRIMARY KEY,
    source_filename TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    template_type TEXT NOT NULL,
    template_version TEXT NOT NULL,
    state TEXT NOT NULL,
    shipment_count INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    source_path TEXT NOT NULL,
    zip_path TEXT,
    error_code TEXT,
    error_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE shipments (
    batch_id TEXT NOT NULL,
    shipment_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    sailing_manual INTEGER NOT NULL DEFAULT 0,
    output_path TEXT,
    PRIMARY KEY (batch_id, shipment_id),
    FOREIGN KEY (batch_id) REFERENCES processing_batches(id) ON DELETE CASCADE
);

CREATE TABLE shipment_items (
    batch_id TEXT NOT NULL,
    shipment_id TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    fields_json TEXT NOT NULL,
    drawing_ids_json TEXT NOT NULL,
    PRIMARY KEY (batch_id, shipment_id, source_row),
    FOREIGN KEY (batch_id, shipment_id) REFERENCES shipments(batch_id, shipment_id)
        ON DELETE CASCADE
);

CREATE TABLE app_defaults (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

__all__ = ["SCHEMA_V1"]
