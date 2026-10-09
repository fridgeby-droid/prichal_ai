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


-- Payment/check ledger.
-- This is the source of truth for shift revenue.
-- One row = one Saby Payments[] record (fiscal/nonfiscal payment/check).
CREATE TABLE IF NOT EXISTS sale_payments (
    point_id BIGINT NOT NULL,
    sale_id BIGINT NOT NULL,

    payment_key TEXT NOT NULL,

    payment_id BIGINT,
    check_number TEXT NOT NULL DEFAULT '',

    carried_at TIMESTAMPTZ,
    opened_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,

    business_date DATE,
    business_shift_type TEXT
        CHECK (
            business_shift_type IS NULL
            OR business_shift_type IN ('DAY', 'NIGHT')
        ),

    seller_id BIGINT,
    seller_name TEXT NOT NULL DEFAULT '',

    saby_shift_id BIGINT,
    saby_shift_number TEXT NOT NULL DEFAULT '',
    teller_id BIGINT,

    amount NUMERIC(18, 4) NOT NULL DEFAULT 0,
    signed_amount NUMERIC(18, 4) NOT NULL DEFAULT 0,

    cash_sum NUMERIC(18, 4) NOT NULL DEFAULT 0,
    bank_sum NUMERIC(18, 4) NOT NULL DEFAULT 0,
    certificate_sum NUMERIC(18, 4) NOT NULL DEFAULT 0,
    salary_sum NUMERIC(18, 4) NOT NULL DEFAULT 0,

    nonfiscal BOOLEAN NOT NULL DEFAULT FALSE,
    is_return BOOLEAN NOT NULL DEFAULT FALSE,

    source TEXT NOT NULL DEFAULT 'saby_payment',

    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (point_id, sale_id, payment_key),

    FOREIGN KEY (point_id, sale_id)
        REFERENCES sales(point_id, sale_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sale_payments_business_date
    ON sale_payments(business_date);

CREATE INDEX IF NOT EXISTS idx_sale_payments_point_business_date
    ON sale_payments(point_id, business_date);

CREATE INDEX IF NOT EXISTS idx_sale_payments_shift
    ON sale_payments(saby_shift_id, business_date);

CREATE INDEX IF NOT EXISTS idx_sale_payments_seller
    ON sale_payments(seller_id, business_date);


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


-- Persistent historical import state.
-- next_date is the next Причал business date still to process.
CREATE TABLE IF NOT EXISTS backfill_runs (
    id BIGSERIAL PRIMARY KEY,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,

    date_from DATE NOT NULL,
    date_to DATE NOT NULL,
    next_date DATE NOT NULL,

    chunk_days INTEGER NOT NULL DEFAULT 7,

    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (
            status IN (
                'PENDING',
                'RUNNING',
                'PAUSED',
                'ERROR',
                'COMPLETED'
            )
        ),

    completed_days INTEGER NOT NULL DEFAULT 0,
    chunks_completed INTEGER NOT NULL DEFAULT 0,

    stores_count INTEGER NOT NULL DEFAULT 0,
    sales_upserted BIGINT NOT NULL DEFAULT 0,
    items_upserted BIGINT NOT NULL DEFAULT 0,
    shifts_built BIGINT NOT NULL DEFAULT 0,

    last_chunk_from DATE,
    last_chunk_to DATE,

    last_error TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_backfill_runs_status
    ON backfill_runs(status, id DESC);


-- ============================================================
-- PAYROLL FOUNDATION
-- ============================================================

-- Store DAY/NIGHT plans.
-- weekday: NULL = default for all weekdays; 0=Mon ... 6=Sun.
CREATE TABLE IF NOT EXISTS shift_plans (
    id BIGSERIAL PRIMARY KEY,

    point_id BIGINT NOT NULL
        REFERENCES stores(point_id)
        ON DELETE CASCADE,

    shift_type TEXT NOT NULL
        CHECK (shift_type IN ('DAY','NIGHT')),

    weekday SMALLINT
        CHECK (weekday IS NULL OR (weekday BETWEEN 0 AND 6)),

    plan_amount NUMERIC(18, 2) NOT NULL
        CHECK (plan_amount >= 0),

    valid_from DATE NOT NULL,
    valid_to DATE,

    active BOOLEAN NOT NULL DEFAULT TRUE,

    source TEXT NOT NULL DEFAULT 'MANUAL',
    note TEXT NOT NULL DEFAULT '',

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE INDEX IF NOT EXISTS idx_shift_plans_lookup
    ON shift_plans(
        point_id,
        shift_type,
        valid_from,
        valid_to,
        weekday,
        active
    );


-- Versioned compensation policy.
-- Config remains columnar for deterministic payroll plus JSON metadata
-- for future role-specific extensions.
CREATE TABLE IF NOT EXISTS payroll_policies (
    id BIGSERIAL PRIMARY KEY,

    role TEXT NOT NULL
        CHECK (role IN ('SELLER','NIGHT_ASSISTANT')),

    version INTEGER NOT NULL,

    name TEXT NOT NULL,

    valid_from DATE NOT NULL,
    valid_to DATE,

    active BOOLEAN NOT NULL DEFAULT TRUE,

    base_per_shift NUMERIC(18, 2) NOT NULL DEFAULT 0,

    -- Seller default: STORE_SHIFT_REVENUE.
    kpi_basis TEXT NOT NULL DEFAULT 'STORE_SHIFT_REVENUE',

    -- JSON array:
    -- [{"min_ratio":1.0,"max_ratio":1.25,"percent":0.03}, ...]
    kpi_tiers JSONB NOT NULL DEFAULT '[]'::jsonb,

    exam_percent NUMERIC(8, 5) NOT NULL DEFAULT 0,
    exam_basis TEXT NOT NULL DEFAULT 'PERSONAL_MONTH_REVENUE',

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(role, version),

    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE INDEX IF NOT EXISTS idx_payroll_policy_lookup
    ON payroll_policies(role, valid_from, valid_to, active);


-- One identity layer across Saby and Причал Core.
CREATE TABLE IF NOT EXISTS employee_identity_map (
    id BIGSERIAL PRIMARY KEY,

    role TEXT NOT NULL
        CHECK (role IN ('SELLER','NIGHT_ASSISTANT')),

    full_name TEXT NOT NULL,

    saby_seller_id BIGINT,
    core_employee_id TEXT,

    valid_from DATE NOT NULL DEFAULT CURRENT_DATE,
    valid_to DATE,

    active BOOLEAN NOT NULL DEFAULT TRUE,

    source TEXT NOT NULL DEFAULT 'MANUAL',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE INDEX IF NOT EXISTS idx_employee_identity_saby
    ON employee_identity_map(saby_seller_id, active);

CREATE INDEX IF NOT EXISTS idx_employee_identity_core
    ON employee_identity_map(core_employee_id, active);

CREATE INDEX IF NOT EXISTS idx_employee_identity_name
    ON employee_identity_map(LOWER(full_name), role, active);


-- Monthly exam result. month must be first day of month.
CREATE TABLE IF NOT EXISTS employee_exam_results (
    id BIGSERIAL PRIMARY KEY,

    identity_id BIGINT NOT NULL
        REFERENCES employee_identity_map(id)
        ON DELETE CASCADE,

    month DATE NOT NULL,

    passed BOOLEAN NOT NULL,
    score NUMERIC(8, 3),

    source TEXT NOT NULL DEFAULT 'MANUAL',
    note TEXT NOT NULL DEFAULT '',

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(identity_id, month),

    CHECK (EXTRACT(DAY FROM month) = 1)
);


-- Seed the currently agreed seller policy.
-- Historical policy is immutable: future changes create v2/v3...
INSERT INTO payroll_policies(
    role,
    version,
    name,
    valid_from,
    valid_to,
    active,
    base_per_shift,
    kpi_basis,
    kpi_tiers,
    exam_percent,
    exam_basis,
    metadata
)
VALUES(
    'SELLER',
    1,
    'Seller Policy v1',
    DATE '2026-07-01',
    NULL,
    TRUE,
    2000.00,
    'STORE_SHIFT_REVENUE',
    '[
        {"min_ratio": 1.00, "max_ratio": 1.25, "percent": 0.03},
        {"min_ratio": 1.25, "max_ratio": null, "percent": 0.05}
    ]'::jsonb,
    0.03,
    'PERSONAL_MONTH_REVENUE',
    '{
        "description": "Current seller compensation scheme",
        "returns_in_revenue": false
    }'::jsonb
)
ON CONFLICT(role, version) DO NOTHING;

-- Retail scope: RC is retained in raw tables but excluded from all retail reads.
CREATE OR REPLACE VIEW excluded_retail_points AS SELECT 23109 AS point_id;
CREATE OR REPLACE VIEW retail_stores AS
    SELECT * FROM stores
    WHERE point_id NOT IN (SELECT point_id FROM excluded_retail_points);
CREATE OR REPLACE VIEW retail_sales AS
    SELECT * FROM sales
    WHERE point_id NOT IN (SELECT point_id FROM excluded_retail_points);
CREATE OR REPLACE VIEW retail_sale_payments AS
    SELECT * FROM sale_payments
    WHERE point_id NOT IN (SELECT point_id FROM excluded_retail_points);
CREATE OR REPLACE VIEW retail_sale_items AS
    SELECT * FROM sale_items
    WHERE point_id NOT IN (SELECT point_id FROM excluded_retail_points);
CREATE OR REPLACE VIEW retail_cash_shifts AS
    SELECT * FROM cash_shifts
    WHERE point_id NOT IN (SELECT point_id FROM excluded_retail_points);
CREATE OR REPLACE VIEW retail_employee_work_shifts AS
    SELECT * FROM employee_work_shifts
    WHERE point_id NOT IN (SELECT point_id FROM excluded_retail_points);
CREATE OR REPLACE VIEW retail_seller_shifts AS
    SELECT * FROM seller_shifts
    WHERE point_id NOT IN (SELECT point_id FROM excluded_retail_points);
CREATE OR REPLACE VIEW retail_shift_plans AS
    SELECT * FROM shift_plans
    WHERE point_id NOT IN (SELECT point_id FROM excluded_retail_points);
