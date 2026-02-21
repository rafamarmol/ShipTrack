import os
import re
import sqlite3
from datetime import datetime, date
from flask import Flask, request, jsonify, render_template, g

app = Flask(__name__)

# ─── Database config ─────────────────────────────────────────────────────────
# If DATABASE_URL env var is set (Railway provides this), use PostgreSQL.
# Otherwise fall back to local SQLite for development.

DATABASE_URL = os.environ.get('DATABASE_URL', '')
USE_PG = DATABASE_URL.startswith('postgres')

if USE_PG:
    import psycopg2
    import psycopg2.extras
    # Railway may provide postgres:// but psycopg2 needs postgresql://
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
else:
    SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shipping.db')


# ─── Database helpers ────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        if USE_PG:
            g.db = psycopg2.connect(DATABASE_URL)
            g.db.autocommit = False
        else:
            g.db = sqlite3.connect(SQLITE_PATH)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA journal_mode=WAL")
            g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


def db_cursor(db):
    """Return a cursor that gives dict-like rows."""
    if USE_PG:
        return db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        return db.cursor()


def q(sql):
    """Convert ? placeholders to %s for PostgreSQL."""
    if USE_PG:
        return sql.replace('?', '%s')
    return sql


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    """Create the normalized schema."""
    if USE_PG:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_number TEXT PRIMARY KEY,
                customer_name TEXT DEFAULT '',
                customer_id INTEGER,
                po_number TEXT DEFAULT '',
                ship_date TEXT DEFAULT '',
                in_hands_date TEXT DEFAULT '',
                order_type TEXT DEFAULT '',
                total_groups INTEGER,
                total_boxes INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                pushed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS order_groups (
                id SERIAL PRIMARY KEY,
                order_number TEXT NOT NULL,
                group_letter TEXT NOT NULL,
                box_count INTEGER,
                group_type TEXT DEFAULT '',
                UNIQUE(order_number, group_letter),
                FOREIGN KEY (order_number) REFERENCES orders(order_number)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS order_designs (
                id SERIAL PRIMARY KEY,
                order_number TEXT NOT NULL,
                group_letter TEXT NOT NULL,
                design_type TEXT DEFAULT '',
                design_id TEXT DEFAULT '',
                design_name TEXT DEFAULT '',
                design_location TEXT DEFAULT '',
                FOREIGN KEY (order_number) REFERENCES orders(order_number)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scans (
                id SERIAL PRIMARY KEY,
                order_number TEXT NOT NULL,
                group_letter TEXT NOT NULL,
                box_number INTEGER NOT NULL,
                raw_barcode TEXT NOT NULL,
                scanned_by TEXT DEFAULT '',
                scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(order_number, group_letter, box_number)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_scans_order ON scans(order_number)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_scans_date ON scans(scanned_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)")
        conn.commit()
        cur.close()
        conn.close()
    else:
        db = sqlite3.connect(SQLITE_PATH)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                order_number TEXT PRIMARY KEY,
                customer_name TEXT DEFAULT '',
                customer_id INTEGER,
                po_number TEXT DEFAULT '',
                ship_date TEXT DEFAULT '',
                in_hands_date TEXT DEFAULT '',
                order_type TEXT DEFAULT '',
                total_groups INTEGER,
                total_boxes INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                pushed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS order_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_number TEXT NOT NULL,
                group_letter TEXT NOT NULL,
                box_count INTEGER,
                group_type TEXT DEFAULT '',
                UNIQUE(order_number, group_letter),
                FOREIGN KEY (order_number) REFERENCES orders(order_number)
            );

            CREATE TABLE IF NOT EXISTS order_designs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_number TEXT NOT NULL,
                group_letter TEXT NOT NULL,
                design_type TEXT DEFAULT '',
                design_id TEXT DEFAULT '',
                design_name TEXT DEFAULT '',
                design_location TEXT DEFAULT '',
                FOREIGN KEY (order_number) REFERENCES orders(order_number)
            );

            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_number TEXT NOT NULL,
                group_letter TEXT NOT NULL,
                box_number INTEGER NOT NULL,
                raw_barcode TEXT NOT NULL,
                scanned_by TEXT DEFAULT '',
                scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(order_number, group_letter, box_number)
            );

            CREATE INDEX IF NOT EXISTS idx_scans_order ON scans(order_number);
            CREATE INDEX IF NOT EXISTS idx_scans_date ON scans(scanned_at);
            CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
        """)
        db.commit()
        db.close()


def migrate_db():
    """Migrate from the old flat scans table to the new normalized schema (SQLite only)."""
    if USE_PG:
        return  # PostgreSQL starts fresh on Railway — no migration needed

    db = sqlite3.connect(SQLITE_PATH)
    db.execute("PRAGMA journal_mode=WAL")

    # Check if migration is needed
    cursor = db.execute("PRAGMA table_info(scans)")
    columns = [row[1] for row in cursor.fetchall()]
    if 'customer_code' not in columns:
        db.close()
        return  # Already migrated or fresh install

    print("[migrate] Migrating from old schema to new schema...")
    db.execute("PRAGMA foreign_keys=OFF")

    # Ensure new tables exist first
    db.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            order_number TEXT PRIMARY KEY,
            customer_name TEXT DEFAULT '',
            customer_id INTEGER,
            po_number TEXT DEFAULT '',
            ship_date TEXT DEFAULT '',
            in_hands_date TEXT DEFAULT '',
            order_type TEXT DEFAULT '',
            total_groups INTEGER,
            total_boxes INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            pushed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS order_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT NOT NULL,
            group_letter TEXT NOT NULL,
            box_count INTEGER,
            group_type TEXT DEFAULT '',
            UNIQUE(order_number, group_letter),
            FOREIGN KEY (order_number) REFERENCES orders(order_number)
        );
        CREATE TABLE IF NOT EXISTS order_designs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT NOT NULL,
            group_letter TEXT NOT NULL,
            design_type TEXT DEFAULT '',
            design_id TEXT DEFAULT '',
            design_name TEXT DEFAULT '',
            design_location TEXT DEFAULT '',
            FOREIGN KEY (order_number) REFERENCES orders(order_number)
        );
    """)

    # 1. Migrate order data from old scans
    db.execute("""
        INSERT OR IGNORE INTO orders (order_number, customer_name, po_number, total_groups, total_boxes)
        SELECT order_number,
               COALESCE(customer_code, ''),
               COALESCE(po_number, ''),
               MAX(total_groups),
               MAX(total_boxes_in_order)
        FROM scans
        WHERE order_number IS NOT NULL
        GROUP BY order_number
    """)

    # 2. Migrate group data
    db.execute("""
        INSERT OR IGNORE INTO order_groups (order_number, group_letter, box_count)
        SELECT order_number, group_letter, MAX(total_boxes_in_group)
        FROM scans
        WHERE group_letter IS NOT NULL AND group_letter != ''
        GROUP BY order_number, group_letter
    """)

    # 3. Rebuild scans table with new slim schema
    db.execute("ALTER TABLE scans RENAME TO _scans_old")
    db.executescript("""
        CREATE TABLE scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT NOT NULL,
            group_letter TEXT NOT NULL,
            box_number INTEGER NOT NULL,
            raw_barcode TEXT NOT NULL,
            scanned_by TEXT DEFAULT '',
            scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(order_number, group_letter, box_number)
        );
        CREATE INDEX IF NOT EXISTS idx_scans_order ON scans(order_number);
        CREATE INDEX IF NOT EXISTS idx_scans_date ON scans(scanned_at);
    """)
    db.execute("""
        INSERT INTO scans (id, order_number, group_letter, box_number, raw_barcode, scanned_by, scanned_at)
        SELECT id, order_number, group_letter, box_number, raw_barcode, scanned_by, scanned_at
        FROM _scans_old
    """)
    db.execute("DROP TABLE _scans_old")

    db.commit()
    db.close()
    print("[migrate] Migration complete.")


# ─── Barcode parsing ─────────────────────────────────────────────────────────

def parse_barcode(raw, data=None):
    """
    Parse a barcode string into (order_number, group_letter, box_number).
    Supported formats:
        "20316A1"     — concatenated (QR code format from EmbroTrack)
        "20316-A-1"   — dash-separated
        "20316 A 1"   — space-separated
        "20316"       — order-only (group & box from form fields in data dict)
    Returns (order_number, group_letter, box_number) or raises ValueError.
    """
    if data is None:
        data = {}

    # Try dash-separated: "20316-A-1"
    parts = raw.split('-')
    if len(parts) == 3 and parts[0].strip().isdigit() and parts[2].strip().isdigit():
        return parts[0].strip(), parts[1].strip().upper(), int(parts[2].strip())

    # Try space-separated: "20316 A 1"
    space_parts = raw.split()
    if len(space_parts) == 3 and space_parts[0].isdigit() and space_parts[2].isdigit():
        return space_parts[0], space_parts[1].upper(), int(space_parts[2])

    # Try concatenated: "20316A1", "20316A12", "20316AB1"
    m = re.match(r'^(\d+)([A-Za-z]+)(\d+)$', raw)
    if m:
        return m.group(1), m.group(2).upper(), int(m.group(3))

    # Try digits-only: just the order number
    if raw.isdigit():
        group_letter = (data.get('group_letter') or '').strip().upper()
        box_num_raw = data.get('box_number')
        if not group_letter or box_num_raw is None or str(box_num_raw).strip() == '':
            raise ValueError('Order-only barcode detected. Please provide group letter and box number.')
        return raw, group_letter, int(box_num_raw)

    raise ValueError('Unrecognized barcode format: ' + raw)


# ─── Helper to read rows as dicts ────────────────────────────────────────────

def fetch_one(db, sql, params=()):
    """Execute a query and return one row as a dict (or None)."""
    cur = db_cursor(db)
    cur.execute(q(sql), params)
    row = cur.fetchone()
    cur.close()
    if row is None:
        return None
    if USE_PG:
        return dict(row)
    else:
        return dict(row)


def fetch_all(db, sql, params=()):
    """Execute a query and return all rows as list of dicts."""
    cur = db_cursor(db)
    cur.execute(q(sql), params)
    rows = cur.fetchall()
    cur.close()
    if USE_PG:
        return [dict(r) for r in rows]
    else:
        return [dict(r) for r in rows]


def execute(db, sql, params=()):
    """Execute a write query."""
    cur = db_cursor(db)
    cur.execute(q(sql), params)
    cur.close()


# ─── Pages ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('scan.html')


@app.route('/history')
def history_page():
    return render_template('history.html')


@app.route('/report')
def report_page():
    return render_template('report.html')


# ─── API: Order push from EmbroTrack ─────────────────────────────────────────

@app.route('/api/orders', methods=['POST'])
def receive_order():
    """Receive full order data from EmbroTrack at label-generation time."""
    data = request.get_json()
    if not data or not data.get('order_number'):
        return jsonify({'error': 'Missing order_number'}), 400

    order_number = str(data['order_number'])
    groups = data.get('groups', [])
    total_boxes = sum(g.get('box_count', 0) for g in groups)

    db = get_db()

    # Upsert order (supports re-generation of labels)
    if USE_PG:
        execute(db, """
            INSERT INTO orders
            (order_number, customer_name, customer_id, po_number,
             ship_date, in_hands_date, order_type, total_groups, total_boxes, pushed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (order_number) DO UPDATE SET
                customer_name = EXCLUDED.customer_name,
                customer_id = EXCLUDED.customer_id,
                po_number = EXCLUDED.po_number,
                ship_date = EXCLUDED.ship_date,
                in_hands_date = EXCLUDED.in_hands_date,
                order_type = EXCLUDED.order_type,
                total_groups = EXCLUDED.total_groups,
                total_boxes = EXCLUDED.total_boxes,
                pushed_at = CURRENT_TIMESTAMP
        """, (
            order_number,
            data.get('customer_name', ''),
            data.get('customer_id'),
            data.get('po_number', ''),
            data.get('ship_date', ''),
            data.get('in_hands_date', ''),
            data.get('order_type', ''),
            len(groups),
            total_boxes
        ))
    else:
        execute(db, """
            INSERT OR REPLACE INTO orders
            (order_number, customer_name, customer_id, po_number,
             ship_date, in_hands_date, order_type, total_groups, total_boxes, pushed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            order_number,
            data.get('customer_name', ''),
            data.get('customer_id'),
            data.get('po_number', ''),
            data.get('ship_date', ''),
            data.get('in_hands_date', ''),
            data.get('order_type', ''),
            len(groups),
            total_boxes
        ))

    # Replace groups and designs (idempotent)
    execute(db, "DELETE FROM order_designs WHERE order_number = ?", (order_number,))
    execute(db, "DELETE FROM order_groups WHERE order_number = ?", (order_number,))

    for group in groups:
        group_letter = (group.get('group_letter') or '').upper()
        execute(db, """
            INSERT INTO order_groups (order_number, group_letter, box_count, group_type)
            VALUES (?, ?, ?, ?)
        """, (order_number, group_letter, group.get('box_count'), group.get('group_type', '')))

        for design in group.get('designs', []):
            execute(db, """
                INSERT INTO order_designs
                (order_number, group_letter, design_type, design_id, design_name, design_location)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                order_number, group_letter,
                design.get('design_type', ''),
                str(design.get('design_id', '')),
                design.get('design_name', ''),
                design.get('design_location', '')
            ))

    db.commit()
    return jsonify({
        'success': True,
        'message': 'Order {} received ({} groups, {} boxes).'.format(order_number, len(groups), total_boxes)
    }), 201


@app.route('/api/orders/<order_number>', methods=['GET'])
def get_order_info(order_number):
    """Lookup pre-populated order data (used by scan page for auto-fill)."""
    db = get_db()
    order = fetch_one(db, "SELECT * FROM orders WHERE order_number = ?", (order_number,))
    if not order:
        return jsonify({'found': False}), 404

    groups = fetch_all(db,
        "SELECT * FROM order_groups WHERE order_number = ? ORDER BY group_letter",
        (order_number,))

    designs = fetch_all(db,
        "SELECT * FROM order_designs WHERE order_number = ? ORDER BY group_letter, design_type",
        (order_number,))

    # Organize designs by group
    designs_by_group = {}
    for d in designs:
        gl = d['group_letter']
        if gl not in designs_by_group:
            designs_by_group[gl] = []
        designs_by_group[gl].append({
            'design_type': d['design_type'],
            'design_id': d['design_id'],
            'design_name': d['design_name'],
            'design_location': d['design_location']
        })

    # Get scan progress
    scanned = fetch_all(db,
        "SELECT group_letter, box_number FROM scans WHERE order_number = ? ORDER BY group_letter, box_number",
        (order_number,))

    return jsonify({
        'found': True,
        'order_number': order['order_number'],
        'customer_name': order['customer_name'],
        'customer_id': order['customer_id'],
        'po_number': order['po_number'],
        'ship_date': order['ship_date'],
        'in_hands_date': order['in_hands_date'],
        'order_type': order['order_type'],
        'total_groups': order['total_groups'],
        'total_boxes': order['total_boxes'],
        'pushed_at': str(order['pushed_at']) if order['pushed_at'] else None,
        'groups': [{
            'group_letter': g_row['group_letter'],
            'box_count': g_row['box_count'],
            'group_type': g_row['group_type'],
            'designs': designs_by_group.get(g_row['group_letter'], [])
        } for g_row in groups],
        'boxes_scanned': [{'group': r['group_letter'], 'box': r['box_number']} for r in scanned]
    })


# ─── API: Scanning ──────────────────────────────────────────────────────────

@app.route('/api/scan', methods=['POST'])
def scan_box():
    data = request.get_json()
    raw = (data.get('barcode') or '').strip()
    scanned_by = (data.get('scanned_by') or '').strip()

    if not raw:
        return jsonify({'error': 'No barcode provided'}), 400

    try:
        order_number, group_letter, box_number = parse_barcode(raw, data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    db = get_db()

    # Auto-create a minimal order record if it doesn't exist yet
    existing_order = fetch_one(db,
        "SELECT order_number FROM orders WHERE order_number = ?", (order_number,))

    if not existing_order:
        execute(db,
            "INSERT INTO orders (order_number, customer_name, po_number) VALUES (?, ?, ?)",
            (order_number,
             (data.get('customer_name') or data.get('customer_code') or '').strip(),
             (data.get('po_number') or '').strip()))

    # Check for duplicate scan
    existing_scan = fetch_one(db,
        "SELECT id FROM scans WHERE order_number = ? AND group_letter = ? AND box_number = ?",
        (order_number, group_letter, box_number))

    if existing_scan:
        return jsonify({
            'error': 'Duplicate scan',
            'message': 'Box {}-{}-{} has already been scanned.'.format(order_number, group_letter, box_number)
        }), 409

    execute(db,
        "INSERT INTO scans (order_number, group_letter, box_number, raw_barcode, scanned_by) VALUES (?, ?, ?, ?, ?)",
        (order_number, group_letter, box_number, raw, scanned_by))
    db.commit()

    # Return current order progress
    scanned = fetch_all(db,
        "SELECT group_letter, box_number FROM scans WHERE order_number = ? ORDER BY group_letter, box_number",
        (order_number,))

    # Get order info for the response
    order = fetch_one(db, "SELECT * FROM orders WHERE order_number = ?", (order_number,))

    return jsonify({
        'success': True,
        'message': 'Box {}-{}-{} scanned successfully.'.format(order_number, group_letter, box_number),
        'order_number': order_number,
        'group_letter': group_letter,
        'box_number': box_number,
        'customer_name': order['customer_name'] if order else '',
        'po_number': order['po_number'] if order else '',
        'total_boxes': order['total_boxes'] if order else None,
        'boxes_scanned': [{'group': r['group_letter'], 'box': r['box_number']} for r in scanned]
    })


@app.route('/api/scan/<int:scan_id>', methods=['DELETE'])
def undo_scan(scan_id):
    db = get_db()
    scan = fetch_one(db, "SELECT * FROM scans WHERE id = ?", (scan_id,))
    if not scan:
        return jsonify({'error': 'Scan not found'}), 404
    execute(db, "DELETE FROM scans WHERE id = ?", (scan_id,))
    db.commit()
    return jsonify({'success': True, 'message': 'Scan removed.'})


# ─── API: Order detail (with scans) ─────────────────────────────────────────

@app.route('/api/order/<order_number>')
def get_order(order_number):
    db = get_db()

    order = fetch_one(db, "SELECT * FROM orders WHERE order_number = ?", (order_number,))
    scans = fetch_all(db,
        "SELECT * FROM scans WHERE order_number = ? ORDER BY group_letter, box_number",
        (order_number,))

    if not order and not scans:
        return jsonify({'error': 'Order not found'}), 404

    groups = fetch_all(db,
        "SELECT * FROM order_groups WHERE order_number = ? ORDER BY group_letter",
        (order_number,))

    designs = fetch_all(db,
        "SELECT * FROM order_designs WHERE order_number = ? ORDER BY group_letter",
        (order_number,))

    # Build groups map with scan data
    groups_map = {}
    for grp in groups:
        groups_map[grp['group_letter']] = {
            'group_letter': grp['group_letter'],
            'box_count': grp['box_count'],
            'group_type': grp['group_type'],
            'designs': [],
            'scans': []
        }

    for d in designs:
        gl = d['group_letter']
        if gl in groups_map:
            groups_map[gl]['designs'].append({
                'design_type': d['design_type'],
                'design_id': d['design_id'],
                'design_name': d['design_name'],
                'design_location': d['design_location']
            })

    for s in scans:
        gl = s['group_letter']
        scan_data = {
            'id': s['id'],
            'box_number': s['box_number'],
            'scanned_by': s['scanned_by'],
            'scanned_at': str(s['scanned_at']) if s['scanned_at'] else None
        }
        if gl in groups_map:
            groups_map[gl]['scans'].append(scan_data)
        else:
            groups_map[gl] = {
                'group_letter': gl,
                'box_count': None,
                'group_type': '',
                'designs': [],
                'scans': [scan_data]
            }

    return jsonify({
        'order_number': order_number,
        'customer_name': order['customer_name'] if order else '',
        'po_number': order['po_number'] if order else '',
        'ship_date': order['ship_date'] if order else '',
        'in_hands_date': order['in_hands_date'] if order else '',
        'order_type': order['order_type'] if order else '',
        'total_groups': order['total_groups'] if order else None,
        'total_boxes': order['total_boxes'] if order else None,
        'boxes_scanned_count': len(scans),
        'groups': [groups_map[k] for k in sorted(groups_map.keys())]
    })


# ─── API: History ────────────────────────────────────────────────────────────

@app.route('/api/history')
def get_history():
    db = get_db()
    date_filter = request.args.get('date', '')
    search = request.args.get('search', '').strip()

    query = """
        SELECT o.order_number, o.customer_name, o.po_number,
               o.ship_date, o.in_hands_date, o.order_type,
               o.total_groups, o.total_boxes,
               COUNT(s.id) as boxes_scanned,
               MIN(s.scanned_at) as first_scan,
               MAX(s.scanned_at) as last_scan
        FROM orders o
        LEFT JOIN scans s ON o.order_number = s.order_number
        WHERE 1=1
    """
    params = []

    if date_filter:
        query += " AND DATE(s.scanned_at) = ?"
        params.append(date_filter)

    if search:
        query += " AND (o.order_number LIKE ? OR o.customer_name LIKE ? OR o.po_number LIKE ?)"
        like = '%' + search + '%'
        params.extend([like, like, like])

    query += """
        GROUP BY o.order_number, o.customer_name, o.po_number,
                 o.ship_date, o.in_hands_date, o.order_type,
                 o.total_groups, o.total_boxes, o.pushed_at
        ORDER BY COALESCE(MAX(s.scanned_at), o.pushed_at) DESC
    """

    rows = fetch_all(db, query, params)
    return jsonify([{
        'order_number': r['order_number'],
        'customer_name': r['customer_name'],
        'po_number': r['po_number'],
        'ship_date': r['ship_date'],
        'in_hands_date': r['in_hands_date'],
        'order_type': r['order_type'],
        'total_groups': r['total_groups'],
        'total_boxes': r['total_boxes'],
        'boxes_scanned': r['boxes_scanned'],
        'first_scan': str(r['first_scan']) if r['first_scan'] else None,
        'last_scan': str(r['last_scan']) if r['last_scan'] else None
    } for r in rows])


# ─── API: Daily report ───────────────────────────────────────────────────────

@app.route('/api/report')
def get_report():
    db = get_db()
    report_date = request.args.get('date', date.today().isoformat())

    orders = fetch_all(db, """
        SELECT o.order_number, o.customer_name, o.po_number,
               o.ship_date, o.in_hands_date, o.order_type,
               o.total_groups, o.total_boxes,
               COUNT(s.id) as boxes_scanned,
               MIN(s.scanned_at) as first_scan,
               MAX(s.scanned_at) as last_scan
        FROM scans s
        JOIN orders o ON o.order_number = s.order_number
        WHERE DATE(s.scanned_at) = ?
        GROUP BY o.order_number, o.customer_name, o.po_number,
                 o.ship_date, o.in_hands_date, o.order_type,
                 o.total_groups, o.total_boxes
        ORDER BY MIN(s.scanned_at)
    """, (report_date,))

    details = {}
    for order in orders:
        scans = fetch_all(db, """
            SELECT s.id, s.group_letter, s.box_number, s.scanned_by, s.scanned_at,
                   g.box_count as total_boxes_in_group, g.group_type
            FROM scans s
            LEFT JOIN order_groups g ON g.order_number = s.order_number AND g.group_letter = s.group_letter
            WHERE s.order_number = ? AND DATE(s.scanned_at) = ?
            ORDER BY s.group_letter, s.box_number
        """, (order['order_number'], report_date))
        details[order['order_number']] = [{
            'id': s['id'],
            'group_letter': s['group_letter'],
            'box_number': s['box_number'],
            'total_boxes_in_group': s['total_boxes_in_group'],
            'group_type': s['group_type'],
            'scanned_by': s['scanned_by'],
            'scanned_at': str(s['scanned_at']) if s['scanned_at'] else None
        } for s in scans]

    return jsonify({
        'date': report_date,
        'total_orders': len(orders),
        'total_boxes': sum(r['boxes_scanned'] for r in orders),
        'orders': [{
            'order_number': r['order_number'],
            'customer_name': r['customer_name'],
            'po_number': r['po_number'],
            'ship_date': r['ship_date'],
            'in_hands_date': r['in_hands_date'],
            'order_type': r['order_type'],
            'total_groups': r['total_groups'],
            'total_boxes': r['total_boxes'],
            'boxes_scanned': r['boxes_scanned'],
            'first_scan': str(r['first_scan']) if r['first_scan'] else None,
            'last_scan': str(r['last_scan']) if r['last_scan'] else None,
            'scans': details[r['order_number']]
        } for r in orders]
    })


# ─── API: Stats ──────────────────────────────────────────────────────────────

@app.route('/api/stats')
def get_stats():
    db = get_db()
    today = date.today().isoformat()

    today_boxes = fetch_one(db,
        "SELECT COUNT(*) as c FROM scans WHERE DATE(scanned_at) = ?", (today,))['c']

    today_orders = fetch_one(db,
        "SELECT COUNT(DISTINCT order_number) as c FROM scans WHERE DATE(scanned_at) = ?", (today,))['c']

    total_boxes = fetch_one(db, "SELECT COUNT(*) as c FROM scans")['c']
    total_orders = fetch_one(db, "SELECT COUNT(DISTINCT order_number) as c FROM scans")['c']

    pending_orders = fetch_one(db, """
        SELECT COUNT(*) as c FROM orders o
        WHERE NOT EXISTS (SELECT 1 FROM scans s WHERE s.order_number = o.order_number)
    """)['c']

    return jsonify({
        'today_boxes': today_boxes,
        'today_orders': today_orders,
        'total_boxes': total_boxes,
        'total_orders': total_orders,
        'pending_orders': pending_orders
    })


# ─── API: CSV Export (all scans) ─────────────────────────────────────────────

@app.route('/api/export/scans')
def export_scans_csv():
    """Export scans as a 2-column CSV: Order Number, Status.
    Optional query params: from (start date), to (end date).
    If neither is provided, exports all time."""
    db = get_db()
    date_from = request.args.get('from', '')
    date_to = request.args.get('to', '')

    query = """
        SELECT o.order_number, o.total_boxes, COUNT(s.id) as boxes_scanned
        FROM orders o
        LEFT JOIN scans s ON o.order_number = s.order_number
    """
    conditions = []
    params = []

    if date_from:
        conditions.append("DATE(s.scanned_at) >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("DATE(s.scanned_at) <= ?")
        params.append(date_to)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += """
        GROUP BY o.order_number, o.total_boxes
        HAVING COUNT(s.id) > 0
        ORDER BY MAX(s.scanned_at) DESC
    """

    rows = fetch_all(db, query, params)

    import io
    output = io.StringIO()
    output.write('Order Number,Status\n')
    for r in rows:
        status = 'Scanned' if (r['total_boxes'] and r['boxes_scanned'] >= r['total_boxes']) else 'Partial Scan'
        output.write('"{}","{}"\n'.format(r['order_number'], status))

    # Build filename
    if date_from and date_to:
        fname = 'shiptrack-scans-{}-to-{}.csv'.format(date_from, date_to)
    elif date_from:
        fname = 'shiptrack-scans-from-{}.csv'.format(date_from)
    elif date_to:
        fname = 'shiptrack-scans-through-{}.csv'.format(date_to)
    else:
        fname = 'shiptrack-all-scans.csv'

    from flask import Response
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=' + fname}
    )


if __name__ == '__main__':
    migrate_db()
    init_db()
    app.run(host='0.0.0.0', port=5050, debug=True)
else:
    # Running under gunicorn (Railway)
    migrate_db()
    init_db()
