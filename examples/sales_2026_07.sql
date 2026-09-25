BEGIN;

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    customer_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    region TEXT NOT NULL
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit_price REAL NOT NULL CHECK (unit_price >= 0)
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    order_no TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'pending', 'cancelled')),
    total_amount REAL NOT NULL DEFAULT 0 CHECK (total_amount >= 0)
);

CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price REAL NOT NULL CHECK (unit_price >= 0),
    discount_amount REAL NOT NULL DEFAULT 0 CHECK (discount_amount >= 0),
    line_amount REAL NOT NULL CHECK (line_amount >= 0)
);

CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);

INSERT INTO customers(id, customer_code, name, region) VALUES
    (1, 'C001', '星海商贸', '华东'),
    (2, 'C002', '远山科技', '华北'),
    (3, 'C003', '南风零售', '华南'),
    (4, 'C004', '西岭实业', '西部'),
    (5, 'C005', '江城供应链', '华中'),
    (6, 'C006', '云帆网络', '华东'),
    (7, 'C007', '北辰百货', '华北'),
    (8, 'C008', '海角生活', '华南'),
    (9, 'C009', '绿洲贸易', '西部'),
    (10, 'C010', '中原优选', '华中');

INSERT INTO products(id, sku, name, category, unit_price) VALUES
    (1, 'P001', '轻薄笔记本', '电脑', 5999.00),
    (2, 'P002', '商务显示器', '电脑', 1899.00),
    (3, 'P003', '无线键盘', '配件', 399.00),
    (4, 'P004', '人体工学鼠标', '配件', 299.00),
    (5, 'P005', '降噪耳机', '音频', 1299.00),
    (6, 'P006', '会议麦克风', '音频', 899.00),
    (7, 'P007', '移动固态硬盘', '存储', 799.00),
    (8, 'P008', '桌面扩展坞', '配件', 699.00);

WITH RECURSIVE sequence(id) AS (
    VALUES(1)
    UNION ALL
    SELECT id + 1 FROM sequence WHERE id < 62
)
INSERT INTO orders(id, order_no, customer_id, order_date, status)
SELECT
    id,
    printf('SO-202607-%04d', id),
    ((id - 1) % 10) + 1,
    printf('2026-07-%02d', CAST(((id - 1) / 2) AS INTEGER) + 1),
    CASE
        WHEN id % 10 = 0 THEN 'cancelled'
        WHEN id % 4 = 0 THEN 'pending'
        ELSE 'completed'
    END
FROM sequence;

WITH item_seed AS (
    SELECT
        id AS order_id,
        ((id - 1) % 8) + 1 AS product_id,
        ((id - 1) % 3) + 1 AS quantity,
        CASE WHEN id % 7 = 0 THEN 0.10 ELSE 0 END AS discount_rate
    FROM orders
)
INSERT INTO order_items(
    id, order_id, product_id, quantity, unit_price, discount_amount, line_amount
)
SELECT
    seed.order_id * 10 + 1,
    seed.order_id,
    seed.product_id,
    seed.quantity,
    product.unit_price,
    ROUND(product.unit_price * seed.quantity * seed.discount_rate, 2),
    ROUND(product.unit_price * seed.quantity * (1 - seed.discount_rate), 2)
FROM item_seed AS seed
JOIN products AS product ON product.id = seed.product_id;

WITH item_seed AS (
    SELECT
        id AS order_id,
        ((id + 2) % 8) + 1 AS product_id,
        (id % 2) + 1 AS quantity,
        CASE WHEN id % 11 = 0 THEN 0.05 ELSE 0 END AS discount_rate
    FROM orders
)
INSERT INTO order_items(
    id, order_id, product_id, quantity, unit_price, discount_amount, line_amount
)
SELECT
    seed.order_id * 10 + 2,
    seed.order_id,
    seed.product_id,
    seed.quantity,
    product.unit_price,
    ROUND(product.unit_price * seed.quantity * seed.discount_rate, 2),
    ROUND(product.unit_price * seed.quantity * (1 - seed.discount_rate), 2)
FROM item_seed AS seed
JOIN products AS product ON product.id = seed.product_id;

UPDATE orders
SET total_amount = (
    SELECT ROUND(SUM(line_amount), 2)
    FROM order_items
    WHERE order_items.order_id = orders.id
);

COMMIT;
