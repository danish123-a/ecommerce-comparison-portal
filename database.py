import sqlite3
import os
from datetime import datetime

DEFAULT_DB = 'prices.db'

def get_connection(db_path=DEFAULT_DB):
    """Returns a connection to the SQLite database."""
    return sqlite3.connect(db_path)

def init_db(db_path=DEFAULT_DB):
    """Initializes the database schema if it doesn't exist."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    # Create products table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            platform TEXT NOT NULL,
            title TEXT NOT NULL,
            target_price REAL,
            image_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Run a simple migration if the database existed but didn't have image_url
    try:
        cursor.execute("ALTER TABLE products ADD COLUMN image_url TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    
    # Create price history table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            price REAL NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products (id) ON DELETE CASCADE
        )
    ''')
    
    conn.commit()
    conn.close()

def add_product(db_path, url, platform, title, target_price=None, image_url=None):
    """Adds a new product to be tracked, returning its ID."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO products (url, platform, title, target_price, image_url)
            VALUES (?, ?, ?, ?, ?)
        ''', (url, platform, title, target_price, image_url))
        conn.commit()
        product_id = cursor.lastrowid
        return product_id
    except sqlite3.IntegrityError:
        # Product already exists, update properties and retrieve its ID
        cursor.execute('''
            UPDATE products
            SET title = ?, target_price = COALESCE(?, target_price), image_url = COALESCE(?, image_url)
            WHERE url = ?
        ''', (title, target_price, image_url, url))
        conn.commit()
        cursor.execute('SELECT id FROM products WHERE url = ?', (url,))
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        conn.close()

def log_price(db_path, product_id, price, timestamp=None):
    """Logs a price entry for a product at a given timestamp."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    if timestamp is None:
        cursor.execute('''
            INSERT INTO price_history (product_id, price)
            VALUES (?, ?)
        ''', (product_id, price))
    else:
        cursor.execute('''
            INSERT INTO price_history (product_id, price, timestamp)
            VALUES (?, ?, ?)
        ''', (product_id, price, timestamp))
    conn.commit()
    conn.close()

def get_price_history(db_path, product_id):
    """Retrieves all logged prices for a specific product, ordered by time."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT price, timestamp FROM price_history
        WHERE product_id = ?
        ORDER BY timestamp ASC
    ''', (product_id,))
    rows = cursor.fetchall()
    conn.close()
    return [{'price': r[0], 'timestamp': r[1]} for r in rows]

def get_all_products(db_path):
    """Retrieves all tracked products along with their latest logged price."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    # Fetch products and join with the latest price entry
    cursor.execute('''
        SELECT p.id, p.url, p.platform, p.title, p.target_price, p.created_at, p.image_url,
               (SELECT price FROM price_history WHERE product_id = p.id ORDER BY timestamp DESC LIMIT 1) as latest_price
        FROM products p
    ''')
    rows = cursor.fetchall()
    conn.close()
    
    products = []
    for r in rows:
        products.append({
            'id': r[0],
            'url': r[1],
            'platform': r[2],
            'title': r[3],
            'target_price': r[4],
            'created_at': r[5],
            'image_url': r[6],
            'latest_price': r[7]
        })
    return products

def get_product(db_path, product_id):
    """Retrieves a single product detail."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, url, platform, title, target_price, created_at, image_url,
               (SELECT price FROM price_history WHERE product_id = id ORDER BY timestamp DESC LIMIT 1) as latest_price
        FROM products
        WHERE id = ?
    ''', (product_id,))
    r = cursor.fetchone()
    conn.close()
    
    if r:
        return {
            'id': r[0],
            'url': r[1],
            'platform': r[2],
            'title': r[3],
            'target_price': r[4],
            'created_at': r[5],
            'image_url': r[6],
            'latest_price': r[7]
        }
    return None

def delete_product(db_path, product_id):
    """Deletes a product and its associated price history."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    # Enable foreign keys just in case cascade delete is needed
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute('DELETE FROM products WHERE id = ?', (product_id,))
    # Ensure cleanup of price history is run even if PRAGMA is off
    cursor.execute('DELETE FROM price_history WHERE product_id = ?', (product_id,))
    conn.commit()
    conn.close()
