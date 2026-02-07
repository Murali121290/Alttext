import os
import sqlite3
import psycopg2
import time
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/alttext")

def get_db_connection():
    # Try PostgreSQL first
    try:
        conn = psycopg2.connect(DATABASE_URL)
        print("Connected to PostgreSQL")
        return conn, "postgres"
    except Exception as e:
        print(f"PostgreSQL connection attempt failed: {e}")
    
    # Fallback to SQLite
    print("Connecting to SQLite (alttext.db)")
    conn = sqlite3.connect("alttext.db")
    return conn, "sqlite"

def reset_admin():
    conn, db_type = get_db_connection()
    try:
        cur = conn.cursor()
        new_pass = "Murali@12"
        hashed = generate_password_hash(new_pass)
        
        print(f"Resetting password for user 'admin'...")
        
        if db_type == "postgres":
            cur.execute("UPDATE users SET password = %s WHERE username = 'admin'", (hashed,))
        else:
            cur.execute("UPDATE users SET password = ? WHERE username = 'admin'", (hashed,))
            
        if cur.rowcount == 0:
            print("User 'admin' not found in database along the checked paths.")
            # Optional: Create if missing? 
            # The prompt asked to "reset", implying existence. But let's stick to update.
        else:
            conn.commit()
            print(f"SUCCESS: Password for 'admin' has been reset to '{new_pass}'")
            
    except Exception as e:
        print(f"Error during reset: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    reset_admin()
