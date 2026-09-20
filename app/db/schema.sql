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

    -- Effective fiscal/check timestamp. Prefer Payments.CarriedWTZ.
    sale_datetime TIMESTAMPTZ,

    -- Original Saby DateWTZ.
    order_datetime TIMESTAMPTZ,

    check_time_source TEXT NOT NULL DEFAULT '',

    -- Причал operational date:
    -- 2026-09-19 means 19.09 08:00 -> 20.09 07:59:59.
    business_date DATE,

    -- Check bucket inside business day.
    business_shift_type TEXT
        CHECK (
            business_shift_type IS NULL
            OR business_shift_type IN ('DAY', 'NIGHT')
        ),

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

ALTER TABLE sales
    ADD COLUMN IF NOT EXISTS order_datetime TIMESTAMPTZ;

ALTER TABLE sales
    ADD COLUMN IF NOT EXISTS check_time_source TEXT NOT NULL DEFAULT '';

ALTER TABLE sales
    ADD COLUMN IF NOT EXISTS business_date DATE;

ALTER TABLE sales
    ADD COLUMN IF NOT EXISTS business_shift_type TEXT;

CREATE INDEX IF NOT EXISTS idx_sales_datetime
    ON sales(sale_datetime);

CREATE INDEX IF NOT EXISTS idx_sales_business_date
    ON sales(business_date);

CREATE INDEX IF NOT EXISTS idx_sales_point_business_date
    ON sales(point_id, business_date);

CREATE INDEX IF NOT EXISTS idx_sales_seller_business_date
    ON sales(seller_id, business_date);

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

-- Saby/native/fallback cash-shift facts.
CREATE TABLE IF NOT EXISTS cash_shifts (
    id BIGSERIAL PRIMARY KEY,

    cash_shift_key TEXT NOT NULL UNIQUE,

    point_id BIGINT NOT NULL
        REFERENCES stores(point_id)
        ON DELETE CASCADE,

    business_date DATE NOT NULL,

    shift_type TEXT NOT NULL
        CHECK (shift_type IN ('DAY', 'NIGHT')),

    seller_key TEXT NOT NULL,
    seller_id BIGINT,
    seller_name TEXT NOT NULL,

    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NOT NULL,

    check_count INTEGER NOT NULL DEFAULT 0,
    net_revenue NUMERIC(18, 4) NOT NULL DEFAULT 0,

    source TEXT NOT NULL,
    confidence NUMERIC(5, 4) NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'REVIEW',

    saby_shift_id BIGINT,
    saby_shift_number TEXT NOT NULL DEFAULT '',

    duration_hours NUMERIC(10, 3) NOT NULL DEFAULT 0,
    dominant_share NUMERIC(5, 4) NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cash_shifts_business_date
    ON cash_shifts(business_date);

CREATE INDEX IF NOT EXISTS idx_cash_shifts_seller
    ON cash_shifts(seller_id, business_date);

-- Paid/operational employee shift after consolidation of Saby cash shifts.
CREATE TABLE IF NOT EXISTS employee_work_shifts (
    id BIGSERIAL PRIMARY KEY,

    point_id BIGINT NOT NULL
        REFERENCES stores(point_id)
        ON DELETE CASCADE,

    business_date DATE NOT NULL,

    shift_type TEXT NOT NULL
        CHECK (shift_type IN ('DAY', 'NIGHT')),

    seller_key TEXT NOT NULL,
    seller_id BIGINT,
    seller_name TEXT NOT NULL,

    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NOT NULL,

    check_count INTEGER NOT NULL DEFAULT 0,
    net_revenue NUMERIC(18, 4) NOT NULL DEFAULT 0,

    cash_shift_count INTEGER NOT NULL DEFAULT 0,
    cash_shift_keys JSONB NOT NULL DEFAULT '[]'::jsonb,

    source TEXT NOT NULL,
    confidence NUMERIC(5, 4) NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'REVIEW',

    duration_hours NUMERIC(10, 3) NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (
        point_id,
        business_date,
        shift_type,
        seller_key,
        started_at
    )
);

CREATE INDEX IF NOT EXISTS idx_employee_work_shifts_date
    ON employee_work_shifts(business_date);

CREATE INDEX IF NOT EXISTS idx_employee_work_shifts_seller
    ON employee_work_shifts(seller_id, business_date);

-- Legacy table retained temporarily for backwards compatibility/migration.
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
    source TEXT NOT NULL DEFAULT 'legacy',
    confidence NUMERIC(5, 4) NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'REVIEW',
    saby_shift_id BIGINT,
    saby_shift_number TEXT NOT NULL DEFAULT '',
    duration_hours NUMERIC(10, 3) NOT NULL DEFAULT 0,
    dominant_share NUMERIC(5, 4) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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
