CREATE TABLE IF NOT EXISTS stores (
    point_id BIGINT PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    locality TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sales (
    point_id BIGINT NOT NULL REFERENCES stores(point_id) ON DELETE CASCADE,
    sale_id BIGINT NOT NULL,
    sale_key TEXT NOT NULL DEFAULT '',
    sale_number TEXT NOT NULL DEFAULT '',
    sale_datetime TIMESTAMPTZ,
    order_datetime TIMESTAMPTZ,
    check_time_source TEXT NOT NULL DEFAULT '',
    opened_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,

    seller_id BIGINT,
    seller_name TEXT NOT NULL DEFAULT '',

    saby_shift_id BIGINT,
    saby_shift_number TEXT NOT NULL DEFAULT '',
    teller_id BIGINT,

    customer_id BIGINT,
    customer_name TEXT NOT NULL DEFAULT '',

    total_price NUMERIC(18, 4) NOT NULL DEFAULT 0,
    total_discount NUMERIC(18, 4) NOT NULL DEFAULT 0,
    is_return BOOLEAN NOT NULL DEFAULT FALSE,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,

    warehouse_id BIGINT,
    warehouse_name TEXT NOT NULL DEFAULT '',

    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (point_id, sale_id)
);

CREATE INDEX IF NOT EXISTS idx_sales_datetime
    ON sales(sale_datetime);

CREATE INDEX IF NOT EXISTS idx_sales_point_datetime
    ON sales(point_id, sale_datetime);

CREATE INDEX IF NOT EXISTS idx_sales_seller_datetime
    ON sales(seller_name, sale_datetime);

CREATE TABLE IF NOT EXISTS sale_items (
    point_id BIGINT NOT NULL,
    sale_id BIGINT NOT NULL,
    item_key TEXT NOT NULL,

    item_id BIGINT,
    line_number INTEGER,

    product_id BIGINT,
    product_uuid TEXT NOT NULL DEFAULT '',
    product_number TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    short_name TEXT NOT NULL DEFAULT '',
    barcode TEXT NOT NULL DEFAULT '',

    quantity NUMERIC(18, 6) NOT NULL DEFAULT 0,
    total_price NUMERIC(18, 4) NOT NULL DEFAULT 0,
    total_discount NUMERIC(18, 4) NOT NULL DEFAULT 0,
    planned_cost NUMERIC(18, 4) NOT NULL DEFAULT 0,
    total_cost NUMERIC(18, 4) NOT NULL DEFAULT 0,

    is_return BOOLEAN NOT NULL DEFAULT FALSE,
    refused BOOLEAN NOT NULL DEFAULT FALSE,
    unit_name TEXT NOT NULL DEFAULT '',

    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,

    PRIMARY KEY (point_id, sale_id, item_key),
    FOREIGN KEY (point_id, sale_id)
        REFERENCES sales(point_id, sale_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sale_items_product_uuid
    ON sale_items(product_uuid);

CREATE INDEX IF NOT EXISTS idx_sale_items_name
    ON sale_items(name);

CREATE TABLE IF NOT EXISTS seller_shifts (
    id BIGSERIAL PRIMARY KEY,
    point_id BIGINT NOT NULL REFERENCES stores(point_id) ON DELETE CASCADE,

    work_date DATE NOT NULL,
    shift_type TEXT NOT NULL CHECK (shift_type IN ('DAY', 'NIGHT')),
    seller_key TEXT NOT NULL,
    seller_id BIGINT,
    seller_name TEXT NOT NULL,

    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NOT NULL,

    check_count INTEGER NOT NULL DEFAULT 0,
    net_revenue NUMERIC(18, 4) NOT NULL DEFAULT 0,

    source TEXT NOT NULL DEFAULT 'reconstructed',
    confidence NUMERIC(5, 4) NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'REVIEW',

    saby_shift_id BIGINT,
    saby_shift_number TEXT NOT NULL DEFAULT '',

    duration_hours NUMERIC(10, 3) NOT NULL DEFAULT 0,
    dominant_share NUMERIC(5, 4) NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (
        point_id,
        work_date,
        shift_type,
        seller_key,
        started_at
    )
);

CREATE INDEX IF NOT EXISTS idx_seller_shifts_work_date
    ON seller_shifts(work_date);

CREATE INDEX IF NOT EXISTS idx_seller_shifts_seller
    ON seller_shifts(seller_name, work_date);

CREATE TABLE IF NOT EXISTS sync_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    date_from DATE NOT NULL,
    date_to DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    stores_count INTEGER NOT NULL DEFAULT 0,
    sales_upserted INTEGER NOT NULL DEFAULT 0,
    items_upserted INTEGER NOT NULL DEFAULT 0,
    shifts_built INTEGER NOT NULL DEFAULT 0,
    error_text TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- v0.2.2 migration
ALTER TABLE sales ADD COLUMN IF NOT EXISTS order_datetime TIMESTAMPTZ;
ALTER TABLE sales ADD COLUMN IF NOT EXISTS check_time_source TEXT NOT NULL DEFAULT '';
