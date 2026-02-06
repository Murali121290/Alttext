import psycopg2
import psycopg2.extras
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/alttext")
OUTPUT_FOLDER = "outputs"

try:
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    print("Checking database connection...")
    cursor.execute("SELECT version()")
    print(f"Connected to: {cursor.fetchone()['version']}")

    cursor.execute("SELECT * FROM jobs WHERE filename LIKE '%29.pdf%'")
    rows = cursor.fetchall()
    
    if not rows:
        print("No job found for 29.pdf")
    else:
        for row in rows:
            print(f"Job ID: {row['id']}")
            print(f"Filename: {row['filename']}")
            print(f"Status: {row['status']}")
            print(f"Output File: {row['output_file']}")
            print(f"Error Msg: {row['error_msg']}")
            print("-" * 20)
            
            if row['output_file']:
                out_path = os.path.join(OUTPUT_FOLDER, row['output_file'])
                if os.path.exists(out_path):
                    print(f"File {out_path} exists. Size: {os.path.getsize(out_path)} bytes")
                else:
                    print(f"File {out_path} DOES NOT EXIST.")

except Exception as e:
    print(f"Error: {e}")
finally:
    if 'conn' in locals(): conn.close()
