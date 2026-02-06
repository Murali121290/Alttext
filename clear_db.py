import sqlite3
import psycopg2
import os
from dotenv import load_dotenv

# Load params same as main app
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/alttext")

def  get_connection():
    # Try Postgres
    try:
        conn = psycopg2.connect(DATABASE_URL)
        print("[INFO] Connected to PostgreSQL")
        return conn, False
    except:
        print("[INFO] PostgreSQL failed, using SQLite")
        conn = sqlite3.connect("alttext.db")
        return conn, True

def clear_operational_data():
    conn, is_sqlite = get_connection()
    cur = conn.cursor()
    
    confirm = input("This will DELETE ALL Batches and Jobs. Users will remain. Type 'yes' to proceed: ")
    if confirm != "yes":
        print("Cancelled.")
        return

    try:
        # Delete data
        cur.execute("DELETE FROM jobs")
        cur.execute("DELETE FROM batches")
        
        # Reset sequences/counters if possible
        if is_sqlite:
            cur.execute("DELETE FROM sqlite_sequence WHERE name='jobs' OR name='batches'")
        else:
            cur.execute("ALTER SEQUENCE jobs_id_seq RESTART WITH 1")
            cur.execute("ALTER SEQUENCE batches_id_seq RESTART WITH 1")
            
        conn.commit()
        print("✅ Data cleared successfully.")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    clear_operational_data()
