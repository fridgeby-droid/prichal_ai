"""Execute the production ordering expression against representative queue rows."""
import sqlite3
from app.services.bonus_load import QUEUE_ORDER


def test_new_before_errors_and_oldest_retry_rotates_across_runs():
    conn=sqlite3.connect(':memory:')
    conn.executescript('''CREATE TABLE sales(point_id INTEGER,sale_id INTEGER,business_date TEXT);
      CREATE TABLE observations(point_id INTEGER,sale_id INTEGER,status TEXT,attempted_at TEXT);
      INSERT INTO sales VALUES (5590,8812,'2026-07-01'),(5590,8813,'2026-07-01'),
                              (398,200000,'2026-10-23');
      INSERT INTO observations VALUES(5590,8812,'ERROR','2026-10-24T01:00:00'),
                                     (5590,8813,'ERROR','2026-10-25T01:00:00');''')
    query='''SELECT s.sale_id FROM sales s LEFT JOIN observations b
        ON s.point_id=b.point_id AND s.sale_id=b.sale_id
        WHERE b.point_id IS NULL OR b.status='ERROR' ORDER BY '''+QUEUE_ORDER
    assert [r[0] for r in conn.execute(query)]==[200000,8812,8813]
    conn.execute("INSERT INTO observations VALUES(398,200000,'OK','2026-10-26T01:00:00')")
    assert [r[0] for r in conn.execute(query)]==[8812,8813]
    # A failed attempt is committed; next process sees the updated timestamp.
    conn.execute("UPDATE observations SET attempted_at='2026-10-26T02:00:00' WHERE sale_id=8812")
    conn.commit()
    assert [r[0] for r in conn.execute(query)]==[8813,8812]
    conn.close()


def test_equal_retry_time_has_stable_tiebreaker():
    conn=sqlite3.connect(':memory:')
    conn.executescript('''CREATE TABLE sales(point_id INTEGER,sale_id INTEGER,business_date TEXT);
      CREATE TABLE observations(point_id INTEGER,sale_id INTEGER,status TEXT,attempted_at TEXT);
      INSERT INTO sales VALUES(1,3,'2026-07-01'),(1,2,'2026-07-01');
      INSERT INTO observations VALUES(1,3,'ERROR','2026-10-26'),(1,2,'ERROR','2026-10-26');''')
    rows=conn.execute('SELECT s.sale_id FROM sales s JOIN observations b USING(point_id,sale_id) ORDER BY '+QUEUE_ORDER)
    assert [r[0] for r in rows]==[2,3]
    conn.close()
